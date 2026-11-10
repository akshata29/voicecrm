"""Live Teams ingestion: Graph transcripts -> pipeline, polling, webhook references, idempotency and failure reporting.

Graph is mocked at the HTTP layer with the documented URL shapes; the pipeline behind it is the real one (rules engine,
local PII, local CRM simulator), so a transcript really becomes a reviewable, CRM-matched proposal.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from voicecrm.adapters.llm.fake import FakeLlm
from voicecrm.adapters.sources.teams import TranscriptRef, render_teams_vtt
from voicecrm.adapters.sources.teams_graph import GraphError
from voicecrm.config import Settings
from voicecrm.domain.conversation import utcnow
from voicecrm.domain.enums import ConversationStatus
from voicecrm.pipeline import teams_live
from voicecrm.pipeline.teams_live import (
    HISTORY_MAX_DAYS, IMPORT_MAX, TeamsImportSelection, TeamsIngestor, _iso, _listed_name, _name_from_alias, _parse_time)

from .test_stats import CALLS
from .test_llm_pipeline import _service

TENANT, CLIENT, SECRET = "11111111-2222-3333-4444-555555555555", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "s3cr3t-value-123456"
ORG = "8b081ef6-4792-4def-b2c9-c363a1bf41d5"
MEETING_ID = "MSo4YjA4MWVmNi00NzkyLTRkZWYtYjJjOS1jMzYzYTFiZjQxZDUqMCoqMTk6bWVldGluZ19ZMkU0"
TID = "VOICECRM-TRANSCRIPT-0001"
GRAPH = "https://graph.microsoft.com/v1.0"
TOKEN = re.compile(r"https://login\.microsoftonline\.com/.*/oauth2/v2\.0/token")
CALL = next(c for c in CALLS if c.fixture_id == "pb-teams-equity-class-c")
VTT = render_teams_vtt(CALL)
BASE = f"{GRAPH}/users/{ORG}/onlineMeetings/{MEETING_ID}"
MEETING = {
    "subject": "Contoso Woodgrove | Equity sleeve review", "startDateTime": "2026-11-10T15:00:00Z",
    "endDateTime": "2026-11-10T15:12:00Z",
    "participants": {
        "organizer": {"upn": CALL.seller.email, "identity": {"user": {"id": ORG, "displayName": CALL.seller.display_name}}},
        "attendees": [{"upn": c.email, "role": "attendee", "identity": {"user": {"displayName": c.display_name}}}
                      for c in CALL.customers],
    },
}


def settings(tmp_path, **kw) -> Settings:
    base = dict(_env_file=None, state_dir=tmp_path / "state", teams_mode="poll", teams_tenant_id=TENANT,
                teams_client_id=CLIENT, teams_client_secret=SECRET, teams_organizers=ORG, teams_poll_seconds=5)
    return Settings(**{**base, **kw})


def listing(**item) -> dict:
    now = utcnow()
    entry = {"id": TID, "meetingId": MEETING_ID, "createdDateTime": _iso(now - timedelta(minutes=14)),
             "endDateTime": _iso(now - timedelta(minutes=2)), **item}
    return {"value": [entry]}


@pytest.fixture()
def graph():
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN).mock(return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600}))
        yield router


def user_route(router, ident: str, response: httpx.Response):
    return router.get(url__regex=rf"{re.escape(GRAPH)}/users/{re.escape(ident)}(\?.*)?$").mock(return_value=response)


def mock_graph(router, *, items=None, vtt=VTT, meeting=MEETING, content=None, directory=True):
    if directory:                                                    # best-effort lookup of the organiser's name and address
        user_route(router, ORG, httpx.Response(200, json={"id": ORG, "displayName": CALL.seller.display_name,
                                                          "mail": CALL.seller.email}))
    lister = router.get(url__regex=rf"{re.escape(GRAPH)}/users/{ORG}/onlineMeetings/getAllTranscripts\(.*\)").mock(
        return_value=httpx.Response(200, json=items if items is not None else listing()))
    router.get(BASE).mock(return_value=httpx.Response(200, json=meeting) if meeting is not None else httpx.Response(403, json={
        "error": {"code": "Forbidden", "message": "no"}}))
    download = router.get(f"{BASE}/transcripts/{TID}/content").mock(
        return_value=content or httpx.Response(200, content=vtt.encode()))
    return lister, download


def ingestor(tmp_path, **kw) -> tuple[TeamsIngestor, object]:
    h = _service(tmp_path / "svc", FakeLlm(lambda call: {}, model="x"))
    ing = TeamsIngestor.from_settings(h.service, settings(tmp_path, **kw), http_client=httpx.AsyncClient(verify=False))
    return ing, h


async def test_a_finished_transcript_becomes_a_crm_matched_proposal(graph, tmp_path):
    lister, download = mock_graph(graph)
    ing, h = ingestor(tmp_path)
    assert await ing.poll_once() == 1
    await h.service.drain()
    request = lister.calls.last.request
    assert request.url.path == f"/v1.0/users/{ORG}/onlineMeetings/getAllTranscripts(meetingOrganizerUserId='{ORG}',startDateTime={request.url.path.rsplit('startDateTime=', 1)[1]}"
    assert request.url.params["$top"] == "50" and download.call_count == 1
    assert download.calls.last.request.headers["Accept"] == "text/vtt"
    (rec,) = h.store.list_conversations()
    assert rec.status == ConversationStatus.needs_review and rec.meta.source.value == "teams"
    assert rec.meta.source_interaction_id == TID and rec.meta.title == MEETING["subject"]
    assert rec.meta.seller.display_name == CALL.seller.display_name and rec.meta.seller.email == CALL.seller.email
    assert [p.email for p in rec.meta.participants] == [c.email for c in CALL.customers]
    assert rec.proposal.crm_match.account is not None and rec.proposal.crm_match.account.id == CALL.gold.expect_crm_account
    status = ing.status()
    assert (status.ingested, status.failed, status.last_poll_ok, status.last_error) == (1, 0, True, None)
    assert status.recent[0].outcome == "ingested" and status.recent[0].conversation_id == rec.meta.conversation_id


async def test_a_second_poll_and_a_restart_do_not_ingest_it_again(graph, tmp_path):
    _, download = mock_graph(graph)
    ing, h = ingestor(tmp_path)
    assert await ing.poll_once() == 1 and await ing.poll_once() == 0
    await h.service.drain()
    restarted = TeamsIngestor.from_settings(h.service, settings(tmp_path), http_client=httpx.AsyncClient(verify=False))
    assert await restarted.poll_once() == 0
    assert download.call_count == 1 and len(h.store.list_conversations()) == 1


async def test_forgetting_the_status_card_does_not_bring_old_meetings_back(graph, tmp_path):
    _, download = mock_graph(graph)
    ing, h = ingestor(tmp_path)
    assert await ing.poll_once() == 1
    await h.service.drain()
    before = ing.status()
    assert before.ingested == 1 and before.recent and before.last_ingest_at is not None
    ing.forget()
    after = ing.status()
    assert (after.ingested, after.duplicates, after.failed, after.recent, after.last_ingest_at) == (0, 0, 0, [], None)
    assert after.last_poll_ok is True and after.organizers == before.organizers      # only the history is cleared
    assert await ing.poll_once() == 0 and download.call_count == 1                   # what was ingested stays remembered


async def test_the_pipeline_itself_deduplicates_if_the_seen_file_is_lost(graph, tmp_path):
    _, download = mock_graph(graph)
    ing, h = ingestor(tmp_path)
    assert await ing.poll_once() == 1
    await h.service.drain()
    (tmp_path / "state" / "teams_seen.json").unlink()
    again = TeamsIngestor.from_settings(h.service, settings(tmp_path), http_client=httpx.AsyncClient(verify=False))
    assert await again.poll_once() == 0 and download.call_count == 2
    assert len(h.store.list_conversations()) == 1 and again.status().duplicates == 1


async def test_a_transcript_still_being_written_waits_until_it_ends_or_is_old(graph, tmp_path):
    now = utcnow()
    running = {"value": [{"id": TID, "meetingId": MEETING_ID, "createdDateTime": _iso(now - timedelta(minutes=3))}]}
    _, download = mock_graph(graph, items=running)
    ing, _ = ingestor(tmp_path)
    assert await ing.poll_once() == 0 and download.call_count == 0
    stale = {"value": [{"id": TID, "meetingId": MEETING_ID, "createdDateTime": _iso(now - timedelta(minutes=30))}]}
    graph.get(url__regex=rf"{re.escape(GRAPH)}/users/{ORG}/onlineMeetings/getAllTranscripts\(.*\)").mock(
        return_value=httpx.Response(200, json=stale))
    assert await ing.poll_once() == 1


async def test_a_denied_speaker_attribution_is_reported_and_retried_later(graph, tmp_path):
    denied = httpx.Response(403, json={"error": {"code": "Forbidden", "message": "x",
                                                  "innerError": {"code": "SpeakerAttributionNotAllowed"}}})
    _, download = mock_graph(graph, content=denied)
    ing, h = ingestor(tmp_path)
    assert await ing.poll_once() == 0
    status = ing.status()
    assert status.failed == 1 and status.last_poll_ok is False and "speaker attribution" in status.last_error
    assert status.recent[0].outcome == "failed"
    assert await ing.poll_once() == 0 and download.call_count == 1          # backing off, not hammering Graph
    ing._retry_at.clear()
    download.mock(return_value=httpx.Response(200, content=VTT.encode()))
    assert await ing.poll_once() == 1 and ing.status().last_error is None
    await h.service.drain()


async def test_missing_meeting_details_do_not_block_the_transcript(graph, tmp_path):
    mock_graph(graph, meeting=None)
    ing, h = ingestor(tmp_path)
    assert await ing.poll_once() == 1
    await h.service.drain()
    (rec,) = h.store.list_conversations()
    assert rec.meta.title == "Teams meeting" and rec.meta.participants and rec.status == ConversationStatus.needs_review


async def test_an_empty_transcript_is_reported_not_raised(graph, tmp_path):
    mock_graph(graph, vtt="WEBVTT\n\n")
    ing, _ = ingestor(tmp_path)
    assert await ing.poll_once() == 0
    assert ing.status().failed == 1 and "could not be read" in ing.status().last_error


async def test_organisers_given_as_a_upn_are_resolved_to_their_object_id(graph, tmp_path):
    lookup = user_route(graph, "seller@contoso.example", httpx.Response(200, json={
        "id": ORG, "displayName": "Pat Seller", "mail": "pat.seller@contoso.example"}))
    mock_graph(graph, directory=False)
    ing, h = ingestor(tmp_path, teams_organizers="seller@contoso.example")
    assert await ing.poll_once() == 1
    await h.service.drain()
    assert lookup.call_count == 1 and lookup.calls.last.request.url.params["$select"] == "id,displayName,mail,userPrincipalName"
    (rec,) = h.store.list_conversations()
    assert rec.meta.seller.display_name == "Pat Seller" and rec.meta.seller.email == "pat.seller@contoso.example"


async def test_an_unresolvable_upn_explains_the_object_id_alternative(graph, tmp_path):
    user_route(graph, "seller@contoso.example", httpx.Response(
        403, json={"error": {"code": "Authorization_RequestDenied", "message": "Insufficient privileges"}}))
    ing, _ = ingestor(tmp_path, teams_organizers="seller@contoso.example")
    with pytest.raises(GraphError, match="object id"):
        await ing.poll_once()
    assert ing.status().last_poll_ok is False


async def test_an_object_id_works_even_without_permission_to_read_the_directory(graph, tmp_path):
    mock_graph(graph, directory=False)
    user_route(graph, ORG, httpx.Response(403, json={"error": {"code": "Authorization_RequestDenied", "message": "no"}}))
    ing, h = ingestor(tmp_path)
    assert await ing.poll_once() == 1
    await h.service.drain()
    (rec,) = h.store.list_conversations()
    assert rec.meta.seller.email == CALL.seller.email               # the meeting's organiser entry supplies the address


@pytest.mark.parametrize("email,labels,expected", [
    ("AishaW@contoso.example", ["Aisha West"], "Aisha West"),                         # first name + last initial
    ("aisha.west@contoso.example", ["Aisha West"], "Aisha West"),
    ("awest@contoso.example", ["Aisha West"], "Aisha West"),                         # first initial + last name
    ("aishawest@contoso.example", ["Unknown", "Dana Whitfield", "Aisha West"], "Aisha West"),
    ("DanaW@contoso.example", ["Aisha West", "Dana Whitfield"], "Dana Whitfield"),
    ("ana.garcia@contoso.example", ["Ana García"], "Ana García"),                     # accents are ignored when comparing
    ("AishaW@contoso.example", ["Aisha West", "Aisha West"], "Aisha West"),           # the same speaker twice is one speaker
    ("AishaW@contoso.example", ["Aisha West", "Aisha Wong"], None),                   # two speakers fit: it names nobody
    ("sam@contoso.example", ["Aisha West"], None),
    ("AishaW@contoso.example", ["Unknown"], None),
    ("AishaW@contoso.example", [], None),
    ("not-an-address", ["Aisha West"], None),
    (None, ["Aisha West"], None),
])
def test_an_email_alias_is_matched_to_the_speaker_it_abbreviates(email, labels, expected):
    assert _name_from_alias(email, labels) == expected


async def test_an_organiser_without_a_display_name_is_named_from_the_transcripts_speaker_label(graph, tmp_path):
    """Application permissions give the organiser's address but no name (seen on a live tenant): "AishaW" is not a name."""
    vtt = VTT.replace(f"<v {CALL.seller.display_name}>", "<v Aisha West>")
    organizer = {"upn": "AishaW@contoso.example", "identity": {"user": {"id": ORG}}}
    meeting = {**MEETING, "participants": {**MEETING["participants"], "organizer": organizer}}
    mock_graph(graph, vtt=vtt, meeting=meeting, directory=False)
    user_route(graph, ORG, httpx.Response(403, json={"error": {"code": "Authorization_RequestDenied", "message": "no"}}))
    ing, h = ingestor(tmp_path)
    assert await ing.poll_once() == 1
    await h.service.drain()
    (rec,) = h.store.list_conversations()
    assert rec.meta.seller.display_name == "Aisha West" and rec.meta.seller.email == "AishaW@contoso.example"
    assert rec.status == ConversationStatus.needs_review


