"""Zoom: webhook validation and signatures, recording files, transcript parsing/rendering (HTTP mocked)."""
from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
import respx

from voicecrm.adapters.sources.zoom import (
    RecordingFile, ZoomError, download_recording_file, download_token_of, parse_zoom_transcript_json, parse_zoom_vtt,
    recording_files, recording_info, render_recording_completed_payload, render_zoom_transcript_json, render_zoom_vtt,
    sign_request, url_validation_response, verify_signature, zoom_json_meta,
)

from .test_sources_fixtures import make_call, spanish_call

SECRET = "webhook-secret-token"
NOW = 1_700_000_000

# shape documented by Zoom (developers.zoom.us blog: "Download recordings using webhooks")
WEBHOOK = {
    "payload": {
        "account_id": "account_id",
        "object": {
            "uuid": "WEz4RT2lSyKx2MD9Z+lYfA==", "id": 87565330005, "host_id": "host_id",
            "topic": "Recordings download URL/access token 01", "type": 2, "start_time": "2023-12-01T20:01:56Z",
            "timezone": "America/New_York", "host_email": "host@example.com", "duration": 1, "total_size": 3500663,
            "recording_count": 3, "share_url": "https://us02web.zoom.us/rec/share/ZUrD3XZS2qn5nW5IZLynVU",
            "recording_files": [
                {"id": "23e2a1b0", "meeting_id": "WEz4RT2lSyKx2MD9Z+lYfA==", "recording_start": "2023-12-01T20:02:05Z",
                 "recording_end": "2023-12-01T20:03:14Z", "file_type": "M4A", "file_extension": "M4A", "file_size": 1107341,
                 "play_url": "https://us02web.zoom.us/rec/play/14q", "download_url": "https://us02web.zoom.us/rec/webhook_download/14q-E",
                 "status": "completed", "recording_type": "audio_only"},
                {"id": "4608677c", "meeting_id": "WEz4RT2lSyKx2MD9Z+lYfA==", "file_type": "TRANSCRIPT", "file_extension": "VTT",
                 "file_size": 2048, "download_url": "https://us02web.zoom.us/rec/webhook_download/tr-1", "status": "completed",
                 "recording_type": "audio_transcript"},
                {"id": "no-url", "file_type": "MP4"},
            ],
        },
    },
    "event_ts": 1701461193266, "event": "recording.completed", "download_token": "<JWT_TOKEN>",
}


# ---- url validation / signatures ------------------------------------------------------------------------------------
def test_url_validation_matches_the_documented_construction():
    result = url_validation_response("qgg8vlvZRS6UYooatFL8Aw", SECRET)
    expected = hmac.new(SECRET.encode(), b"qgg8vlvZRS6UYooatFL8Aw", hashlib.sha256).hexdigest()
    assert result == {"plainToken": "qgg8vlvZRS6UYooatFL8Aw", "encryptedToken": expected}
    assert len(result["encryptedToken"]) == 64


def test_signature_message_is_v0_timestamp_body():
    body = b'{"event":"session.started","payload":{"account_id":"A"},"event_ts":1658940994914}'
    message = b"v0:1658940994:" + body
    expected = "v0=" + hmac.new(SECRET.encode(), message, hashlib.sha256).hexdigest()
    headers = {"x-zm-request-timestamp": "1658940994", "x-zm-signature": expected}
    assert verify_signature(headers, body, SECRET, tolerance_seconds=None) is True


def test_sign_request_round_trips():
    body = json.dumps(WEBHOOK).encode()
    headers = sign_request(body, SECRET, timestamp=NOW)
    assert headers["x-zm-signature"].startswith("v0=") and headers["x-zm-request-timestamp"] == str(NOW)
    assert verify_signature(headers, body, SECRET, now=NOW + 10)
    assert verify_signature(headers, body.decode(), SECRET, now=NOW + 10)                 # str bodies too


