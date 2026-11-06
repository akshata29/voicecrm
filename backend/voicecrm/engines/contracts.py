"""Engine contracts: the interfaces between the pipeline and the pure-Python engines.

The engines agent implements these in separate modules (see the module map below); the pipeline codes against
the Protocols only. Engines are synchronous and deterministic except `Extractor`, which may call an LlmPort.

Module map (implementation files, all under voicecrm/engines/):
  notes_policy.py      NotesPolicyEngine   PII redaction + sensitive / small-talk / relationship labelling
  catalogue_index.py   (helper)            in-process lexical + fuzzy index over the catalogue, alias overlay
  product_resolver.py  ProductResolverEngine
  topics.py            TopicEngine
  tasks.py             TaskEngine
  summarizer.py        SummaryEngine       rules-based (extractive) note sections
  extractor.py         Extractor           rules | llm | hybrid orchestration, LLM prompt + JSON schema
  validator.py         Validator           leak check, evidence check, catalogue/taxonomy check
  templates.py         NoteRenderer + select_template()
  retention.py         RetentionEngine
  cost.py              CostEngine
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field

from ..domain.base import VModel
from ..domain.catalogue import Catalogue, LearnedAlias
from ..domain.conversation import CanonicalConversation, ConversationMeta
from ..domain.enums import MentionType, SpeakerRole
from ..domain.extraction import (
    ExtractionResult, NoteSection, PolicyAction, ProductMention, RelationshipFact, TaskItem, TopicTag,
    ValidationReport,
)
from ..domain.metering import CostBreakdown, PricingTable, UsageRecord
from ..domain.pipeline import RetentionDecision
from ..domain.policy import MarketPolicy, NotesPolicy
from ..domain.templates import SummaryTemplate
from ..ports.pii import PiiSpan

SegmentLabel = Literal["business", "small_talk", "sensitive", "relationship"]

# Evidence marker for facts a reviewer asserted by hand (no transcript segment supports them; a human vouches for them).
REVIEWER_EVIDENCE = "reviewer edit"


class PolicySegment(VModel):
    """A transcript segment after the notes policy. `text` is what downstream stages are allowed to see."""

    id: int
    speaker_id: str
    speaker_role: SpeakerRole = SpeakerRole.unknown
    text: str                                  # PII tokens substituted, e.g. "[PAYMENT_CARD]"; "" when sensitive-omitted
    label: SegmentLabel = "business"
    locale: str | None = None


class NotesPolicyResult(VModel):
    segments: list[PolicySegment]
    actions: list[PolicyAction] = Field(default_factory=list)
    relationship_facts: list[RelationshipFact] = Field(default_factory=list)
    redacted_conversation: CanonicalConversation   # segments redacted/omitted exactly as `segments`; safe to persist per retention policy


class RawProductMention(VModel):
    surface: str
    segment_ids: list[int]
    quote: str
    share_class_stated: str | None = None      # canonical share-class key if the speaker said one
    mention_type: MentionType = MentionType.info_only
    explicit: bool = False                     # True when the mention type came from a wording cue, not a default
    branded: bool = True                       # False for brand-less references ("the equity fund")
    negated_classes: list[str] = Field(default_factory=list)   # share classes the speaker ruled out ("not class A")
    corrects: bool = False                     # the sentence corrects an earlier statement ("actually, it's ...")


class ExtractionContext(VModel):
    conversation: CanonicalConversation        # the redacted conversation (never the original)
    policy: NotesPolicyResult
    template: SummaryTemplate
    call_date: date
    language: str                              # dominant language, "en" or "es"
    business_line: str
    top_k_products: int = 3
    product_review_threshold: float = 0.75


class ExtractionOutput(VModel):
    result: ExtractionResult
    usage: list[UsageRecord] = Field(default_factory=list)   # LLM usage; empty for the rules engine


@runtime_checkable
class NotesPolicyEngine(Protocol):
    def apply(
        self, conv: CanonicalConversation, *, notes: NotesPolicy, pii_spans: dict[int, list[PiiSpan]]
    ) -> NotesPolicyResult:
        """pii_spans is keyed by Segment.id; span offsets are relative to that segment's text."""
        ...


@runtime_checkable
class ProductResolverEngine(Protocol):
    def detect_mentions(self, segments: list[PolicySegment], *, language: str) -> list[RawProductMention]: ...

    def resolve(
        self, mentions: list[RawProductMention], *, business_line: str, top_k: int = 3, review_threshold: float = 0.75
    ) -> list[ProductMention]:
        """Exact share class when stated; otherwise needs_review with the fund's classes as top-k candidates."""
        ...

    def reload_aliases(self, aliases: list[LearnedAlias]) -> None: ...

    def phrase_list(self, limit: int = 500) -> list[str]:
        """Catalogue-derived phrases for Azure Speech phrase lists (fund names, share-class phrases)."""
        ...


@runtime_checkable
class TopicEngine(Protocol):
    def classify(self, segments: list[PolicySegment], *, language: str, business_line: str) -> list[TopicTag]: ...


@runtime_checkable
class TaskEngine(Protocol):
    def extract(self, segments: list[PolicySegment], meta: ConversationMeta, *, language: str) -> list[TaskItem]:
        """Commitments ("I'll follow up with you") -> tasks. Relative dates resolve against meta.started_at."""
        ...


@runtime_checkable
class SummaryEngine(Protocol):
    def summarize(
        self, ctx: ExtractionContext, *, products: list[ProductMention], topics: list[TopicTag], tasks: list[TaskItem]
    ) -> list[NoteSection]: ...


@runtime_checkable
class Extractor(Protocol):
    name: str

    async def extract(self, ctx: ExtractionContext) -> ExtractionOutput: ...


@runtime_checkable
class Validator(Protocol):
    def validate(
        self, result: ExtractionResult, ctx: ExtractionContext, *, notes: NotesPolicy
    ) -> tuple[ExtractionResult, ValidationReport]:
        """Returns a cleaned result (leaks scrubbed, unsupported claims dropped) and the report of what was found."""
        ...


@runtime_checkable
class NoteRenderer(Protocol):
    def render(
        self, result: ExtractionResult, template: SummaryTemplate, meta: ConversationMeta, *, catalogue: Catalogue
    ) -> str:
        """Markdown note exactly as it will be written to the CRM."""
        ...


@runtime_checkable
class RetentionEngine(Protocol):
    def decide(self, policy: MarketPolicy, *, now: datetime) -> RetentionDecision: ...


@runtime_checkable
class CostEngine(Protocol):
    def estimate(self, usage: list[UsageRecord], pricing: PricingTable) -> CostBreakdown: ...
