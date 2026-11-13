"""Live demo page: run a load test from the UI, and check the live services with the same probes as the preflight CLI."""
from __future__ import annotations

import logging
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ...container import Container
from ...pipeline.load import LoadEstimate, LoadRequest, LoadStatus
from ...preflight import ALL_PROBES, Check, readiness, run_preflight
from ..deps import get_container

logger = logging.getLogger(__name__)
router = APIRouter()

TEAMS_OFF = ("set VOICECRM_TEAMS_MODE=poll and the VOICECRM_TEAMS_* app settings in .env, then restart "
             "(see docs/live-demo.md, scenario B)")
PHONE_OFF = ("set VOICECRM_PHONE_MODE=acs and the VOICECRM_PHONE_* settings in .env, then restart "
             "(see docs/phone-line.md)")


class PreflightRequest(BaseModel):
    only: list[Literal["speech", "language", "model", "search", "vault", "crm", "teams", "phone"]] | None = None


class PreflightCheck(BaseModel):
    name: str
    status: Literal["pass", "warn", "fail", "skip"]
    detail: str = ""
    hint: str = ""
    seconds: float | None = None


class ReadinessItem(BaseModel):
    scenario: str
    verdict: str                               # READY | NOT READY | NOT CONFIGURED
    why: str


class PreflightResponse(BaseModel):
    checks: list[PreflightCheck]
    readiness: list[ReadinessItem]
    seconds: float


class DeletedLoadCalls(BaseModel):
    deleted: int


# ---- load test -------------------------------------------------------------------------------------------------------
@router.get("/live/load", response_model=LoadStatus)
async def load_status(c: Container = Depends(get_container)) -> LoadStatus:
    return c.load.status()


@router.post("/live/load/estimate", response_model=LoadEstimate)
async def load_estimate(body: LoadRequest, c: Container = Depends(get_container)) -> LoadEstimate:
    """Tokens, list-price cost and rough duration of a run, before anything is spent."""
    return c.load.estimate(body)


@router.post("/live/load", response_model=LoadStatus, status_code=202)
async def load_start(body: LoadRequest, c: Container = Depends(get_container)) -> LoadStatus:
    """Starts the run in the background; poll GET /live/load. Billed services need `confirm: true`."""
    return await c.load.start(body)


@router.post("/live/load/stop", response_model=LoadStatus)
async def load_stop(c: Container = Depends(get_container)) -> LoadStatus:
    return await c.load.stop()


@router.delete("/live/load/calls", response_model=DeletedLoadCalls)
async def load_clear(c: Container = Depends(get_container)) -> DeletedLoadCalls:
    """Removes every call a load test created (inbox, vault, state store and the local CRM simulator)."""
    return DeletedLoadCalls(deleted=await c.load.clear())


# ---- service checks --------------------------------------------------------------------------------------------------
def _relevant(name: str, wanted: set[str]) -> bool:
    if wanted == {"teams"}:
        return name.startswith(("teams", "config: teams", "live probes"))
    if wanted == {"phone"}:
        return name.startswith(("phone", "config: phone", "live probes"))
    return True


@router.post("/live/preflight", response_model=PreflightResponse)
async def preflight(body: PreflightRequest, request: Request, c: Container = Depends(get_container)) -> PreflightResponse:
    """Probes the live services this API is running on with small synthetic requests (a few cents at most)."""
    state = request.app.state
    if getattr(state, "preflight_running", False):
        raise HTTPException(409, "a service check is already running")
    wanted = set(body.only) if body.only else set(ALL_PROBES)
    state.preflight_running = True
    started = time.perf_counter()
    try:
        checks = await run_preflight(c.settings, only=wanted, container=c)
    finally:
        state.preflight_running = False
    if "teams" in wanted and c.settings.teams_mode == "off" and body.only:
        checks.append(Check("teams: Graph access", "skip", "Teams live ingestion is off (VOICECRM_TEAMS_MODE=off)", TEAMS_OFF))
    if "phone" in wanted and c.settings.phone_mode == "off" and body.only:
        checks.append(Check("phone: line", "skip", "the phone line is off (VOICECRM_PHONE_MODE=off)", PHONE_OFF))
    shown: list[Check] = [ch for ch in checks if _relevant(ch.name, wanted)]
    return PreflightResponse(
        checks=[PreflightCheck(name=ch.name, status=ch.status, detail=ch.detail, hint=ch.hint, seconds=ch.seconds)
                for ch in shown],
        readiness=[ReadinessItem(scenario=s, verdict=v, why=w) for s, v, w in readiness(checks, c.settings)],
        seconds=round(time.perf_counter() - started, 1))