async def test_polling_needs_organisers_and_the_app_settings(tmp_path):
    ing, _ = ingestor(tmp_path, teams_organizers="")
    with pytest.raises(GraphError, match="no organisers"):
        await ing.poll_once()
    h = _service(tmp_path / "svc2", FakeLlm(lambda call: {}))
    bare = TeamsIngestor.from_settings(h.service, settings(tmp_path, teams_client_secret=""))
    assert bare.status().configured is False and "client secret" in bare.status().problem
    with pytest.raises(GraphError, match="client secret"):
        await bare.poll_once()


async def test_webhook_references_are_ingested_with_the_organiser_from_the_notification(graph, tmp_path):
    _, download = mock_graph(graph)
    ing, h = ingestor(tmp_path, teams_mode="webhook", teams_organizers="")
    ref = TranscriptRef(meeting_id=MEETING_ID, transcript_id=TID, organizer_id=ORG, resource="x")
    adhoc = TranscriptRef(meeting_id="call", transcript_id="t2", organizer_id=ORG, resource="y", kind="adhoc_call")
    assert await ing.ingest_refs([ref, adhoc]) == 1 and download.call_count == 1
    await h.service.drain()
    assert ing.status().ingested == 1


async def test_a_notification_without_an_organiser_falls_back_to_the_configured_ones(graph, tmp_path):
    other = "99999999-9999-9999-9999-999999999999"
    user_route(graph, other, httpx.Response(404, json={"error": {"code": "Request_ResourceNotFound", "message": "no"}}))
    graph.get(f"{GRAPH}/users/{other}/onlineMeetings/{MEETING_ID}/transcripts/{TID}/content").mock(
        return_value=httpx.Response(404, json={"error": {"code": "NotFound", "message": "no"}}))
    graph.get(f"{GRAPH}/users/{other}/onlineMeetings/{MEETING_ID}").mock(return_value=httpx.Response(404, json={}))
    mock_graph(graph)
    ing, h = ingestor(tmp_path, teams_mode="webhook", teams_organizers=f"{other},{ORG}")
    ref = TranscriptRef(meeting_id=MEETING_ID, transcript_id=TID, organizer_id=None, resource="x")
    assert await ing.ingest_refs([ref]) == 1
    await h.service.drain()


