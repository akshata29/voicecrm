"""Genesys: transcript parsing/rendering, notification extraction and the OAuth client (HTTP mocked)."""
from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from voicecrm.adapters.sources.genesys import (
    GenesysClient, GenesysError, GenesysNotification, genesys_meta, handle_notification, parse_genesys_transcript,
    render_genesys_transcript,
)

from .test_sources_fixtures import make_call, spanish_call

CONVERSATION = "11111111-aaaa-4bbb-8ccc-222222222222"
COMMUNICATION = "33333333-dddd-4eee-8fff-444444444444"
ORIGIN = 1619441069196

SAMPLE = {
    "conversationId": CONVERSATION, "communicationId": COMMUNICATION, "mediaType": "call",
    "conversationStartTime": ORIGIN, "startTime": ORIGIN, "duration": {"milliseconds": 6885},
    "participants": [{"participantPurpose": "customer", "initialDirection": "inbound", "ani": "tel:+15550100"}],
    "transcripts": [{
        "phrases": [
            {"participantPurpose": "external", "text": "I would like to ask about fees", "startTimeMs": ORIGIN + 2500,
             "duration": {"milliseconds": 1800}, "confidence": 0.93},
            {"participantPurpose": "internal", "text": "Thank you for calling.", "startTimeMs": ORIGIN + 159,
             "duration": {"milliseconds": 1200}, "confidence": 0.95,
             "words": [{"word": "Thank", "startTimeMs": ORIGIN + 159, "duration": {"milliseconds": 200}}]},
            {"participantPurpose": "external", "startTimeMs": ORIGIN + 4600, "duration": {"milliseconds": 900},
             "words": [{"word": "thank", "confidence": 0.9}, {"word": "you", "confidence": 0.9}]},
            {"participantPurpose": "ivr", "text": "Your call is recorded.", "offset": {"milliseconds": 10},
             "duration": {"milliseconds": 500}},
            {"participantPurpose": "external", "text": "   "},
        ],
        "analytics": {"sentiment": [{"participant": "external", "sentiment": 0.7}]},
    }],
}


# ---- transcript ------------------------------------------------------------------------------------------------------
def test_phrases_become_time_ordered_segments_with_agent_and_customer_sides():
    segments = parse_genesys_transcript(SAMPLE)
    assert [(s.id, s.speaker_id, s.text, s.start_ms, s.end_ms) for s in segments] == [
        (0, "ivr", "Your call is recorded.", 10, 510),
        (1, "agent", "Thank you for calling.", 159, 1359),
        (2, "customer", "I would like to ask about fees", 2500, 4300),
        (3, "customer", "thank you", 4600, 5500),                                       # text rebuilt from the words
    ]


def test_relative_offsets_and_flat_millisecond_fields_are_supported():
    data = {"transcripts": [{"phrases": [
        {"participantPurpose": "internal", "text": "a", "offsetMs": 1000, "durationMs": 500},
        {"participantPurpose": "external", "text": "b", "startTimeMs": 2000, "duration": {"milliseconds": 100}},
        {"participantPurpose": "agent", "text": "c", "alternatives": []},
    ]}]}
    assert [(s.speaker_id, s.start_ms, s.end_ms) for s in parse_genesys_transcript(data)] == [
        ("agent", 0, 0), ("agent", 1000, 1500), ("customer", 2000, 2100)]


def test_alternatives_are_the_last_resort_for_text():
    data = {"transcripts": [{"phrases": [
        {"participantPurpose": "external", "alternatives": [{"transcript": "from alternatives", "confidence": 0.8}]}]}]}
    assert parse_genesys_transcript(data)[0].text == "from alternatives"


@pytest.mark.parametrize("data", [{}, {"transcripts": []}, {"transcripts": [None, {"phrases": None}]}, {"transcripts": "x"}])
def test_transcripts_without_phrases_give_no_segments(data):
    assert parse_genesys_transcript(data) == []


