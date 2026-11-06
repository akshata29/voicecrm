"""Preflight for Teams live ingestion: configuration checks and a Graph probe that names the fix for each failure."""
from __future__ import annotations

import re

import httpx
import pytest
import respx

from voicecrm import preflight
from voicecrm.adapters.sources.teams import render_teams_vtt
from voicecrm.config import Settings
from voicecrm.preflight import Check, check_config, probe_teams, readiness

from .test_stats import CALLS

TENANT, CLIENT, SECRET = "11111111-2222-3333-4444-555555555555", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "s3cr3t-value-123456"
ORG = "8b081ef6-4792-4def-b2c9-c363a1bf41d5"
GRAPH = "https://graph.microsoft.com/v1.0"
TOKEN = re.compile(r"https://login\.microsoftonline\.com/.*/oauth2/v2\.0/token")
CALL = next(c for c in CALLS if c.fixture_id == "pb-teams-equity-class-c")
APP = dict(teams_mode="poll", teams_tenant_id=TENANT, teams_client_id=CLIENT, teams_client_secret=SECRET, teams_organizers=ORG)


def make(tmp_path, **kw) -> Settings:
    return Settings(_env_file=None, state_dir=tmp_path / "state", demo_mode=False, **{**APP, **kw})


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


@pytest.fixture()
def graph(monkeypatch):
    # the probe builds its own HTTP client; skipping certificate loading keeps the tests fast
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real(*a, **{**k, "verify": False}))
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN).mock(return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600}))
        router.get(url__regex=rf"{re.escape(GRAPH)}/users/{ORG}(\?.*)?$").mock(
            return_value=httpx.Response(200, json={"id": ORG, "displayName": "Pat Seller", "mail": "pat@contoso.example"}))
        yield router


def listing(router, *, items=None, status=200, body=None):
    return router.get(url__regex=rf"{re.escape(GRAPH)}/users/{ORG}/onlineMeetings/getAllTranscripts\(.*\)").mock(
        return_value=httpx.Response(status, json=body if body is not None else {"value": items if items is not None else []}))


def test_off_means_no_teams_checks(tmp_path):
    assert not [c for c in check_config(make(tmp_path, teams_mode="off")) if "teams" in c.name]


def test_a_complete_poll_configuration_passes(tmp_path):
    checks = by_name(check_config(make(tmp_path)))
    assert checks["config: teams app"].status == "pass" and "config: teams organisers" not in checks


@pytest.mark.parametrize("missing", ["teams_tenant_id", "teams_client_id", "teams_client_secret"])
def test_a_missing_app_setting_fails_and_is_named(tmp_path, missing):
    check = by_name(check_config(make(tmp_path, **{missing: ""})))["config: teams app"]
    assert check.status == "fail" and missing.removeprefix("teams_").upper() in check.detail


def test_polling_needs_organisers_but_webhooks_only_warn(tmp_path):
    assert by_name(check_config(make(tmp_path, teams_organizers="")))["config: teams organisers"].status == "fail"
    assert by_name(check_config(make(tmp_path, teams_organizers="", teams_mode="webhook")))["config: teams organisers"].status == "warn"


def test_webhook_mode_needs_an_https_url_and_a_client_state(tmp_path):
    bad = by_name(check_config(make(tmp_path, teams_mode="webhook", teams_public_base_url="http://localhost:8010")))
    assert bad["config: teams webhook"].status == "fail"
    assert "https" in bad["config: teams webhook"].hint and "CLIENT_STATE" in bad["config: teams webhook"].hint
    good = by_name(check_config(make(tmp_path, teams_mode="webhook", teams_public_base_url="https://x.devtunnels.ms",
                                     teams_client_state="long-random-state")))
    assert good["config: teams webhook"].status == "pass"


async def test_a_working_setup_downloads_the_newest_transcript_and_checks_speakers(tmp_path, graph):
    listing(graph, items=[{"id": "t-old", "meetingId": "m1", "createdDateTime": "2026-11-03T09:00:00Z"},
                          {"id": "t-new", "meetingId": "m2", "createdDateTime": "2026-11-03T10:00:00Z"}])
    content = graph.get(f"{GRAPH}/users/{ORG}/onlineMeetings/m2/transcripts/t-new/content").mock(
        return_value=httpx.Response(200, content=render_teams_vtt(CALL).encode()))
    graph.get(f"{GRAPH}/users/{ORG}/onlineMeetings/m2").mock(return_value=httpx.Response(200, json={
        "subject": "Equity review", "participants": {"attendees": [{"upn": "a@b.example"}]}}))
    checks = by_name(await probe_teams(make(tmp_path)))
    assert content.call_count == 1
    assert checks["teams: app sign-in"].status == "pass" and "client aaaaaaaa" in checks["teams: app sign-in"].detail
    assert f"teams: list transcripts ({ORG})" in checks and "2 transcript" in checks[f"teams: list transcripts ({ORG})"].detail
    assert checks["teams: download transcript"].status == "pass" and "speakers" in checks["teams: download transcript"].detail
    assert "Equity review" in checks["teams: meeting details"].detail
    assert checks["teams: Graph access"].status == "pass"
    assert checks["teams: Graph access"].detail == "app sign-in, transcript listing and download"
    assert readiness(list(checks.values()), make(tmp_path))[1][1] == "READY"
    assert readiness(list(checks.values()), make(tmp_path))[1][2] == "app sign-in, transcript listing and download"


