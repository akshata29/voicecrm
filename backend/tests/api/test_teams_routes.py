"""Teams routes: status, poll now, the authenticated Graph webhook and the lifecycle endpoint."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from voicecrm.adapters.sources.teams_graph import GraphError
from voicecrm.pipeline.teams_live import TeamsStatus

from .helpers import API

ORG = "8b081ef6-4792-4def-b2c9-c363a1bf41d5"


def notification(client_state: str, transcript: str = "T1") -> dict:
    return {"value": [{"subscriptionId": "sub-1", "changeType": "created", "clientState": client_state, "tenantId": "t",
                       "resource": f"users/{ORG}/onlineMeetings('M1')/transcripts('{transcript}')"}]}


class StubIngestor:
    def __init__(self, polled: int = 2, error: Exception | None = None) -> None:
        self.refs: list = []
        self.polled, self.error = polled, error
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
