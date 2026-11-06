"""SQLite implementation of StateStore.

Holds conversation records (metadata + proposal), stage events, usage, reviewer feedback, learned aliases and
evaluation runs. It never holds transcript text: the pipeline strips evidence quotes from stored proposals when the
market policy discards transcripts, and the transcript itself only ever lives in the vault.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..domain.catalogue import LearnedAlias
from ..domain.enums import ConversationStatus
from ..domain.evaluation import EvalRun
from ..domain.metering import UsageRecord
from ..domain.pipeline import ConversationRecord, StageEvent
from ..ports.state import FeedbackRecord

logger = logging.getLogger(__name__)

_DELETE_CHUNK = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  conversation_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  source_interaction_id TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  data TEXT NOT NULL,
  batch TEXT
);
CREATE INDEX IF NOT EXISTS idx_conv_source ON conversations(source, source_interaction_id);
CREATE INDEX IF NOT EXISTS idx_conv_status ON conversations(status, created_at);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_conv ON events(conversation_id, id);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_conv ON usage(conversation_id);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS aliases (
  surface TEXT NOT NULL,
  product_id TEXT NOT NULL,
  data TEXT NOT NULL,
  PRIMARY KEY (surface, product_id)
);
CREATE TABLE IF NOT EXISTS eval_runs (
  run_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  data TEXT NOT NULL
);
"""