async def test_no_transcripts_yet_is_a_pass_with_instructions(tmp_path, graph):
    listing(graph, items=[])
    checks = by_name(await probe_teams(make(tmp_path)))
    detail = checks[f"teams: list transcripts ({ORG})"].detail
    assert "no transcripts in the last 24 h" in detail and checks["teams: Graph access"].status == "pass"
    assert "untested" in checks["teams: Graph access"].detail and "download" in checks["teams: Graph access"].detail
    verdict, why = readiness(list(checks.values()), make(tmp_path))[1][1:]
    assert verdict == "READY" and "untested" in why                  # ready, and honest about what was not proven


async def test_the_admin_switch_being_off_is_diagnosed(tmp_path, graph):
    listing(graph, status=403, body={"error": {"code": "Forbidden", "message": "x",
                                               "innerError": {"code": "GraphAccessToTranscriptsDisabled"}}})
    checks = by_name(await probe_teams(make(tmp_path)))
    failed = checks[f"teams: list transcripts ({ORG})"]
    assert failed.status == "fail" and "Transcript API access" in failed.hint
    assert checks["teams: Graph access"].status == "fail"
    assert readiness(list(checks.values()), make(tmp_path))[1][1] == "NOT READY"


async def test_a_missing_access_policy_is_diagnosed(tmp_path, graph):
    listing(graph, status=403, body={"error": {"code": "Forbidden", "message": "No application access policy found for this app."}})
    failed = by_name(await probe_teams(make(tmp_path)))[f"teams: list transcripts ({ORG})"]
    assert "Grant-CsApplicationAccessPolicy" in failed.hint


async def test_unattributed_transcripts_are_flagged(tmp_path, graph):
    listing(graph, items=[{"id": "t1", "meetingId": "m1", "createdDateTime": "2026-11-03T10:00:00Z"}])
    graph.get(f"{GRAPH}/users/{ORG}/onlineMeetings/m1/transcripts/t1/content").mock(return_value=httpx.Response(
        200, content=b"WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nHello there\n"))
    graph.get(f"{GRAPH}/users/{ORG}/onlineMeetings/m1").mock(return_value=httpx.Response(200, json={}))
    download = by_name(await probe_teams(make(tmp_path)))["teams: download transcript"]
    assert download.status == "fail" and "speaker attribution" in download.hint


async def test_meeting_details_missing_is_only_a_warning(tmp_path, graph):
    listing(graph, items=[{"id": "t1", "meetingId": "m1", "createdDateTime": "2026-11-03T10:00:00Z"}])
    graph.get(f"{GRAPH}/users/{ORG}/onlineMeetings/m1/transcripts/t1/content").mock(
        return_value=httpx.Response(200, content=render_teams_vtt(CALL).encode()))
    graph.get(f"{GRAPH}/users/{ORG}/onlineMeetings/m1").mock(return_value=httpx.Response(403, json={"error": {"code": "Forbidden", "message": "x"}}))
    checks = by_name(await probe_teams(make(tmp_path)))
    assert checks["teams: meeting details"].status == "warn" and checks["teams: Graph access"].status == "warn"


async def test_a_wrong_secret_stops_at_sign_in_and_never_prints_it(tmp_path, graph):
    graph.post(TOKEN).mock(return_value=httpx.Response(401, json={"error": "invalid_client",
                                                                   "error_description": "AADSTS7000215: Invalid client secret provided."}))
    checks = await probe_teams(make(tmp_path))
    assert [c.name for c in checks] == ["teams: app sign-in", "teams: Graph access"]
    text = " ".join(f"{c.detail} {c.hint}" for c in checks)
    assert "secret VALUE" in text and SECRET not in text


async def test_webhook_mode_looks_for_a_transcript_subscription(tmp_path, graph):
    listing(graph, items=[])
    graph.get(f"{GRAPH}/subscriptions").mock(return_value=httpx.Response(200, json={"value": []}))
    missing = by_name(await probe_teams(make(tmp_path, teams_mode="webhook")))["teams: webhook subscription"]
    assert missing.status == "warn" and "teams subscribe" in missing.hint
    graph.get(f"{GRAPH}/subscriptions").mock(return_value=httpx.Response(200, json={"value": [{
        "id": "s1", "resource": "communications/onlineMeetings/getAllTranscripts", "expirationDateTime": "2026-11-03T12:00:00Z"}]}))
    found = by_name(await probe_teams(make(tmp_path, teams_mode="webhook")))["teams: webhook subscription"]
    assert found.status == "pass" and "2026-11-03T12:00:00Z" in found.detail


async def test_missing_app_settings_return_a_single_failure(tmp_path):
    (check,) = await probe_teams(make(tmp_path, teams_client_secret=""))
    assert check.name == "teams: Graph access" and check.status == "fail" and "client secret" in check.detail


async def test_a_broken_teams_config_does_not_block_the_azure_probes(tmp_path):
    settings = make(tmp_path, teams_client_secret="")
    checks = await preflight.run_preflight(settings, only={"teams"})
    names = [c.name for c in checks]
    assert "teams: Graph access" in names and "live probes" not in names
    assert by_name(checks)["teams: Graph access"].status == "fail"
