"""Preflight: static configuration checks, secret masking, hints and the readiness summary (no network)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from voicecrm import preflight
from voicecrm.config import Settings
from voicecrm.preflight import Check, check_config, format_report, hint_for, readiness, redact


def make(tmp_path: Path, **kw) -> Settings:
    kw.setdefault("demo_mode", False)
    return Settings(_env_file=None, state_dir=tmp_path / "state", **kw)


LIVE = dict(
    transcription_mode="azure_speech", llm_mode="hybrid", pii_mode="azure_language", azure_auth="key",
    speech_endpoint="https://res.cognitiveservices.azure.com/", speech_key="speech-secret-key-1",
    language_endpoint="https://res.cognitiveservices.azure.com/", language_key="language-secret-key-1",
    foundry_endpoint="https://res.services.ai.azure.com/openai/v1", foundry_deployment="chat41", foundry_key="foundry-secret-key-1",
)


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


def test_all_mock_configuration_is_valid_but_says_nothing_is_live(tmp_path):
    (modes,) = check_config(make(tmp_path))
    assert modes.status == "warn" and "live: none" in modes.detail


def test_a_complete_live_configuration_passes(tmp_path):
    checks = by_name(check_config(make(tmp_path, **LIVE)))
    assert all(c.status == "pass" for c in checks.values()), [c for c in checks.values() if c.status != "pass"]
    assert "live: transcription, llm, pii" in checks["config: modes"].detail
    assert checks["config: llm endpoint"].detail == "res.services.ai.azure.com"


def test_incomplete_live_service_outside_demo_mode_is_a_clear_failure(tmp_path):
    (check,) = check_config(make(tmp_path, transcription_mode="azure_speech", azure_auth="key"))
    assert check.status == "fail" and "speech_endpoint" in check.detail.lower()


def test_demo_mode_hides_a_missing_setting_so_it_is_called_out(tmp_path):
    checks = by_name(check_config(make(tmp_path, demo_mode=True, transcription_mode="azure_speech", azure_auth="key")))
    assert checks["config: fallback (transcription)"].status == "warn"
    assert checks["config: modes"].detail.startswith("live: none")


def test_demo_mode_with_live_services_warns(tmp_path):
    checks = by_name(check_config(make(tmp_path, demo_mode=True, **LIVE)))
    assert checks["config: demo mode"].status == "warn" and "VOICECRM_DEMO_MODE=false" in checks["config: demo mode"].hint


def test_entra_with_leftover_keys_warns_that_the_keys_are_ignored(tmp_path):
    checks = by_name(check_config(make(tmp_path, **{**LIVE, "azure_auth": "entra"})))
    auth = checks["config: auth"]
    assert auth.status == "warn" and "ignored" in auth.detail and "VOICECRM_AZURE_AUTH=key" in auth.hint


def test_key_auth_without_a_key_fails(tmp_path):
    (check,) = check_config(make(tmp_path, **{**LIVE, "foundry_key": ""}))           # an incomplete live service is caught up front
    assert check.status == "fail" and "foundry_key" in check.detail.lower()


def test_regional_endpoint_fails_under_entra_but_is_fine_with_keys(tmp_path):
    regional = {**LIVE, "speech_endpoint": "https://eastus.api.cognitive.microsoft.com"}
    assert by_name(check_config(make(tmp_path, **regional)))["config: transcription endpoint"].status == "pass"
    entra = by_name(check_config(make(tmp_path, **{**regional, "azure_auth": "entra"})))
    assert entra["config: transcription endpoint"].status == "fail" and "custom-domain" in entra["config: transcription endpoint"].hint


def test_placeholders_and_odd_hosts_are_flagged(tmp_path):
    placeholder = by_name(check_config(make(tmp_path, **{**LIVE, "language_endpoint": "https://<your-resource>.cognitiveservices.azure.com"})))
    assert placeholder["config: pii endpoint"].status == "fail"
    odd = by_name(check_config(make(tmp_path, **{**LIVE, "language_endpoint": "https://example.org"})))
    assert odd["config: pii endpoint"].status == "warn"


def test_dataverse_pointing_at_msx_is_a_hard_failure(tmp_path):
    checks = by_name(check_config(make(tmp_path, crm_mode="dataverse", dataverse_url="https://microsoftsales.crm.dynamics.com")))
    assert checks["config: crm endpoint"].status == "fail" and "MSX" in checks["config: crm endpoint"].hint


def test_secrets_and_bearer_tokens_are_masked(tmp_path):
    settings = make(tmp_path, **LIVE)
    token = "eyJhbGciOiJSUzI1NiIs.eyJ0aWQiOiIxMjM0NTY3OA.c2lnbmF0dXJlLXBhcnQtaGVyZQ"
    text = f"HTTP 401 key=speech-secret-key-1 other=foundry-secret-key-1 Bearer {token}"
    masked = redact(text, settings)
    assert "secret-key" not in masked and token not in masked
    assert masked.count("<secret>") == 2 and "<token>" in masked


@pytest.mark.parametrize("message, needle", [
    ("HTTP 400 Tenant provided in token does not match resource token: Token tenant X does not match resource tenant.", "VOICECRM_AZURE_TENANT_ID"),
    ("CredentialUnavailableError: Failed to invoke the Azure CLI", "az login"),
    ("AzureSpeechError: ... HTTP 401 ...", "belongs to that resource"),
    ("HTTP 403 forbidden", "network rules"),
    ("LlmRequestError: HTTP 404 DeploymentNotFound", "FOUNDRY_DEPLOYMENT"),
    ("HTTP 429 rate limit exceeded", "VOICECRM_MAX_CONCURRENCY"),
    ("httpx.ConnectError: getaddrinfo failed", "reach the endpoint"),
    ("ReadTimeout: timed out", "timed out"),
])
def test_known_failures_get_a_concrete_hint(tmp_path, message, needle):
    assert needle in hint_for(message, make(tmp_path))


def test_an_unrecognised_failure_gets_no_made_up_hint(tmp_path):
    assert hint_for("something unexpected", make(tmp_path)) == ""


async def test_a_failing_probe_reports_instead_of_raising_and_masks_the_key(tmp_path):
    settings = make(tmp_path, **LIVE)

    async def boom() -> str:
        raise RuntimeError("HTTP 401 for key speech-secret-key-1")

    check = await preflight._timed("speech: transcription", settings, boom())
    assert check.status == "fail" and "<secret>" in check.detail and "speech-secret-key-1" not in check.detail
    assert "belongs to that resource" in check.hint and check.seconds is not None


async def test_a_slow_probe_is_a_warning_not_a_failure(tmp_path):
    async def slow() -> str:
        return "ok"

    check = await preflight._timed("x", make(tmp_path), slow(), warn_over=-1)
    assert check.status == "warn" and "slower" in check.hint


async def test_the_language_probe_needs_the_card_number_to_be_detected(tmp_path):
    class Pii:
        def __init__(self, categories):
            self.categories = categories

        async def detect_many(self, texts, *, language):
            return [[SimpleNamespace(category=c) for c in self.categories]]

    def container(categories):
        return SimpleNamespace(settings=make(tmp_path), deps=SimpleNamespace(pii=Pii(categories)))

    ok = await preflight.probe_language(container(["payment_card", "personal_contact"]))
    assert ok.status == "pass" and "payment_card" in ok.detail
    partial = await preflight.probe_language(container(["payment_card"]))
    assert partial.status == "pass" and "e-mail not flagged" in partial.detail
    missed = await preflight.probe_language(container(["personal_contact"]))
    assert missed.status == "fail" and "card number was not detected" in missed.detail


def test_readiness_and_the_report(tmp_path):
    settings = make(tmp_path, **LIVE)
    checks = [Check("speech: transcription", "pass", "ok", seconds=2.0), Check("language: PII detection", "pass", "ok", seconds=0.8),
              Check("model: structured output", "pass", "ok", seconds=3.0)]
    (a, b, c) = readiness(checks, settings)
    assert a[1] == "READY" and b[1] == "NOT CONFIGURED" and c[1] == "READY"
    broken = [*checks[:2], Check("model: structured output", "fail", "boom", "fix it", 1.0)]
    assert readiness(broken, settings)[0][1] == "NOT READY" and readiness(broken, settings)[2][1] == "NOT READY"
    assert readiness(checks[:2], settings)[0][1] == "NOT READY"                      # a probe that never ran is not ready
    report = format_report(broken, settings)
    assert "[FAIL] model: structured output" in report and "-> fix it" in report and "Scenario readiness" in report


async def test_nothing_is_probed_when_the_configuration_is_invalid(tmp_path):
    checks = await preflight.run_preflight(make(tmp_path, transcription_mode="azure_speech", azure_auth="key"))
    assert [c.name for c in checks] == ["config", "live probes"] and checks[1].status == "skip"
