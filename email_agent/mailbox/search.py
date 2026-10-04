"""Meaning-based search (Revision 12, Stage 9): Gemini embeddings + a FAISS index, mixed with full-text search.
Adapted from S17 `core/memory/embeddings.py`, `vector_index.py` and the hybrid score in `core/memory/store.py`.

    Embedder        gemini-embedding-001 (one vector per text, so a batch of texts is one call; 768 dimensions,
                    normalised); document and query task types; key failover and retries; paced to
                    `embed_per_minute` texts (found live: the free tier counts every text in a batch as one request,
                    100 a minute, so one batch of a mailbox used the whole minute and the next call got 429)
    VectorIndex     vectors in SQLite (the source of truth: the `vectors` table of the store that owns them) and a FAISS
                    IndexIDMap2(IndexFlatIP) file beside it, rebuilt from SQLite whenever the two differ, so a lost or
                    stale file costs a rebuild, never a wrong answer (S17)
    MailSearch      one vector per message in mailbox.sqlite + state/<instance>/mail.faiss
    MemorySearch    one vector per memory in memory.sqlite + state/<instance>/memory.faiss

Scores (S17's hybrid): cosine similarity + HYBRID_BONUS when the item also matches by full text. The scope check
(our mailboxes, the run's conversations, the party) always comes from SQLite, never from the index: the index only
suggests ids. Vectors carry the embedder's fingerprint (model and size); a change of model re-embeds lazily.
faiss and numpy are imported only when a search is made, so full-text-only runs never load them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from collections import deque
from pathlib import Path
from typing import Any, Literal

from google import genai
from google.genai import types
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from email_agent.common.sqlite_db import transaction
from email_agent.config import Settings

logger = logging.getLogger(__name__)

HYBRID_BONUS = 0.2             # S17: a full-text match on top of the vector score
EMBED_BATCH = 100              # texts per embedding call
TEXT_CHARS = 2000              # of a message's body, after its subject

Task = Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _retryable(e: BaseException) -> bool:
    return getattr(e, "code", None) in (429, 500, 502, 503, 504)


class EmbedError(RuntimeError):
    pass


class Embedder:
    """Gemini embeddings. Keys are tried in order; a refused key (401/403) is skipped for the rest of the process."""

    def __init__(self, settings: Settings):
        self.model, self.dims = settings.embed_model, settings.embed_dims
        self.per_minute = max(1, settings.embed_per_minute)
        self._sent: deque[tuple[float, int]] = deque()    # (when, how many texts) in the last minute
        self._clients = [(slot, genai.Client(api_key=k.get_secret_value())) for slot, k in settings.gemini_keys()]
        if not self._clients:
            raise EmbedError("meaning-based search needs a Gemini key (GEMINI_API_KEY)")
        self._dead: set[int] = set()

    @property
    def fingerprint(self) -> str:
        return f"gemini:{self.model}:{self.dims}"

    async def embed(self, texts: list[str], task: Task) -> list[list[float]]:
        out: list[list[float]] = []
        size = min(EMBED_BATCH, self.per_minute)
        for i in range(0, len(texts), size):
            batch = texts[i:i + size]
            await self._pace(len(batch))
            out += await self._batch(batch, task)
        return out

    async def _pace(self, n: int) -> None:
        """Wait until sending `n` more texts keeps the last minute within `per_minute`."""
        while True:
            now = time.monotonic()
            while self._sent and now - self._sent[0][0] >= 60:
                self._sent.popleft()
            used = sum(k for _, k in self._sent)
            if used + n <= self.per_minute or not self._sent:
                self._sent.append((now, n))
                return
            wait = 60 - (now - self._sent[0][0]) + 0.5
            logger.info("embedding: %d texts sent in the last minute; waiting %.0f s", used, wait)
            await asyncio.sleep(wait)

    async def _batch(self, texts: list[str], task: Task) -> list[list[float]]:
        import numpy as np
        reasons: list[str] = []
        for slot, client in self._clients:
            if slot in self._dead:
                continue
            try:
                async for attempt in AsyncRetrying(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=15, max=65),
                                                   retry=retry_if_exception(_retryable), reraise=True):
                    with attempt:
                        reply = await client.aio.models.embed_content(
                            model=self.model, contents=texts,
                            config=types.EmbedContentConfig(task_type=task, output_dimensionality=self.dims))
            except Exception as e:  # noqa: BLE001 — try the next key, then give up with every key's reason
                code = getattr(e, "code", None)
                if code == 400:                       # our input is wrong: another key would refuse it too
                    raise EmbedError(f"{self.model} refused the input: {str(e)[:300]}") from None
                if code in (401, 403):
                    self._dead.add(slot)
                reasons.append(f"key #{slot}: {type(e).__name__} {code}: {str(e)[:120]}")
                logger.warning("embedding with key #%s failed: %s", slot, f"{type(e).__name__}: {str(e)[:160]}")
                continue
            vectors = np.asarray([e.values or [] for e in reply.embeddings or []], dtype="float32")
            if vectors.shape[0] != len(texts):
                raise EmbedError(f"{self.model} returned {vectors.shape[0]} vectors for {len(texts)} texts")
            vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)   # cosine = inner product
            return vectors.tolist()
        raise EmbedError("no Gemini key could embed: " + " | ".join(reasons or ["every key was refused earlier"]))


class VectorIndex:
    """Vectors of one store (its `vectors` table) and their FAISS file."""

    def __init__(self, con: sqlite3.Connection, path: Path):
        self.con, self.path = con, path
        self.ids_path = path.with_suffix(path.suffix + ".ids.json")
        self._index: Any = None

    # ── writing ──────────────────────────────────────────────────────────────
    def stale(self, items: dict[str, str], fingerprint: str) -> list[str]:
        """The item ids (of `items`: id → content hash) whose vector is missing, out of date or from another model."""
        have = {r["item_id"]: (r["content_hash"], r["fingerprint"])
                for r in self.con.execute("SELECT item_id, content_hash, fingerprint FROM vectors")}
        return [i for i, h in items.items() if have.get(i) != (h, fingerprint)]

    def put(self, rows: list[tuple[str, str | None, str | None, str, list[float]]], fingerprint: str) -> None:
        """(item id, group, scope, content hash, vector) rows."""
        import numpy as np
        with transaction(self.con):
            self.con.executemany(
                "INSERT INTO vectors (item_id, grp, scope, content_hash, fingerprint, vector) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (item_id) DO UPDATE SET grp = excluded.grp, scope = excluded.scope, "
                "content_hash = excluded.content_hash, fingerprint = excluded.fingerprint, vector = excluded.vector",
                [(i, g, s, h, fingerprint, np.asarray(v, dtype="float32").tobytes()) for i, g, s, h, v in rows])
        self._index = None

    def keep_only(self, item_ids: set[str]) -> int:
        """Remove vectors of items that no longer exist."""
        gone = [r["item_id"] for r in self.con.execute("SELECT item_id FROM vectors") if r["item_id"] not in item_ids]
        if gone:
            with transaction(self.con):
                self.con.executemany("DELETE FROM vectors WHERE item_id = ?", [(g,) for g in gone])
            self._index = None
        return len(gone)

    def count(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]

    # ── the FAISS file ───────────────────────────────────────────────────────
    def _load(self, fingerprint: str) -> Any:
        """The FAISS index for the current vectors: the saved file if it matches SQLite exactly, else rebuilt."""
        import faiss
        import numpy as np
        if self._index is not None:
            return self._index
        rids = [r[0] for r in self.con.execute("SELECT rid FROM vectors WHERE fingerprint = ? ORDER BY rid",
                                               (fingerprint,))]
        marker = {"fingerprint": fingerprint, "rids": rids}
        if self.path.exists() and self.ids_path.exists():
            try:
                if json.loads(self.ids_path.read_text()) == marker:
                    self._index = faiss.read_index(str(self.path))
                    return self._index
            except (OSError, ValueError, RuntimeError):
                logger.warning("%s could not be read; rebuilding it from SQLite", self.path.name)
        rows = self.con.execute("SELECT rid, vector FROM vectors WHERE fingerprint = ? ORDER BY rid",
                                (fingerprint,)).fetchall()
        index = None
        if rows:
            matrix = np.vstack([np.frombuffer(r["vector"], dtype="float32") for r in rows])
            index = faiss.IndexIDMap2(faiss.IndexFlatIP(matrix.shape[1]))
            index.add_with_ids(matrix, np.asarray([r["rid"] for r in rows], dtype="int64"))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            faiss.write_index(index, str(self.path))
            self.ids_path.write_text(json.dumps(marker))
        self._index = index
        return index

    def search(self, query: list[float], fingerprint: str, limit: int) -> list[tuple[str, str | None, str | None, float]]:
        """(item id, group, scope, cosine) for the nearest `limit` vectors, best first. Callers apply their scope."""
        import numpy as np
        index = self._load(fingerprint)
        if index is None or index.ntotal == 0:
            return []
        scores, rids = index.search(np.asarray([query], dtype="float32"), min(limit, index.ntotal))
        found = {int(r): float(s) for s, r in zip(scores[0], rids[0]) if r >= 0}
        if not found:
            return []
        rows = self.con.execute(f"SELECT rid, item_id, grp, scope FROM vectors WHERE rid IN ({','.join('?' * len(found))})",
                                list(found)).fetchall()
        return sorted(((r["item_id"], r["grp"], r["scope"], found[r["rid"]]) for r in rows), key=lambda x: -x[3])


class MailSearch:
    """One vector per message in our mailboxes (subject + body text)."""

    def __init__(self, mirror: Any, embedder: Embedder, state_dir: Path, instance: str):
        self.mirror, self.embedder = mirror, embedder
        self.index = VectorIndex(mirror.con, state_dir / instance / "mail.faiss")

    async def update(self, mailbox_ids: list[str]) -> dict[str, int]:
        """Embed new and changed messages (one call per EMBED_BATCH); forget vectors of messages that are gone."""
        texts = {m.id: (m.thread_id, m.mailbox_id, message_text(m)) for m in self.mirror.all_messages(mailbox_ids)}
        texts = {i: v for i, v in texts.items() if v[2]}  # an empty message has nothing to find by meaning (and the
                                                           # API refuses an empty text: found live, the "Test" threads)
        hashes = {i: content_hash(t) for i, (_, _, t) in texts.items()}
        todo = self.index.stale(hashes, self.embedder.fingerprint)
        if todo:
            vectors = await self.embedder.embed([texts[i][2] for i in todo], "RETRIEVAL_DOCUMENT")
            self.index.put([(i, texts[i][0], texts[i][1], hashes[i], v) for i, v in zip(todo, vectors)],
                           self.embedder.fingerprint)
        removed = await asyncio.to_thread(self.index.keep_only, set(texts))
        return {"messages": len(texts), "embedded": len(todo), "removed": removed}

    async def rank(self, query: str, mailbox_ids: list[str], *, only: set[str] | None = None,
                   limit: int = 200, bonus: float = HYBRID_BONUS) -> list[tuple[str, float, bool]]:
        """(thread id, score, full-text match) for conversations in scope, best first: a conversation's best message
        score, + HYBRID_BONUS when it matches by full text (every word, as the full-text rule does)."""
        [qv] = await self.embedder.embed([query], "RETRIEVAL_QUERY")
        allowed = set(mailbox_ids)
        best: dict[str, float] = {}
        for _, thread, mailbox, score in self.index.search(qv, self.embedder.fingerprint, limit):
            if mailbox in allowed and thread and (only is None or thread in only):
                best[thread] = max(best.get(thread, -1.0), score)
        fts = self.mirror.candidate_ids(mailbox_ids, query) or set()
        if only is not None:
            fts &= only
        for t in fts:
            best.setdefault(t, 0.0)
        return sorted(((t, s + (bonus if t in fts else 0.0), t in fts) for t, s in best.items()),
                      key=lambda x: -x[1])


class MemorySearch:
    """One vector per current memory (the read copy of AgentMemory)."""

    def __init__(self, memory: Any, embedder: Embedder, state_dir: Path, instance: str):
        self.memory, self.embedder = memory, embedder
        self.index = VectorIndex(memory.con, state_dir / instance / "memory.faiss")

    async def update(self) -> dict[str, int]:
        records = self.memory.embeddable()
        hashes = {r.id: content_hash(r.text) for r in records}
        todo = self.index.stale(hashes, self.embedder.fingerprint)
        if todo:
            by_id = {r.id: r for r in records}
            vectors = await self.embedder.embed([by_id[i].text[:TEXT_CHARS] for i in todo], "RETRIEVAL_DOCUMENT")
            self.index.put([(i, by_id[i].scope.party_id, None, hashes[i], v) for i, v in zip(todo, vectors)],
                           self.embedder.fingerprint)
        removed = await asyncio.to_thread(self.index.keep_only, set(hashes))
        return {"memories": len(records), "embedded": len(todo), "removed": removed}

    async def scores(self, query: str, limit: int = 200) -> dict[str, float]:
        """memory id → cosine with the query (the store applies scope and lifecycle)."""
        [qv] = await self.embedder.embed([query], "RETRIEVAL_QUERY")
        return {i: s for i, _, _, s in self.index.search(qv, self.embedder.fingerprint, limit)}


def select_candidates(ranked: list[tuple[str, float, bool]], margin: float, cap: int) -> tuple[set[str], set[str]]:
    """Hybrid selection (judge_threads and the evaluation): (full-text matches, conversations added by meaning).
    `ranked` comes from MailSearch.rank(..., bonus=0.0): plain cosine. Meaning adds at most `cap` conversations, and
    only those within `margin` of the best score: the right ones cluster at the top, and a fixed top N on a small
    mailbox adds half of it (found live: 10 of Suryodaya's 20, and triage then flagged three that were off topic)."""
    by_text = {t for t, _, fts in ranked if fts}
    scores = [s for _, s, _ in ranked if s > 0]
    if not scores:
        return by_text, set()
    best = max(scores)
    by_meaning = [t for t, s, fts in ranked if not fts and s > 0 and s >= best - margin][:cap]
    return by_text, set(by_meaning)


def message_text(m: Any) -> str:
    body = " ".join((m.body_text or m.snippet or "").split())
    return f"{m.subject or ''}\n{body[:TEXT_CHARS]}".strip()
