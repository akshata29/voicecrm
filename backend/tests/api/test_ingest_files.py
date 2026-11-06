"""File upload ingestion (POST /ingest/upload) and the source webhooks (POST /webhooks/zoom|teams|genesys)."""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import time
import wave
from typing import Any

import pytest
from fastapi.testclient import TestClient

from voicecrm.adapters.sources import render_for_channel, zoom
from voicecrm.container import Container
from voicecrm.domain.enums import Channel

from .helpers import API, scripted_calls, wait_for_status

CALLS = scripted_calls()
CLEAN = "pb-teams-equity-class-c"
CALL = CALLS[CLEAN]
GOLD = CALL.gold
CUSTOMER = CALL.customers[0]
SECRET = "whsec-test-secret"

# A tiny two-party conversation that mirrors the commitments of the CLEAN scripted call.
SCRIPT = [
    ("Sam Rivera", "Good morning, Dana. Thanks for joining. I wanted to walk you through the Contoso Equity Fund, Class C."),
    ("Dana Whitfield", "Morning, Sam. We are looking at the Contoso Equity Fund, Class C for our pension plan, and fees matter to us."),
    ("Sam Rivera", "Understood. I will send you the fee comparison by Friday, and I will share the updated fact sheet tomorrow."),
    ("Dana Whitfield", "Great, thank you. That would be very helpful for our committee."),
]


def _stamp(milliseconds: int) -> str:
    seconds, millis = divmod(milliseconds, 1000)
    return f"00:{seconds // 60:02d}:{seconds % 60:02d}.{millis:03d}"


CUE_START, CUE_STEP, CUE_LENGTH = 1000, 6500, 5500
SCRIPT_SECONDS = (CUE_START + (len(SCRIPT) - 1) * CUE_STEP + CUE_LENGTH) / 1000


def teams_vtt(script: list[tuple[str, str]] = SCRIPT) -> str:
    """Teams export style: WEBVTT, timing line, then the text inside a <v Speaker> voice tag."""
    lines = ["WEBVTT", ""]
    for n, (speaker, text) in enumerate(script):
        start = CUE_START + n * CUE_STEP
        lines += [f"{_stamp(start)} --> {_stamp(start + CUE_LENGTH)}", f"<v {speaker}>{text}</v>", ""]
    return "\n".join(lines)


def zoom_vtt(script: list[tuple[str, str]] = SCRIPT) -> str:
    """Zoom cloud-recording style: numbered cues and 'Speaker: text' payload lines."""
    lines = ["WEBVTT", ""]
    for n, (speaker, text) in enumerate(script, start=1):
        start = CUE_START + (n - 1) * CUE_STEP
        lines += [str(n), f"{_stamp(start)} --> {_stamp(start + CUE_LENGTH)}", f"{speaker}: {text}", ""]
    return "\n".join(lines)


def silent_wav(seconds: float = 0.5) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * int(8000 * seconds))
    return buffer.getvalue()


def form(channel: str = "teams", **extra: str) -> dict[str, str]:
    fields = {"channel": channel, "market": CALL.market, "business_line": CALL.business_line,
              "seller_name": CALL.seller.display_name, "seller_email": CALL.seller.email,
              "customer_name": CUSTOMER.display_name, "customer_email": CUSTOMER.email,
              "customer_org": CALL.customer_org, "title": "Uploaded call", "started_at": CALL.started_at, **extra}
    return {key: value for key, value in fields.items() if value}


def upload(client: TestClient, filename: str, data: bytes | str, content_type: str, **fields: str):
    payload = data.encode("utf-8") if isinstance(data, str) else data
    return client.post(f"{API}/ingest/upload", files={"file": (filename, payload, content_type)}, data=fields)


def conversation_count(client: TestClient) -> int:
    return len(client.get(f"{API}/conversations", params={"limit": 500}).json())


def _assert_gold_content(detail: dict[str, Any]) -> None:
    result = detail["record"]["proposal"]["result"]
    assert {m["chosen_product_id"] for m in result["products"]} == {g.product_id for g in GOLD.products}
    for expected in GOLD.tasks:
        assert any(t["due_date"] == expected.due_date.isoformat()
                   and all(k.lower() in (t["title"] + " " + (t["description"] or "")).lower() for k in expected.keywords)
                   for t in result["tasks"]), expected.keywords


