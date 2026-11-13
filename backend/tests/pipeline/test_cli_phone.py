"""`voicecrm phone ...`: the self-check, the line identity and the Event Grid subscriptions, against mocked HTTP."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from voicecrm import cli

from ..phone_support import LINE, PUBLIC, SECRET, phone_settings

RESOURCE = ("/subscriptions/11111111-2222-3333-4444-555555555555/resourceGroups/rg-demo/providers/"
            "Microsoft.Communication/communicationServices/acs-demo")
BASE = f"https://management.azure.com{RESOURCE}/providers/Microsoft.EventGrid/eventSubscriptions"
HEALTH = f"{PUBLIC}/api/v1/webhooks/phone/health"
UP = httpx.Response(200, json={"service": "voicecrm-phone"})


class Credential:
    async def get_token(self, *scopes, **kwargs):
        return SimpleNamespace(token="arm-token")

    async def close(self) -> None:
        return None


def configure(monkeypatch, tmp_path, **kw):
    settings = phone_settings(tmp_path, **{"phone_ingress_port": 8011, "phone_acs_resource_id": RESOURCE, **kw})
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr("voicecrm.adapters.credentials.build_credential", lambda tenant=None: Credential())
    return settings


@pytest.fixture()
def http():
    with respx.mock(assert_all_called=False) as router:
        yield router


def subscription(name: str, state: str = "Succeeded", event_type: str = "Microsoft.Communication.IncomingCall") -> dict:
    return {"name": name, "properties": {"provisioningState": state, "filter": {"includedEventTypes": [event_type]}, "destination": {
        "properties": {"endpointUrl": f"{PUBLIC}/api/v1/webhooks/phone/events?key={SECRET}"}}}}


def test_every_command_needs_the_line_to_be_switched_on(monkeypatch, tmp_path, capsys):
    configure(monkeypatch, tmp_path, phone_mode="off")
    for command in ("check", "line", "subscribe", "subscriptions", "unsubscribe"):
        assert cli.main(["phone", command]) == 2
    assert "VOICECRM_PHONE_MODE" in capsys.readouterr().out


def test_check_with_incomplete_settings_fails_without_calling_anything(monkeypatch, tmp_path, http, capsys):
    configure(monkeypatch, tmp_path, phone_webhook_secret="")
    assert cli.main(["phone", "check"]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] config: phone line" in out and "VOICECRM_PHONE_WEBHOOK_SECRET" in out and "[FAIL] phone: line" in out
    assert http.calls.call_count == 0


def test_check_probes_the_line_and_reports_each_step(monkeypatch, tmp_path, http, capsys):
    configure(monkeypatch, tmp_path)
    http.get(HEALTH).mock(return_value=UP)
    assert cli.main(["phone", "check"]) == 0
    out = capsys.readouterr().out
    for name in ("phone: ACS access", "phone: public URL", "phone: seller", "phone: recording notice", "phone: line"):
        assert name in out
    assert "[WARN] phone: seller" in out and SECRET not in out                                # no seller online is only a warning


def test_check_fails_when_the_tunnel_does_not_reach_the_ingress(monkeypatch, tmp_path, http, capsys):
    configure(monkeypatch, tmp_path)
    http.get(HEALTH).mock(side_effect=httpx.ConnectError("refused"))
    assert cli.main(["phone", "check"]) == 1
    assert "[FAIL] phone: public URL" in capsys.readouterr().out


def test_line_shows_the_identity_the_dialer_calls(monkeypatch, tmp_path, capsys):
    configure(monkeypatch, tmp_path)
    assert cli.main(["phone", "line"]) == 0
    out = capsys.readouterr().out
    assert LINE in out and "+1 800 555 0100" in out and "phone_state.json" in out


def test_subscribe_creates_both_subscriptions_with_the_secret_in_the_url(monkeypatch, tmp_path, http, capsys):
    configure(monkeypatch, tmp_path)
    http.get(HEALTH).mock(return_value=UP)
    puts = http.put(url__regex=rf"{BASE}/voicecrm-phone-.*").mock(return_value=httpx.Response(201, json=subscription("x", "Creating")))
    http.get(f"{BASE}/voicecrm-phone-calls").mock(return_value=httpx.Response(200, json=subscription("voicecrm-phone-calls")))
    http.get(f"{BASE}/voicecrm-phone-recordings").mock(return_value=httpx.Response(200, json=subscription(
        "voicecrm-phone-recordings", event_type="Microsoft.Communication.RecordingFileStatusUpdated")))
    assert cli.main(["phone", "subscribe"]) == 0
    assert puts.call_count == 2
    sent = [json.loads(call.request.content)["properties"]["destination"]["properties"]["endpointUrl"] for call in puts.calls]
    assert sent == [f"{PUBLIC}/api/v1/webhooks/phone/events?key={SECRET}"] * 2
    out = capsys.readouterr().out
    assert "subscribed voicecrm-phone-calls" in out and "IncomingCall" in out and "RecordingFileStatusUpdated" in out
    assert SECRET not in out and "key=***" in out                                            # the secret is never printed


def test_subscribe_first_checks_that_the_tunnel_reaches_the_ingress(monkeypatch, tmp_path, http, capsys):
    configure(monkeypatch, tmp_path)
    http.get(HEALTH).mock(return_value=httpx.Response(404))
    assert cli.main(["phone", "subscribe"]) == 1
    out = capsys.readouterr().out
    assert "does not reach the phone ingress" in out and "run_phone_tunnel" in out
    assert not [c for c in http.calls if "management.azure.com" in str(c.request.url)]       # nothing was created


def test_subscribe_needs_the_resource_id_and_an_https_url(monkeypatch, tmp_path, http, capsys):
    configure(monkeypatch, tmp_path, phone_acs_resource_id="")
    assert cli.main(["phone", "subscribe"]) == 2 and "VOICECRM_PHONE_ACS_RESOURCE_ID" in capsys.readouterr().out
    configure(monkeypatch, tmp_path, phone_acs_resource_id="not-an-arm-id")
    assert cli.main(["phone", "subscribe"]) == 2 and "VOICECRM_PHONE_ACS_RESOURCE_ID" in capsys.readouterr().out
    configure(monkeypatch, tmp_path, phone_public_base_url="http://localhost:8011")
    assert cli.main(["phone", "subscribe"]) == 2 and "https" in capsys.readouterr().out


def test_a_refused_subscription_prints_the_fix_and_never_the_secret(monkeypatch, tmp_path, http, capsys):
    configure(monkeypatch, tmp_path)
    http.get(HEALTH).mock(return_value=UP)
    http.put(f"{BASE}/voicecrm-phone-calls").mock(return_value=httpx.Response(403, json={"error": {
        "code": "AuthorizationFailed", "message": f"no access (url had key={SECRET})"}}))
    assert cli.main(["phone", "subscribe"]) == 1
    out = capsys.readouterr().out
    assert "AuthorizationFailed" in out and "Contributor" in out and SECRET not in out


def test_subscriptions_and_unsubscribe(monkeypatch, tmp_path, http, capsys):
    configure(monkeypatch, tmp_path)
    http.get(BASE).mock(return_value=httpx.Response(200, json={"value": [subscription("voicecrm-phone-calls"), subscription("other")]}))
    deleted = http.delete(url__regex=rf"{BASE}/voicecrm-phone-.*").mock(return_value=httpx.Response(200))
    assert cli.main(["phone", "subscriptions"]) == 0
    out = capsys.readouterr().out
    assert "voicecrm-phone-calls" in out and "other" not in out and SECRET not in out
    assert cli.main(["phone", "unsubscribe"]) == 0 and deleted.call_count == 2
    assert "deleted voicecrm-phone-calls, voicecrm-phone-recordings" in capsys.readouterr().out


def test_an_empty_listing_says_how_to_create_the_subscriptions(monkeypatch, tmp_path, http, capsys):
    configure(monkeypatch, tmp_path)
    http.get(BASE).mock(return_value=httpx.Response(200, json={"value": []}))
    assert cli.main(["phone", "subscriptions"]) == 0 and "phone subscribe" in capsys.readouterr().out
