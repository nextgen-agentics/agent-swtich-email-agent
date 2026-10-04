"""MailboxStore: our own copy of our mailboxes' conversations and messages (Revision 12, Stage 2).

`state/<instance>/mailbox.sqlite`, shared by every run on that instance and kept up to date by `sync.MailboxSync`.
Reads come from here instead of downloading the whole mailbox on every tool call (the old `_listing()`, capped at 1000
rows). Tables:
  threads, messages    the platform rows as JSON, plus the columns we filter on
  thread_facts         per conversation, what `conversation.py` works out from its messages (overview, digest, price
                       view, content hash), rebuilt only when the conversation changes — so selecting candidates is SQL
  thread_text          FTS5 full-text index (trigram tokenizer = substring matching, like the old word search), one row
                       per conversation: subject, senders and every message's text
  sync_state           per (table, mailbox): the newest `updated_at` seen (the watermark) and the last full pass

Writes made by a run are copied in right after the platform accepts them (`apply_thread_update`), so later reads in the
same run see them, as the old live reads did. Dry-run writes are never copied in.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from email_agent.common.sqlite_db import VECTORS_SQL, connect, transaction
from email_agent.contracts.agent import (
    ConversationDigest,
    ConversationOverview,
    PriceConversation,
)
from email_agent.contracts.mirror import SyncState
from email_agent.contracts.platform import EmailMessage, EmailThread
from email_agent.mailbox.conversation import (
    FACTS_VERSION,
    content_hash,
    digest,
    overview,
    price_view,
)

MAILBOX_FILE = "mailbox.sqlite"
DROP_FIELDS = {"body_html", "raw_headers"}        # large, and nothing the agent works out uses them

SCHEMA = [
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE threads (id TEXT PRIMARY KEY, mailbox_id TEXT, updated_at TEXT, row TEXT NOT NULL);
    CREATE INDEX threads_mailbox ON threads(mailbox_id);
    CREATE TABLE messages (id TEXT PRIMARY KEY, thread_id TEXT, mailbox_id TEXT, updated_at TEXT, row TEXT NOT NULL);
    CREATE INDEX messages_thread ON messages(thread_id);
    CREATE INDEX messages_mailbox ON messages(mailbox_id);
    CREATE TABLE thread_facts (
        thread_id TEXT PRIMARY KEY REFERENCES threads(id) ON DELETE CASCADE, mailbox_id TEXT,
        content_hash TEXT NOT NULL, folder TEXT, has_inbound INTEGER, newest_real_from TEXT,
        we_replied_after_them INTEGER, newest_real_at TEXT, summary_current INTEGER, our_last_at TEXT,
        overview TEXT NOT NULL, digest TEXT NOT NULL, price TEXT);
    CREATE INDEX facts_mailbox ON thread_facts(mailbox_id);
    CREATE VIRTUAL TABLE thread_text USING fts5(thread_id UNINDEXED, body, tokenize='trigram');
    CREATE TABLE sync_state (entity TEXT NOT NULL, mailbox_id TEXT NOT NULL, watermark TEXT, last_full_at TEXT,
        PRIMARY KEY (entity, mailbox_id))
    """,
    VECTORS_SQL,                      # Stage 9: one vector per message (search.MailSearch)
]


def _row_json(row: EmailThread | EmailMessage) -> str:
    data = row.model_dump(mode="json")
    return json.dumps({k: v for k, v in data.items() if k not in DROP_FIELDS}, sort_keys=True, ensure_ascii=False)


def _marks(values: Iterable[Any]) -> str:
    return ",".join("?" * len(list(values)))