async def test_a_notification_that_cannot_be_located_is_reported(graph, tmp_path):
    ing, _ = ingestor(tmp_path, teams_mode="webhook", teams_organizers="")
    assert await ing.ingest_refs([TranscriptRef(meeting_id=MEETING_ID, transcript_id=TID, resource="x")]) == 0
    assert "organiser" in ing.status().last_error


async def test_the_poller_runs_only_in_poll_mode_and_stops_cleanly(tmp_path):
    ing, _ = ingestor(tmp_path)
    started = asyncio.Event()

    async def fake_poll() -> int:
        started.set()
        await asyncio.sleep(60)
        return 0

    ing.poll_once = fake_poll                                      # type: ignore[method-assign]
    ing.start()
    await asyncio.wait_for(started.wait(), 2)
    assert ing._task is not None and not ing._task.done()
    await ing.stop()
    assert ing._task is None
    webhook, _ = ingestor(tmp_path / "w", teams_mode="webhook")
    webhook.start()
    assert webhook._task is None
    await webhook.stop()


# ---- earlier meetings: list what Graph still holds, then import the ones picked ---------------------------------
OTHER = "99999999-9999-9999-9999-999999999999"
LISTER = rf"{re.escape(GRAPH)}/users/{ORG}/onlineMeetings/getAllTranscripts\(.*\)"


