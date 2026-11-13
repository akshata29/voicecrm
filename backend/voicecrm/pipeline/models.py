"""Pipeline inputs and wiring."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from ..config import Settings
from ..domain.catalogue import Catalogue
from ..domain.conversation import ConversationMeta, Segment
from ..domain.enums import TranscriptOrigin
from ..domain.base import VModel
from ..domain.metering import PricingTable
from ..domain.policy import PolicyBundle
from ..domain.taxonomy import Taxonomy
from ..domain.templates import SummaryTemplate
from ..engines.contracts import (
    CostEngine, Extractor, NoteRenderer, NotesPolicyEngine, ProductResolverEngine, RetentionEngine, Validator,
)
from ..ports.crm import CrmPort
from ..ports.pii import PiiPort
from ..ports.state import StateStore
from ..ports.transcription import TranscriptionPort
from ..ports.vault import VaultPort


class IngestInput(VModel):
    """What an adapter hands to the pipeline: either a platform transcript or audio, plus metadata."""

    meta: ConversationMeta
    kind: Literal["transcript", "audio"]
    segments: list[Segment] = []
    speakers: list[str] = []                   # raw speaker labels in order of first appearance
    transcript_origin: TranscriptOrigin | None = None
    language: str | None = None
    audio_bytes: bytes | None = None
    audio_filename: str | None = None
    audio_content_type: str | None = None
    channel_speakers: list[str] = []           # dual-channel audio: the participant id on each channel (index = channel)
    fixture_id: str | None = None
    batch_id: str | None = None                # tags the calls of one load test
    asr_noise: bool = False
    requested_template_id: str | None = None
    engine: str | None = None                  # rules | llm | hybrid (None = configured default)


class IngestResult(VModel):
    conversation_id: str
    duplicate_of: str | None = None


@dataclass
class PipelineDeps:
    settings: Settings
    store: StateStore
    transcription: TranscriptionPort
    pii: PiiPort
    vault: VaultPort
    crm: CrmPort
    resolver: ProductResolverEngine
    notes_engine: NotesPolicyEngine
    extractors: dict[str, Extractor]           # "rules" always present; "llm" / "hybrid" when a model is configured
    validator: Validator
    renderer: NoteRenderer
    retention_engine: RetentionEngine
    cost_engine: CostEngine
    catalogue: Catalogue
    taxonomy: Taxonomy
    templates: dict[str, SummaryTemplate]
    pricing: PricingTable
    policy_provider: Callable[[], PolicyBundle]
    select_template: Callable[..., SummaryTemplate]
    default_engine: str = "rules"
    max_concurrency: int = 4
    extras: dict[str, object] = field(default_factory=dict)