def test_metadata_hints():
    meta = genesys_meta(SAMPLE)
    assert meta["source_interaction_id"] == CONVERSATION and meta["communication_id"] == COMMUNICATION
    assert meta["started_at"].startswith("2021-04-26T") and meta["duration_seconds"] == 6.885
    assert meta["direction"] == "inbound"


@pytest.mark.parametrize("call", [make_call(), spanish_call()], ids=["english", "spanish"])
def test_round_trip_reproduces_sides_order_and_text(call):
    data = json.loads(json.dumps(render_genesys_transcript(call), ensure_ascii=False))
    segments = parse_genesys_transcript(data)
    assert [s.speaker_id for s in segments] == ["agent" if t.speaker in ("seller", "internal") else "customer" for t in call.turns]
    assert [s.text for s in segments] == [t.text for t in call.turns]
    assert [s.id for s in segments] == list(range(len(call.turns)))
    assert all(a.end_ms <= b.start_ms for a, b in zip(segments, segments[1:], strict=False))


def test_rendered_transcript_follows_the_documented_schema():
    call = make_call()
    data = render_genesys_transcript(call)
    assert data["mediaType"] == "call" and data["conversationStartTime"] > 10**11
    assert data["duration"] == {"milliseconds": call.duration_minutes * 60_000}
    phrase = data["transcripts"][0]["phrases"][0]
    assert phrase["participantPurpose"] == "internal"
    assert phrase["startTimeMs"] == data["conversationStartTime"] + phrase["offset"]["milliseconds"]
    assert [w["word"] for w in phrase["words"]] == call.turns[0].text.split()
    assert genesys_meta(data)["source_interaction_id"] == data["conversationId"]
    assert render_genesys_transcript(call) == data                                              # deterministic


# ---- notifications ---------------------------------------------------------------------------------------------------------
def test_notification_api_call_event_with_participants():
    payload = {"topicName": "v2.users.u1.conversations.calls", "version": "2", "eventBody": {
        "id": CONVERSATION, "participants": [
            {"purpose": "agent", "calls": [{"id": "agent-call", "state": "connected"}]},
            {"purpose": "customer", "calls": [{"id": COMMUNICATION, "state": "disconnected",
                                               "disconnectedTime": "2026-11-10T15:12:00Z"}]},
        ]}}
    note = handle_notification(payload)
    assert isinstance(note, GenesysNotification)
    assert (note.conversation_id, note.communication_id, note.ended) == (CONVERSATION, COMMUNICATION, True)
    assert note.topic == "v2.users.u1.conversations.calls"


def test_live_call_is_not_marked_ended():
    payload = {"topicName": "t", "eventBody": {"id": CONVERSATION, "participants": [
        {"purpose": "customer", "calls": [{"id": COMMUNICATION, "state": "connected"}]}]}}
    assert handle_notification(payload).ended is False


def test_eventbridge_envelope():
    payload = {"version": "0", "detail-type": "v2.detail.events.conversation.x.end", "source": "aws.partner/genesys.com",
               "time": "2026-11-10T15:12:00Z", "detail": {
                   "topicName": f"v2.detail.events.conversation.{CONVERSATION}.customer.end",
                   "eventBody": {"conversationId": CONVERSATION, "communicationId": COMMUNICATION,
                                 "eventTime": "2026-11-10T15:12:00.000Z", "endTime": 1}}}
    note = handle_notification(payload)
    assert (note.conversation_id, note.communication_id, note.ended) == (CONVERSATION, COMMUNICATION, True)
    assert note.event_time == "2026-11-10T15:12:00.000Z"


def test_architect_style_flat_payload():
    note = handle_notification({"conversationId": CONVERSATION, "communicationId": COMMUNICATION, "agentEmail": "a@b.example"})
    assert (note.conversation_id, note.communication_id, note.topic) == (CONVERSATION, COMMUNICATION, None)