@pytest.mark.parametrize("mutate", [
    lambda h, b, s: (h, b + b" ", s),                                       # tampered body
    lambda h, b, s: (h, b, "other-secret"),                                  # wrong secret
    lambda h, b, s: ({**h, "x-zm-signature": "v0=" + "0" * 64}, b, s),       # wrong signature
    lambda h, b, s: ({"x-zm-signature": h["x-zm-signature"]}, b, s),         # missing timestamp
    lambda h, b, s: ({"x-zm-request-timestamp": h["x-zm-request-timestamp"]}, b, s),   # missing signature
    lambda h, b, s: ({**h, "x-zm-request-timestamp": "abc"}, b, s),          # unparsable timestamp
])
def test_bad_signatures_are_rejected(mutate):
    body = b'{"event":"x"}'
    headers = sign_request(body, SECRET, timestamp=NOW)
    headers, body, secret = mutate(headers, body, SECRET)
    assert verify_signature(headers, body, secret, now=NOW) is False


def test_signature_check_uses_the_raw_body_not_reserialised_json():
    raw = b'{"event": "x",  "payload": {}}'
    headers = sign_request(raw, SECRET, timestamp=NOW)
    assert verify_signature(headers, raw, SECRET, now=NOW)
    assert not verify_signature(headers, json.dumps(json.loads(raw)).encode(), SECRET, now=NOW)


def test_header_names_are_case_insensitive_and_stale_timestamps_fail():
    body = b"{}"
    headers = {k.title(): v for k, v in sign_request(body, SECRET, timestamp=NOW).items()}     # X-Zm-Signature ...
    assert verify_signature(headers, body, SECRET, now=NOW)
    assert not verify_signature(headers, body, SECRET, now=NOW + 301)
    assert verify_signature(headers, body, SECRET, now=NOW + 301, tolerance_seconds=None)
    assert verify_signature(headers, body, SECRET, now=NOW + 301, tolerance_seconds=600)


# ---- recording files ---------------------------------------------------------------------------------------------------
def test_recording_files_lists_files_with_download_urls():
    files = recording_files(WEBHOOK)
    assert [f.file_type for f in files] == ["M4A", "TRANSCRIPT"]                       # the MP4 without a URL is skipped
    audio, transcript = files
    assert isinstance(audio, RecordingFile) and audio.is_audio and not audio.is_transcript
    assert transcript.is_transcript and transcript.file_extension == "VTT"
    assert audio.download_url == "https://us02web.zoom.us/rec/webhook_download/14q-E"
    assert (audio.recording_type, audio.file_size, audio.status) == ("audio_only", 1107341, "completed")


def test_download_token_and_event_summary():
    assert download_token_of(WEBHOOK) == "<JWT_TOKEN>"
    assert download_token_of({"payload": {"download_access_token": "abc"}}) == "abc"
    info = recording_info(WEBHOOK)
    assert (info.event, info.meeting_id, info.uuid, info.host_email, info.duration_minutes) == (
        "recording.completed", "87565330005", "WEz4RT2lSyKx2MD9Z+lYfA==", "host@example.com", 1)
    assert info.download_token == "<JWT_TOKEN>" and len(info.files) == 2


@pytest.mark.parametrize("payload", [{}, {"payload": {}}, {"payload": {"object": {"recording_files": "x"}}}, {"payload": None}])
def test_payloads_without_files_yield_nothing(payload):
    assert recording_files(payload) == []


@pytest.fixture()
def zoom_api():
    with respx.mock(assert_all_called=False) as router:
        yield router


async def test_download_sends_the_download_token_as_a_bearer_token(zoom_api):
    route = zoom_api.get("https://us02web.zoom.us/rec/webhook_download/14q-E").respond(200, content=b"M4A-BYTES")
    audio = recording_files(WEBHOOK)[0]
    async with httpx.AsyncClient() as client:
        assert await download_recording_file(client, audio, "tok-24h") == b"M4A-BYTES"
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok-24h"


async def test_redirects_are_followed_without_leaking_the_token_to_other_hosts(zoom_api):
    zoom_api.get("https://us02web.zoom.us/rec/webhook_download/14q-E").respond(
        302, headers={"Location": "https://cdn.zoom.us/signed/abc?sig=1"})
    cdn = zoom_api.get("https://cdn.zoom.us/signed/abc").respond(200, content=b"DATA")
    async with httpx.AsyncClient() as client:
        assert await download_recording_file(client, recording_files(WEBHOOK)[0], "tok") == b"DATA"
    assert "Authorization" not in cdn.calls.last.request.headers


