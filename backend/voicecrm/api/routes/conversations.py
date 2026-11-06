"""Conversations: list, detail, transcript, approve, reject, reprocess."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ...container import Container
from ...domain.enums import ConversationStatus
from ...domain.pipeline import ApproveRequest
from ..deps import detail, get_container, list_item
from ..schemas import ConversationDetail, ConversationListItem, ReprocessRequest, RejectRequest

router = APIRouter()


@router.get("/conversations", response_model=list[ConversationListItem])
async def list_conversations(
    status: list[ConversationStatus] | None = Query(None), limit: int = Query(100, le=500), offset: int = 0,
    c: Container = Depends(get_container),
) -> list[ConversationListItem]:
    recs = c.deps.store.list_conversations(status=status, limit=limit, offset=offset)
    order = {ConversationStatus.needs_review: 0, ConversationStatus.processing: 1, ConversationStatus.received: 1}
    items = [list_item(c, r) for r in recs]
    return sorted(items, key=lambda i: (order.get(i.status, 2), -i.created_at.timestamp()))


@router.get("/conversations/{cid}", response_model=ConversationDetail)
async def get_conversation(cid: str, c: Container = Depends(get_container)) -> ConversationDetail:
    return await detail(c, c.service._require(cid))


@router.get("/conversations/{cid}/transcript")
async def get_transcript(cid: str, c: Container = Depends(get_container)):
    conv, source, note = await c.service.get_transcript(cid)
    if conv is None:
        return JSONResponse(status_code=404, content={"detail": note})
    return JSONResponse(content=conv.model_dump(mode="json"), headers={"X-Transcript-Source": source or ""})


@router.post("/conversations/{cid}/approve", response_model=ConversationDetail)
async def approve(cid: str, body: ApproveRequest | None = None, c: Container = Depends(get_container)) -> ConversationDetail:
    rec = await c.service.approve(cid, body or ApproveRequest())
    return await detail(c, rec)


@router.post("/conversations/{cid}/reject", response_model=ConversationDetail)
async def reject(cid: str, body: RejectRequest | None = None, c: Container = Depends(get_container)) -> ConversationDetail:
    rec = await c.service.reject(cid, body.reason if body else None)
    return await detail(c, rec)


@router.post("/conversations/{cid}/reprocess", response_model=ConversationDetail)
async def reprocess(cid: str, body: ReprocessRequest, c: Container = Depends(get_container)) -> ConversationDetail:
    rec = await c.service.reprocess(cid, body.template_id, body.engine)
    return await detail(c, rec)
