"""Phone line webhooks: Event Grid notifications and Call Automation callbacks.

These are the only routes the public internet ever needs. They live in their own router so the ingress listener (a second
uvicorn server, see api/phone_ingress.py) can serve them and nothing else: tunnel THAT port, never the API port.

* ``POST /webhooks/phone/events?key=<secret>``   Event Grid (IncomingCall, RecordingFileStatusUpdated). The key is the
  secret in the subscription's URL; without it every request is refused, including the subscription handshake.
* ``POST /webhooks/phone/calls/{call_id}?t=<token>``   Call Automation callbacks for one call. The token is an HMAC of the call
  id, so only a URL this app handed to Azure is accepted.
* ``GET  /webhooks/phone/health``   answers "this is the phone ingress" (no secret, no data): the self-check uses it to prove the
  tunnel reaches the right port.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ...adapters.sources import phone as wire
from ...container import Container
from ..deps import get_container

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_BODY_BYTES = 1_000_000                       # Event Grid batches are far smaller; this only stops abuse


def _line(c: Container):
    if c.phone is None:
        raise HTTPException(503, "the phone line is off (VOICECRM_PHONE_MODE)")
    return c.phone


async def _json_body(request: Request):
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(413, "request body too large")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(413, "request body too large")
    try:
        return await request.json()
    except ValueError as exc:
        raise HTTPException(400, "request body is not valid JSON") from exc


@router.get("/webhooks/phone/health")
async def phone_health(c: Container = Depends(get_container)) -> dict:
    return {"service": "voicecrm-phone", "ok": True, "line": c.phone is not None}


@router.options("/webhooks/phone/events")
async def phone_events_handshake(request: Request, key: str | None = None, c: Container = Depends(get_container)) -> Response:
    """CloudEvents delivery asks permission with an OPTIONS request before it sends anything."""
    line = _line(c)
    if not wire.valid_event_key(line.settings.phone_webhook_secret, key):
        raise HTTPException(401, "unauthorised")
    origin = request.headers.get("webhook-request-origin") or "*"
    return Response(status_code=200, headers={"WebHook-Allowed-Origin": origin, "Allow": "POST, OPTIONS"})


@router.post("/webhooks/phone/events")
async def phone_events(request: Request, key: str | None = None, c: Container = Depends(get_container)) -> dict:
    line = _line(c)
    if not wire.valid_event_key(line.settings.phone_webhook_secret, key):
        logger.warning("phone events: request refused (wrong or missing key)")
        raise HTTPException(401, "unauthorised")
    events = wire.as_events(await _json_body(request))
    code = wire.validation_code(events)
    if code:                                                  # Event Grid's one-off ownership handshake
        return {"validationResponse": code}
    await line.handle_events(events)
    return {"status": "accepted", "events": len(events)}


@router.post("/webhooks/phone/calls/{call_id}")
async def phone_callbacks(call_id: str, request: Request, t: str | None = None, c: Container = Depends(get_container)) -> dict:
    line = _line(c)
    if not wire.valid_callback_token(line.settings.phone_webhook_secret, call_id, t):
        logger.warning("phone callbacks: request refused (bad token)")
        raise HTTPException(401, "unauthorised")
    callbacks = wire.parse_callbacks(await _json_body(request))
    line.dispatch_callbacks(call_id, callbacks)               # processed in order in the background: ACS wants a fast 200
    return {"status": "accepted", "events": len(callbacks)}