async def test_only_zoom_hosts_are_fetched_by_default(zoom_api):
    evil = RecordingFile(file_type="M4A", download_url="http://169.254.169.254/latest/meta-data")
    async with httpx.AsyncClient() as client:
        with pytest.raises(ZoomError, match="not allowed"):
            await download_recording_file(client, evil, "tok")
    assert len(zoom_api.calls) == 0

    local = RecordingFile(file_type="TRANSCRIPT", download_url="http://localhost:8000/sim/fixture/transcript")
    zoom_api.get("http://localhost:8000/sim/fixture/transcript").respond(200, content=b"WEBVTT")
    async with httpx.AsyncClient() as client:
        assert await download_recording_file(client, local, "tok", allowed_hosts=None) == b"WEBVTT"


async def test_look_alike_hosts_are_rejected(zoom_api):
    async with httpx.AsyncClient() as client:
        for url in ("https://evilzoom.us/x", "https://zoom.us.evil.example/x"):
            with pytest.raises(ZoomError):
                await download_recording_file(client, RecordingFile(file_type="M4A", download_url=url), "tok")


async def test_download_failures_raise(zoom_api):
    zoom_api.get("https://us02web.zoom.us/rec/webhook_download/14q-E").respond(401)
    async with httpx.AsyncClient() as client:
        with pytest.raises(ZoomError, match="401"):
            await download_recording_file(client, recording_files(WEBHOOK)[0], "expired")


# ---- VTT ---------------------------------------------------------------------------------------------------------------------
ZOOM_VTT = """WEBVTT

1
00:00:05.680 --> 00:00:09.720
Rodney Seller: Hello, thanks for joining. Note: this is a recorded line.

2
00:00:10.000 --> 00:00:12.000
Priya Raman: Hi Rodney.

3
00:00:12.500 --> 00:00:14.000
and one more thing about fees.
"""


def test_zoom_vtt_cues_have_a_speaker_prefix():
    segments = parse_zoom_vtt(ZOOM_VTT)
    assert [(s.speaker_id, s.text) for s in segments] == [
        ("Rodney Seller", "Hello, thanks for joining. Note: this is a recorded line."),
        ("Priya Raman", "Hi Rodney."),
        ("Priya Raman", "and one more thing about fees."),            # an unlabelled cue continues the last speaker
    ]
    assert [(s.id, s.start_ms, s.end_ms) for s in segments][0] == (0, 5680, 9720)


def test_captions_without_speaker_names_stay_unattributed():
    text = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nNote: this is not a speaker\n\n2\n00:00:02.000 --> 00:00:03.000\nplain caption\n"
    assert [(s.speaker_id, s.text) for s in parse_zoom_vtt(text)] == [
        ("Unknown", "Note: this is not a speaker"), ("Unknown", "plain caption")]


def test_multi_line_cues_are_joined():
    text = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:04.000\nAna: first line\nsecond line\n"
    assert parse_zoom_vtt(text)[0].text == "first line second line"


@pytest.mark.parametrize("call", [make_call(), spanish_call()], ids=["english", "spanish"])
def test_zoom_vtt_round_trip(call):
    segments = parse_zoom_vtt(render_zoom_vtt(call))
    names = {"seller": "Rodney Seller", "cust1": "Priya Raman", "cust2": "Marcus Lee", "int1": "Dana Colleague"}
    ids = [t.speaker_id or {"seller": "seller", "customer": "cust1", "internal": "int1"}[t.speaker] for t in call.turns]
    assert [s.speaker_id for s in segments] == [names[i] for i in ids]
    assert [s.text for s in segments] == [t.text for t in call.turns]


def test_zoom_vtt_cues_are_numbered_from_one():
    lines = render_zoom_vtt(make_call()).splitlines()
    assert lines[:4] == ["WEBVTT", "", "1", "00:00:01.500 --> 00:00:06.440"]