class SqliteStateStore:
    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if self._path != ":memory:":
            # WAL + NORMAL: a commit no longer waits for the disk. A power cut can lose the last transactions but never
            # corrupts the file, which is the right trade for demo state (a bulk run commits tens of thousands of rows).
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._tx() as cur:
            cur.executescript(_SCHEMA)
            self._migrate(cur)

    @staticmethod
    def _migrate(cur: sqlite3.Cursor) -> None:
        """Bring a database created by an earlier version up to the current shape (the batch tag for load tests)."""
        columns = {row["name"] for row in cur.execute("PRAGMA table_info(conversations)").fetchall()}
        if "batch" not in columns:
            cur.execute("ALTER TABLE conversations ADD COLUMN batch TEXT")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_conv_batch ON conversations(batch)")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- conversations ----------------------------------------------------------------------------
    def save_conversation(self, rec: ConversationRecord) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO conversations(conversation_id, source, source_interaction_id, status, created_at, updated_at, data,"
                " batch) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(conversation_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at,"
                " data=excluded.data, batch=excluded.batch",
                (
                    rec.meta.conversation_id, rec.meta.source.value, rec.meta.source_interaction_id, rec.status.value,
                    rec.created_at.isoformat(), rec.updated_at.isoformat(), rec.model_dump_json(), rec.batch_id,
                ),
            )

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        with self._tx() as cur:
            row = cur.execute("SELECT data FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        return ConversationRecord.model_validate_json(row["data"]) if row else None

    def find_by_source(self, source: str, source_interaction_id: str) -> ConversationRecord | None:
        with self._tx() as cur:
            row = cur.execute(
                "SELECT data FROM conversations WHERE source=? AND source_interaction_id=? ORDER BY created_at DESC LIMIT 1",
                (source, source_interaction_id),
            ).fetchone()
        return ConversationRecord.model_validate_json(row["data"]) if row else None

    def list_conversations(
        self, *, status: list[ConversationStatus] | None = None, limit: int = 100, offset: int = 0
    ) -> list[ConversationRecord]:
        sql = "SELECT data FROM conversations"
        args: list[object] = []
        if status:
            sql += " WHERE status IN (%s)" % ",".join("?" for _ in status)
            args += [s.value for s in status]
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self._tx() as cur:
            rows = cur.execute(sql, args).fetchall()
        return [ConversationRecord.model_validate_json(r["data"]) for r in rows]

    def batch_conversation_ids(self) -> list[str]:
        """Ids of every conversation a load test created (oldest first)."""
        with self._tx() as cur:
            rows = cur.execute(
                "SELECT conversation_id FROM conversations WHERE batch IS NOT NULL ORDER BY created_at").fetchall()
        return [r["conversation_id"] for r in rows]

    def count_batch(self) -> int:
        with self._tx() as cur:
            return int(cur.execute("SELECT COUNT(*) AS n FROM conversations WHERE batch IS NOT NULL").fetchone()["n"])

    def delete_conversations(self, conversation_ids: list[str]) -> int:
        """Remove conversations with their events, usage and feedback. Returns how many conversations were removed."""
        removed = 0
        with self._tx() as cur:
            for start in range(0, len(conversation_ids), _DELETE_CHUNK):
                chunk = conversation_ids[start:start + _DELETE_CHUNK]
                marks = ",".join("?" for _ in chunk)
                for table in ("events", "usage", "feedback"):
                    cur.execute(f"DELETE FROM {table} WHERE conversation_id IN ({marks})", chunk)
                removed += cur.execute(f"DELETE FROM conversations WHERE conversation_id IN ({marks})", chunk).rowcount
        return removed

    # ---- events / usage / feedback ----------------------------------------------------------------
    def add_event(self, ev: StageEvent) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO events(conversation_id, ts, data) VALUES (?,?,?)",
                (ev.conversation_id, ev.ts.isoformat(), ev.model_dump_json()),
            )

    def list_events(self, conversation_id: str) -> list[StageEvent]:
        with self._tx() as cur:
            rows = cur.execute("SELECT data FROM events WHERE conversation_id=? ORDER BY id", (conversation_id,)).fetchall()
        return [StageEvent.model_validate_json(r["data"]) for r in rows]

    def add_usage(self, u: UsageRecord) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO usage(conversation_id, ts, data) VALUES (?,?,?)",
                (u.conversation_id, u.ts.isoformat(), u.model_dump_json()),
            )

    def list_usage(self, conversation_id: str | None = None) -> list[UsageRecord]:
        with self._tx() as cur:
            if conversation_id:
                rows = cur.execute("SELECT data FROM usage WHERE conversation_id=? ORDER BY id", (conversation_id,)).fetchall()
            else:
                rows = cur.execute("SELECT data FROM usage ORDER BY id").fetchall()
        return [UsageRecord.model_validate_json(r["data"]) for r in rows]

    def add_feedback(self, fb: FeedbackRecord) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO feedback(conversation_id, ts, data) VALUES (?,?,?)",
                (fb.conversation_id, fb.ts.isoformat(), fb.model_dump_json()),
            )

    def list_feedback(self) -> list[FeedbackRecord]:
        with self._tx() as cur:
            rows = cur.execute("SELECT data FROM feedback ORDER BY id").fetchall()
        return [FeedbackRecord.model_validate_json(r["data"]) for r in rows]

    # ---- learned aliases --------------------------------------------------------------------------
    def upsert_alias(self, alias: LearnedAlias) -> None:
        with self._tx() as cur:
            row = cur.execute(
                "SELECT data FROM aliases WHERE surface=? AND product_id=?", (alias.surface, alias.product_id)
            ).fetchone()
            if row:
                existing = LearnedAlias.model_validate_json(row["data"])
                alias = existing.model_copy(update={"count": existing.count + max(alias.count, 1)})
            cur.execute(
                "INSERT INTO aliases(surface, product_id, data) VALUES (?,?,?)"
                " ON CONFLICT(surface, product_id) DO UPDATE SET data=excluded.data",
                (alias.surface, alias.product_id, alias.model_dump_json()),
            )

    def list_aliases(self) -> list[LearnedAlias]:
        with self._tx() as cur:
            rows = cur.execute("SELECT data FROM aliases ORDER BY surface").fetchall()
        return [LearnedAlias.model_validate_json(r["data"]) for r in rows]

    def clear_aliases(self) -> int:
        with self._tx() as cur:
            n = cur.execute("SELECT COUNT(*) AS n FROM aliases").fetchone()["n"]
            cur.execute("DELETE FROM aliases")
        return int(n)

    # ---- evaluation runs --------------------------------------------------------------------------
    def save_eval_run(self, run: EvalRun) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO eval_runs(run_id, created_at, data) VALUES (?,?,?)"
                " ON CONFLICT(run_id) DO UPDATE SET data=excluded.data",
                (run.run_id, run.created_at.isoformat(), run.model_dump_json()),
            )

    def get_eval_run(self, run_id: str) -> EvalRun | None:
        with self._tx() as cur:
            row = cur.execute("SELECT data FROM eval_runs WHERE run_id=?", (run_id,)).fetchone()
        return EvalRun.model_validate_json(row["data"]) if row else None

    def list_eval_runs(self, limit: int = 20) -> list[EvalRun]:
        with self._tx() as cur:
            rows = cur.execute("SELECT data FROM eval_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [EvalRun.model_validate_json(r["data"]) for r in rows]
