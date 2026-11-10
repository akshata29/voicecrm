"""Local CRM simulator: a SQLite stand-in for Dataverse (phone calls, tasks, insights, accounts, contacts, opportunities).

It implements `CrmPort` with the same matching rules as the Dataverse adapter (`matching.resolve_match`) and the same
write contract: idempotent on `CrmPackage.idempotency_key`, and structurally incapable of storing transcript text -
`guards.assert_no_transcript` rejects a package whose transcript pointer is prose or that carries a transcript dump.
All seed data is synthetic.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from ...domain.conversation import ConversationMeta, utcnow
from ...domain.crm import CrmMatch, CrmPackage, CrmRecordRef, CrmSeed, CrmWriteResult
from .guards import TranscriptLeakError, assert_no_transcript
from .matching import AccountRow, ContactRow, OpportunityRow, normalize_phone, resolve_match

logger = logging.getLogger(__name__)

ENTITIES = ("phonecall", "task", "insight", "account", "contact", "opportunity", "systemuser")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT);
CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, name TEXT NOT NULL, domain TEXT, segment TEXT);
CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT, phone TEXT, account_id TEXT, title TEXT);
CREATE TABLE IF NOT EXISTS opportunities (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, account_id TEXT, contact_id TEXT, stage TEXT);
CREATE TABLE IF NOT EXISTS phonecalls (
    id TEXT PRIMARY KEY, subject TEXT NOT NULL, description TEXT, direction TEXT, start_at TEXT, end_at TEXT,
    duration_minutes INTEGER, regarding_entity TEXT, regarding_id TEXT, contact_id TEXT, owner_email TEXT,
    idempotency_key TEXT, conversation_id TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, subject TEXT NOT NULL, description TEXT, due TEXT, priority TEXT, regarding_entity TEXT,
    regarding_id TEXT, owner_email TEXT, source_task_id TEXT, phonecall_id TEXT, idempotency_key TEXT,
    conversation_id TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS insights (
    id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL, conversation_id TEXT, source TEXT,
    source_interaction_id TEXT, template_id TEXT, language TEXT, summary_markdown TEXT, sections_json TEXT,
    relationship_facts_json TEXT, engine TEXT, model TEXT, confidence REAL, review_status TEXT,
    transcript_pointer TEXT, phonecall_id TEXT, regarding_entity TEXT, regarding_id TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS insight_products (
    insight_id TEXT NOT NULL, product_id TEXT NOT NULL, PRIMARY KEY (insight_id, product_id));
CREATE TABLE IF NOT EXISTS insight_topics (
    insight_id TEXT NOT NULL, topic_id TEXT NOT NULL, PRIMARY KEY (insight_id, topic_id));
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY, conversation_id TEXT, created_at TEXT NOT NULL, refs_json TEXT);
CREATE INDEX IF NOT EXISTS ix_contacts_email ON contacts (lower(email));
CREATE INDEX IF NOT EXISTS ix_opps_account ON opportunities (account_id);
CREATE INDEX IF NOT EXISTS ix_phonecalls_created ON phonecalls (created_at);
"""

_REGARDING_NAME = """
CASE {p}.regarding_entity
    WHEN 'account' THEN (SELECT name FROM accounts WHERE id = {p}.regarding_id)
    WHEN 'opportunity' THEN (SELECT name FROM opportunities WHERE id = {p}.regarding_id)
    WHEN 'contact' THEN (SELECT name FROM contacts WHERE id = {p}.regarding_id)
END"""


