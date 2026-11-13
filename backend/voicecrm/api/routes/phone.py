"""Phone line operator routes: status, browser tokens and the seller console registration (the API port, never tunnelled)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...container import Container
from ...pipeline.phone_live import PhoneStatus, PhoneToken, PhoneTokenRequest
from ...ports.telephony import TelephonyError
from ..deps import get_container

logger = logging.getLogger(__name__)
router = APIRouter()

OFF = ("The phone line is off. Set VOICECRM_PHONE_MODE=acs and the VOICECRM_PHONE_* settings in .env, then restart "
       "(see docs/phone-line.md).")


class SellerRegistration(BaseModel):
    identity: str = Field(min_length=1, max_length=300)
    name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=254)


class SellerRegistered(BaseModel):
    registered: bool
    ttl_seconds: int = 0


@router.get("/sources/phone", response_model=PhoneStatus)
async def phone_status(c: Container = Depends(get_container)) -> PhoneStatus:
    if c.phone is None:
        return PhoneStatus(mode="off", configured=False, problem=OFF)
    return c.phone.status()


@router.post("/phone/token", response_model=PhoneToken)
async def phone_token(body: PhoneTokenRequest, c: Container = Depends(get_container)) -> PhoneToken:
    """A VoIP access token for the browser dialer (role caller) or the seller console (role seller)."""
    if c.phone is None or c.phone.status().problem:
        raise HTTPException(409, (c.phone.status().problem if c.phone else None) or OFF)
    try:
        return await c.phone.issue_token(body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except TelephonyError as exc:
        raise HTTPException(409 if exc.status is None else 502, str(exc)) from exc


@router.post("/phone/seller", response_model=SellerRegistered)
async def phone_seller_register(body: SellerRegistration, c: Container = Depends(get_container)) -> SellerRegistered:
    """Registers (and heartbeats) a browser as the seller console: new calls ring it while it keeps checking in."""
    if c.phone is None:
        raise HTTPException(409, OFF)
    return SellerRegistered(registered=True, ttl_seconds=c.phone.register_console(body.identity, body.name, body.email))


@router.delete("/phone/seller", response_model=SellerRegistered)
async def phone_seller_unregister(identity: str, c: Container = Depends(get_container)) -> SellerRegistered:
    if c.phone is None:
        raise HTTPException(409, OFF)
    c.phone.unregister_console(identity)
    return SellerRegistered(registered=False)
