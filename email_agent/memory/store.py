"""Long-lived memory per instance: `state/<instance>/memory.sqlite` (Revision 12).

Stage 4: the verdict cache (memory layer 3, "derived knowledge"). A shard's verdict on a conversation is kept under a
key made of everything the verdict depends on: the conversation's content hash, the skill, the skill file's and the
judging prompt's own hashes, and — for date-bound skills — today. Change any of them and the key changes, so a stale
verdict is never reused. A run with the cache on judges only conversations that changed. Harness runs keep it off, so
their scores measure the model, not the cache.

Stage 7: long-term memory (layer 2) and episodes (layer 7), adapted from S17 `core/memory/store.py`:
  - `memories` is the read copy of the platform's AgentMemory (the source of truth; synced by memory.sync.MemorySync) plus our
    own episodes, one MemoryRecord per row, with a full-text index (S17's lexical path; BUG-014: the platform's own
    memory search never matches).
  - Every record has a scope (instance → mailbox → party → conversation → run). A broader record is readable in a
    narrower request, never the other way (S17 `_scope_where`): a Cardinal request reads Cardinal's and the company's
    memories, never another party's.
  - Every record has at least one source (S17: no durable memory without provenance). A memory we create keeps the
    request or message it came from, which the platform row cannot hold; a later sync keeps it.
  - Only current, unexpired records are recalled; the platform's switch-off (is_active false) is honoured.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from email_agent.common.sqlite_db import VECTORS_SQL, connect, transaction
from email_agent.contracts.memory import Episode, MemoryRecord, MemoryScope, SourceRef
from email_agent.contracts.platform import AgentMemory

MEMORY_FILE = "memory.sqlite"
HYBRID_BONUS = 0.2                    # as search.HYBRID_BONUS (S17), without importing the embedding client
KINDS = {"fact", "preference", "instruction", "relationship", "context"}
SCOPE_COLUMNS = ("mailbox_id", "party_id", "thread_id", "run_id")

SCHEMA = [
    """
    CREATE TABLE verdicts (key TEXT PRIMARY KEY, thread_id TEXT NOT NULL, skill TEXT NOT NULL, verdict TEXT NOT NULL,
        model TEXT, created_at TEXT NOT NULL);
    CREATE INDEX verdicts_thread ON verdicts(thread_id)
    """,
    """
    CREATE TABLE memories (id TEXT PRIMARY KEY, kind TEXT NOT NULL, instance TEXT NOT NULL, mailbox_id TEXT,
        party_id TEXT, thread_id TEXT, run_id TEXT, origin TEXT NOT NULL, status TEXT NOT NULL, valid_to TEXT,
        importance REAL, created_at TEXT NOT NULL, updated_at TEXT, record TEXT NOT NULL);
    CREATE INDEX memories_scope ON memories(instance, party_id, status);
    CREATE INDEX memories_kind ON memories(kind, created_at);
    CREATE VIRTUAL TABLE memories_fts USING fts5(id UNINDEXED, text);
    CREATE TABLE memory_sync (instance TEXT PRIMARY KEY, watermark TEXT, synced_at TEXT NOT NULL)
    """,
    VECTORS_SQL,                      # Stage 9: one vector per memory (search.MemorySearch)
]


def verdict_key(thread_id: str, content_hash: str, skill: str, skill_hash: str, prompt_hash: str,
                day: str | None) -> str:
    return hashlib.sha256("|".join([thread_id, content_hash, skill, skill_hash, prompt_hash, day or ""]).encode()).hexdigest()


class VerdictCache:
    def __init__(self, path: Path):
        self.con: sqlite3.Connection = connect(path, SCHEMA)

    @classmethod
    def for_instance(cls, state_dir: Path, instance: str) -> "VerdictCache":
        return cls(state_dir / instance / MEMORY_FILE)

    def close(self) -> None:
        self.con.close()

    def get_many(self, keys: dict[str, str]) -> dict[str, tuple[dict[str, Any], str | None]]:
        """thread id → (saved verdict, the model that made it), for the keys present."""
        if not keys:
            return {}
        by_key = {k: t for t, k in keys.items()}
        rows = self.con.execute(f"SELECT key, verdict, model FROM verdicts WHERE key IN ({','.join('?' * len(by_key))})",
                                list(by_key))
        return {by_key[r["key"]]: (json.loads(r["verdict"]), r["model"]) for r in rows}

    def put_many(self, items: list[tuple[str, str, str, dict[str, Any]]], model: str | None) -> None:
        """(key, thread id, skill, verdict) rows."""
        now = datetime.now(timezone.utc).isoformat()
        with transaction(self.con):
            self.con.executemany(
                "INSERT OR REPLACE INTO verdicts (key, thread_id, skill, verdict, model, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                [(k, t, s, json.dumps(v, sort_keys=True), model, now) for k, t, s, v in items])


def _thread_of(content: str) -> str | None:
    """A price-agreement memory names its conversation on its machine-readable first line (flows.price_memory_content)."""
    first = content.split("\n", 1)[0]
    if not first.startswith("price-agreement |"):
        return None
    return next((p.split("=", 1)[1].strip() for p in first.split("|") if p.strip().startswith("thread=")), None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expired(valid_to: str | None) -> bool:
    if not valid_to:
        return False
    try:
        t = datetime.fromisoformat(valid_to.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)) <= datetime.now(timezone.utc)


class LongTermMemory:
    def __init__(self, path: Path, instance: str):
        self.con: sqlite3.Connection = connect(path, SCHEMA)
        self.instance = instance

    @classmethod
    def for_instance(cls, state_dir: Path, instance: str) -> "LongTermMemory":
        return cls(state_dir / instance / MEMORY_FILE, instance)

    def close(self) -> None:
        self.con.close()

    # ── writing ──────────────────────────────────────────────────────────────
    def _put(self, r: MemoryRecord) -> None:
        """Inside a transaction: one record, its columns and its full-text row."""
        self.con.execute(
            """INSERT OR REPLACE INTO memories (id, kind, instance, mailbox_id, party_id, thread_id, run_id, origin,
                   status, valid_to, importance, created_at, updated_at, record)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (r.id, r.kind, r.scope.instance, r.scope.mailbox_id, r.scope.party_id, r.scope.thread_id, r.scope.run_id,
             r.origin, r.status, r.valid_to, r.importance, r.created_at, r.updated_at, r.model_dump_json()))
        self.con.execute("DELETE FROM memories_fts WHERE id = ?", (r.id,))
        self.con.execute("INSERT INTO memories_fts (id, text) VALUES (?, ?)", (r.id, r.text))

    def get(self, record_id: str) -> MemoryRecord | None:
        row = self.con.execute("SELECT record FROM memories WHERE id = ?", (record_id,)).fetchone()
        return MemoryRecord.model_validate_json(row["record"]) if row else None

    def from_platform(self, row: AgentMemory, extra: list[SourceRef] | None = None) -> MemoryRecord:
        """A platform AgentMemory as a record: party scope (company scope when it has no party). Sources we added
        earlier (the request or message a memory came from) are kept."""
        old = self.get(row.id)
        platform = SourceRef(uri=f"agentswitch:{self.instance}/AgentMemory/{row.id}", author=row.created_by or "unknown",
                             captured_at=row.created_at or _now())
        ours = [s for s in (old.sources if old else []) if not s.uri.startswith("agentswitch:")] + list(extra or [])
        content = row.content or ""
        return MemoryRecord(
            id=row.id, kind=row.category if row.category in KINDS else "fact",
            scope=MemoryScope(instance=self.instance, party_id=row.party_id),
            text=content, sources=[platform, *dict.fromkeys(ours)], origin="platform",
            status="current" if row.is_active else "switched_off", valid_to=row.expires_at, importance=row.importance,
            created_at=row.created_at or _now(), updated_at=row.updated_at,
            metadata={k: v for k, v in {"source": row.source, "created_by": row.created_by,
                                        "thread": _thread_of(content)}.items() if v})

    def upsert_platform(self, rows: list[AgentMemory], extra: dict[str, list[SourceRef]] | None = None) -> int:
        """The read copy takes these platform rows; returns how many were new or changed."""
        changed = 0
        with transaction(self.con):
            for row in rows:
                new = self.from_platform(row, (extra or {}).get(row.id))
                old = self.get(row.id)
                if old is None or old.model_dump(exclude={"sources"}) != new.model_dump(exclude={"sources"}) \
                        or old.sources != new.sources:
                    self._put(new)
                    changed += 1
        return changed

    def mark_gone(self, party_id: str, live_ids: set[str]) -> int:
        """After a full live read of one party: its platform rows the platform no longer has are marked gone."""
        rows = self.con.execute("SELECT id FROM memories WHERE origin = 'platform' AND party_id = ? AND status != 'gone'",
                                (party_id,)).fetchall()
        gone = [r["id"] for r in rows if r["id"] not in live_ids]
        with transaction(self.con):
            for rid in gone:
                rec = self.get(rid)
                if rec:
                    self._put(rec.model_copy(update={"status": "gone"}))
        return len(gone)

    def sync_watermark(self) -> str | None:
        row = self.con.execute("SELECT watermark FROM memory_sync WHERE instance = ?", (self.instance,)).fetchone()
        return row["watermark"] if row else None

    def set_sync_watermark(self, watermark: str | None) -> None:
        with transaction(self.con):
            self.con.execute("INSERT OR REPLACE INTO memory_sync (instance, watermark, synced_at) VALUES (?, ?, ?)",
                             (self.instance, watermark, _now()))

    # ── recall (S17 scope rule + full-text) ──────────────────────────────────
    @staticmethod
    def _scope_where(scope: MemoryScope) -> tuple[str, list[Any]]:
        clauses, values = ["instance = ?"], [scope.instance]
        for column in SCOPE_COLUMNS:
            value = getattr(scope, column)
            clauses.append(f"({column} IS NULL OR {column} = ?)" if value else f"{column} IS NULL")
            if value:
                values.append(value)
        return " AND ".join(clauses), values

    def _matching(self, query: str | None) -> dict[str, float]:
        """id → full-text rank (lower is better) for records containing any word of 3+ characters."""
        words = [w for w in (query or "").lower().replace("'", " ").split() if len(w) >= 3]
        if not words:
            return {}
        match = " OR ".join('"' + w.replace('"', '""') + '"' for w in words)
        try:
            rows = self.con.execute("SELECT id, rank FROM memories_fts WHERE memories_fts MATCH ?", (match,)).fetchall()
        except sqlite3.OperationalError:
            return {}
        return {r["id"]: r["rank"] for r in rows}

    def embeddable(self) -> list[MemoryRecord]:
        """Current memories (not episodes): what meaning-based search indexes (Stage 9)."""
        rows = self.con.execute("SELECT record FROM memories WHERE status = 'current' AND kind != 'episode'").fetchall()
        return [MemoryRecord.model_validate_json(r["record"]) for r in rows]

    def recall(self, scope: MemoryScope, query: str | None = None, *, limit: int = 8,
               kinds: set[str] | None = None, vector: dict[str, float] | None = None,
               vector_floor: float = 0.0) -> tuple[list[MemoryRecord], int]:
        """The top `limit` current, unexpired records readable in `scope`, and how many there are in all.
        With a party: that party's records first, then the company's; full-text matches first within each.
        Without a party: only records matching the query.
        `vector` (hybrid search, Stage 9: memory id → cosine with the query): within each scope level, records are
        ordered by cosine + HYBRID_BONUS for a full-text match (S17), and without a party a record at or above
        `vector_floor` counts as matching even with no shared word. The scope rule is applied first, as always."""
        where, values = self._scope_where(scope)
        kinds = kinds or KINDS
        rows = self.con.execute(f"SELECT record FROM memories WHERE {where} AND status = 'current' AND kind IN "
                                f"({','.join('?' * len(kinds))})", (*values, *sorted(kinds))).fetchall()
        records = [r for r in (MemoryRecord.model_validate_json(x["record"]) for x in rows) if not _expired(r.valid_to)]
        hits = self._matching(query)
        if vector is not None:
            def score(r: MemoryRecord) -> float:
                return vector.get(r.id, 0.0) + (HYBRID_BONUS if r.id in hits else 0.0)
            if not scope.party_id:
                records = [r for r in records if r.id in hits or vector.get(r.id, 0.0) >= vector_floor]
            records.sort(key=lambda r: (0 if r.scope.party_id else 1, -score(r)))
            return records[:limit], len(records)
        if not scope.party_id:
            records = [r for r in records if r.id in hits]

        records.sort(key=lambda r: r.created_at[:19], reverse=True)          # newest first among equals
        records.sort(key=lambda r: (0 if r.scope.party_id else 1, 0 if r.id in hits else 1, hits.get(r.id, 0.0),
                                    -(r.importance or 0.0)))
        return records[:limit], len(records)

    # ── episodes (layer 7) ───────────────────────────────────────────────────
    def save_episode(self, ep: Episode, mailbox_ids: list[str], author: str) -> None:
        """One record per run (replaced when the run is resumed). Mailbox scope when the run worked in one mailbox,
        company scope when it worked in several."""
        scope = MemoryScope(instance=self.instance, mailbox_id=mailbox_ids[0] if len(mailbox_ids) == 1 else None)
        record = MemoryRecord(id=f"episode:{ep.run_id}", kind="episode", scope=scope, text=ep.line(), origin="local",
                              sources=[SourceRef(uri=f"run:{ep.run_id}", author=author, excerpt=ep.request[:300])],
                              metadata={"episode": ep.model_dump(mode="json")})
        old = self.get(record.id)
        if old:
            record = record.model_copy(update={"created_at": old.created_at, "updated_at": _now()})
        with transaction(self.con):
            self._put(record)

    def recent_episodes(self, mailbox_ids: list[str], exclude_run: str, n: int = 3) -> list[Episode]:
        """The last `n` runs readable in this run's scope (its mailbox, or the company when it has several)."""
        scope = MemoryScope(instance=self.instance, mailbox_id=mailbox_ids[0] if len(mailbox_ids) == 1 else None)
        where, values = self._scope_where(scope)
        rows = self.con.execute(f"SELECT record FROM memories WHERE {where} AND kind = 'episode' AND id != ? "
                                "ORDER BY created_at DESC LIMIT ?", (*values, f"episode:{exclude_run}", n)).fetchall()
        return [Episode.model_validate(MemoryRecord.model_validate_json(r["record"]).metadata["episode"]) for r in rows]
