"""Minimal fake engines so the orchestrator can be tested independently of the real engines."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from voicecrm.domain.conversation import CanonicalConversation, Segment
from voicecrm.domain.enums import Engine, MentionType, OwnerRole, SpeakerRole
from voicecrm.domain.extraction import (
    Evidence, ExtractionResult, NoteSection, PolicyAction, ProductCandidate, ProductMention, TaskItem, TopicTag,
    ValidationFinding, ValidationReport,
)
from voicecrm.domain.metering import CostBreakdown, CostLine, PricingTable, UsageRecord
from voicecrm.domain.pipeline import RetentionDecision
from voicecrm.domain.policy import MarketPolicy, NotesPolicy
from voicecrm.engines.contracts import ExtractionContext, ExtractionOutput, NotesPolicyResult, PolicySegment

CARD = re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b")


class FakeNotes:
    """Redacts PII spans to tokens and omits any segment mentioning 'cancer'."""

    def apply(self, conv: CanonicalConversation, *, notes: NotesPolicy, pii_spans) -> NotesPolicyResult:
        segments, redacted, actions = [], [], []
        for seg in conv.segments:
            text = seg.text
            for span in sorted(pii_spans.get(seg.id, []), key=lambda s: s.start, reverse=True):
                text = text[: span.start] + f"[{span.category.upper()}]" + text[span.end:]
                actions.append(PolicyAction(kind="pii_redacted", category=span.category, segment_ids=[seg.id], detail="token"))
            label = "business"
            if "cancer" in text.lower():
                label, text = "sensitive", ""
                actions.append(PolicyAction(kind="omitted_sensitive", category="medical", segment_ids=[seg.id], detail="omitted"))
            role = SpeakerRole.seller if seg.speaker_id == conv.meta.seller.id else SpeakerRole.customer
            segments.append(PolicySegment(id=seg.id, speaker_id=seg.speaker_id, speaker_role=role, text=text, label=label,
                                          locale=seg.locale))
            redacted.append(Segment(id=seg.id, speaker_id=seg.speaker_id, start_ms=seg.start_ms, end_ms=seg.end_ms,
                                    text=text or "[omitted: medical]", locale=seg.locale))
        return NotesPolicyResult(segments=segments, actions=actions,
                                 redacted_conversation=conv.model_copy(update={"segments": redacted}))


class FakeResolver:
    def __init__(self) -> None:
        self.aliases: list = []

    def detect_mentions(self, segments, *, language): return []
    def resolve(self, mentions, *, business_line, top_k=3, review_threshold=0.75): return []
    def reload_aliases(self, aliases) -> None: self.aliases = list(aliases)
    def phrase_list(self, limit=500): return ["Contoso Equity Fund"]


class FakeExtractor:
    """One ambiguous product (class not stated), one topic, one dated task, every template section filled."""

    name = "rules"

    def __init__(self, *, ambiguous: bool = True, fail: Exception | None = None, confidence: float = 0.95) -> None:
        self.ambiguous, self.fail, self.confidence, self.calls = ambiguous, fail, confidence, 0

    async def extract(self, ctx: ExtractionContext) -> ExtractionOutput:
        self.calls += 1
        if self.fail:
            raise self.fail
        segs = {s.id: s for s in ctx.conversation.segments}
        ev = Evidence(segment_ids=[1], quote=segs[1].text if 1 in segs else "quote")
        sections = [NoteSection(key=s.key, title=s.title, text=f"{s.title} text" if s.kind == "text" else None,
                                items=[f"{s.title} item"] if s.kind == "list" else []) for s in ctx.template.sections]
        cands = [ProductCandidate(product_id="P-C", display_name="Contoso Equity Fund Class C", fund_name="Contoso Equity Fund",
                                  share_class="C", score=0.5),
                 ProductCandidate(product_id="P-A", display_name="Contoso Equity Fund Class A", fund_name="Contoso Equity Fund",
                                  share_class="A", score=0.5)]
        mention = ProductMention(
            mention_id="m1", surface="Contoso Equity Fund", evidence=[ev], candidates=cands,
            chosen_product_id=None if self.ambiguous else "P-C", confidence=0.4 if self.ambiguous else 0.97,
            mention_type=MentionType.interest, needs_review=self.ambiguous,
            reasons=["share_class_not_stated"] if self.ambiguous else [])
        task = TaskItem(task_id="t1", title="Send fee comparison", owner_role=OwnerRole.seller, due_date=date(2026, 11, 13),
                        due_text="by Friday", due_basis="stated", confidence=0.9, evidence=[ev])
        result = ExtractionResult(
            template_id=ctx.template.template_id, sections=sections, products=[mention],
            topics=[TopicTag(topic_id="fees_expenses", label="Fees and expenses", confidence=0.9, evidence=[ev])],
            tasks=[task], overall_confidence=self.confidence, engine=Engine.rules)
        return ExtractionOutput(result=result)


class FakeValidator:
    """Flags 16-digit card numbers anywhere in the note fields."""

    def validate(self, result: ExtractionResult, ctx: ExtractionContext, *, notes: NotesPolicy):
        text = " ".join([*(s.text or "" for s in result.sections), *(i for s in result.sections for i in s.items),
                         *(t.title for t in result.tasks)])
        leaks = len(CARD.findall(text))
        findings = [ValidationFinding(severity="error", code="leak.payment_card", message="card number in note")] * leaks
        return result, ValidationReport(passed=not leaks, findings=findings, leak_count=leaks)


class FakeRenderer:
    def render(self, result, template, meta, *, catalogue) -> str:
        lines = [f"# {template.name}"]
        for s in result.sections:
            lines.append(f"## {s.title}")
            lines.append(s.text or "")
            lines.extend(f"- {i}" for i in s.items)
        return "\n".join(lines)


class FakeRetention:
    def decide(self, policy: MarketPolicy, *, now: datetime) -> RetentionDecision:
        keep = policy.transcript_retention == "on"
        return RetentionDecision(
            market=policy.market, transcript_stored=keep,
            transcript_expires_at=now + timedelta(days=policy.transcript_retention_days or 0) if keep else None,
            explanation=f"{policy.market}: transcript {'retained' if keep else 'discarded'} per policy; CRM never holds it.")


class FakeCost:
    def estimate(self, usage: list[UsageRecord], pricing: PricingTable) -> CostBreakdown:
        tokens = sum((u.tokens_in or 0) + (u.tokens_out or 0) for u in usage)
        seconds = sum(u.audio_seconds or 0 for u in usage)
        lines = [CostLine(service="llm", quantity=tokens, unit="tokens", est_cost=tokens * 1e-6),
                 CostLine(service="speech", quantity=seconds, unit="s", est_cost=seconds / 3600.0)]
        return CostBreakdown(lines=lines, total=sum(line.est_cost for line in lines))


def fake_select_template(templates, meta, processing, requested=None):
    if requested:
        return templates[requested]
    return templates[processing.default_template_by_business_line.get(meta.business_line, "pb_institutional")]
