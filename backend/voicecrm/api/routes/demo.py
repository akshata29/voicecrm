"""Demo housekeeping: what a reset would remove, and the reset itself (Live demo page, "Reset demo data")."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...container import Container
from ...pipeline.demo_reset import DemoCounts, DemoData, DemoResetRequest, inspect, reset
from ...pipeline.service import PipelineError
from ..deps import get_container

router = APIRouter()


@router.get("/demo/data", response_model=DemoData)
async def demo_data(c: Container = Depends(get_container)) -> DemoData:
    """What a reset would remove right now, and whether it can run (it cannot against a real CRM or while calls run)."""
    return await inspect(c)


@router.post("/demo/reset", response_model=DemoCounts)
async def demo_reset(body: DemoResetRequest, c: Container = Depends(get_container)) -> DemoCounts:
    """Removes every call, CRM row, vault copy, learned alias, evaluation run and saved policy change. Needs `confirm: true`."""
    if not body.confirm:
        raise PipelineError("confirmation required: this removes every call, CRM row written by the pipeline, vault copy, "
                            "learned alias, evaluation run and saved policy change; resend with confirm=true", status=409)
    return await reset(c)
