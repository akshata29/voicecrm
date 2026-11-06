"""Pipeline state: events, retention decision, proposal under review, the persisted conversation record."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import Field

from .base import VModel
from .conversation import ConversationMeta, utcnow
from .crm import CrmMatch, CrmPackage, CrmWriteResult
from .enums import ConversationStatus, Stage, StageStatus, TranscriptOrigin
from .extraction import ExtractionResult, PolicyAction, ValidationReport


class StageEvent(VModel):
    conversation_id: str
    stage: Stage
    status: StageStatus
    ts: datetime = Field(default_factory=utcnow)
    duration_ms: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class RetentionDecision(VModel):
    """What the market policy decided. Shown in the UI so the per-market toggle is visible."""

    market: str
    transcript_stored: bool
    transcript_redacted: bool = True
    transcript_expires_at: datetime | None = None
    audio_expires_at: datetime | None = None   # None = audio deleted immediately after transcription
    crm_contains_transcript: bool = False      # invariant: always False
    explanation: str


class ReviewInfo(VModel):
    state: Literal["pending", "approved", "auto_approved", "rejected"] = "pending"
    reasons: list[str] = Field(default_factory=list)       # why a human is needed, e.g. "product_needs_pick"
    auto_approve_eligible: bool = False
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None
    clicks: int = 0                            # reviewer clicks spent (the "fewer clicks" metric)
    seconds_to_review: float | None = None     # review-ready -> decision


class Proposal(VModel):
    template_id: str
    note_markdown: str                         # rendered note exactly as it would land in CRM
    result: ExtractionResult
    validation: ValidationReport
    crm_match: CrmMatch
    crm_preview: CrmPackage | None = None
    review: ReviewInfo = Field(default_factory=ReviewInfo)
    policy_actions: list[PolicyAction] = Field(default_factory=list)
    ready_at: datetime | None = None           # when it became review-ready (latency metric)


class TaskEdit(VModel):
    task_id: str
    title: str | None = None
    due_date: date | None = None
    drop: bool = False


class ApproveRequest(VModel):
    """Reviewer decision. Every field is optional: the happy path is an empty body (one click)."""

    product_choices: dict[str, str] = Field(default_factory=dict)       # mention_id -> product_id
    drop_mentions: list[str] = Field(default_factory=list)              # mention_ids to remove
    topic_ids: list[str] | None = None                                  # replace topics
    task_edits: list[TaskEdit] = Field(default_factory=list)
    section_edits: dict[str, str] = Field(default_factory=dict)         # section key -> text
    template_id: str | None = None                                      # re-render with another template first
    clicks: int = 1


class ConversationRecord(VModel):
    meta: ConversationMeta
    status: ConversationStatus = ConversationStatus.received
    language: str | None = None
    transcript_origin: TranscriptOrigin | None = None
    engine: str | None = None
    template_id: str | None = None
    audio_seconds: float | None = None
    proposal: Proposal | None = None
    retention: RetentionDecision | None = None
    crm_result: CrmWriteResult | None = None
    error: str | None = None
    fixture_id: str | None = None              # set when created from a scripted demo call
    batch_id: str | None = None                # set on calls created by a load test, so they can be removed together
    version: int = 1                           # bumped on reprocess; part of the idempotency key
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