def selection(**kw) -> TeamsImportSelection:
    return TeamsImportSelection(**{"organizer": ORG, "meeting_id": MEETING_ID, "transcript_id": TID, **kw})


def transcript_entry(tid: str, meeting_id: str, days_ago: float, minutes: int = 30) -> dict:
    created = utcnow() - timedelta(days=days_ago)
    return {"id": tid, "meetingId": meeting_id, "createdDateTime": _iso(created),
            "endDateTime": _iso(created + timedelta(minutes=minutes))}


def earlier_meeting(router, meeting_id: str, subject: str, *, tid: str | None = None) -> None:
    """Details (and, with ``tid``, the transcript download) of one more meeting the organiser hosted."""
    base = f"{GRAPH}/users/{ORG}/onlineMeetings/{meeting_id}"
    router.get(base).mock(return_value=httpx.Response(200, json={**MEETING, "subject": subject}))
    if tid:
        router.get(f"{base}/transcripts/{tid}/content").mock(return_value=httpx.Response(200, content=VTT.encode()))


def requested_since(call) -> datetime:
    stamp = re.search(r"startDateTime=([^)]+)\)", call.request.url.path).group(1)
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


async def test_history_lists_earlier_meetings_newest_first_and_marks_what_the_inbox_has(graph, tmp_path):
    items = {"value": [transcript_entry("T-OLD", "MEET-OLD", days_ago=20), transcript_entry(TID, MEETING_ID, days_ago=3)]}
    lister, download = mock_graph(graph, items=items)
    earlier_meeting(graph, "MEET-OLD", "Woodgrove | Quarterly review")
    ing, h = ingestor(tmp_path)

    history = await ing.history()
    assert history.days == 30 and not history.truncated and history.errors == []
    assert abs((utcnow() - timedelta(days=30)) - requested_since(lister.calls.last)) < timedelta(minutes=1)
    assert lister.calls.last.request.url.params["$top"] == "50"
    newest, oldest = history.items
    assert (newest.transcript_id, oldest.transcript_id) == (TID, "T-OLD")                       # newest first
    assert newest.title == MEETING["subject"] and oldest.title == "Woodgrove | Quarterly review"
    assert newest.organizer == ORG and newest.organizer_name == CALL.seller.display_name
    assert newest.meeting_id == MEETING_ID and newest.duration_seconds == 30 * 60
    assert newest.started_at == datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
    assert newest.attendees == [c.display_name for c in CALL.customers]                      # the organiser is not a guest
    assert [i.state for i in history.items] == ["new", "new"] and newest.conversation_id is None
    assert download.call_count == 0 and not h.store.list_conversations()                      # listing ingests nothing

    await ing.import_transcripts([selection()])
    await h.service.drain()
    (rec,) = h.store.list_conversations()
    again = await ing.history()
    assert [i.state for i in again.items] == ["in_inbox", "new"]
    assert again.items[0].conversation_id == rec.meta.conversation_id
    assert again.items[0].conversation_status == rec.status.value


