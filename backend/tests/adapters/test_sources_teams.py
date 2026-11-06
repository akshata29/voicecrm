"""Teams: WebVTT parsing/rendering and the Graph notification / transcript download helpers (HTTP mocked)."""
from __future__ import annotations

import httpx
import pytest
import respx

from voicecrm.adapters.sources import teams
from voicecrm.adapters.sources.teams import (
    TeamsGraphError, TranscriptRef, acquire_graph_token, fetch_transcript_for, fetch_transcript_vtt,
    handle_graph_notification, parse_teams_vtt, render_teams_vtt, transcript_guid, validation_response,
)
from voicecrm.domain.enums import Channel

from .test_sources_fixtures import make_call, spanish_call

GUID = "7f1d2a3b-4c5d-4e6f-8a9b-0c1d2e3f4a5b"
SAMPLE = f"""WEBVTT

{GUID}/12-0
00:00:03.435 --> 00:00:06.915
<v Rodney Seller>Hello Priya, thanks for joining the call today.</v>

{GUID}/13-0
00:00:07.100 --> 00:00:09.020
<v Priya Raman>Thanks, Rodney. We are looking at
the equity fund &amp; its fees.</v>

{GUID}/14-0
00:00:09.500 --> 00:00:10.250
<v Rodney Seller>Great.</v><v Priya Raman>Yes!</v>
"""


# ---- parsing -----------------------------------------------------------------------------------------------------
def test_parses_voice_tagged_cues_into_segments():
    segments = parse_teams_vtt(SAMPLE)
    assert [(s.id, s.speaker_id, s.start_ms, s.end_ms) for s in segments] == [
        (0, "Rodney Seller", 3435, 6915), (1, "Priya Raman", 7100, 9020),
        (2, "Rodney Seller", 9500, 10250), (3, "Priya Raman", 9500, 10250),
    ]
    assert segments[0].text == "Hello Priya, thanks for joining the call today."
    assert segments[1].text == "Thanks, Rodney. We are looking at the equity fund & its fees."
    assert [s.text for s in segments[2:]] == ["Great.", "Yes!"]


def test_the_common_cue_guid_is_the_transcript_id():
    assert transcript_guid(SAMPLE) == GUID
    assert transcript_guid(SAMPLE.replace(f"{GUID}/13-0", "11111111-4c5d-4e6f-8a9b-0c1d2e3f4a5b/13-0")) is None
    assert transcript_guid("WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\n<v A>hi</v>\n") is None


def test_tolerates_bom_crlf_voice_classes_and_unlabelled_text():
    text = "\ufeffWEBVTT\r\n\r\n00:01.000 --> 00:02.500 align:start\r\n<v.loud Ana María>¡Hola! <i>Buenos</i> días</v>\r\n\r\n" \
           "00:03.000 --> 00:04.000\r\nno voice tag here\r\n"
    first, second = parse_teams_vtt(text)
    assert (first.speaker_id, first.text, first.start_ms, first.end_ms) == ("Ana María", "¡Hola! Buenos días", 1000, 2500)
    assert (second.speaker_id, second.text) == ("Unknown", "no voice tag here")


def test_graph_unattributed_format_with_a_blank_line_after_the_timing_is_read():
    text = "00:00:01.500 --> 00:00:04.000 \n\nHello, thanks for joining. \n\n00:00:04.000 --> 00:00:07.200 \n\nGlad to be here. \n"
    assert [(s.speaker_id, s.text, s.start_ms) for s in parse_teams_vtt(text)] == [
        ("Unknown", "Hello, thanks for joining.", 1500), ("Unknown", "Glad to be here.", 4000)]


def test_negative_offsets_clamp_to_zero_and_notes_are_ignored():
    text = "WEBVTT\n\nNOTE a comment\n\n-00:00:01.200 --> 00:00:02.000\n<v A>early</v>\n"
    (segment,) = parse_teams_vtt(text)
    assert (segment.start_ms, segment.end_ms, segment.text) == (0, 2000, "early")


def test_empty_and_cue_free_input_gives_no_segments():
    assert parse_teams_vtt("") == [] and parse_teams_vtt("WEBVTT\n\n") == []


# ---- rendering / round trip --------------------------------------------------------------------------------------------
def test_render_produces_voice_tagged_escaped_cues():
    call = make_call()
    text = render_teams_vtt(call)
    lines = text.splitlines()
    assert lines[0] == "WEBVTT" and lines[1] == ""
    assert lines[2].endswith("/0-0") and lines[3] == "00:00:01.500 --> 00:00:06.440"      # 13 words * 380 ms from 1.5 s
    assert lines[4].startswith("<v Rodney Seller>Hello Priya") and lines[4].endswith("</v>")
    assert "fees &amp; expenses &lt;a lot&gt;." in text
    assert render_teams_vtt(call) == text                                  # deterministic


