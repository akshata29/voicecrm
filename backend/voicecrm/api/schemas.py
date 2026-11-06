"""HTTP request/response shapes for /api/v1. The frontend mirrors these (and the domain models) exactly."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from ..config import ResolvedMode
from ..domain.enums import Channel, ConversationStatus, Stage, StageStatus
from ..domain.metering import CostBreakdown, UsageRecord
from ..domain.pipeline import ConversationRecord, RetentionDecision, StageEvent
from ..domain.policy import MarketPolicy, NotesPolicy, ProcessingPolicy
from ..ports.vault import VaultItem


class TemplateInfo(BaseModel):
    template_id: str
    name: str
    description: str
    business_lines: list[str]
    call_types: list[str]
    sections: list[str]                        # section titles


class ConfigResponse(BaseModel):
    app_version: str
    demo_mode: bool
    modes: dict[str, ResolvedMode]             # transcription | llm | search | pii | crm | vault
    markets: list[MarketPolicy]
    templates: list[TemplateInfo]
    business_lines: list[str]
    channels: list[str]
    engine: str                                # rules | llm | hybrid (effective)
    catalogue_products: int
    catalogue_synthetic: bool
    pricing_illustrative: bool
    llm_deployment: str | None = None          # the model deployment in use; only when the model is live

class ConversationListItem(BaseModel):
    conversation_id: str
    status: ConversationStatus
    source: Channel
    market: str
    business_line: str
    language: str | None
    template_id: str | None
    title: str | None
    seller_name: str
    customer_org: str | None
    started_at: datetime
    created_at: datetime
    product_count: int = 0
    topic_count: int = 0
    task_count: int = 0
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)
    auto_approve_eligible: bool = False
    latency_seconds: float | None = None       # ingest -> review-ready
    cost_usd: float | None = None
    fixture_id: str | None = None
    batch_id: str | None = None                # set on calls created by a load test (Live demo page)

class StageRecordView(BaseModel):
    stage: Stage
    status: StageStatus
    started_at: datetime | None = None
    duration_ms: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class ConversationDetail(BaseModel):
    record: ConversationRecord
    stages: list[StageRecordView]
    events: list[StageEvent]
    usage: list[UsageRecord]
    cost: CostBreakdown
    transcript_available: bool
    transcript_note: str                       # why it is / is not available (market policy explanation)
    vault_items: list[VaultItem]


class IngestResponse(BaseModel):
    conversation_id: str
    duplicate_of: str | None = None            # set when the same (source, source_interaction_id) was already ingested


class SimulateRequest(BaseModel):
    fixture_id: str
    channel: Channel | None = None             # override the fixture's channel
    market: str | None = None
    business_line: str | None = None
    template_id: str | None = None
    via: str = "transcript"                    # transcript | audio (audio path uses the mock transcriber's sidecar, or live Speech)
    asr_noise: bool = False                    # inject realistic ASR mistakes (e.g. "class see")


class FixtureInfo(BaseModel):
    fixture_id: str
    title: str
    description: str
    channel: Channel
    language: str
    market: str
    business_line: str
    call_type: str | None
    tags: list[str]
    duration_minutes: int
    turns: int
    recording_seconds: float | None = None     # a real speech recording of this call exists (play it, then run it via audio)


class ReprocessRequest(BaseModel):
    template_id: str | None = None
    engine: str | None = None                  # rules | llm | hybrid


class RejectRequest(BaseModel):
    reason: str | None = None


class PoliciesResponse(BaseModel):
    markets: list[MarketPolicy]
    notes: NotesPolicy
    processing: ProcessingPolicy


class CatalogueStats(BaseModel):
    products: int
    families: int
    share_classes: dict[str, int]
    asset_classes: dict[str, int]
    synthetic: bool
    note: str
    learned_aliases: int


class ProductSearchHit(BaseModel):
    product_id: str
    display_name: str
    fund_name: str
    share_class: str
    asset_class: str
    score: float


class MetricsSummary(BaseModel):
    conversations: int
    by_status: dict[str, int]
    by_channel: dict[str, int]
    written: int
    needs_review: int
    auto_approved: int
    latency_p50_seconds: float | None
    latency_p95_seconds: float | None
    mean_cost_usd: float | None
    mean_clicks_to_approve: float | None
    mean_seconds_to_review: float | None
    cost_illustrative: bool = True


class EvalRunRequest(BaseModel):
    engine: str | None = None
    with_asr_noise: bool = False
    apply_learning: bool = False               # apply learned aliases (from reviewer corrections) before the run
    label: str | None = None


class SweepResponse(BaseModel):
    deleted: list[VaultItem]


class RetentionPreview(BaseModel):
    market: str
    decision: RetentionDecision