class MailboxStore:
    def __init__(self, path: Path):
        self.path = path
        self.con: sqlite3.Connection = connect(path, SCHEMA)

    @classmethod
    def for_instance(cls, state_dir: Path, instance: str) -> "MailboxStore":
        return cls(state_dir / instance / MAILBOX_FILE)

    def close(self) -> None:
        self.con.close()

    # ── sync bookkeeping ──────────────────────────────────────────────────────
    def sync_state(self, entity: str, mailbox_id: str) -> SyncState:
        r = self.con.execute("SELECT * FROM sync_state WHERE entity = ? AND mailbox_id = ?", (entity, mailbox_id)).fetchone()
        table = "threads" if entity == "EmailThread" else "messages"
        rows = self.con.execute(f"SELECT COUNT(*) FROM {table} WHERE mailbox_id = ?", (mailbox_id,)).fetchone()[0]
        if r is None:
            return SyncState(entity=entity, mailbox_id=mailbox_id, rows=rows)
        return SyncState(entity=entity, mailbox_id=mailbox_id, watermark=r["watermark"],
                         last_full_at=r["last_full_at"], rows=rows)

    def set_sync_state(self, entity: str, mailbox_id: str, watermark: str | None, full: bool) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.con.execute(
            """INSERT INTO sync_state (entity, mailbox_id, watermark, last_full_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(entity, mailbox_id) DO UPDATE SET watermark = excluded.watermark,
               last_full_at = COALESCE(excluded.last_full_at, sync_state.last_full_at)""",
            (entity, mailbox_id, watermark, now if full else None))

    # ── storing platform rows ─────────────────────────────────────────────────
    def upsert_threads(self, rows: list[EmailThread]) -> set[str]:
        """Store conversations; returns the ids that are new or changed."""
        changed: set[str] = set()
        with transaction(self.con):
            for t in rows:
                text = _row_json(t)
                old = self.con.execute("SELECT row FROM threads WHERE id = ?", (t.id,)).fetchone()
                if old is not None and old["row"] == text:
                    continue
                self.con.execute("INSERT OR REPLACE INTO threads (id, mailbox_id, updated_at, row) VALUES (?, ?, ?, ?)",
                                 (t.id, t.mailbox_id, t.updated_at, text))
                changed.add(t.id)
        return changed

    def upsert_messages(self, rows: list[EmailMessage]) -> tuple[int, set[str]]:
        """Store messages; returns (messages new or changed, the conversation ids they belong to)."""
        touched: set[str] = set()
        changed = 0
        with transaction(self.con):
            for m in rows:
                text = _row_json(m)
                old = self.con.execute("SELECT row, thread_id FROM messages WHERE id = ?", (m.id,)).fetchone()
                if old is not None and old["row"] == text:
                    continue
                if old is not None and old["thread_id"]:
                    touched.add(old["thread_id"])
                self.con.execute(
                    "INSERT OR REPLACE INTO messages (id, thread_id, mailbox_id, updated_at, row) VALUES (?, ?, ?, ?, ?)",
                    (m.id, m.thread_id, getattr(m, "mailbox_id", None), m.updated_at, text))
                changed += 1
                if m.thread_id:
                    touched.add(m.thread_id)
        return changed, touched

    def delete_missing(self, entity: str, mailbox_id: str, keep: set[str]) -> tuple[int, set[str]]:
        """After a full pass: remove rows of this mailbox the platform no longer has. Returns (count, conversations
        touched)."""
        table = "threads" if entity == "EmailThread" else "messages"
        col = "id" if table == "threads" else "thread_id"
        gone = [(r["id"], r[col]) for r in self.con.execute(f"SELECT id, {col} FROM {table} WHERE mailbox_id = ?",
                                                             (mailbox_id,)) if r["id"] not in keep]
        with transaction(self.con):
            for row_id, _ in gone:
                self.con.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
                if table == "threads":
                    self.con.execute("DELETE FROM thread_text WHERE thread_id = ?", (row_id,))
        return len(gone), {t for _, t in gone if t}

    # ── worked-out facts ──────────────────────────────────────────────────────
    def _facts_key(self, our_emails: set[str]) -> str:
        return f"{FACTS_VERSION}|{','.join(sorted(e.lower() for e in our_emails))}"

    def facts_stale(self, our_emails: set[str]) -> bool:
        """True when the facts were built by other code or for other addresses: everything must be rebuilt."""
        r = self.con.execute("SELECT value FROM meta WHERE key = 'facts_key'").fetchone()
        return r is None or r["value"] != self._facts_key(our_emails)

    def all_thread_ids(self) -> set[str]:
        return {r["id"] for r in self.con.execute("SELECT id FROM threads")}

    def rebuild_facts(self, thread_ids: Iterable[str], our_emails: set[str]) -> int:
        """Work out each conversation's facts again from its stored rows (conversation.py, no LLM)."""
        n = 0
        with transaction(self.con):
            for tid in sorted(set(thread_ids)):
                r = self.con.execute("SELECT row, mailbox_id FROM threads WHERE id = ?", (tid,)).fetchone()
                self.con.execute("DELETE FROM thread_text WHERE thread_id = ?", (tid,))
                if r is None:                                  # messages of a conversation we don't hold
                    self.con.execute("DELETE FROM thread_facts WHERE thread_id = ?", (tid,))
                    continue
                thread = EmailThread.model_validate_json(r["row"])
                messages = [EmailMessage.model_validate_json(m["row"]) for m in
                            self.con.execute("SELECT row FROM messages WHERE thread_id = ?", (tid,))]
                ov = overview(thread, messages, our_emails)
                dg = digest(thread, messages, our_emails)
                pv = price_view(thread, messages, our_emails)
                self.con.execute(
                    """INSERT OR REPLACE INTO thread_facts (thread_id, mailbox_id, content_hash, folder, has_inbound,
                       newest_real_from, we_replied_after_them, newest_real_at, summary_current, our_last_at, overview,
                       digest, price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (tid, r["mailbox_id"], content_hash(thread, messages), ov.folder, int(ov.has_inbound),
                     ov.newest_real_from, int(ov.we_replied_after_them), ov.newest_real_at, int(dg.summary_current),
                     pv.our_last_at if pv else None, ov.model_dump_json(), dg.model_dump_json(),
                     pv.model_dump_json() if pv else None))
                senders = " ".join(sorted({(m.from_email or "") for m in messages}))
                body = "\n".join([thread.subject or "", senders] + [(m.body_text or m.snippet or "") for m in messages])
                self.con.execute("INSERT INTO thread_text (thread_id, body) VALUES (?, ?)", (tid, body))
                n += 1
            self.con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('facts_key', ?)",
                             (self._facts_key(our_emails),))
        return n

    def apply_thread_update(self, thread_id: str, fields: dict[str, Any], our_emails: set[str]) -> bool:
        """Copy a write the platform accepted into our copy, so later reads in the run see it."""
        r = self.con.execute("SELECT row FROM threads WHERE id = ?", (thread_id,)).fetchone()
        if r is None:
            return False
        row = json.loads(r["row"])
        row.update(fields)
        with transaction(self.con):
            self.con.execute("UPDATE threads SET row = ? WHERE id = ?", (json.dumps(row, sort_keys=True), thread_id))
        self.rebuild_facts([thread_id], our_emails)
        return True

    # ── reads for the agent's tools ───────────────────────────────────────────
    def candidate_ids(self, mailbox_ids: list[str], search: str | None) -> set[str] | None:
        """Full-text pre-filter: conversations whose text contains every search word of 3+ characters. None = no
        filter possible (no search, or only short words). Callers still apply their exact word check."""
        words = [w for w in (search or "").lower().split() if len(w) >= 3]
        if not words:
            return None
        query = " AND ".join('"' + w.replace('"', '""') + '"' for w in words)
        rows = self.con.execute(
            f"""SELECT t.thread_id FROM thread_text t JOIN threads th ON th.id = t.thread_id
                WHERE thread_text MATCH ? AND th.mailbox_id IN ({_marks(mailbox_ids)})""",
            (query, *mailbox_ids)).fetchall()
        return {r["thread_id"] for r in rows}

    def newest_change(self, mailbox_ids: list[str]) -> tuple[str | None, str | None]:
        """(newest message updated_at, newest conversation updated_at) in these mailboxes: the watcher's first cursor."""
        marks = _marks(mailbox_ids)
        msg = self.con.execute(f"SELECT MAX(updated_at) FROM messages WHERE mailbox_id IN ({marks})", mailbox_ids).fetchone()[0]
        thr = self.con.execute(f"SELECT MAX(updated_at) FROM threads WHERE mailbox_id IN ({marks})", mailbox_ids).fetchone()[0]
        return msg, thr

    def messages_since(self, mailbox_ids: list[str], since: str) -> list[EmailMessage]:
        """Messages in these mailboxes changed after `since` (platform time, compared as the platform writes it)."""
        rows = self.con.execute(f"SELECT row FROM messages WHERE mailbox_id IN ({_marks(mailbox_ids)}) AND updated_at > ? "
                                "ORDER BY updated_at", (*mailbox_ids, since))
        return [EmailMessage.model_validate_json(r["row"]) for r in rows]

    def threads_since(self, mailbox_ids: list[str], since: str) -> list[EmailThread]:
        rows = self.con.execute(f"SELECT row FROM threads WHERE mailbox_id IN ({_marks(mailbox_ids)}) AND updated_at > ? "
                                "ORDER BY updated_at", (*mailbox_ids, since))
        return [EmailThread.model_validate_json(r["row"]) for r in rows]

    def all_messages(self, mailbox_ids: list[str]) -> list[EmailMessage]:
        rows = self.con.execute(f"SELECT row FROM messages WHERE mailbox_id IN ({_marks(mailbox_ids)})", mailbox_ids)
        return [EmailMessage.model_validate_json(r["row"]) for r in rows]

    def fts_rank(self, mailbox_ids: list[str], search: str, *, any_word: bool = False) -> list[str]:
        """Conversations containing every search word of 3+ characters (or any of them), best full-text rank first:
        the evaluation's "full text only" lists (selection itself uses candidate_ids, every word)."""
        words = [w for w in (search or "").lower().replace(",", " ").split() if len(w) >= 3]
        if not words:
            return []
        query = (" OR " if any_word else " AND ").join('"' + w.replace('"', '""') + '"' for w in words)
        rows = self.con.execute(
            f"""SELECT t.thread_id FROM thread_text t JOIN threads th ON th.id = t.thread_id
                WHERE thread_text MATCH ? AND th.mailbox_id IN ({_marks(mailbox_ids)}) ORDER BY rank""",
            (query, *mailbox_ids)).fetchall()
        return list(dict.fromkeys(r["thread_id"] for r in rows))

    def message(self, message_id: str) -> EmailMessage | None:
        row = self.con.execute("SELECT row FROM messages WHERE id = ?", (message_id,)).fetchone()
        return EmailMessage.model_validate_json(row["row"]) if row else None

    def message_thread(self, message_id: str, mailbox_ids: list[str]) -> str | None:
        """The conversation of one message in our mailboxes (None if it is not one of ours)."""
        row = self.con.execute(f"SELECT thread_id FROM messages WHERE id = ? AND mailbox_id IN ({_marks(mailbox_ids)})",
                               (message_id, *mailbox_ids)).fetchone()
        return row["thread_id"] if row else None

    def threads(self, mailbox_ids: list[str]) -> dict[str, EmailThread]:
        rows = self.con.execute(f"SELECT row FROM threads WHERE mailbox_id IN ({_marks(mailbox_ids)})", mailbox_ids)
        return {t.id: t for t in (EmailThread.model_validate_json(r["row"]) for r in rows)}

    def overviews(self, mailbox_ids: list[str], *, folder: str = "all",
                  only_waiting_on_us: bool = False) -> list[tuple[str, ConversationOverview]]:
        """(mailbox id, overview) per conversation, newest real message first."""
        sql = f"SELECT mailbox_id, overview FROM thread_facts WHERE mailbox_id IN ({_marks(mailbox_ids)})"
        args: list[Any] = list(mailbox_ids)
        if folder != "all":
            sql, args = sql + " AND folder = ?", args + [folder]
        if only_waiting_on_us:
            sql += " AND has_inbound = 1 AND newest_real_from = 'them' AND we_replied_after_them = 0"
        rows = self.con.execute(sql + " ORDER BY newest_real_at DESC, thread_id", args)
        return [(r["mailbox_id"], ConversationOverview.model_validate_json(r["overview"])) for r in rows]

    def digests(self, mailbox_ids: list[str], *, only_stale_summaries: bool = False,
                ids: set[str] | None = None) -> list[tuple[str, ConversationDigest]]:
        sql = f"SELECT thread_id, mailbox_id, digest FROM thread_facts WHERE mailbox_id IN ({_marks(mailbox_ids)})"
        if only_stale_summaries:
            sql += " AND summary_current = 0"
        rows = self.con.execute(sql + " ORDER BY newest_real_at DESC, thread_id", mailbox_ids)
        return [(r["mailbox_id"], ConversationDigest.model_validate_json(r["digest"]))
                for r in rows if ids is None or r["thread_id"] in ids]

    def price_views(self, mailbox_ids: list[str], *,
                    ids: set[str] | None = None) -> list[tuple[EmailThread, str, PriceConversation]]:
        """(conversation, mailbox id, price view) for conversations about prices or orders, our last message first."""
        rows = self.con.execute(
            f"""SELECT f.thread_id, f.mailbox_id, f.price, th.row FROM thread_facts f JOIN threads th ON th.id = f.thread_id
                WHERE f.price IS NOT NULL AND f.mailbox_id IN ({_marks(mailbox_ids)})
                ORDER BY f.our_last_at DESC, f.thread_id""", mailbox_ids)
        return [(EmailThread.model_validate_json(r["row"]), r["mailbox_id"], PriceConversation.model_validate_json(r["price"]))
                for r in rows if ids is None or r["thread_id"] in ids]

    def content_hashes(self, thread_ids: Iterable[str]) -> dict[str, str]:
        ids = list(thread_ids)
        rows = self.con.execute(f"SELECT thread_id, content_hash FROM thread_facts WHERE thread_id IN ({_marks(ids)})", ids)
        return {r["thread_id"]: r["content_hash"] for r in rows}

    def counts(self) -> tuple[int, int]:
        return (self.con.execute("SELECT COUNT(*) FROM threads").fetchone()[0],
                self.con.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