# ---- transcript uploads ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("flavour", "filename", "text", "channel", "origin"), [
    ("teams", "teams-call.vtt", teams_vtt(), "teams", "teams_platform"),
    ("zoom", "zoom-call.vtt", zoom_vtt(), "zoom", "zoom_platform"),
], ids=["teams-webvtt-voice-tags", "zoom-webvtt-speaker-prefix"])
def test_webvtt_upload_is_ingested_and_reaches_review(client: TestClient, flavour: str, filename: str, text: str,
                                                       channel: str, origin: str) -> None:
    response = upload(client, filename, text, "text/vtt", **form(channel))
    assert response.status_code == 200, response.text
    conversation_id = response.json()["conversation_id"]
    assert response.json()["duplicate_of"] is None

    detail = wait_for_status(client, conversation_id)
    record, proposal = detail["record"], detail["record"]["proposal"]
    assert record["status"] == "needs_review", record["error"]
    meta = record["meta"]
    assert (meta["source"], record["transcript_origin"]) == (channel, origin)
    assert (meta["title"], meta["seller"]["email"], meta["customer_org"], meta["market"]) == (
        "Uploaded call", CALL.seller.email, CALL.customer_org, "US")
    assert meta["started_at"].startswith(CALL.started_at[:19])
    assert meta["source_interaction_id"].startswith("upload-")
    assert meta["duration_seconds"] == pytest.approx(SCRIPT_SECONDS)
    assert record["language"].startswith("en")

    _assert_gold_content(detail)
    assert proposal["crm_match"]["account"]["id"] == GOLD.expect_crm_account
    assert proposal["crm_match"]["method"] == "email"
    assert proposal["review"]["reasons"] == []

    segments = client.get(f"{API}/conversations/{conversation_id}/transcript").json()["segments"]
    assert len(segments) == len(SCRIPT)
    assert [s["speaker_id"] == "seller" for s in segments] == [name == CALL.seller.display_name for name, _ in SCRIPT]


def test_upload_without_metadata_uses_documented_defaults(client: TestClient) -> None:
    response = upload(client, "weekly-sync.vtt", teams_vtt(), "text/vtt")
    detail = wait_for_status(client, response.json()["conversation_id"])
    meta = detail["record"]["meta"]
    assert detail["record"]["status"] == "needs_review", detail["record"]["error"]
    assert meta["source"] == "upload"
    assert (meta["market"], meta["business_line"]) == ("US", "professional_buyer")
    assert meta["seller"]["email"] == "demo.seller@example.com"
    assert meta["title"] == "weekly-sync.vtt"
    assert meta["source_interaction_id"].startswith("upload-")


@pytest.mark.parametrize("channel", ["zoom", "genesys", "upload"])
def test_rendered_json_transcripts_can_be_uploaded(client: TestClient, channel: str) -> None:
    """Zoom timeline JSON, Genesys transcript JSON and the canonical VoiceCRM conversation JSON."""
    payload = render_for_channel(CALL, Channel(channel), zoom_format="json")
    assert payload.filename.endswith(".json")
    response = upload(client, payload.filename, payload.data, payload.content_type, **form(channel))
    assert response.status_code == 200, response.text
    detail = wait_for_status(client, response.json()["conversation_id"])
    assert detail["record"]["status"] == "needs_review", detail["record"]["error"]
    assert detail["record"]["meta"]["source"] == channel
    _assert_gold_content(detail)


# ---- de-duplication ----------------------------------------------------------------------------------------------------
def test_uploading_the_same_file_twice_returns_the_existing_conversation(client: TestClient) -> None:
    script = SCRIPT + [("Sam Rivera", "Dedup marker one.")]
    first = upload(client, "dup.vtt", teams_vtt(script), "text/vtt", **form())
    first_id = first.json()["conversation_id"]
    wait_for_status(client, first_id)
    before = conversation_count(client)

    again = upload(client, "dup.vtt", teams_vtt(script), "text/vtt", **form())
    assert again.status_code == 200
    assert again.json() == {"conversation_id": first_id, "duplicate_of": first_id}
    assert conversation_count(client) == before