class _SimLookup:
    """CrmLookup over the SQLite tables."""

    def __init__(self, crm: LocalCrm) -> None:
        self._crm = crm

    async def contacts_by_email(self, emails: Sequence[str]) -> list[ContactRow]:
        if not emails:
            return []
        marks = ",".join("?" for _ in emails)
        sql = (
            "SELECT c.id, c.name, c.email, c.account_id, a.name AS account_name FROM contacts c "
            f"LEFT JOIN accounts a ON a.id = c.account_id WHERE lower(c.email) IN ({marks})"
        )
        return [_contact(r) for r in self._crm._query(sql, [e.lower() for e in emails])]

    async def contacts_by_phone(self, phones: Sequence[str]) -> list[ContactRow]:
        wanted = set(phones)
        if not wanted:
            return []
        sql = (
            "SELECT c.id, c.name, c.email, c.phone, c.account_id, a.name AS account_name FROM contacts c "
            "LEFT JOIN accounts a ON a.id = c.account_id WHERE c.phone IS NOT NULL AND c.phone != ''"
        )
        found: list[ContactRow] = []
        for row in self._crm._query(sql):          # phone formats vary, so normalise in Python (the table is small)
            number = normalize_phone(row["phone"])
            if number in wanted:
                found.append(_contact(row, phones=(number,)))
        return found

    async def candidate_accounts(self, names: Sequence[str], domains: Sequence[str]) -> list[AccountRow]:
        return [AccountRow(r["id"], r["name"], r["domain"]) for r in self._crm._query("SELECT * FROM accounts")]

    async def candidate_contacts(self, names: Sequence[str]) -> list[ContactRow]:
        sql = (
            "SELECT c.id, c.name, c.email, c.account_id, a.name AS account_name FROM contacts c "
            "LEFT JOIN accounts a ON a.id = c.account_id"
        )
        return [_contact(r) for r in self._crm._query(sql)]

    async def contacts_of_account(self, account_id: str) -> list[ContactRow]:
        sql = (
            "SELECT c.id, c.name, c.email, c.account_id, a.name AS account_name FROM contacts c "
            "LEFT JOIN accounts a ON a.id = c.account_id WHERE c.account_id = ?"
        )
        return [_contact(r) for r in self._crm._query(sql, [account_id])]

    async def account_by_id(self, account_id: str) -> AccountRow | None:
        rows = self._crm._query("SELECT * FROM accounts WHERE id = ?", [account_id])
        return AccountRow(rows[0]["id"], rows[0]["name"], rows[0]["domain"]) if rows else None

    async def open_opportunities(self, account_id: str) -> list[OpportunityRow]:
        rows = self._crm._query("SELECT * FROM opportunities WHERE account_id = ? ORDER BY rowid", [account_id])
        return [OpportunityRow(r["id"], r["name"], r["account_id"], r["contact_id"], r["stage"]) for r in rows]


def _contact(row: sqlite3.Row, *, phones: tuple[str, ...] = ()) -> ContactRow:
    return ContactRow(row["id"], row["name"], row["email"], row["account_id"], row["account_name"], phones)


def _iso(value: datetime) -> str:
    return value.isoformat()


