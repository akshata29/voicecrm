"""Preflight for the phone line: configuration checks, the live self-check and the scenario D verdict (no network)."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
import respx

from voicecrm import preflight
from voicecrm.adapters.telephony.fake import FakeTelephony
from voicecrm.config import Settings
from voicecrm.pipeline.phone_live import PhoneLine
from voicecrm.ports.telephony import TelephonyError
from voicecrm.preflight import Check, check_config, probe_phone, readiness

from ..phone_support import CONSOLE, PUBLIC, SECRET, phone_settings

HEALTH = f"{PUBLIC}/api/v1/webhooks/phone/health"
UP = dict(phone_mode="acs", phone_seller="", phone_seller_name="Aisha West")


def make(tmp_path, **kw) -> Settings:
    return phone_settings(tmp_path, demo_mode=False, **{"phone_ingress_port": 8011, **kw})


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


def container(tmp_path, *, tel: FakeTelephony | None = None, **kw) -> SimpleNamespace:
    settings = make(tmp_path, **kw)
    return SimpleNamespace(settings=settings, phone=PhoneLine(None, tel or FakeTelephony(), settings=settings))


# ---- configuration ---------------------------------------------------------------------------------------------------
def test_off_means_no_phone_checks(tmp_path):
    assert not [c for c in check_config(make(tmp_path, phone_mode="off")) if "phone" in c.name]


def test_a_complete_configuration_passes(tmp_path):
    checks = by_name(check_config(make(tmp_path)))
    assert checks["config: phone line"].status == "pass" and "VOICECRM_PHONE_MODE=acs" in checks["config: phone line"].detail
    assert [n for n in checks if n.startswith("config: phone") and checks[n].status != "pass"] == []


@pytest.mark.parametrize("setting,named", [
    ("phone_acs_endpoint", "VOICECRM_PHONE_ACS_ENDPOINT"), ("phone_public_base_url", "VOICECRM_PHONE_PUBLIC_BASE_URL"),
    ("phone_webhook_secret", "VOICECRM_PHONE_WEBHOOK_SECRET")])
def test_a_missing_setting_fails_and_is_named(tmp_path, setting, named):
    check = by_name(check_config(make(tmp_path, **{setting: ""})))["config: phone line"]
    assert check.status == "fail" and named in check.detail and "phone-line.md" in check.hint


def test_a_connection_string_replaces_the_endpoint(tmp_path):
    checks = by_name(check_config(make(tmp_path, phone_acs_endpoint="", phone_acs_connection_string="endpoint=https://x.communication.azure.com/;accesskey=a2V5")))
    assert checks["config: phone line"].status == "pass"


@pytest.mark.parametrize("endpoint", ["https://acs.example.com", "https://<your-resource>.communication.azure.com", "not a url"])
def test_the_endpoint_must_be_a_communication_services_host(tmp_path, endpoint):
    check = by_name(check_config(make(tmp_path, phone_acs_endpoint=endpoint)))["config: phone endpoint"]
    assert check.status == "fail" and "communication.azure.com" in check.hint


def test_the_public_url_must_be_https_and_the_secret_long_enough(tmp_path):
    checks = by_name(check_config(make(tmp_path, phone_public_base_url="http://localhost:8011", phone_webhook_secret="short")))
    assert checks["config: phone public URL"].status == "fail" and "https" in checks["config: phone public URL"].hint
    assert checks["config: phone webhook secret"].status == "warn"
    assert "short" not in " ".join(c.detail + c.hint for c in checks.values())               # the secret is never echoed


def test_a_required_notice_needs_a_voice_connection(tmp_path):
    bad = by_name(check_config(make(tmp_path, phone_cognitive_endpoint="")))["config: phone notice"]
    assert bad.status == "fail" and "COGNITIVE_ENDPOINT" in bad.hint and "REQUIRE_NOTICE" in bad.hint
    assert "config: phone notice" not in by_name(check_config(make(tmp_path, phone_cognitive_endpoint="", phone_require_notice=False)))


def test_a_disabled_ingress_listener_is_called_out(tmp_path):
    check = by_name(check_config(make(tmp_path, phone_ingress_port=0)))["config: phone ingress"]
    assert check.status == "warn" and "never be put on the internet" in check.hint


def test_the_phone_secrets_are_masked_in_every_message():
    settings = Settings(_env_file=None, phone_webhook_secret="webhook-secret-value-123", phone_acs_connection_string="endpoint=x;accesskey=abcdefgh12345")
    assert "webhook-secret-value-123" in preflight.secrets_of(settings)
    assert "<secret>" in preflight.redact("failed at /events?key=webhook-secret-value-123", settings)


async def test_broken_phone_settings_do_not_stop_the_azure_probes(tmp_path):
    checks = await preflight.run_preflight(make(tmp_path, phone_webhook_secret=""), only={"phone"})
    names = by_name(checks)
    assert names["config: phone line"].status == "fail"
    assert names["phone: line"].status == "fail" and "incomplete" in names["phone: line"].detail
    assert "live probes" not in names                                                        # the early exit is for Azure settings


# ---- the live self-check ------------------------------------------------------------------------------------------------
async def test_a_working_line_passes_every_step(tmp_path):
    c = container(tmp_path)
    c.phone.register_console(CONSOLE, "Aisha West", None)
    with respx.mock as router:
        router.get(HEALTH).mock(return_value=httpx.Response(200, json={"service": "voicecrm-phone"}))
        checks = by_name(await probe_phone(c))
    assert {n: ch.status for n, ch in checks.items()} == {
        "phone: ACS access": "pass", "phone: public URL": "pass", "phone: seller": "pass",
        "phone: recording notice": "pass", "phone: line": "pass"}
    assert "Aisha West" in checks["phone: line"].detail and checks["phone: line"].seconds is not None


async def test_no_seller_online_is_a_warning_not_a_failure(tmp_path):
    with respx.mock as router:
        router.get(HEALTH).mock(return_value=httpx.Response(200, json={"service": "voicecrm-phone"}))
        checks = by_name(await probe_phone(container(tmp_path)))
    assert checks["phone: seller"].status == "warn" and checks["phone: line"].status == "warn"
    assert "see the steps above" in checks["phone: line"].detail


async def test_a_refused_credential_and_a_dead_tunnel_fail_with_hints(tmp_path):
    tel = FakeTelephony()
    tel.fail["issue_token"] = TelephonyError("the identity API refused the credential (HTTP 403)")
    with respx.mock as router:
        router.get(HEALTH).mock(side_effect=httpx.ConnectError("refused"))
        checks = by_name(await probe_phone(container(tmp_path, tel=tel, phone_line_identity="")))
    assert checks["phone: ACS access"].status == "fail" and "refused the credential" in checks["phone: ACS access"].detail
    assert checks["phone: public URL"].status == "fail" and "tunnel" in checks["phone: public URL"].hint
    assert checks["phone: line"].status == "fail"


async def test_a_secret_in_an_error_is_masked(tmp_path):
    tel = FakeTelephony()
    tel.fail["issue_token"] = TelephonyError(f"bad request to /events?key={SECRET}")
    with respx.mock as router:
        router.get(HEALTH).mock(return_value=httpx.Response(200, json={"service": "voicecrm-phone"}))
        checks = await probe_phone(container(tmp_path, tel=tel, phone_line_identity=""))
    assert SECRET not in " ".join(c.detail + c.hint for c in checks)


async def test_a_container_without_a_line_says_so(tmp_path):
    (check,) = await probe_phone(SimpleNamespace(settings=make(tmp_path), phone=None))
    assert (check.name, check.status) == ("phone: line", "fail") and "VOICECRM_PHONE_MODE=acs" in check.hint


# ---- scenario D ------------------------------------------------------------------------------------------------------------
LIVE_A = [Check("speech: transcription", "pass", "ok", seconds=2.0), Check("language: PII detection", "pass", "ok", seconds=0.8),
          Check("model: structured output", "pass", "ok", seconds=3.0)]


def scenario_d(checks, settings):
    return readiness(checks, settings)[3]


def test_scenario_d_is_not_configured_while_the_line_is_off(tmp_path):
    scenario, verdict, why = scenario_d(LIVE_A, make(tmp_path, phone_mode="off"))
    assert scenario.startswith("D") and verdict == "NOT CONFIGURED" and "docs/phone-line.md" in why


def test_scenario_d_is_ready_when_the_line_and_speech_work(tmp_path):
    checks = [*LIVE_A, Check("phone: line", "pass", "Communication Services access, public URL, seller and recording notice", seconds=1.0)]
    _, verdict, why = scenario_d(checks, make(tmp_path))
    assert verdict == "READY" and "recording notice" in why


def test_scenario_d_needs_real_speech_and_a_healthy_line(tmp_path):
    line = Check("phone: line", "pass", "fine", seconds=1.0)
    assert scenario_d([line], make(tmp_path))[1] == "READY"                                   # only the phone was probed
    assert scenario_d([Check("speech: transcription", "fail", "x", seconds=1.0), line], make(tmp_path))[1] == "NOT READY"
    assert scenario_d([Check("speech: transcription", "skip", "mock"), line], make(tmp_path))[1] == "NOT READY"
    assert scenario_d([*LIVE_A, Check("phone: line", "fail", "x", seconds=1.0)], make(tmp_path))[1] == "NOT READY"
    assert scenario_d(LIVE_A, make(tmp_path))[1:] == ("NOT READY", "phone not probed")