async def test_history_without_meeting_details_still_lists_the_transcript(graph, tmp_path):
    entry = transcript_entry(TID, MEETING_ID, days_ago=5)
    mock_graph(graph, items={"value": [entry]}, meeting=None)
    ing, _ = ingestor(tmp_path)
    (item,) = (await ing.history()).items
    assert item.title is None and item.attendees == [] and item.state == "new"
    assert item.started_at == _parse_time(entry["createdDateTime"])                          # the transcript's own time
    assert item.organizer_name == CALL.seller.display_name


async def test_history_labels_people_without_a_known_name_by_address(graph, tmp_path):
    """No directory access: "AishaW" is an alias, not a name, so the list shows the address instead of "Aishaw"."""
    organizer = {"upn": "AishaW@contoso.example", "identity": {"user": {"id": ORG}}}
    guests = [{"upn": "pat.guest@contoso.example", "identity": {"user": {}}}, {"upn": "PatG2@contoso.example", "identity": {"user": {}}},
              {"upn": "aishaw@contoso.example", "identity": {"user": {}}}]
    mock_graph(graph, meeting={**MEETING, "participants": {"organizer": organizer, "attendees": guests}}, directory=False)
    user_route(graph, ORG, httpx.Response(403, json={"error": {"code": "Authorization_RequestDenied", "message": "no"}}))
    ing, _ = ingestor(tmp_path)
    (item,) = (await ing.history()).items
    assert item.organizer_name == "AishaW@contoso.example"
    assert item.attendees == ["Pat Guest", "PatG2@contoso.example"]                            # the organiser is not a guest