def test_conversation_id_can_come_from_the_topic_name():
    note = handle_notification({"topicName": f"v2.conversations.{CONVERSATION}.transcription", "eventBody": {
        "eventTime": "2026-11-10T15:00:00Z", "communicationId": COMMUNICATION}})
    assert (note.conversation_id, note.communication_id) == (CONVERSATION, COMMUNICATION)


@pytest.mark.parametrize("payload", [{}, {"eventBody": {"foo": "bar"}}, {"topicName": "channel.metadata"}, [], None, "x"])
def test_payloads_without_a_conversation_are_ignored(payload):
    assert handle_notification(payload) is None


# ---- client ------------------------------------------------------------------------------------------------------------------
LOGIN = "https://login.mypurecloud.com/oauth/token"
API = "https://api.mypurecloud.com"
TRANSCRIPT_URL_PATH = f"/api/v2/speechandtextanalytics/conversations/{CONVERSATION}/communications/{COMMUNICATION}/transcripturl"
PRESIGNED = "https://transcripts.s3.example.com/abc?X-Amz-Signature=sig"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class Sleeper:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


@pytest.fixture()
def genesys_api():
    with respx.mock(assert_all_called=False) as router:
        router.post(LOGIN).respond(200, json={"access_token": "tok-1", "token_type": "bearer", "expires_in": 86399})
        yield router


def make_client(**kwargs) -> GenesysClient:
    kwargs.setdefault("sleep", Sleeper())
    kwargs.setdefault("http", _HTTP)
    return GenesysClient("client-id", "client-secret", **kwargs)


_HTTP = httpx.AsyncClient()


async def test_client_credentials_request_follows_the_documented_form(genesys_api):
    token = await make_client().access_token()
    request = genesys_api.calls.last.request
    assert token == "tok-1"
    assert request.headers["Authorization"] == "Basic " + base64.b64encode(b"client-id:client-secret").decode()
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert request.content == b"grant_type=client_credentials"


async def test_token_is_cached_until_shortly_before_expiry(genesys_api):
    clock = Clock()
    client = make_client(clock=clock)
    await client.access_token()
    await client.access_token()
    assert len(genesys_api.calls) == 1
    clock.now += 86399 - 30
    await client.access_token()
    assert len(genesys_api.calls) == 2


async def test_region_changes_the_hosts():
    with respx.mock(assert_all_called=False) as router:
        router.post("https://login.mypurecloud.ie/oauth/token").respond(200, json={"access_token": "eu", "expires_in": 60})
        route = router.get(f"https://api.mypurecloud.ie{TRANSCRIPT_URL_PATH}").respond(200, json={"url": PRESIGNED})
        assert await make_client(environment="mypurecloud.ie").transcript_url(CONVERSATION, COMMUNICATION) == PRESIGNED
    assert route.calls.last.request.headers["Authorization"] == "Bearer eu"


async def test_token_failure_is_a_typed_error():
    with respx.mock(assert_all_called=False) as router:
        router.post(LOGIN).respond(401, json={"error": "invalid_client"})
        with pytest.raises(GenesysError) as caught:
            await make_client().access_token()
    assert caught.value.status == 401


async def test_the_presigned_url_is_downloaded_without_the_authorization_header(genesys_api):
    genesys_api.get(f"{API}{TRANSCRIPT_URL_PATH}").respond(200, json={"url": PRESIGNED})
    download = genesys_api.get(PRESIGNED).respond(200, json=SAMPLE)
    data = await make_client().download_transcript(CONVERSATION, COMMUNICATION)
    assert data["conversationId"] == CONVERSATION
    assert "Authorization" not in download.calls.last.request.headers
    url_request = genesys_api.calls[1].request
    assert url_request.headers["Authorization"] == "Bearer tok-1" and url_request.url.path == TRANSCRIPT_URL_PATH


async def test_fetch_segments_parses_the_downloaded_transcript(genesys_api):
    genesys_api.get(f"{API}{TRANSCRIPT_URL_PATH}").respond(200, json={"url": PRESIGNED})
    genesys_api.get(PRESIGNED).respond(200, json=SAMPLE)
    segments = await make_client().fetch_segments(CONVERSATION, COMMUNICATION)
    assert [s.speaker_id for s in segments] == ["ivr", "agent", "customer", "customer"]


