"""Dependency helpers and view builders shared by the routers."""
from __future__ import annotations

from datetime import datetime

from fastapi import Request

from ..container import Container
from ..domain.enums import ConversationStatus, Stage, StageStatus
from ..domain.metering import CostBreakdown
from ..domain.pipeline import ConversationRecord, StageEvent
from .schemas import ConversationDetail, ConversationListItem, StageRecordView

STAGE_ORDER = [Stage.ingest, Stage.transcribe, Stage.normalize, Stage.policy, Stage.extract, Stage.resolve,
               Stage.validate, Stage.review, Stage.retention, Stage.write]


def get_container(request: Request) -> Container:
    return request.app.state.container


def stage_views(events: list[StageEvent]) -> list[StageRecordView]:
    """Collapse started/completed events into one row per stage (latest status wins)."""
    out: list[StageRecordView] = []
    for stage in STAGE_ORDER:
        evs = [e for e in events if e.stage == stage]
        if not evs:
            continue
        started: datetime | None = next((e.ts for e in evs if e.status == StageStatus.started), evs[0].ts)
        last = evs[-1]
        detail: dict = {}
        for e in evs:
            detail.update(e.detail)
        out.append(StageRecordView(stage=stage, status=last.status, started_at=started, duration_ms=last.duration_ms,
                                   detail=detail))
    return out


def cost_for(container: Container, conversation_id: str) -> CostBreakdown:
    usage = container.deps.store.list_usage(conversation_id)
    return container.deps.cost_engine.estimate(usage, container.deps.pricing)


def list_item(container: Container, rec: ConversationRecord, *, with_cost: bool = True) -> ConversationListItem:
    prop = rec.proposal
    latency = None
    if prop and prop.ready_at:
        latency = round((prop.ready_at - rec.created_at).total_seconds(), 2)
    result = prop.result if prop else None
    return ConversationListItem(
        conversation_id=rec.meta.conversation_id, status=rec.status, source=rec.meta.source, market=rec.meta.market,
        business_line=rec.meta.business_line, language=rec.language, template_id=rec.template_id, title=rec.meta.title,
        seller_name=rec.meta.seller.display_name, customer_org=rec.meta.customer_org, started_at=rec.meta.started_at,
        created_at=rec.created_at,
        product_count=len(result.products) if result else 0, topic_count=len(result.topics) if result else 0,
        task_count=len(result.tasks) if result else 0,
        needs_review=rec.status == ConversationStatus.needs_review,
        review_reasons=prop.review.reasons if prop and rec.status == ConversationStatus.needs_review else [],
        auto_approve_eligible=bool(prop and prop.review.auto_approve_eligible),
        latency_seconds=latency,
        cost_usd=round(cost_for(container, rec.meta.conversation_id).total, 6) if with_cost else None,
        fixture_id=rec.fixture_id, batch_id=rec.batch_id,
    )


async def detail(container: Container, rec: ConversationRecord) -> ConversationDetail:
    cid = rec.meta.conversation_id
    store = container.deps.store
    items = await container.deps.vault.list_items(cid)
    kinds = {i.kind for i in items}
    available = ("working" in kinds and rec.status in (ConversationStatus.needs_review, ConversationStatus.failed)) or (
        "transcript" in kinds)
    if available:
        note = ("Redacted working copy, kept only while the proposal awaits review." if "working" in kinds and "transcript" not in kinds
                else "Redacted transcript retained in the vault per market policy.")
    elif rec.retention:
        note = rec.retention.explanation
    else:
        note = "No transcript is available yet."
    events = store.list_events(cid)
    return ConversationDetail(
        record=rec, stages=stage_views(events), events=events, usage=store.list_usage(cid), cost=cost_for(container, cid),
        transcript_available=available, transcript_note=note, vault_items=items)