def test_an_explicit_source_interaction_id_is_the_deduplication_key(client: TestClient) -> None:
    first = upload(client, "a.vtt", teams_vtt(SCRIPT + [("Sam Rivera", "Dedup marker two.")]), "text/vtt",
                   **form(source_interaction_id="meeting-4711"))
    first_id = first.json()["conversation_id"]
    detail = wait_for_status(client, first_id)
    assert detail["record"]["meta"]["source_interaction_id"] == "meeting-4711"

    other_bytes = upload(client, "b.vtt", teams_vtt(SCRIPT + [("Sam Rivera", "Dedup marker three.")]), "text/vtt",
                         **form(source_interaction_id="meeting-4711"))
    assert other_bytes.json()["duplicate_of"] == first_id
    other_channel = upload(client, "a.vtt", teams_vtt(SCRIPT + [("Sam Rivera", "Dedup marker two.")]), "text/vtt",
                           **form("zoom", source_interaction_id="meeting-4711"))
    assert other_channel.json()["duplicate_of"] is None
    wait_for_status(client, other_channel.json()["conversation_id"])


def test_reuploading_a_rejected_call_creates_a_new_version(client: TestClient) -> None:
    text = teams_vtt(SCRIPT + [("Sam Rivera", "Dedup marker four.")])
    first_id = upload(client, "r.vtt", text, "text/vtt", **form()).json()["conversation_id"]
    first = wait_for_status(client, first_id)
    assert client.post(f"{API}/conversations/{first_id}/reject").status_code == 200

    again = upload(client, "r.vtt", text, "text/vtt", **form())
    assert again.json()["duplicate_of"] is None and again.json()["conversation_id"] != first_id
    second = wait_for_status(client, again.json()["conversation_id"])
    assert second["record"]["version"] == first["record"]["version"] + 1
    assert second["record"]["proposal"]["crm_preview"]["idempotency_key"].endswith(":v2")


# ---- audio uploads (mock transcriber) ----------------------------------------------------------------------------------
def test_audio_the_mock_transcriber_does_not_know_fails_with_an_explanation(client: TestClient) -> None:
    response = upload(client, "call.wav", silent_wav(), "audio/wav", **form("mobile"))
    assert response.status_code == 200
    detail = wait_for_status(client, response.json()["conversation_id"])
    assert detail["record"]["status"] == "failed"
    assert "not a known demo recording" in detail["record"]["error"]
    assert detail["record"]["proposal"] is None


def test_demo_audio_is_transcribed_by_the_mock_transcriber(client: TestClient) -> None:
    response = upload(client, f"fixture-{CLEAN}.wav", silent_wav(), "audio/wav", **form("mobile", fixture_id=CLEAN))
    detail = wait_for_status(client, response.json()["conversation_id"])
    assert detail["record"]["status"] == "needs_review", detail["record"]["error"]
    assert detail["record"]["transcript_origin"] == "fixture"
    assert [(u["service"], u["provider"]) for u in detail["usage"] if u["stage"] == "transcribe"] == [("speech", "mock")]
    _assert_gold_content(detail)


# ---- rejected input ---------------------------------------------------------------------------------------------------------
GARBAGE = [
    ("empty-file", "empty.vtt", b"", "text/vtt", "empty file"),
    ("binary", "data.bin", b"\x00\x01\x02garbage\xff\xfe", "application/octet-stream", "could not parse"),
    ("plain-text", "notes.txt", b"hello there this is not a transcript", "text/plain", "could not parse"),
    ("invalid-json", "x.json", b"{not json", "application/json", "could not parse"),
    ("unrecognised-json", "x.json", b'{"hello": "world"}', "application/json", "could not parse"),
    ("json-array", "x.json", b"[1, 2, 3]", "application/json", "could not parse"),
    ("canonical-json-without-segments", "x.json", b'{"meta": {}, "segments": []}', "application/json", "could not parse"),
    ("vtt-without-cues", "x.vtt", b"WEBVTT\n\n", "text/vtt", "could not parse"),
    ("vtt-with-garbage-body", "x.vtt", b"WEBVTT\n\nthis is not a cue\n", "text/vtt", "could not parse"),
    ("truncated-wav-header", "x.wav", b"RIFF\x00\x00\x00\x00WAVEfmt ", "audio/wav", None),
]


