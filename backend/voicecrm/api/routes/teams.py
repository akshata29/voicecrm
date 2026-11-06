"""Live Teams ingestion: status, an on-demand poll, and the Graph lifecycle webhook."""
from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...adapters.sources import teams
from ...adapters.sources.teams_graph import GraphError
from ...container import Container
from ...pipeline.teams_live import TeamsStatus
from ..deps import get_container

logger = logging.getLogger(__name__)
router = APIRouter()

OFF = ("Live Teams ingestion is off. Set VOICECRM_TEAMS_MODE=poll and the VOICECRM_TEAMS_* app settings in .env, then restart "
       "(see docs/live-demo.md, scenario B).")


class TeamsPollResult(BaseModel):
    ingested: int
    status: TeamsStatus


@router.get("/sources/teams", response_model=TeamsStatus)
async def teams_status(c: Container = Depends(get_container)) -> TeamsStatus:
    if c.teams is None:
        return TeamsStatus(mode="off", configured=False, problem=OFF)
    return c.teams.status()


@router.post("/sources/teams/poll", response_model=TeamsPollResult)
async def teams_poll(c: Container = Depends(get_container)) -> TeamsPollResult:
    """Looks for new transcripts right now instead of waiting for the next scheduled poll."""
    if c.teams is None:
        raise HTTPException(409, OFF)
    try:
        created = await c.teams.poll_once()
    except GraphError as exc:
        raise HTTPException(409 if exc.status is None else 502, str(exc)) from exc
    return TeamsPollResult(ingested=created, status=c.teams.status())


async def _renew(c: Container, subscription_id: str) -> None:
    try:
        await c.teams.graph.renew_subscription(subscription_id, 55)
        logger.info("renewed Graph subscription %s", subscription_id)
    except Exception:
        logger.warning("could not renew Graph subscription %s", subscription_id, exc_info=True)


@router.post("/webhooks/teams/lifecycle")
async def teams_lifecycle(request: Request, background: BackgroundTasks, validationToken: str | None = None,
                          c: Container = Depends(get_container)):
    """Graph lifecycle notifications: reauthorizationRequired is answered by renewing the subscription."""
    if validationToken:
        return Response(content=teams.validation_response(validationToken), media_type="text/plain")
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "request body is not valid JSON") from exc
    state = c.settings.teams_client_state
    for item in payload.get("value", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict) or not state or not hmac.compare_digest(
                str(item.get("clientState") or "").encode(), state.encode()):
            continue
        event = item.get("lifecycleEvent")
        logger.info("Graph lifecycle event %s for subscription %s", event, item.get("subscriptionId"))
        if event == "reauthorizationRequired" and c.teams is not None and c.teams.graph is not None and item.get("subscriptionId"):
            background.add_task(_renew, c, str(item["subscriptionId"]))
    return JSONResponse(status_code=202, content={"status": "accepted"})
