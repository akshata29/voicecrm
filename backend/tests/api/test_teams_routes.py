"""Teams routes: status, poll now, earlier meetings (list + import), the authenticated Graph webhook and the lifecycle endpoint."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from voicecrm.adapters.sources.teams_graph import GraphError
from voicecrm.domain.conversation import utcnow
from voicecrm.pipeline.teams_live import (
    IMPORT_MAX, TeamsHistory, TeamsHistoryItem, TeamsImportOutcome, TeamsImportResult, TeamsStatus)

from .helpers import API

ORG = "8b081ef6-4792-4def-b2c9-c363a1bf41d5"


def notification(client_state: str, transcript: str = "T1") -> dict:
    return {"value": [{"subscriptionId": "sub-1", "changeType": "created", "clientState": client_state, "tenantId": "t",
                       "resource": f"users/{ORG}/onlineMeetings('M1')/transcripts('{transcript}')"}]}


def pick(transcript: str = "T1") -> dict:
    return {"organizer": ORG, "meeting_id": "M1", "transcript_id": transcript}


class StubIngestor:
    def __init__(self, polled: int = 2, error: Exception | None = None) -> None:
        self.refs: list = []
        self.polled, self.error = polled, error
        self.days: list[int] = []
        self.imported: list = []
        self.graph = SimpleNamespace(renewed=[])

        async def renew(subscription_id: str, minutes: int) -> None:
            self.graph.renewed.append((subscription_id, minutes))

        self.graph.renew_subscription = renew

    async def ingest_refs(self, refs) -> int:
        self.refs += refs
        return len(refs)

    async def poll_once(self) -> int:
        if self.error:
            raise self.error
        return self.polled

    async def history(self, days: int) -> TeamsHistory:
        if self.error:
            raise self.error
        self.days.append(days)
        item = TeamsHistoryItem(organizer=ORG, meeting_id="M1", transcript_id="T1", title="Renewal call")
        return TeamsHistory(days=days, since=utcnow() - timedelta(days=days), organizers=[ORG], items=[item])

    async def import_transcripts(self, selections) -> TeamsImportResult:
        if self.error:
            raise self.error
        self.imported += selections
        outcomes = [TeamsImportOutcome(transcript_id=s.transcript_id, outcome="ingested", conversation_id=f"c-{s.transcript_id}")
                    for s in selections]
        return TeamsImportResult(ingested=len(outcomes), outcomes=outcomes)

    def status(self) -> TeamsStatus:
        return TeamsStatus(mode="poll", configured=True, ingested=self.polled)


@pytest.fixture()
def teams(api_container, monkeypatch):
    stub = StubIngestor()
    monkeypatch.setattr(api_container, "teams", stub)
    monkeypatch.setattr(api_container.settings, "teams_client_state", "good-state")
    return stub


def test_status_when_teams_is_off_says_how_to_enable_it(client: TestClient):
    body = client.get(f"{API}/sources/teams").json()
    assert body["mode"] == "off" and body["configured"] is False and "VOICECRM_TEAMS_MODE=poll" in body["problem"]


def test_poll_now_is_refused_when_teams_is_off(client: TestClient):
    response = client.post(f"{API}/sources/teams/poll")
    assert response.status_code == 409 and "off" in response.json()["detail"]


def test_poll_now_reports_what_it_ingested(client: TestClient, teams):
    body = client.post(f"{API}/sources/teams/poll").json()
    assert body["ingested"] == 2 and body["status"]["mode"] == "poll"
    assert client.get(f"{API}/sources/teams").json()["configured"] is True


@pytest.mark.parametrize("error,status", [(GraphError("no", status=403, hint="fix it"), 502), (GraphError("not configured"), 409)])
def test_poll_errors_carry_the_fix_and_a_sensible_status(client: TestClient, teams, error, status):
    teams.error = error
    response = client.post(f"{API}/sources/teams/poll")
    assert response.status_code == status and error.message in response.json()["detail"]


def test_earlier_meetings_are_refused_when_teams_is_off(client: TestClient):
    listing = client.get(f"{API}/sources/teams/history")
    assert listing.status_code == 409 and "off" in listing.json()["detail"]
    importing = client.post(f"{API}/sources/teams/import", json={"items": [pick()]})
    assert importing.status_code == 409 and "off" in importing.json()["detail"]


def test_history_lists_the_earlier_meetings_for_a_window_of_days(client: TestClient, teams):
    body = client.get(f"{API}/sources/teams/history").json()                          # a month unless told otherwise
    assert body["days"] == 30 and body["organizers"] == [ORG] and body["truncated"] is False and body["errors"] == []
    assert [(i["transcript_id"], i["title"], i["state"]) for i in body["items"]] == [("T1", "Renewal call", "new")]
    assert client.get(f"{API}/sources/teams/history", params={"days": 7}).json()["days"] == 7
    assert teams.days == [30, 7]


@pytest.mark.parametrize("days", [0, -1, 91, "many"])
def test_history_window_must_be_between_one_and_ninety_days(client: TestClient, teams, days):
    assert client.get(f"{API}/sources/teams/history", params={"days": days}).status_code == 422
    assert teams.days == []


@pytest.mark.parametrize("error,status", [(GraphError("no", status=403, hint="fix it"), 502), (GraphError("not configured"), 409)])
def test_history_errors_carry_the_fix_and_a_sensible_status(client: TestClient, teams, error, status):
    teams.error = error
    response = client.get(f"{API}/sources/teams/history")
    assert response.status_code == status and error.message in response.json()["detail"]


def test_import_runs_the_picked_transcripts(client: TestClient, teams):
    body = client.post(f"{API}/sources/teams/import", json={"items": [pick("T1"), pick("T2")]}).json()
    assert (body["ingested"], body["duplicates"], body["failed"]) == (2, 0, 0)
    assert [(o["transcript_id"], o["outcome"], o["conversation_id"]) for o in body["outcomes"]] == [
        ("T1", "ingested", "c-T1"), ("T2", "ingested", "c-T2")]
    assert [(s.organizer, s.meeting_id, s.transcript_id) for s in teams.imported] == [(ORG, "M1", "T1"), (ORG, "M1", "T2")]


@pytest.mark.parametrize("body", [
    {}, {"items": []}, {"items": [pick()] * (IMPORT_MAX + 1)},
    {"items": [{"organizer": ORG, "meeting_id": "M1"}]},                                # no transcript id
    {"items": [{**pick(), "title": "not a field"}]},                                    # unknown keys are refused
    {"items": [{**pick(), "transcript_id": ""}]},
])
def test_import_refuses_a_malformed_request(client: TestClient, teams, body):
    assert client.post(f"{API}/sources/teams/import", json=body).status_code == 422
    assert teams.imported == []


@pytest.mark.parametrize("error,status", [
    (ValueError("someone is not one of the configured organisers"), 422),
    (GraphError("no", status=403, hint="fix it"), 502), (GraphError("not configured"), 409)])
def test_import_errors_carry_the_reason_and_a_sensible_status(client: TestClient, teams, error, status):
    teams.error = error
    response = client.post(f"{API}/sources/teams/import", json={"items": [pick()]})
    assert response.status_code == status and getattr(error, "message", str(error)) in response.json()["detail"]


def test_graph_validation_is_answered_with_the_decoded_token(client: TestClient):
    response = client.post(f"{API}/webhooks/teams", params={"validationToken": "abc def=="})
    assert response.status_code == 200 and response.text == "abc def==" and response.headers["content-type"].startswith("text/plain")


def test_notifications_with_the_right_client_state_are_ingested_in_the_background(client: TestClient, teams):
    response = client.post(f"{API}/webhooks/teams", json=notification("good-state"))
    assert response.status_code == 202 and response.json()["ingesting"] is True
    assert [(r.meeting_id, r.transcript_id, r.organizer_id) for r in teams.refs] == [("M1", "T1", ORG)]


def test_notifications_with_a_wrong_client_state_are_dropped(client: TestClient, teams):
    response = client.post(f"{API}/webhooks/teams", json=notification("forged"))
    assert response.status_code == 202 and response.json()["transcripts"] == 0 and teams.refs == []


def test_without_live_ingestion_notifications_are_acknowledged_but_not_ingested(client: TestClient):
    body = client.post(f"{API}/webhooks/teams", json=notification("anything")).json()
    assert body["transcripts"] == 1 and body["ingesting"] is False and "VOICECRM_TEAMS_MODE" in body["note"]


def test_live_ingestion_refuses_unauthenticated_notifications(client: TestClient, api_container, monkeypatch):
    stub = StubIngestor()
    monkeypatch.setattr(api_container, "teams", stub)
    monkeypatch.setattr(api_container.settings, "teams_client_state", "")
    body = client.post(f"{API}/webhooks/teams", json=notification("")).json()
    assert body["ingesting"] is False and "CLIENT_STATE" in body["note"] and stub.refs == []


def test_the_lifecycle_endpoint_also_needs_a_client_state(client: TestClient, api_container, monkeypatch):
    stub = StubIngestor()
    monkeypatch.setattr(api_container, "teams", stub)
    monkeypatch.setattr(api_container.settings, "teams_client_state", "")
    payload = {"value": [{"subscriptionId": "sub-1", "lifecycleEvent": "reauthorizationRequired", "clientState": ""}]}
    assert client.post(f"{API}/webhooks/teams/lifecycle", json=payload).status_code == 202
    assert stub.graph.renewed == []


def test_lifecycle_validation_and_reauthorization(client: TestClient, teams):
    assert client.post(f"{API}/webhooks/teams/lifecycle", params={"validationToken": "xyz"}).text == "xyz"
    payload = {"value": [{"subscriptionId": "sub-9", "lifecycleEvent": "reauthorizationRequired", "clientState": "good-state"},
                         {"subscriptionId": "sub-evil", "lifecycleEvent": "reauthorizationRequired", "clientState": "forged"},
                         {"subscriptionId": "sub-8", "lifecycleEvent": "missed", "clientState": "good-state"}]}
    assert client.post(f"{API}/webhooks/teams/lifecycle", json=payload).status_code == 202
    assert teams.graph.renewed == [("sub-9", 55)]
