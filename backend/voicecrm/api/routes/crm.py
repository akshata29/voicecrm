"""CRM view, vault inspection, retention sweep."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ...container import Container
from ...ports.vault import VaultItem
from ..deps import get_container
from ..schemas import SweepResponse

router = APIRouter()
_ENTITIES = {"phonecall", "task", "insight", "account", "contact", "opportunity"}


@router.get("/crm/records")
async def crm_records(entity: str = Query("phonecall"), limit: int = Query(100, le=500),
                      c: Container = Depends(get_container)) -> list[dict[str, Any]]:
    if entity not in _ENTITIES:
        raise HTTPException(422, f"entity must be one of {sorted(_ENTITIES)}")
    return await c.deps.crm.list_records(entity, limit)


@router.get("/crm/health")
async def crm_health(c: Container = Depends(get_container)) -> dict[str, Any]:
    return await c.deps.crm.health()


@router.get("/vault/items", response_model=list[VaultItem])
async def vault_items(conversation_id: str | None = None, c: Container = Depends(get_container)) -> list[VaultItem]:
    return await c.deps.vault.list_items(conversation_id)


@router.post("/retention/sweep", response_model=SweepResponse)
async def retention_sweep(c: Container = Depends(get_container)) -> SweepResponse:
    return SweepResponse(deleted=await c.service.sweep_retention())
