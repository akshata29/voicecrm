"""Contract tests: every shipped data file validates against the domain models."""
from __future__ import annotations

from voicecrm import data_loader
from voicecrm.config import Settings
from voicecrm.domain.enums import Channel


def test_templates_load(data_dir):
    templates = data_loader.load_templates(data_dir)
    assert {"pb_institutional", "retail_advisor"} <= set(templates)
    assert all(t.sections for t in templates.values())


def test_policies_load_and_yaml_bools_are_coerced(data_dir):
    bundle = data_loader.load_policies(data_dir)
    assert bundle.markets["US"].transcript_retention == "off"
    assert bundle.markets["EU"].transcript_retention == "on"
    assert bundle.markets["EU"].transcript_retention_days == 1825
    pref = bundle.processing.transcription_source_preference
    assert set(pref) == {c.value for c in Channel}
    assert set(bundle.processing.default_template_by_business_line.values()) <= set(data_loader.load_templates(data_dir))


def test_pricing_is_flagged_illustrative(data_dir):
    pricing = data_loader.load_pricing(data_dir)
    assert pricing.illustrative is True
    assert "default" in pricing.llm


def test_demo_mode_falls_back_to_mock(tmp_path):
    s = Settings(_env_file=None, state_dir=tmp_path, demo_mode=True, transcription_mode="azure_speech", crm_mode="dataverse")
    modes = s.resolved_modes()
    assert modes["transcription"].effective == "mock" and not modes["transcription"].live
    assert "VOICECRM_SPEECH_ENDPOINT" in (modes["transcription"].reason or "")
    assert modes["crm"].effective == "local"


def test_live_mode_resolves_when_configured(tmp_path):
    s = Settings(_env_file=None, state_dir=tmp_path, demo_mode=True, transcription_mode="azure_speech",
                 speech_endpoint="https://example.cognitiveservices.azure.com")
    assert s.resolved_modes()["transcription"].live is True


def test_strict_mode_raises_when_live_service_incomplete(tmp_path):
    import pytest

    s = Settings(_env_file=None, state_dir=tmp_path, demo_mode=False, llm_mode="foundry")
    with pytest.raises(ValueError):
        s.resolved_modes()
