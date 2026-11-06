"""`voicecrm teams ...`: subscription management and the access check, against a mocked Graph."""
from __future__ import annotations

import json
import re

import httpx
import pytest
import respx

from voicecrm import cli
from voicecrm.config import Settings

TENANT, CLIENT, SECRET = "11111111-2222-3333-4444-555555555555", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "s3cr3t-value-123456"
ORG = "8b081ef6-4792-4def-b2c9-c363a1bf41d5"
GRAPH = "https://graph.microsoft.com/v1.0"
TOKEN = re.compile(r"https://login\.microsoftonline\.com/.*/oauth2/v2\.0/token")


def configure(monkeypatch, tmp_path, **kw) -> Settings:
    base = dict(_env_file=None, state_dir=tmp_path, teams_mode="webhook", teams_tenant_id=TENANT, teams_client_id=CLIENT,
                teams_client_secret=SECRET, teams_public_base_url="https://demo.devtunnels.ms", teams_client_state="state-xyz")
    settings = Settings(**{**base, **kw})
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real(*a, **{**k, "verify": False}))
    return settings


@pytest.fixture()
def graph():
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN).mock(return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600}))
        yield router


def test_subscribe_asks_graph_to_post_to_the_public_url(monkeypatch, tmp_path, graph, capsys):
    configure(monkeypatch, tmp_path)
    created = graph.post(f"{GRAPH}/subscriptions").mock(return_value=httpx.Response(201, json={
        "id": "sub-1", "expirationDateTime": "2026-11-03T12:00:00Z"}))
    assert cli.main(["teams", "subscribe"]) == 0
    body = json.loads(created.calls.last.request.content)
    assert body["notificationUrl"] == "https://demo.devtunnels.ms/api/v1/webhooks/teams"
    assert body["resource"] == "communications/onlineMeetings/getAllTranscripts" and body["clientState"] == "state-xyz"
    assert "lifecycleNotificationUrl" not in body
    assert "subscribed sub-1" in capsys.readouterr().out


def test_a_long_subscription_also_registers_the_lifecycle_endpoint_and_one_organiser(monkeypatch, tmp_path, graph):
    configure(monkeypatch, tmp_path)
    graph.get(url__regex=rf"{re.escape(GRAPH)}/users/{ORG}(\?.*)?$").mock(return_value=httpx.Response(200, json={"id": ORG}))
    created = graph.post(f"{GRAPH}/subscriptions").mock(return_value=httpx.Response(201, json={"id": "sub-2", "expirationDateTime": "x"}))
    assert cli.main(["teams", "subscribe", "--minutes", "240", "--organizer", ORG]) == 0
    body = json.loads(created.calls.last.request.content)
    assert body["resource"] == f"users/{ORG}/onlineMeetings/getAllTranscripts"
    assert body["lifecycleNotificationUrl"] == "https://demo.devtunnels.ms/api/v1/webhooks/teams/lifecycle"


def test_subscribe_without_a_public_url_or_client_state_explains_why(monkeypatch, tmp_path, graph, capsys):
    configure(monkeypatch, tmp_path, teams_public_base_url="http://localhost:8010")
    assert cli.main(["teams", "subscribe"]) == 2
    assert "https URL" in capsys.readouterr().out and graph.calls.call_count == 0


def test_a_refused_subscription_prints_the_admin_fix(monkeypatch, tmp_path, graph, capsys):
    configure(monkeypatch, tmp_path)
    graph.post(f"{GRAPH}/subscriptions").mock(return_value=httpx.Response(403, json={"error": {
        "code": "Forbidden", "message": "denied", "innerError": {"code": "GraphAccessToTranscriptsDisabled"}}}))
    assert cli.main(["teams", "subscribe"]) == 1
    assert "Transcript API access" in capsys.readouterr().out


def test_subscriptions_and_unsubscribe(monkeypatch, tmp_path, graph, capsys):
    configure(monkeypatch, tmp_path)
    graph.get(f"{GRAPH}/subscriptions").mock(return_value=httpx.Response(200, json={"value": [{
        "id": "sub-1", "resource": "communications/onlineMeetings/getAllTranscripts",
        "expirationDateTime": "2026-11-03T12:00:00Z", "notificationUrl": "https://demo.devtunnels.ms/api/v1/webhooks/teams"}]}))
    deleted = graph.delete(f"{GRAPH}/subscriptions/sub-1").mock(return_value=httpx.Response(204))
    assert cli.main(["teams", "subscriptions"]) == 0 and "sub-1" in capsys.readouterr().out
    assert cli.main(["teams", "unsubscribe", "sub-1"]) == 0 and deleted.call_count == 1


def test_check_requires_teams_to_be_switched_on(monkeypatch, tmp_path, capsys):
    configure(monkeypatch, tmp_path, teams_mode="off")
    assert cli.main(["teams", "check"]) == 2
    assert "VOICECRM_TEAMS_MODE" in capsys.readouterr().out


def test_check_with_an_incomplete_app_fails_without_calling_graph(monkeypatch, tmp_path, graph, capsys):
    configure(monkeypatch, tmp_path, teams_mode="poll", teams_client_secret="")
    assert cli.main(["teams", "check"]) == 1
    out = capsys.readouterr().out
    assert "CLIENT_SECRET" in out and "[FAIL]" in out and graph.calls.call_count == 0