@pytest.mark.parametrize(("filename", "data", "content_type", "message"), [g[1:] for g in GARBAGE],
                         ids=[g[0] for g in GARBAGE])
def test_unsupported_or_garbage_uploads_are_client_errors_not_server_errors(
        client: TestClient, filename: str, data: bytes, content_type: str, message: str | None) -> None:
    before = conversation_count(client)
    response = upload(client, filename, data, content_type, **form())
    if message is None:  # a bare audio header is accepted as audio; it then fails during (mock) transcription
        assert response.status_code == 200
        assert wait_for_status(client, response.json()["conversation_id"])["record"]["status"] == "failed"
        return
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert message in detail
    assert "hello there" not in detail, "file content must never be echoed back"
    assert conversation_count(client) == before


def test_upload_form_validation(client: TestClient) -> None:
    assert upload(client, "x.vtt", teams_vtt(), "text/vtt", channel="fax").status_code == 422
    assert client.post(f"{API}/ingest/upload", data={"channel": "teams"}).status_code == 422


def test_unparseable_started_at_is_ignored_rather_than_rejected(client: TestClient) -> None:
    response = upload(client, "late.vtt", teams_vtt(SCRIPT + [("Sam Rivera", "Dedup marker five.")]), "text/vtt",
                      **form(started_at="not-a-date"))
    assert response.status_code == 200
    detail = wait_for_status(client, response.json()["conversation_id"])
    assert detail["record"]["status"] == "needs_review"
    assert detail["record"]["meta"]["started_at"]


# ---- Zoom webhook ---------------------------------------------------------------------------------------------------------
def _hmac_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def signed_headers(body: bytes, *, secret: str = SECRET, timestamp: int | None = None) -> dict[str, str]:
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    return {"x-zm-request-timestamp": stamp, "x-zm-signature": "v0=" + _hmac_hex(secret, f"v0:{stamp}:".encode() + body),
            "content-type": "application/json"}


@pytest.fixture()
def zoom_secret(monkeypatch: pytest.MonkeyPatch, api_container: Container) -> str:
    monkeypatch.setattr(api_container.settings, "zoom_webhook_secret", SECRET)
    return SECRET


def test_zoom_url_validation_returns_the_plain_token_and_its_hmac(client: TestClient, zoom_secret: str) -> None:
    response = client.post(f"{API}/webhooks/zoom", json={"event": "endpoint.url_validation",
                                                         "payload": {"plainToken": "q9Ws2xT"}})
    assert response.status_code == 200
    assert response.json() == {"plainToken": "q9Ws2xT", "encryptedToken": _hmac_hex(zoom_secret, b"q9Ws2xT")}


def test_zoom_url_validation_needs_a_configured_secret(client: TestClient) -> None:
    response = client.post(f"{API}/webhooks/zoom", json={"event": "endpoint.url_validation",
                                                         "payload": {"plainToken": "q9Ws2xT"}})
    assert response.status_code == 503
    assert "VOICECRM_ZOOM_WEBHOOK_SECRET" in response.json()["detail"]


def test_zoom_events_must_carry_a_valid_signature_when_a_secret_is_set(client: TestClient, zoom_secret: str) -> None:
    body = json.dumps({"event": "meeting.started", "payload": {}}).encode()
    ok = client.post(f"{API}/webhooks/zoom", content=body, headers=signed_headers(body))
    assert ok.status_code == 202
    assert ok.json()["status"] == "accepted" and ok.json()["recording_files"] == 0

    tampered = client.post(f"{API}/webhooks/zoom", content=body + b" ", headers=signed_headers(body))
    bad_signature = client.post(f"{API}/webhooks/zoom", content=body, headers={**signed_headers(body), "x-zm-signature": "v0=00"})
    wrong_secret = client.post(f"{API}/webhooks/zoom", content=body, headers=signed_headers(body, secret="another-secret"))
    unsigned = client.post(f"{API}/webhooks/zoom", content=body, headers={"content-type": "application/json"})
    stale = client.post(f"{API}/webhooks/zoom", content=body, headers=signed_headers(body, timestamp=int(time.time()) - 3600))
    for response in (tampered, bad_signature, wrong_secret, unsigned, stale):
        assert response.status_code == 401
        assert response.json() == {"detail": "invalid Zoom signature"}