@pytest.mark.parametrize("call", [make_call(), spanish_call()], ids=["english", "spanish"])
def test_round_trip_reproduces_speakers_order_and_text(call):
    segments = parse_teams_vtt(render_teams_vtt(call))
    names = {"seller": "Rodney Seller", "cust1": "Priya Raman", "cust2": "Marcus Lee", "int1": "Dana Colleague"}
    ids = [t.speaker_id or {"seller": "seller", "customer": "cust1", "internal": "int1"}[t.speaker] for t in call.turns]
    assert [s.speaker_id for s in segments] == [names[i] for i in ids]
    assert [s.text for s in segments] == [t.text for t in call.turns]
    assert [s.id for s in segments] == list(range(len(call.turns)))
    assert all(a.end_ms <= b.start_ms for a, b in zip(segments, segments[1:], strict=False))
    assert transcript_guid(render_teams_vtt(call)) is not None


# ---- Graph change notifications ------------------------------------------------------------------------------------------
def notification(resource: str, **extra):
    return {"subscriptionId": "sub-1", "changeType": "created", "clientState": "s3cret", "tenantId": "tenant-1",
            "resource": resource, "resourceData": {"id": "MSM-transcript", "@odata.type": "#Microsoft.Graph.callTranscript"},
            **extra}


def test_extracts_tenant_meeting_and_transcript_ids_from_the_notification():
    payload = {"value": [notification("users/organizer-1/onlineMeetings('MSo-meeting')/transcripts('MSM-transcript')")],
               "validationTokens": ["jwt"]}
    (ref,) = handle_graph_notification(payload)
    assert isinstance(ref, TranscriptRef)
    assert (ref.tenant_id, ref.meeting_id, ref.transcript_id, ref.organizer_id) == ("tenant-1", "MSo-meeting", "MSM-transcript", "organizer-1")
    assert (ref.subscription_id, ref.change_type, ref.kind) == ("sub-1", "created", "online_meeting")
    assert ref.resource.startswith("users/organizer-1/")


@pytest.mark.parametrize("resource,organizer", [
    ("communications/onlineMeetings('M1')/transcripts('T1')", None),
    ("communications/onlineMeetings/M1/transcripts/T1", None),
    ("/users/u9/onlineMeetings/M1/transcripts/T1", "u9"),
])
def test_other_resource_spellings(resource, organizer):
    (ref,) = handle_graph_notification({"value": [notification(resource)]})
    assert (ref.meeting_id, ref.transcript_id, ref.organizer_id) == ("M1", "T1", organizer)


def test_ad_hoc_call_transcripts_are_flagged():
    (ref,) = handle_graph_notification({"value": [notification("users/u1/adhoccalls/call-9/transcripts('T9')")]})
    assert (ref.kind, ref.meeting_id, ref.transcript_id, ref.organizer_id) == ("adhoc_call", "call-9", "T9", "u1")


def test_resource_data_id_is_the_fallback_resource():
    item = {"changeType": "created", "tenantId": "t", "resourceData": {
        "@odata.id": "users/u1/onlineMeetings('M1')/transcripts('T1')"}}
    assert handle_graph_notification({"value": [item]})[0].transcript_id == "T1"


def test_only_created_transcript_notifications_count_and_duplicates_collapse():
    resource = "users/u1/onlineMeetings('M1')/transcripts('T1')"
    payload = {"value": [
        notification(resource), notification(resource),
        notification(resource, changeType="updated"), notification(resource, changeType="deleted"),
        {"subscriptionId": "sub-1", "lifecycleEvent": "reauthorizationRequired", "tenantId": "tenant-1"},
        notification("users/u1/onlineMeetings('M1')/recordings('R1')"),
        "not-a-dict",
    ]}
    assert [r.transcript_id for r in handle_graph_notification(payload)] == ["T1"]


def test_client_state_is_checked_when_expected():
    good = notification("users/u1/onlineMeetings('M1')/transcripts('T1')")
    bad = notification("users/u1/onlineMeetings('M2')/transcripts('T2')", clientState="forged")
    refs = handle_graph_notification({"value": [good, bad]}, expected_client_state="s3cret")
    assert [r.meeting_id for r in refs] == ["M1"]
    assert len(handle_graph_notification({"value": [good, bad]})) == 2


@pytest.mark.parametrize("payload", [{}, {"value": None}, {"value": "x"}, [], None])
def test_malformed_payloads_yield_nothing(payload):
    assert handle_graph_notification(payload) == []


