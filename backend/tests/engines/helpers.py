"""Builders shared by the engine tests: policy segments, call metadata and a transcript -> extraction-context shortcut."""
from __future__ import annotations

import json
from datetime import UTC, datetime

from voicecrm.adapters.pii.local import LocalPiiDetector
from voicecrm.domain.conversation import CanonicalConversation, ConversationMeta, Participant, Segment
from voicecrm.domain.enums import Channel, SpeakerRole
from voicecrm.domain.policy import NotesPolicy
from voicecrm.domain.templates import SummaryTemplate
from voicecrm.engines.contracts import ExtractionContext, NotesPolicyResult, PolicySegment
from voicecrm.engines.notes_policy import RulesNotesPolicyEngine

CALL_START = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)         # a Tuesday: the anchor for relative due dates
SELLER_ID, CUSTOMER_ID, COLLEAGUE_ID = "seller", "cust1", "colleague"
_ROLES = {"s": SpeakerRole.seller, "c": SpeakerRole.customer, "i": SpeakerRole.internal}
_SPEAKERS = {"s": SELLER_ID, "c": CUSTOMER_ID, "i": COLLEAGUE_ID}


def seg(i: int, text: str, who: str = "c", *, label: str = "business") -> PolicySegment:
    """who: 's' seller, 'c' customer, 'i' internal colleague."""
    return PolicySegment(id=i, speaker_id=_SPEAKERS[who], speaker_role=_ROLES[who], text=text, label=label)


def segments(*turns: tuple[str, str]) -> list[PolicySegment]:
    return [seg(i, text, who) for i, (who, text) in enumerate(turns)]


def make_meta(business_line: str = "professional_buyer", *, started_at: datetime = CALL_START, market: str = "US",
              source: Channel = Channel.teams, customer: str = "Priya Nair") -> ConversationMeta:
    return ConversationMeta(
        source=source, source_interaction_id="src-1", title="Equity call", started_at=started_at,
        seller=Participant(id=SELLER_ID, display_name="Sam Seller", email="sam@example.com", role=SpeakerRole.seller),
        participants=[Participant(id=CUSTOMER_ID, display_name=customer, email=f"{customer.split()[0].lower()}@northwind.example",
                                  org="Northwind Pension Fund", role=SpeakerRole.customer),
                      Participant(id=COLLEAGUE_ID, display_name="Ivy Internal", role=SpeakerRole.internal)],
        customer_org="Northwind Pension Fund", market=market, business_line=business_line, language_hint="en-US")


def make_conversation(turns: list[tuple[str, str]], meta: ConversationMeta | None = None, *,
                      language: str = "en-US") -> CanonicalConversation:
    segs = [Segment(id=i, speaker_id=_SPEAKERS[who], start_ms=i * 4000, end_ms=i * 4000 + 3500, text=text, locale=language)
            for i, (who, text) in enumerate(turns)]
    return CanonicalConversation(meta=meta or make_meta(), segments=segs, language=language)


def run_policy(conv: CanonicalConversation, notes: NotesPolicy | None = None) -> NotesPolicyResult:
    detector = LocalPiiDetector()
    spans = {s.id: detector.detect(s.text) for s in conv.segments}
    return RulesNotesPolicyEngine().apply(conv, notes=notes or NotesPolicy(), pii_spans=spans)


def make_context(turns: list[tuple[str, str]], template: SummaryTemplate, *, business_line: str = "professional_buyer",
                 language: str = "en", notes: NotesPolicy | None = None) -> ExtractionContext:
    """Transcript turns -> notes policy (real PII detector) -> the ExtractionContext the extractors consume."""
    conv = make_conversation(turns, make_meta(business_line), language="es-US" if language == "es" else "en-US")
    policy = run_policy(conv, notes)
    return ExtractionContext(conversation=policy.redacted_conversation, policy=policy, template=template,
                             call_date=conv.meta.started_at.date(), language=language, business_line=business_line)


def task_answer(title: str, owner: str = "seller", segs: tuple[int, ...] = (4,), due: str | None = None,
                due_text: str | None = None) -> dict:
    """One task as the model returns it (segment 4 is the seller's commitment in the extractor tests' transcript)."""
    return {"title": title, "description": None, "owner_role": owner, "due_date": due, "due_text": due_text,
            "segment_ids": list(segs), "confidence": 0.9}


def llm_reply(call, **overrides) -> dict:
    """A well-formed model answer built from the prompt payload the engine sent: every section, one topic, one task."""
    payload = json.loads(call.user)
    data = {
        "sections": [{"key": s["key"], "text": f"Model note: {s['key']}" if s["kind"] == "text" else None,
                      "items": [f"Model item {n}" for n in range(9)] if s["kind"] == "list" else []}
                     for s in payload["template"]["sections"]],
        "products": [{"mention_id": m["mention_id"], "chosen_product_id": None, "mention_type": "objection", "confidence": 0.9}
                     for m in payload["product_mentions"]],
        "topics": [{"topic_id": "fees_expenses", "confidence": 0.8, "segment_ids": [3]}],
        "tasks": [task_answer("Send fee comparison", due="2026-11-13", due_text="by Friday")],
        "flags": ["model_flag"], "overall_confidence": 0.95,
    }
    return {**data, **overrides}
