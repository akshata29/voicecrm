"""Extraction outputs: summary sections, product mentions, topics, tasks, validation."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

from .base import VModel
from .enums import Engine, MentionType, OwnerRole


class Evidence(VModel):
    segment_ids: list[int]
    quote: str                                 # verbatim (redacted) text from the transcript supporting the claim


class ProductCandidate(VModel):
    product_id: str
    display_name: str
    fund_name: str
    share_class: str
    score: float                               # 0..1
    matched_on: list[str] = Field(default_factory=list)   # e.g. ["fund_name", "share_class", "alias", "learned_alias"]


class ProductMention(VModel):
    mention_id: str                            # stable within a proposal: "m1", "m2", ...
    surface: str                               # text as spoken, e.g. "Contoso Equity Fund, class C"
    evidence: list[Evidence]
    candidates: list[ProductCandidate]         # best first, at most top_k
    chosen_product_id: str | None              # None when a human must pick
    confidence: float                          # 0..1 for the chosen product
    mention_type: MentionType = MentionType.info_only
    share_class_stated: str | None = None      # share class actually said (canonical key), if any
    needs_review: bool = False
    reasons: list[str] = Field(default_factory=list)      # e.g. "share_class_not_stated", "low_margin", "no_candidate"


class TopicTag(VModel):
    topic_id: str
    label: str
    confidence: float
    evidence: list[Evidence]


class TaskItem(VModel):
    task_id: str                               # "t1", "t2", ...
    title: str
    description: str | None = None
    owner_role: OwnerRole = OwnerRole.seller
    owner_ref: str | None = None               # participant id when known
    due_date: date | None = None
    due_text: str | None = None                # as said, e.g. "by Friday"
    due_basis: Literal["stated", "inferred", "none"] = "none"
    confidence: float
    evidence: list[Evidence]


class NoteSection(VModel):
    key: str                                   # matches TemplateSection.key
    title: str
    text: str | None = None
    items: list[str] = Field(default_factory=list)


class RelationshipFact(VModel):
    text: str                                  # e.g. "Supports the Seattle Seahawks"
    category: str = "interest"
    evidence: list[Evidence] = Field(default_factory=list)


class PolicyAction(VModel):
    """Audit trail of what the notes policy did (shown in the UI and used by evaluation)."""

    kind: Literal["pii_redacted", "omitted_sensitive", "dropped_small_talk", "kept_relationship"]
    category: str | None = None
    segment_ids: list[int] = Field(default_factory=list)
    detail: str


class ExtractionResult(VModel):
    template_id: str
    language_out: str = "en"
    sections: list[NoteSection]
    products: list[ProductMention] = Field(default_factory=list)
    topics: list[TopicTag] = Field(default_factory=list)
    tasks: list[TaskItem] = Field(default_factory=list)
    relationship_facts: list[RelationshipFact] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)        # e.g. "competitor_named", "pricing_discussed"
    overall_confidence: float
    engine: Engine
    model: str | None = None


class ValidationFinding(VModel):
    severity: Literal["error", "warning"]
    code: str                                  # e.g. "leak.payment_card", "evidence.missing", "catalogue.unknown_product"
    message: str
    where: str | None = None


class ValidationReport(VModel):
    passed: bool
    findings: list[ValidationFinding] = Field(default_factory=list)
    leak_count: int = 0                        # sensitive items found in output fields (must be 0 to pass)
    unsupported_claims: int = 0                # claims without transcript evidence