@pytest.mark.parametrize("email,expected", [
    ("aisha.west@contoso.example", "Aisha West"), ("aisha_west@contoso.example", "Aisha West"),
    ("AishaW@contoso.example", None), ("awest@contoso.example", None), ("not-an-address", None), (None, None)])
def test_a_listed_name_comes_only_from_an_address_that_spells_one_out(email, expected):
    assert _listed_name(email) == expected


async def test_history_marks_a_transcript_still_being_written_as_unfinished(graph, tmp_path):
    running = {"value": [{"id": TID, "meetingId": MEETING_ID, "createdDateTime": _iso(utcnow() - timedelta(minutes=3))}]}
    mock_graph(graph, items=running)
    ing, _ = ingestor(tmp_path)
    (item,) = (await ing.history()).items
    assert item.state == "unfinished" and item.duration_seconds is None


async def test_history_clamps_the_window_and_caps_the_list(graph, tmp_path, monkeypatch):
    monkeypatch.setattr(teams_live, "HISTORY_LIMIT", 2)
    entries = [transcript_entry(f"T{n}", f"MEET-{n}", days_ago=n) for n in (4, 1, 3, 2)]
    lister, _ = mock_graph(graph, items={"value": entries})
    for n in range(1, 5):
        earlier_meeting(graph, f"MEET-{n}", f"Meeting {n}")
    ing, _ = ingestor(tmp_path)
    history = await ing.history(days=500)
    assert history.days == HISTORY_MAX_DAYS
    assert abs((utcnow() - timedelta(days=HISTORY_MAX_DAYS)) - requested_since(lister.calls.last)) < timedelta(minutes=1)
    assert [i.transcript_id for i in history.items] == ["T1", "T2"] and history.truncated      # the newest two of four
    assert (await ing.history(days=0)).days == 1


