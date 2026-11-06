"""Ingestion: uploads, scripted-call simulation and source webhooks. All channels converge on PipelineService."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ...adapters.sources import detect_and_parse
from ...adapters.sources.recordings import find_recording
from ...container import Container
from ...domain.conversation import ConsentInfo, ConversationMeta, Participant
from ...domain.enums import Channel, Direction, SpeakerRole
from ...pipeline.fixtures import ingest_from_scripted
from ...pipeline.models import IngestInput
from ..deps import get_container
from ..schemas import FixtureInfo, IngestResponse, SimulateRequest

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/fixtures", response_model=list[FixtureInfo])
async def fixtures(c: Container = Depends(get_container)) -> list[FixtureInfo]:
    out = []
    for f in c.scripted_calls.values():
        recording = find_recording(f.fixture_id, c.settings.recordings_dir)
        out.append(FixtureInfo(
            fixture_id=f.fixture_id, title=f.title, description=f.description, channel=f.channel, language=f.language,
            market=f.market, business_line=f.business_line, call_type=f.call_type, tags=f.tags,
            duration_minutes=f.duration_minutes, turns=len(f.turns),
            recording_seconds=recording.seconds if recording else None))
    return out


@router.get("/fixtures/{fixture_id}/audio")
async def fixture_audio(fixture_id: str, c: Container = Depends(get_container)) -> FileResponse:
    """The real speech recording of a scripted call, so it can be played in the browser before it is processed."""
    if fixture_id not in c.scripted_calls:
        raise HTTPException(404, f"unknown fixture {fixture_id}")
    recording = find_recording(fixture_id, c.settings.recordings_dir)
    if recording is None:
        raise HTTPException(404, "no recording for this call: run tools\\gen_call_audio.py to create it")
    return FileResponse(recording.path, media_type=recording.content_type)


@router.post("/ingest/simulate", response_model=IngestResponse)
async def simulate(body: SimulateRequest, c: Container = Depends(get_container)) -> IngestResponse:
    call = c.scripted_calls.get(body.fixture_id)
    if call is None:
        raise HTTPException(404, f"unknown fixture {body.fixture_id}")
    if body.via not in ("transcript", "audio"):
        raise HTTPException(422, "via must be 'transcript' or 'audio'")
    ing = ingest_from_scripted(call, channel=body.channel, market=body.market, business_line=body.business_line,
                               template_id=body.template_id, via=body.via, asr_noise=body.asr_noise, nonce=uuid4().hex[:6],
                               recordings_dir=c.settings.recordings_dir)
    result = await c.service.submit(ing)
    return IngestResponse(conversation_id=result.conversation_id, duplicate_of=result.duplicate_of)


@router.post("/ingest/upload", response_model=IngestResponse)
async def upload(
    file: UploadFile = File(...),
    channel: Channel = Form(Channel.upload),
    market: str | None = Form(None),
    business_line: str | None = Form(None),
    call_type: str | None = Form(None),
    seller_name: str | None = Form(None),
    seller_email: str | None = Form(None),
    customer_name: str | None = Form(None),
    customer_org: str | None = Form(None),
    customer_email: str | None = Form(None),
    language_hint: str | None = Form(None),
    title: str | None = Form(None),
    started_at: str | None = Form(None),
    source_interaction_id: str | None = Form(None),
    fixture_id: str | None = Form(None),
    c: Container = Depends(get_container),
) -> IngestResponse:
    data = await file.read()
    if not data:
        raise HTTPException(422, "empty file")
    try:
        parsed = detect_and_parse(file.filename or "upload", file.content_type, data, channel=channel)
    except Exception as exc:  # parser errors are user-facing; never echo file content
        raise HTTPException(422, f"could not parse {file.filename}: {type(exc).__name__}") from exc
    hint = parsed.meta_hint or {}
    when = datetime.now(timezone.utc)
    for raw in (started_at, hint.get("started_at")):
        if raw:
            try:
                when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                break
            except ValueError:
                continue
    duration = hint.get("duration_seconds") or (parsed.segments[-1].end_ms / 1000.0 if parsed.segments else None)
    interaction = source_interaction_id or parsed.source_interaction_id or f"upload-{hashlib.sha256(data).hexdigest()[:12]}"
    meta = ConversationMeta(
        source=channel, source_interaction_id=interaction,
        direction=Direction.in_person if channel == Channel.mobile else Direction.outbound,
        title=title or hint.get("title") or file.filename, started_at=when,
        ended_at=when + timedelta(seconds=duration) if duration else None, duration_seconds=duration,
        seller=Participant(id="seller", display_name=seller_name or "Demo Seller",
                           email=seller_email or "demo.seller@example.com", role=SpeakerRole.seller),
        participants=[Participant(id="cust1", display_name=customer_name or "Customer", email=customer_email,
                                  org=customer_org, role=SpeakerRole.customer)],
        customer_org=customer_org, market=market or c.settings.default_market,
        business_line=business_line or c.settings.default_business_line, call_type=call_type,
        language_hint=language_hint or parsed.language,
        consent=ConsentInfo(status="recorded_with_consent", basis="uploaded by seller"))
    ing = IngestInput(
        meta=meta, kind=parsed.kind, segments=parsed.segments, speakers=parsed.speakers, transcript_origin=parsed.origin,
        language=parsed.language, audio_bytes=parsed.audio_bytes, audio_filename=parsed.audio_filename,
        audio_content_type=parsed.audio_content_type, fixture_id=fixture_id)
    result = await c.service.submit(ing)
    return IngestResponse(conversation_id=result.conversation_id, duplicate_of=result.duplicate_of)


# ---- source webhooks: production path. The demo drives the same pipeline through /ingest/simulate. -------------
async def _json_object(request: Request) -> tuple[bytes, dict]:
    body = await request.body()
    try:
        data = json.loads(body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "request body is not valid JSON") from exc
    if not isinstance(data, dict):
        raise HTTPException(400, "request body must be a JSON object")
    return body, data


@router.post("/webhooks/zoom")
async def zoom_webhook(request: Request, c: Container = Depends(get_container)):
    from ...adapters.sources import zoom

    body, payload = await _json_object(request)
    secret = c.settings.zoom_webhook_secret
    if payload.get("event") == "endpoint.url_validation":
        if not secret:
            raise HTTPException(503, "VOICECRM_ZOOM_WEBHOOK_SECRET is not configured")
        token = (payload.get("payload") or {}).get("plainToken") if isinstance(payload.get("payload"), dict) else None
        if not token:
            raise HTTPException(422, "endpoint.url_validation needs payload.plainToken")
        return zoom.url_validation_response(token, secret)
    if secret and not zoom.verify_signature(dict(request.headers), body, secret):
        raise HTTPException(401, "invalid Zoom signature")
    try:
        files = zoom.recording_files(payload) if str(payload.get("event", "")).startswith("recording.") else []
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(422, "unrecognised Zoom recording payload") from exc
    logger.info("zoom webhook %s accepted (%d recording files)", payload.get("event"), len(files))
    return JSONResponse(status_code=202, content={
        "status": "accepted", "recording_files": len(files),
        "note": "Live download needs Zoom OAuth credentials; use /ingest/simulate or /ingest/upload for the demo."})


@router.post("/webhooks/teams")
async def teams_webhook(request: Request, background: BackgroundTasks, validationToken: str | None = None,
                        c: Container = Depends(get_container)):
    from ...adapters.sources import teams

    if validationToken:
        return Response(content=teams.validation_response(validationToken), media_type="text/plain")
    _, payload = await _json_object(request)
    try:
        refs = teams.handle_graph_notification(payload, expected_client_state=c.settings.teams_client_state or None)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(422, "unrecognised Graph notification payload") from exc
    live = c.teams is not None and c.teams.graph is not None and bool(refs)
    # Without a client state anyone could name a meeting for the app to fetch, so live ingestion requires it
    authenticated = bool(c.settings.teams_client_state)
    if live and authenticated:
        background.add_task(_ingest_teams_refs, c, refs)          # Graph expects its answer within seconds
    logger.info("teams notification accepted (%d transcript refs, live ingest %s)", len(refs), "on" if live and authenticated else "off")
    note = None
    if not live:
        note = "Not ingesting: set VOICECRM_TEAMS_MODE and the VOICECRM_TEAMS_* app settings (see docs/live-demo.md)."
    elif not authenticated:
        note = "Not ingesting: set VOICECRM_TEAMS_CLIENT_STATE (any long random string) and subscribe with it, so notifications can be authenticated."
    return JSONResponse(status_code=202, content={
        "status": "accepted", "transcripts": len(refs), "ingesting": live and authenticated, "note": note})


async def _ingest_teams_refs(c: Container, refs) -> None:
    try:
        await c.teams.ingest_refs(refs)
    except Exception:
        logger.warning("could not ingest a Teams transcript from a notification", exc_info=True)


@router.post("/webhooks/genesys")
async def genesys_webhook(request: Request, c: Container = Depends(get_container)):
    from ...adapters.sources import genesys

    _, payload = await _json_object(request)
    try:
        refs = genesys.handle_notification(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(422, "unrecognised Genesys notification payload") from exc
    logger.info("genesys notification accepted (%s)", refs)
    return JSONResponse(status_code=202, content={
        "status": "accepted", "note": "Live fetch needs Genesys OAuth client credentials; the transcript URL expires in 10 minutes."})
