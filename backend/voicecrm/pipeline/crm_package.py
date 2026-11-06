"""Builds the CrmPackage from a reviewed proposal. The transcript is never part of it."""
from __future__ import annotations

from typing import Literal

from ..domain.crm import CrmPackage, CrmRecordRef, InsightPayload, PhoneCallPayload, TaskPayload
from ..domain.enums import Direction, OwnerRole
from ..domain.pipeline import ConversationRecord, Proposal
from ..domain.templates import SummaryTemplate


def idempotency_key(rec: ConversationRecord) -> str:
    return f"{rec.meta.source.value}:{rec.meta.source_interaction_id}:v{rec.version}"


def build_crm_package(
    rec: ConversationRecord,
    proposal: Proposal,
    template: SummaryTemplate,
    *,
    review_status: Literal["approved", "auto_approved"] = "approved",
    transcript_pointer: str | None = None,
) -> CrmPackage:
    meta, match, result = rec.meta, proposal.crm_match, proposal.result
    regarding: CrmRecordRef | None = match.opportunity or match.account
    title = meta.title or meta.customer_org or (match.account.name if match.account else "customer")
    duration = None
    if meta.duration_seconds:
        duration = max(1, round(meta.duration_seconds / 60))
    elif meta.ended_at:
        duration = max(1, round((meta.ended_at - meta.started_at).total_seconds() / 60))

    phone_call = PhoneCallPayload(
        subject=f"{template.name}: {title}",
        description=proposal.note_markdown,
        direction="inbound" if meta.direction == Direction.inbound else "outbound",
        start=meta.started_at,
        end=meta.ended_at,
        duration_minutes=duration,
        regarding=regarding,
        to_contact=match.contact,
        owner_email=meta.seller.email,
    )
    tasks = [
        TaskPayload(
            source_task_id=t.task_id,
            subject=t.title,
            description=t.description,
            due=t.due_date,
            regarding=regarding,
            owner_email=meta.seller.email,
        )
        for t in result.tasks
        if t.owner_role in (OwnerRole.seller, OwnerRole.internal, OwnerRole.unknown)
    ]
    insight = InsightPayload(
        source=meta.source,
        source_interaction_id=meta.source_interaction_id,
        template_id=template.template_id,
        language=result.language_out,
        summary_markdown=proposal.note_markdown,
        sections=result.sections,
        product_ids=[m.chosen_product_id for m in result.products if m.chosen_product_id],
        topic_ids=[t.topic_id for t in result.topics],
        relationship_facts=[f.text for f in result.relationship_facts],
        engine=result.engine.value,
        model=result.model,
        confidence=result.overall_confidence,
        review_status=review_status,
        transcript_pointer=transcript_pointer,
    )
    return CrmPackage(
        idempotency_key=idempotency_key(rec),
        conversation_id=meta.conversation_id,
        regarding=match,
        phone_call=phone_call,
        tasks=tasks,
        insight=insight,
    )