async def test_history_keeps_the_organisers_it_could_search_and_reports_the_rest(graph, tmp_path):
    mock_graph(graph)
    user_route(graph, OTHER, httpx.Response(200, json={"id": OTHER, "displayName": "Dana Other", "mail": "dana@contoso.example"}))
    blocked = graph.get(url__regex=rf"{re.escape(GRAPH)}/users/{OTHER}/onlineMeetings/getAllTranscripts\(.*\)").mock(
        return_value=httpx.Response(403, json={"error": {"code": "Forbidden", "message": "no application access policy"}}))
    ing, _ = ingestor(tmp_path, teams_organizers=f"{ORG},{OTHER}")
    history = await ing.history()
    assert [i.transcript_id for i in history.items] == [TID] and history.organizers == [ORG, OTHER]
    (error,) = history.errors
    assert error.startswith(f"{OTHER}: ") and "403" in error and blocked.call_count == 1
    graph.get(url__regex=LISTER).mock(return_value=httpx.Response(403, json={"error": {"code": "Forbidden", "message": "no"}}))
    with pytest.raises(GraphError, match="403"):                                              # nothing could be searched
        await ing.history()


async def test_history_and_import_need_the_app_settings_and_organisers(tmp_path):
    h = _service(tmp_path / "svc", FakeLlm(lambda call: {}))
    bare = TeamsIngestor.from_settings(h.service, settings(tmp_path, teams_client_secret=""))
    with pytest.raises(GraphError, match="client secret"):
        await bare.history()
    with pytest.raises(GraphError, match="client secret"):
        await bare.import_transcripts([selection()])
    nobody, _ = ingestor(tmp_path / "n", teams_organizers="")
    with pytest.raises(GraphError, match="no organisers"):
        await nobody.history()


async def test_importing_an_earlier_meeting_runs_the_same_pipeline_and_leaves_the_live_counters_alone(graph, tmp_path):
    _, download = mock_graph(graph)
    ing, h = ingestor(tmp_path)
    result = await ing.import_transcripts([selection()])
    await h.service.drain()
    assert (result.ingested, result.duplicates, result.failed) == (1, 0, 0)
    (outcome,) = result.outcomes
    (rec,) = h.store.list_conversations()
    assert (outcome.outcome, outcome.transcript_id, outcome.title) == ("ingested", TID, MEETING["subject"])
    assert outcome.conversation_id == rec.meta.conversation_id
    assert rec.status == ConversationStatus.needs_review and rec.meta.source.value == "teams"
    assert rec.meta.source_interaction_id == TID and rec.meta.seller.display_name == CALL.seller.display_name
    assert rec.proposal.crm_match.account is not None and rec.proposal.crm_match.account.id == CALL.gold.expect_crm_account
    live = ing.status()
    assert (live.ingested, live.duplicates, live.failed, live.recent) == (0, 0, 0, [])
    assert await ing.poll_once() == 0 and download.call_count == 1                              # the poller skips it too
    assert ing.status().duplicates == 0


async def test_an_import_is_not_blocked_by_what_the_poller_already_remembers(graph, tmp_path):
    """After "Reset demo data" the inbox is empty but the poller still remembers the meeting; the import must not."""
    mock_graph(graph)
    ing, h = ingestor(tmp_path)
    assert await ing.poll_once() == 1
    await h.service.drain()
    h.store.clear_all()
    ing.forget()
    assert await ing.poll_once() == 0 and not h.store.list_conversations()                      # remembered: stays gone
    result = await ing.import_transcripts([selection()])
    await h.service.drain()
    assert result.ingested == 1 and len(h.store.list_conversations()) == 1