class LocalCrm:
    """SQLite CRM simulator. `path=":memory:"` gives an isolated, throw-away instance (tests, evaluation runs)."""

    name = "local_sim"

    def __init__(
        self,
        path: str | Path = ":memory:",
        seed: CrmSeed | None = None,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        if self._path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")           # no fsync per commit; the simulator holds throw-away data
            self._db.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._db.executescript(_SCHEMA)
        self._lookup = _SimLookup(self)
        if seed is not None:
            self.load_seed(seed)

    # ---- plumbing --------------------------------------------------------------------------------------
    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, list(params)).fetchall()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def load_seed(self, seed: CrmSeed) -> None:
        """Insert or refresh the synthetic accounts, contacts, opportunities and users."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.executemany(
                    "INSERT OR REPLACE INTO users (id, name, email) VALUES (?, ?, ?)",
                    [(u.id, u.name, u.email) for u in seed.users],
                )
                self._db.executemany(
                    "INSERT OR REPLACE INTO accounts (id, name, domain, segment) VALUES (?, ?, ?, ?)",
                    [(a.id, a.name, a.domain, a.segment) for a in seed.accounts],
                )
                self._db.executemany(
                    "INSERT OR REPLACE INTO contacts (id, name, email, phone, account_id, title) VALUES (?, ?, ?, ?, ?, ?)",
                    [(c.id, c.name, c.email, c.phone, c.account_id, c.title) for c in seed.contacts],
                )
                self._db.executemany(
                    "INSERT OR REPLACE INTO opportunities (id, name, account_id, contact_id, stage) VALUES (?, ?, ?, ?, ?)",
                    [(o.id, o.name, o.account_id, o.contact_id, o.stage) for o in seed.opportunities],
                )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    # ---- CrmPort ---------------------------------------------------------------------------------------
    async def match(self, meta: ConversationMeta) -> CrmMatch:
        return await resolve_match(self._lookup, meta)

    async def write(self, package: CrmPackage) -> CrmWriteResult:
        try:
            assert_no_transcript(package)
        except TranscriptLeakError as exc:
            return CrmWriteResult(ok=False, error=f"rejected: {exc}")
        key = package.idempotency_key.strip()
        if not key:
            return CrmWriteResult(ok=False, error="rejected: idempotency_key is required")

        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if self._db.execute("SELECT 1 FROM idempotency WHERE key = ?", [key]).fetchone():
                    self._db.execute("ROLLBACK")
                    logger.info("CRM write skipped (duplicate idempotency key) for conversation %s", package.conversation_id)
                    return CrmWriteResult(ok=True, skipped_duplicate=True)
                created = self._insert_package(package, key)
                self._db.execute("COMMIT")
            except Exception as exc:
                self._db.execute("ROLLBACK")
                logger.exception("CRM simulator write failed for conversation %s", package.conversation_id)
                return CrmWriteResult(ok=False, error=f"simulator write failed: {type(exc).__name__}")
        logger.info("CRM simulator wrote %d records for conversation %s", len(created), package.conversation_id)
        return CrmWriteResult(ok=True, created=created)

    async def list_records(self, entity: str, limit: int = 100) -> list[dict[str, Any]]:
        handlers = {
            "phonecall": self._list_phonecalls, "task": self._list_tasks, "insight": self._list_insights,
            "account": self._list_accounts, "contact": self._list_contacts, "opportunity": self._list_opportunities,
            "systemuser": self._list_users,
        }
        if entity not in handlers:
            raise ValueError(f"unknown entity {entity!r}; expected one of {', '.join(ENTITIES)}")
        return handlers[entity](max(1, int(limit)))

    async def health(self) -> dict[str, Any]:
        counts = {
            table: self._query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
            for table in ("accounts", "contacts", "opportunities", "users", "phonecalls", "tasks", "insights")
        }
        return {"adapter": self.name, "ok": True, "synthetic": True, "path": self._path, "counts": counts}

    async def delete_conversations(self, conversation_ids: Sequence[str]) -> int:
        """Remove what the given conversations wrote, so a load test can be cleaned up. Returns the phone calls removed.

        Only the simulator can do this: the Dataverse adapter has no delete, so a real CRM is never touched.
        """
        ids = list(conversation_ids)
        removed = 0
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for start in range(0, len(ids), 500):
                    chunk = ids[start:start + 500]
                    marks = ",".join("?" for _ in chunk)
                    insights = f"SELECT id FROM insights WHERE conversation_id IN ({marks})"
                    self._db.execute(f"DELETE FROM insight_products WHERE insight_id IN ({insights})", chunk)
                    self._db.execute(f"DELETE FROM insight_topics WHERE insight_id IN ({insights})", chunk)
                    for table in ("tasks", "insights", "idempotency"):
                        self._db.execute(f"DELETE FROM {table} WHERE conversation_id IN ({marks})", chunk)
                    removed += self._db.execute(f"DELETE FROM phonecalls WHERE conversation_id IN ({marks})", chunk).rowcount
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return removed

    async def clear_written(self) -> dict[str, int]:
        """Remove every phone call, task and insight the pipeline wrote (a demo reset). Returns the rows removed.

        Accounts, contacts, opportunities and users stay, including contacts added with `crm-add`. Like
        delete_conversations, only the simulator can do this: the Dataverse adapter has no such method, so a real CRM is
        never touched.
        """
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                removed = {name: int(self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                           for name, table in (("phonecalls", "phonecalls"), ("tasks", "tasks"), ("insights", "insights"))}
                for table in ("insight_products", "insight_topics", "insights", "tasks", "phonecalls", "idempotency"):
                    self._db.execute(f"DELETE FROM {table}")
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return removed

    # ---- write -----------------------------------------------------------------------------------------
    def _insert_package(self, package: CrmPackage, key: str) -> list[CrmRecordRef]:
        now = _iso(self._clock())
        call = package.phone_call
        regarding = call.regarding or _preferred_regarding(package.regarding)
        contact = call.to_contact or package.regarding.contact
        owner = call.owner_email
        call_id = str(uuid.uuid4())
        duration = call.duration_minutes
        if duration is None and call.end is not None:
            duration = max(0, round((call.end - call.start).total_seconds() / 60))
        self._db.execute(
            "INSERT INTO phonecalls (id, subject, description, direction, start_at, end_at, duration_minutes, "
            "regarding_entity, regarding_id, contact_id, owner_email, idempotency_key, conversation_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                call_id, call.subject, call.description, call.direction, _iso(call.start),
                _iso(call.end) if call.end else None, duration,
                regarding.entity if regarding else None, regarding.id if regarding else None,
                contact.id if contact else None, owner, key, package.conversation_id, now,
            ],
        )
        created = [CrmRecordRef(entity="phonecall", id=call_id, name=call.subject)]

        for task in package.tasks:
            task_id = str(uuid.uuid4())
            target = task.regarding or regarding
            self._db.execute(
                "INSERT INTO tasks (id, subject, description, due, priority, regarding_entity, regarding_id, "
                "owner_email, source_task_id, phonecall_id, idempotency_key, conversation_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    task_id, task.subject, task.description, task.due.isoformat() if task.due else None,
                    task.priority, target.entity if target else None, target.id if target else None,
                    task.owner_email or owner, task.source_task_id, call_id, key, package.conversation_id, now,
                ],
            )
            created.append(CrmRecordRef(entity="task", id=task_id, name=task.subject))

        insight = package.insight
        insight_id = str(uuid.uuid4())
        self._db.execute(
            "INSERT INTO insights (id, idempotency_key, conversation_id, source, source_interaction_id, template_id, "
            "language, summary_markdown, sections_json, relationship_facts_json, engine, model, confidence, "
            "review_status, transcript_pointer, phonecall_id, regarding_entity, regarding_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                insight_id, key, package.conversation_id, insight.source.value, insight.source_interaction_id,
                insight.template_id, insight.language, insight.summary_markdown,
                json.dumps([s.model_dump(mode="json") for s in insight.sections], ensure_ascii=False),
                json.dumps(insight.relationship_facts, ensure_ascii=False), insight.engine, insight.model,
                insight.confidence, insight.review_status, insight.transcript_pointer, call_id,
                regarding.entity if regarding else None, regarding.id if regarding else None, now,
            ],
        )
        self._db.executemany(
            "INSERT OR IGNORE INTO insight_products (insight_id, product_id) VALUES (?, ?)",
            [(insight_id, p) for p in insight.product_ids],
        )
        self._db.executemany(
            "INSERT OR IGNORE INTO insight_topics (insight_id, topic_id) VALUES (?, ?)",
            [(insight_id, t) for t in insight.topic_ids],
        )
        created.append(CrmRecordRef(entity="insight", id=insight_id, name=call.subject))
        self._db.execute(
            "INSERT INTO idempotency (key, conversation_id, created_at, refs_json) VALUES (?, ?, ?, ?)",
            [key, package.conversation_id, now, json.dumps([r.model_dump() for r in created])],
        )
        return created

    # ---- list_records ----------------------------------------------------------------------------------
    def _list_phonecalls(self, limit: int) -> list[dict[str, Any]]:
        sql = (
            "SELECT p.id, p.subject, p.description, p.direction, p.start_at AS start, p.end_at AS \"end\", "
            "p.duration_minutes, p.regarding_entity, p.regarding_id, "
            f"{_REGARDING_NAME.format(p='p')} AS regarding_name, p.contact_id, c.name AS contact_name, "
            "p.owner_email, u.name AS owner_name, p.idempotency_key, p.conversation_id, p.created_at "
            "FROM phonecalls p LEFT JOIN contacts c ON c.id = p.contact_id "
            "LEFT JOIN users u ON lower(u.email) = lower(p.owner_email) "
            "ORDER BY p.created_at DESC, p.rowid DESC LIMIT ?"
        )
        return [dict(r) for r in self._query(sql, [limit])]

    def _list_tasks(self, limit: int) -> list[dict[str, Any]]:
        sql = (
            "SELECT t.id, t.subject, t.description, t.due, t.priority, t.regarding_entity, t.regarding_id, "
            f"{_REGARDING_NAME.format(p='t')} AS regarding_name, t.owner_email, u.name AS owner_name, "
            "t.source_task_id, t.phonecall_id, t.conversation_id, t.created_at "
            "FROM tasks t LEFT JOIN users u ON lower(u.email) = lower(t.owner_email) "
            "ORDER BY t.created_at DESC, t.rowid DESC LIMIT ?"
        )
        return [dict(r) for r in self._query(sql, [limit])]

    def _list_insights(self, limit: int) -> list[dict[str, Any]]:
        sql = (
            "SELECT i.*, " + _REGARDING_NAME.format(p="i") + " AS regarding_name FROM insights i "
            "ORDER BY i.created_at DESC, i.rowid DESC LIMIT ?"
        )
        out: list[dict[str, Any]] = []
        for row in self._query(sql, [limit]):
            item = dict(row)
            item["sections"] = json.loads(item.pop("sections_json") or "[]")
            item["relationship_facts"] = json.loads(item.pop("relationship_facts_json") or "[]")
            item["product_ids"] = [
                r["product_id"] for r in self._query(
                    "SELECT product_id FROM insight_products WHERE insight_id = ? ORDER BY rowid", [item["id"]])
            ]
            item["topic_ids"] = [
                r["topic_id"] for r in self._query(
                    "SELECT topic_id FROM insight_topics WHERE insight_id = ? ORDER BY rowid", [item["id"]])
            ]
            out.append(item)
        return out

    def _list_accounts(self, limit: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query("SELECT * FROM accounts ORDER BY rowid DESC LIMIT ?", [limit])]

    def _list_contacts(self, limit: int) -> list[dict[str, Any]]:
        sql = (
            "SELECT c.id, c.name, c.email, c.phone, c.title, c.account_id, a.name AS account_name FROM contacts c "
            "LEFT JOIN accounts a ON a.id = c.account_id ORDER BY c.rowid DESC LIMIT ?"
        )
        return [dict(r) for r in self._query(sql, [limit])]

    def _list_opportunities(self, limit: int) -> list[dict[str, Any]]:
        sql = (
            "SELECT o.id, o.name, o.stage, o.account_id, a.name AS account_name, o.contact_id, c.name AS contact_name "
            "FROM opportunities o LEFT JOIN accounts a ON a.id = o.account_id "
            "LEFT JOIN contacts c ON c.id = o.contact_id ORDER BY o.rowid DESC LIMIT ?"
        )
        return [dict(r) for r in self._query(sql, [limit])]

    def _list_users(self, limit: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query("SELECT * FROM users ORDER BY rowid DESC LIMIT ?", [limit])]


def _preferred_regarding(match: CrmMatch) -> CrmRecordRef | None:
    return match.opportunity or match.account or match.contact


__all__ = ["LocalCrm", "ENTITIES"]