def test_zoom_recording_completed_lists_the_recording_files_without_creating_conversations(
        client: TestClient, zoom_secret: str) -> None:
    payload = zoom.render_recording_completed_payload(CALL, "https://zoom.example.invalid/recordings")
    body = json.dumps(payload).encode()
    before = conversation_count(client)
    response = client.post(f"{API}/webhooks/zoom", content=body, headers=signed_headers(body))
    assert response.status_code == 202
    assert response.json()["recording_files"] == len(payload["payload"]["object"]["recording_files"]) == 2
    assert conversation_count(client) == before


def test_zoom_events_are_accepted_unsigned_while_no_secret_is_configured(client: TestClient) -> None:
    body = json.dumps({"event": "meeting.ended", "payload": {}}).encode()
    response = client.post(f"{API}/webhooks/zoom", content=body, headers={"content-type": "application/json"})
    assert response.status_code == 202


# ---- Teams + Genesys webhooks -----------------------------------------------------------------------------------------------
def test_teams_subscription_validation_echoes_the_decoded_token_as_plain_text(client: TestClient) -> None:
    response = client.post(f"{API}/webhooks/teams", params={"validationToken": "Validation Token: abc+123"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "Validation Token: abc+123"


def test_teams_change_notification_counts_new_transcripts(client: TestClient) -> None:
    created = {"changeType": "created", "tenantId": "tenant-1",
               "resource": "users/organizer-1/onlineMeetings/meeting-1/transcripts/transcript-1"}
    before = conversation_count(client)
    response = client.post(f"{API}/webhooks/teams", json={"value": [created, {**created, "changeType": "deleted"}]})
    assert response.status_code == 202
    assert response.json()["transcripts"] == 1
    assert client.post(f"{API}/webhooks/teams", json={}).json()["transcripts"] == 0
    assert conversation_count(client) == before


def test_genesys_notification_is_acknowledged(client: TestClient) -> None:
    response = client.post(f"{API}/webhooks/genesys", json={"conversationId": "conversation-1"})
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"


MALFORMED_BODIES = {
    "not-json": b"this is not json",
    "json-array": b"[1, 2, 3]",
    "json-string": b'"just a string"',
    "json-null": b"null",
    "invalid-utf8": b"\xff\xfe\x00{",
}


@pytest.mark.parametrize("path", ["zoom", "teams", "genesys"])
@pytest.mark.parametrize("body", list(MALFORMED_BODIES.values()), ids=list(MALFORMED_BODIES))
def test_webhooks_reject_a_malformed_body_with_a_client_error(client: TestClient, path: str, body: bytes) -> None:
    before = conversation_count(client)
    response = client.post(f"{API}/webhooks/{path}", content=body, headers={"content-type": "application/json"})
    assert response.status_code in (400, 422), response.text
    detail = response.json()["detail"]
    assert isinstance(detail, str) and detail and "this is not json" not in detail
    assert conversation_count(client) == before


def test_zoom_malformed_body_is_a_client_error_even_with_a_valid_signature(client: TestClient, zoom_secret: str) -> None:
    body = b"this is not json"
    response = client.post(f"{API}/webhooks/zoom", content=body, headers=signed_headers(body))
    assert response.status_code in (400, 422), response.text


@pytest.mark.parametrize("body", [
    {"event": "endpoint.url_validation"},
    {"event": "endpoint.url_validation", "payload": {}},
    {"event": "endpoint.url_validation", "payload": {"plainToken": ""}},
    {"event": "endpoint.url_validation", "payload": "not-an-object"},
], ids=["no-payload", "empty-payload", "empty-token", "payload-not-an-object"])
def test_zoom_url_validation_without_a_token_is_a_client_error(client: TestClient, zoom_secret: str,
                                                              body: dict[str, object]) -> None:
    response = client.post(f"{API}/webhooks/zoom", json=body)
    assert response.status_code in (400, 422), response.text
    assert response.json()["detail"]