async def test_an_expired_url_is_replaced_once(genesys_api):
    urls = genesys_api.get(f"{API}{TRANSCRIPT_URL_PATH}")
    urls.side_effect = [httpx.Response(200, json={"url": PRESIGNED + "&n=1"}), httpx.Response(200, json={"url": PRESIGNED + "&n=2"})]
    genesys_api.get(PRESIGNED + "&n=1").respond(403, text="expired")
    genesys_api.get(PRESIGNED + "&n=2").respond(200, json=SAMPLE)
    assert (await make_client().download_transcript(CONVERSATION, COMMUNICATION))["conversationId"] == CONVERSATION
    assert len(urls.calls) == 2


async def test_a_second_download_failure_is_reported(genesys_api):
    genesys_api.get(f"{API}{TRANSCRIPT_URL_PATH}").respond(200, json={"url": PRESIGNED})
    genesys_api.get(PRESIGNED).respond(403, text="expired")
    with pytest.raises(GenesysError, match="download failed") as caught:
        await make_client().download_transcript(CONVERSATION, COMMUNICATION)
    assert caught.value.status == 403


async def test_rate_limits_honour_retry_after(genesys_api):
    sleeper = Sleeper()
    route = genesys_api.get(f"{API}{TRANSCRIPT_URL_PATH}")
    route.side_effect = [httpx.Response(429, headers={"Retry-After": "4"}), httpx.Response(200, json={"url": PRESIGNED})]
    assert await make_client(sleep=sleeper).transcript_url(CONVERSATION, COMMUNICATION) == PRESIGNED
    assert sleeper.waits == [4.0]


async def test_rate_limit_retries_are_bounded(genesys_api):
    sleeper = Sleeper()
    genesys_api.get(f"{API}{TRANSCRIPT_URL_PATH}").respond(429, headers={"Retry-After": "1"}, json={"message": "Rate limit exceeded"})
    with pytest.raises(GenesysError, match="Rate limit") as caught:
        await make_client(sleep=sleeper, max_retries=2).transcript_url(CONVERSATION, COMMUNICATION)
    assert caught.value.status == 429 and sleeper.waits == [1.0, 1.0]


async def test_an_expired_access_token_is_refreshed_once(genesys_api):
    genesys_api.post(LOGIN).side_effect = [
        httpx.Response(200, json={"access_token": "old", "expires_in": 86399}),
        httpx.Response(200, json={"access_token": "new", "expires_in": 86399}),
    ]
    route = genesys_api.get(f"{API}{TRANSCRIPT_URL_PATH}")
    route.side_effect = [httpx.Response(401, json={"message": "expired"}), httpx.Response(200, json={"url": PRESIGNED})]
    assert await make_client().transcript_url(CONVERSATION, COMMUNICATION) == PRESIGNED
    assert [c.request.headers["Authorization"] for c in route.calls] == ["Bearer old", "Bearer new"]


async def test_missing_transcript_is_reported_with_the_api_message(genesys_api):
    genesys_api.get(f"{API}{TRANSCRIPT_URL_PATH}").respond(404, json={"message": "Transcript not found", "code": "not.found"})
    with pytest.raises(GenesysError, match="Transcript not found") as caught:
        await make_client().transcript_url(CONVERSATION, COMMUNICATION)
    assert caught.value.status == 404


async def test_communication_ids_prefer_the_customer_side(genesys_api):
    genesys_api.get(f"{API}/api/v2/conversations/{CONVERSATION}").respond(200, json={"participants": [
        {"purpose": "agent", "calls": [{"id": "agent-call"}]},
        {"purpose": "customer", "calls": [{"id": COMMUNICATION}, {"id": COMMUNICATION}]},
        {"purpose": "acd", "calls": []},
    ]})
    assert await make_client().communication_ids(CONVERSATION) == [COMMUNICATION, "agent-call"]
