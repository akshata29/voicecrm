"""Application state store port (conversation records, events, usage, feedback, learned aliases, eval runs).

The store holds metadata, proposals and audit data only. It must NEVER persist transcript text.
Implementation: SQLite (voicecrm.store.sqlite_store). Synchronous on purpose; call from async code directly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field

from ..domain.base import VModel
from ..domain.catalogue import LearnedAlias
from ..domain.conversation import utcnow
from ..domain.enums import ConversationStatus
from ..domain.evaluation import EvalRun
from ..domain.metering import UsageRecord
from ..domain.pipeline import ConversationRecord, StageEvent


class FeedbackRecord(VModel):
    conversation_id: str
    kind: Literal["product_choice", "topic_edit", "task_edit", "section_edit", "reject"]
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=utcnow)


@runtime_checkable
class StateStore(Protocol):
    def save_conversation(self, rec: ConversationRecord) -> None: ...                     # upsert by meta.conversation_id
    def get_conversation(self, conversation_id: str) -> ConversationRecord | None: ...
    def find_by_source(self, source: str, source_interaction_id: str) -> ConversationRecord | None: ...
    def list_conversations(
        self, *, status: list[ConversationStatus] | None = None, limit: int = 100, offset: int = 0
    ) -> list[ConversationRecord]: ...
    def batch_conversation_ids(self) -> list[str]: ...                                    # created by a load test
    def count_batch(self) -> int: ...
    def delete_conversations(self, conversation_ids: list[str]) -> int: ...               # with events, usage, feedback

    def add_event(self, ev: StageEvent) -> None: ...
    def list_events(self, conversation_id: str) -> list[StageEvent]: ...

    def add_usage(self, u: UsageRecord) -> None: ...
    def list_usage(self, conversation_id: str | None = None) -> list[UsageRecord]: ...

    def add_feedback(self, fb: FeedbackRecord) -> None: ...
    def list_feedback(self) -> list[FeedbackRecord]: ...

    def upsert_alias(self, alias: LearnedAlias) -> None: ...                             # increments count on repeat
    def list_aliases(self) -> list[LearnedAlias]: ...
    def clear_aliases(self) -> int: ...

    def save_eval_run(self, run: EvalRun) -> None: ...
    def get_eval_run(self, run_id: str) -> EvalRun | None: ...
    def list_eval_runs(self, limit: int = 20) -> list[EvalRun]: ...

    def count_all(self) -> dict[str, int]: ...                                            # rows per table (what a reset removes)
    def clear_all(self) -> dict[str, int]: ...                                            # every conversation, alias and eval run