async def test_importing_the_same_meeting_twice_never_makes_a_second_conversation(graph, tmp_path):
    mock_graph(graph)
    ing, h = ingestor(tmp_path)
    both = await ing.import_transcripts([selection(), selection()])                            # even in one request
    await h.service.drain()
    assert (both.ingested, both.duplicates) == (1, 1)
    later = await ing.import_transcripts([selection()])
    (outcome,) = later.outcomes
    (rec,) = h.store.list_conversations()
    assert (later.ingested, later.duplicates, outcome.outcome) == (0, 1, "duplicate")
    assert outcome.conversation_id == rec.meta.conversation_id


async def test_importing_several_meetings_at_once(graph, tmp_path):
    mock_graph(graph)
    earlier_meeting(graph, "MEET-2", "Woodgrove | Second meeting", tid="T-2")
    ing, h = ingestor(tmp_path)
    result = await ing.import_transcripts([selection(), selection(meeting_id="MEET-2", transcript_id="T-2")])
    await h.service.drain()
    assert (result.ingested, result.failed) == (2, 0) and len(h.store.list_conversations()) == 2
    assert {o.title for o in result.outcomes} == {MEETING["subject"], "Woodgrove | Second meeting"}


async def test_an_import_only_serves_the_configured_organisers_and_a_sane_number_of_meetings(graph, tmp_path):
    _, download = mock_graph(graph)
    ing, _ = ingestor(tmp_path)
    with pytest.raises(ValueError, match="not one of the configured organisers"):
        await ing.import_transcripts([selection(organizer=OTHER)])
    with pytest.raises(ValueError, match="between 1 and"):
        await ing.import_transcripts([])
    with pytest.raises(ValueError, match="between 1 and"):
        await ing.import_transcripts([selection()] * (IMPORT_MAX + 1))
    assert download.call_count == 0                                                            # refused before any Graph call


async def test_a_failed_import_is_an_outcome_not_an_exception_and_can_be_retried(graph, tmp_path):
    denied = httpx.Response(403, json={"error": {"code": "Forbidden", "message": "x",
                                                  "innerError": {"code": "SpeakerAttributionNotAllowed"}}})
    _, download = mock_graph(graph, content=denied)
    earlier_meeting(graph, "MEET-2", "Woodgrove | Second meeting", tid="T-2")
    ing, h = ingestor(tmp_path)
    result = await ing.import_transcripts([selection(), selection(meeting_id="MEET-2", transcript_id="T-2")])
    await h.service.drain()
    assert (result.ingested, result.failed) == (1, 1)                                          # the other one went through
    bad = next(o for o in result.outcomes if o.transcript_id == TID)
    assert bad.outcome == "failed" and bad.conversation_id is None and "speaker attribution" in bad.detail
    assert ing.status().failed == 0 and ing.status().last_error is None                         # not the poller's to report
    download.mock(return_value=httpx.Response(200, content=VTT.encode()))
    retry = await ing.import_transcripts([selection()])
    await h.service.drain()
    assert retry.ingested == 1 and len(h.store.list_conversations()) == 2


async def test_an_unreadable_transcript_is_reported_for_that_meeting_only(graph, tmp_path):
    mock_graph(graph, vtt="WEBVTT\n\n")
    ing, h = ingestor(tmp_path)
    result = await ing.import_transcripts([selection()])
    assert result.failed == 1 and "could not be read" in result.outcomes[0].detail
    assert not h.store.list_conversations()


async def test_a_failed_conversation_is_listed_as_new_and_can_be_imported_again(graph, tmp_path):
    mock_graph(graph)
    ing, h = ingestor(tmp_path)
    first = await ing.import_transcripts([selection()])
    await h.service.drain()
    (rec,) = h.store.list_conversations()
    rec.status = ConversationStatus.failed
    h.store.save_conversation(rec)
    (item,) = (await ing.history()).items
    assert item.state == "new" and item.conversation_id is None
    again = await ing.import_transcripts([selection()])
    await h.service.drain()
    assert again.ingested == 1 and again.outcomes[0].conversation_id != first.outcomes[0].conversation_id