# ---- JSON --------------------------------------------------------------------------------------------------------------------
def test_json_timeline_speakers_and_times():
    data = {"timeline": [
        {"ts": "00:00:01.360", "end_ts": "00:00:03.160", "text": "Hello", "users": [{"username": "Rodney Seller", "user_id": "9"}]},
        {"ts": "00:00:03.500", "end_ts": "00:00:05.000", "raw_text": "Hi there", "userId": "104"},
        {"ts": "00:00:05.500", "text": "from the left channel", "channelMark": "L"},
        {"ts": 7000, "end_ts": 8000, "text": "numeric times", "users": [{"user_name": "Priya Raman"}]},
        {"ts": "00:00:09.000", "text": "   "},
        "not-a-dict",
    ]}
    segments = parse_zoom_transcript_json(data)
    assert [(s.speaker_id, s.text, s.start_ms, s.end_ms) for s in segments] == [
        ("Rodney Seller", "Hello", 1360, 3160), ("104", "Hi there", 3500, 5000),
        ("channel-L", "from the left channel", 5500, 5500), ("Priya Raman", "numeric times", 7000, 8000)]
    assert [s.id for s in segments] == [0, 1, 2, 3]


def test_json_may_arrive_as_text_or_bytes():
    raw = json.dumps({"timeline": [{"ts": "00:00:01.000", "text": "hola", "users": [{"username": "Ana"}]}]})
    assert parse_zoom_transcript_json(raw)[0].text == "hola"
    assert parse_zoom_transcript_json(raw.encode("utf-8"))[0].speaker_id == "Ana"
    assert parse_zoom_transcript_json({"timeline": []}) == []


@pytest.mark.parametrize("call", [make_call(), spanish_call()], ids=["english", "spanish"])
def test_zoom_json_round_trip(call):
    data = json.loads(json.dumps(render_zoom_transcript_json(call), ensure_ascii=False))
    segments = parse_zoom_transcript_json(data)
    names = {"seller": "Rodney Seller", "cust1": "Priya Raman", "cust2": "Marcus Lee", "int1": "Dana Colleague"}
    ids = [t.speaker_id or {"seller": "seller", "customer": "cust1", "internal": "int1"}[t.speaker] for t in call.turns]
    assert [s.speaker_id for s in segments] == [names[i] for i in ids]
    assert [s.text for s in segments] == [t.text for t in call.turns]
    meta = zoom_json_meta(data)
    assert meta["source_interaction_id"] == f"zoom-{call.fixture_id}" and meta["title"] == call.title
    assert meta["started_at"] == "2026-11-10T15:00:00Z" and meta["duration_seconds"] == call.duration_minutes * 60


# ---- recording.completed payload ------------------------------------------------------------------------------------------------
def test_rendered_webhook_lists_the_audio_and_the_transcript():
    call = make_call()
    payload = render_recording_completed_payload(call, "http://localhost:8000/api/v1/sim/zoom/")
    assert payload["event"] == "recording.completed" and payload["download_token"]
    obj = payload["payload"]["object"]
    assert (obj["topic"], obj["duration"], obj["host_email"]) == (call.title, call.duration_minutes, call.seller.email)
    assert obj["recording_count"] == 2 and obj["total_size"] == sum(f["file_size"] for f in obj["recording_files"])

    audio, transcript = recording_files(payload)
    assert (audio.file_type, audio.recording_type, audio.file_extension) == ("M4A", "audio_only", "M4A")
    assert (transcript.file_type, transcript.recording_type, transcript.file_extension) == ("TRANSCRIPT", "audio_transcript", "VTT")
    assert audio.download_url == f"http://localhost:8000/api/v1/sim/zoom/{call.fixture_id}/m4a"
    assert transcript.download_url.endswith(f"/{call.fixture_id}/transcript")
    assert transcript.file_size == len(render_zoom_vtt(call).encode("utf-8"))
    assert render_recording_completed_payload(call, "http://localhost:8000/api/v1/sim/zoom") == (
        render_recording_completed_payload(call, "http://localhost:8000/api/v1/sim/zoom/"))      # deterministic


def test_a_rendered_webhook_can_be_signed_and_verified():
    payload = render_recording_completed_payload(make_call(), "http://localhost:8000/sim")
    body = json.dumps(payload).encode()
    assert verify_signature(sign_request(body, SECRET, timestamp=NOW), body, SECRET, now=NOW)
