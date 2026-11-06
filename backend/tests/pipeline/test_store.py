"""StateStore tests (in-memory SQLite)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from voicecrm.domain.catalogue import LearnedAlias
from voicecrm.domain.conversation import ConversationMeta, Participant
from voicecrm.domain.enums import Channel, ConversationStatus, SpeakerRole, Stage, StageStatus
from voicecrm.domain.evaluation import EvalMetrics, EvalRun
from voicecrm.domain.metering import UsageRecord
from voicecrm.domain.pipeline import ConversationRecord, StageEvent
from voicecrm.ports.state import FeedbackRecord, StateStore
from voicecrm.store.sqlite_store import SqliteStateStore


def _record(cid: str, source_id: str = "src-1", status: ConversationStatus = ConversationStatus.received) -> ConversationRecord:
    meta = ConversationMeta(
        conversation_id=cid, source=Channel.teams, source_interaction_id=source_id,
        seller=Participant(id="seller", display_name="Sam Seller", role=SpeakerRole.seller),
    )
    return ConversationRecord(meta=meta, status=status)


def test_store_satisfies_port():
    assert isinstance(SqliteStateStore(":memory:"), StateStore)


def test_conversation_roundtrip_update_and_find():
    s = SqliteStateStore(":memory:")
    s.save_conversation(_record("c1"))
    rec = s.get_conversation("c1")
    assert rec and rec.meta.source_interaction_id == "src-1"
    rec.status = ConversationStatus.needs_review
    s.save_conversation(rec)
    assert s.get_conversation("c1").status == ConversationStatus.needs_review
    assert s.find_by_source("teams", "src-1").meta.conversation_id == "c1"
    assert s.find_by_source("zoom", "src-1") is None


def test_list_filters_by_status_newest_first():
    s = SqliteStateStore(":memory:")
    a, b = _record("a", "1"), _record("b", "2", ConversationStatus.needs_review)
    b.created_at = a.created_at.replace(year=a.created_at.year + 1)
    s.save_conversation(a)
    s.save_conversation(b)
    assert [r.meta.conversation_id for r in s.list_conversations()] == ["b", "a"]
    assert [r.meta.conversation_id for r in s.list_conversations(status=[ConversationStatus.needs_review])] == ["b"]


def test_events_usage_feedback_ordering():
    s = SqliteStateStore(":memory:")
    for stage in (Stage.ingest, Stage.transcribe):
        s.add_event(StageEvent(conversation_id="c1", stage=stage, status=StageStatus.completed))
    assert [e.stage for e in s.list_events("c1")] == [Stage.ingest, Stage.transcribe]
    s.add_usage(UsageRecord(conversation_id="c1", stage=Stage.transcribe, service="speech", provider="mock", audio_seconds=30))
    s.add_usage(UsageRecord(conversation_id="c2", stage=Stage.extract, service="llm", provider="mock", tokens_in=10, tokens_out=5))
    assert len(s.list_usage("c1")) == 1 and len(s.list_usage()) == 2
    s.add_feedback(FeedbackRecord(conversation_id="c1", kind="product_choice", payload={"mention_id": "m1"}))
    assert s.list_feedback()[0].payload == {"mention_id": "m1"}


def test_alias_upsert_increments_and_clears():
    s = SqliteStateStore(":memory:")
    s.upsert_alias(LearnedAlias(surface="equity fund see", product_id="SYN-1", created_at=datetime.now(timezone.utc)))
    s.upsert_alias(LearnedAlias(surface="equity fund see", product_id="SYN-1"))
    aliases = s.list_aliases()
    assert len(aliases) == 1 and aliases[0].count == 2
    assert s.clear_aliases() == 1 and s.list_aliases() == []


def test_eval_runs_roundtrip():
    s = SqliteStateStore(":memory:")
    s.save_eval_run(EvalRun(run_id="r1", engine="rules", metrics=EvalMetrics(cases=3), cases=[]))
    assert s.get_eval_run("r1").metrics.cases == 3
    assert [r.run_id for r in s.list_eval_runs()] == ["r1"]


def test_file_backed_store_persists(tmp_path):
    path = tmp_path / "nested" / "state.sqlite"
    s = SqliteStateStore(path)
    s.save_conversation(_record("c9"))
    s.close()
    assert SqliteStateStore(path).get_conversation("c9") is not None


def test_a_load_test_batch_is_listed_counted_and_deleted_with_its_history():
    s = SqliteStateStore(":memory:")
    plain, tagged = _record("a", "1"), _record("b", "2")
    tagged.batch_id = "load-x"
    s.save_conversation(plain)
    s.save_conversation(tagged)
    for cid in ("a", "b"):
        s.add_event(StageEvent(conversation_id=cid, stage=Stage.ingest, status=StageStatus.completed))
        s.add_usage(UsageRecord(conversation_id=cid, stage=Stage.transcribe, service="speech", provider="mock", audio_seconds=3))
        s.add_feedback(FeedbackRecord(conversation_id=cid, kind="reject", payload={}))
    assert s.batch_conversation_ids() == ["b"] and s.count_batch() == 1
    assert s.get_conversation("b").batch_id == "load-x" and s.get_conversation("a").batch_id is None
    assert s.delete_conversations(["b"]) == 1
    assert s.get_conversation("b") is None and s.list_events("b") == [] and s.list_usage("b") == []
    assert [f.conversation_id for f in s.list_feedback()] == ["a"]
    assert s.get_conversation("a") is not None and len(s.list_events("a")) == 1 and s.count_batch() == 0
    assert s.delete_conversations([]) == 0


def test_deleting_more_conversations_than_one_chunk_removes_them_all():
    s = SqliteStateStore(":memory:")
    ids = [f"c{n}" for n in range(1200)]
    for n, cid in enumerate(ids):
        rec = _record(cid, f"src-{n}")
        rec.batch_id = "load-big"
        s.save_conversation(rec)
    assert s.count_batch() == 1200
    assert s.delete_conversations(ids) == 1200 and s.count_batch() == 0


def test_a_database_from_before_the_batch_tag_is_migrated_in_place(tmp_path):
    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE conversations (conversation_id TEXT PRIMARY KEY, source TEXT NOT NULL, source_interaction_id TEXT NOT NULL,"
        " status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, data TEXT NOT NULL);")
    rec = _record("old-1")
    old.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?,?)", ("old-1", "teams", "src-1", "received",
                                                                       rec.created_at.isoformat(), rec.updated_at.isoformat(),
                                                                       rec.model_dump_json()))
    old.commit()
    old.close()
    s = SqliteStateStore(path)
    assert s.get_conversation("old-1").batch_id is None and s.count_batch() == 0
    tagged = _record("new-1", "src-2")
    tagged.batch_id = "load-1"
    s.save_conversation(tagged)
    assert s.batch_conversation_ids() == ["new-1"]
    s.close()
    assert SqliteStateStore(path).count_batch() == 1                    # opening a migrated database again is a no-op