def test_validation_response_echoes_the_decoded_token():
    token = "Validation: Testing client application reachability for subscription Request-Id: abc"
    assert validation_response(token) == token and teams.VALIDATION_CONTENT_TYPE == "text/plain"


# ---- Graph transcript download -----------------------------------------------------------------------------------------------
BASE = "https://graph.microsoft.com/v1.0"


@pytest.fixture()
def graph():
    with respx.mock(assert_all_called=False) as router:
        yield router


async def test_fetch_uses_the_documented_users_path_and_vtt_format(graph):
    route = graph.get(f"{BASE}/users/organizer-1/onlineMeetings/MSo-meeting/transcripts/MSM-transcript/content").respond(
        200, content=("\ufeff" + SAMPLE).encode("utf-8"), headers={"Content-Type": "text/vtt"})
    async with httpx.AsyncClient() as client:
        text = await fetch_transcript_vtt(client, "tok", "MSo-meeting", "MSM-transcript", organizer_id="organizer-1")
    request = route.calls.last.request
    assert request.url.params["$format"] == "text/vtt"
    assert request.headers["Authorization"] == "Bearer tok" and request.headers["Accept"] == "text/vtt"
    assert text.startswith("WEBVTT") and len(parse_teams_vtt(text)) == 4


async def test_without_an_organizer_the_communications_path_is_used(graph):
    route = graph.get(f"{BASE}/communications/onlineMeetings/M1/transcripts/T1/content").respond(200, text="WEBVTT\n")
    async with httpx.AsyncClient() as client:
        assert (await fetch_transcript_vtt(client, "tok", "M1", "T1")).startswith("WEBVTT")
    assert route.called


async def test_ids_are_percent_encoded_in_the_path(graph):
    route = graph.get(url__regex=r".*/transcripts/.*/content.*").respond(200, text="WEBVTT\n")
    async with httpx.AsyncClient() as client:
        await fetch_transcript_vtt(client, "tok", "MSo/1=2", "MSM#3", organizer_id="u 1")
    assert "/users/u%201/onlineMeetings/MSo%2F1%3D2/transcripts/MSM%233/content" in str(route.calls.last.request.url)


async def test_ad_hoc_call_content_path(graph):
    route = graph.get(f"{BASE}/users/u1/adhocCalls/call-9/transcripts/T9/content").respond(200, text="WEBVTT\n")
    ref = TranscriptRef(meeting_id="call-9", transcript_id="T9", organizer_id="u1", resource="r", kind="adhoc_call")
    async with httpx.AsyncClient() as client:
        await fetch_transcript_for(client, "tok", ref)
    assert route.called
    with pytest.raises(ValueError, match="organizer"):
        async with httpx.AsyncClient() as client:
            await fetch_transcript_vtt(client, "tok", "c", "t", kind="adhoc_call")


async def test_speaker_attribution_disabled_is_a_typed_error(graph):
    graph.get(url__regex=r".*/content.*").respond(403, json={"error": {
        "code": "Forbidden", "message": "Speaker-attributed transcript content is disabled for this tenant.",
        "innerError": {"code": "SpeakerAttributionNotAllowed"}}})
    async with httpx.AsyncClient() as client:
        with pytest.raises(TeamsGraphError) as caught:
            await fetch_transcript_vtt(client, "tok", "M1", "T1", organizer_id="u1")
    assert caught.value.status == 403 and caught.value.code == "SpeakerAttributionNotAllowed"
    assert "disabled" in str(caught.value)


async def test_other_failures_are_reported_with_their_status(graph):
    graph.get(url__regex=r".*/content.*").respond(404, text="not json")
    async with httpx.AsyncClient() as client:
        with pytest.raises(TeamsGraphError) as caught:
            await fetch_transcript_vtt(client, "tok", "M1", "T1", organizer_id="u1")
    assert caught.value.status == 404 and caught.value.code is None


async def test_graph_token_uses_the_default_scope():
    seen: list[str] = []

    class Credential:
        async def get_token(self, *scopes, **kwargs):
            seen.extend(scopes)
            return type("T", (), {"token": "graph-token"})()

    assert await acquire_graph_token(Credential()) == "graph-token"
    assert seen == ["https://graph.microsoft.com/.default"]


async def test_download_then_parse_end_to_end(graph):
    call = make_call()
    graph.get(url__regex=r".*/content.*").respond(200, text=render_teams_vtt(call))
    async with httpx.AsyncClient() as client:
        text = await fetch_transcript_vtt(client, "tok", "M1", "T1", organizer_id="u1")
    assert [s.text for s in parse_teams_vtt(text)] == [t.text for t in call.turns]
    assert Channel.teams.value == "teams"
