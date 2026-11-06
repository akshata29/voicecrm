"""Meta, policy, template, taxonomy, catalogue, learned-alias and CRM-view endpoints of the HTTP API."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from voicecrm import __version__
from voicecrm.container import Container
from voicecrm.data_loader import load_templates
from voicecrm.domain.catalogue import LearnedAlias

from .helpers import API

EXPECTED_MODES = {"transcription": "mock", "llm": "rules", "search": "local", "pii": "local", "crm": "local",
                  "vault": "local"}
CHANNELS = {"teams", "zoom", "genesys", "mobile", "upload"}


@pytest.fixture()
def restore_policies(client: TestClient) -> Any:
    """Snapshot of GET /policies; everything is PUT back afterwards so tests cannot leak policy changes."""
    original = client.get(f"{API}/policies").json()
    yield original
    for market in original["markets"]:
        client.put(f"{API}/policies/markets/{market['market']}", json=market)
    client.put(f"{API}/policies/processing", json=original["processing"])
    client.put(f"{API}/policies/notes", json=original["notes"])


def _market(policies: dict[str, Any], key: str) -> dict[str, Any]:
    return next(m for m in policies["markets"] if m["market"] == key)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


# ---- health + config ---------------------------------------------------------------------------------------------
def test_health(client: TestClient) -> None:
    response = client.get(f"{API}/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_config_reports_mock_local_modes_and_demo_mode(client: TestClient) -> None:
    config = client.get(f"{API}/config").json()
    assert config["app_version"] == __version__
    assert config["demo_mode"] is True
    assert set(config["modes"]) == set(EXPECTED_MODES)
    for service, effective in EXPECTED_MODES.items():
        mode = config["modes"][service]
        assert (mode["effective"], mode["live"]) == (effective, False), service
    assert config["engine"] == "rules"
    assert set(config["channels"]) >= CHANNELS


def test_config_lists_markets_templates_and_catalogue(client: TestClient) -> None:
    config = client.get(f"{API}/config").json()
    assert {m["market"] for m in config["markets"]} == {"US", "UK", "EU"}
    templates = {t["template_id"]: t for t in config["templates"]}
    assert set(templates) == set(load_templates())
    assert all(t["sections"] and t["business_lines"] for t in templates.values())
    assert set(config["business_lines"]) == {line for t in templates.values() for line in t["business_lines"]}
    assert config["catalogue_products"] >= 1000
    assert config["catalogue_synthetic"] is True
    assert config["pricing_illustrative"] is True


# ---- policies ----------------------------------------------------------------------------------------------------
def test_policies_expose_markets_notes_and_processing(client: TestClient) -> None:
    policies = client.get(f"{API}/policies").json()
    assert set(policies) == {"markets", "notes", "processing"}
    assert {m["market"] for m in policies["markets"]} == {"US", "UK", "EU"}
    assert all(m["notes"] and m["display_name"] for m in policies["markets"])

    notes = policies["notes"]
    assert {"payment_card", "bank_account", "government_id", "credentials", "medical"} <= set(notes["omit_categories"])
    assert notes["keep_relationship_context"] is True

    processing = policies["processing"]
    assert processing["engine"] == "rules"
    assert processing["auto_approve_enabled"] is False
    assert 0 < processing["product_review_threshold"] <= 1
    assert processing["top_k_products"] >= 3
    assert set(processing["transcription_source_preference"]) >= CHANNELS
    template_ids = {t["template_id"] for t in client.get(f"{API}/templates").json()}
    assert set(processing["default_template_by_business_line"].values()) <= template_ids


def test_market_policy_round_trips_into_retention_preview(client: TestClient, restore_policies: dict[str, Any]) -> None:
    before = client.get(f"{API}/policies/retention-preview", params={"market": "US"}).json()
    assert before["market"] == "US"
    assert before["decision"]["transcript_stored"] is False
    assert before["decision"]["transcript_expires_at"] is None

    us = _market(restore_policies, "US")
    assert us["transcript_retention"] == "off"
    response = client.put(f"{API}/policies/markets/US", json={**us, "transcript_retention": "on",
                                                              "transcript_retention_days": 30})
    assert response.status_code == 200
    assert _market(response.json(), "US")["transcript_retention"] == "on"
    assert _market(client.get(f"{API}/policies").json(), "US")["transcript_retention_days"] == 30
    assert _market(client.get(f"{API}/policies").json(), "UK") == _market(restore_policies, "UK")

    after = client.get(f"{API}/policies/retention-preview", params={"market": "US"}).json()["decision"]
    assert after["transcript_stored"] is True
    assert after["transcript_redacted"] is True
    assert after["crm_contains_transcript"] is False
    expected = datetime.now(UTC) + timedelta(days=30)
    assert abs(_parse(after["transcript_expires_at"]) - expected) < timedelta(days=1)

    restored = client.put(f"{API}/policies/markets/US", json=us)
    assert restored.status_code == 200
    final = client.get(f"{API}/policies/retention-preview", params={"market": "US"}).json()["decision"]
    assert final["transcript_stored"] is False


def test_retention_preview_matches_policy_for_every_market(client: TestClient) -> None:
    for market in client.get(f"{API}/policies").json()["markets"]:
        decision = client.get(f"{API}/policies/retention-preview", params={"market": market["market"]}).json()["decision"]
        assert decision["transcript_stored"] is (market["transcript_retention"] == "on")
        assert decision["crm_contains_transcript"] is False
        assert decision["explanation"]
        if market["transcript_retention"] == "on":
            expected = datetime.now(UTC) + timedelta(days=market["transcript_retention_days"])
            assert abs(_parse(decision["transcript_expires_at"]) - expected) < timedelta(days=1)


def test_market_policy_validation_errors(client: TestClient, restore_policies: dict[str, Any]) -> None:
    us = _market(restore_policies, "US")
    no_days = client.put(f"{API}/policies/markets/US", json={**us, "transcript_retention": "on",
                                                             "transcript_retention_days": None})
    assert no_days.status_code == 422
    assert "transcript_retention_days" in no_days.json()["detail"]

    mismatch = client.put(f"{API}/policies/markets/UK", json=us)
    assert mismatch.status_code == 422

    bad_literal = client.put(f"{API}/policies/markets/US", json={**us, "transcript_retention": "maybe"})
    assert bad_literal.status_code == 422

    unknown = client.get(f"{API}/policies/retention-preview", params={"market": "ZZ"})
    assert unknown.status_code == 404
    assert client.get(f"{API}/policies/retention-preview").status_code == 422

    assert client.get(f"{API}/policies").json()["markets"] == restore_policies["markets"]


def test_processing_policy_accepts_rules_and_round_trips_fields(client: TestClient,
                                                                restore_policies: dict[str, Any]) -> None:
    processing = restore_policies["processing"]
    changed = {**processing, "engine": "rules", "top_k_products": 5, "product_review_threshold": 0.8}
    response = client.put(f"{API}/policies/processing", json=changed)
    assert response.status_code == 200
    assert response.json()["processing"] == changed
    assert client.get(f"{API}/policies").json()["processing"] == changed
    assert client.get(f"{API}/config").json()["engine"] == "rules"


@pytest.mark.parametrize("engine", ["llm", "hybrid"])
def test_processing_policy_rejects_engines_without_a_model(client: TestClient, restore_policies: dict[str, Any],
                                                           engine: str) -> None:
    response = client.put(f"{API}/policies/processing", json={**restore_policies["processing"], "engine": engine})
    assert response.status_code == 422
    assert "not available" in response.json()["detail"]
    assert client.get(f"{API}/policies").json()["processing"]["engine"] == "rules"


def test_processing_policy_rejects_unknown_engine_name(client: TestClient, restore_policies: dict[str, Any]) -> None:
    response = client.put(f"{API}/policies/processing", json={**restore_policies["processing"], "engine": "bogus"})
    assert response.status_code == 422
    assert client.get(f"{API}/policies").json()["processing"] == restore_policies["processing"]


def test_notes_policy_round_trips(client: TestClient, restore_policies: dict[str, Any]) -> None:
    changed = {**restore_policies["notes"], "keep_relationship_context": False, "drop_small_talk": False}
    response = client.put(f"{API}/policies/notes", json=changed)
    assert response.status_code == 200
    assert response.json()["notes"] == changed
    assert client.get(f"{API}/policies").json()["notes"] == changed


# ---- templates, taxonomy, catalogue -------------------------------------------------------------------------------
def test_templates_endpoint_returns_the_data_contract(client: TestClient) -> None:
    templates = client.get(f"{API}/templates").json()
    assert {t["template_id"] for t in templates} == set(load_templates())
    for template in templates:
        keys = [s["key"] for s in template["sections"]]
        assert len(keys) == len(set(keys))
        assert all(s["title"] and s["instruction"] for s in template["sections"])
        assert template["business_lines"]
        assert template["max_words"] > 0


def test_taxonomy_endpoint_lists_topics_with_english_and_spanish_synonyms(client: TestClient) -> None:
    taxonomy = client.get(f"{API}/taxonomy").json()
    assert taxonomy["version"]
    assert taxonomy["synthetic"] is True
    topics = taxonomy["topics"]
    assert len(topics) >= 15
    ids = [t["topic_id"] for t in topics]
    assert len(ids) == len(set(ids))
    for topic in topics:
        assert topic["label"]
        assert topic["synonyms"], topic["topic_id"]
        assert topic["synonyms_i18n"].get("es"), topic["topic_id"]


def test_catalogue_stats_summarise_the_synthetic_catalogue(client: TestClient) -> None:
    stats = client.get(f"{API}/catalogue/stats").json()
    assert stats["products"] >= 1000
    assert stats["families"] >= 300
    assert sum(stats["share_classes"].values()) == stats["products"]
    assert sum(stats["asset_classes"].values()) == stats["products"]
    assert len(stats["share_classes"]) >= 3
    assert stats["synthetic"] is True
    assert "synthetic" in stats["note"].lower()
    assert stats["learned_aliases"] == len(client.get(f"{API}/aliases").json())


def test_catalogue_search_returns_scored_product_hits(client: TestClient) -> None:
    response = client.get(f"{API}/catalogue/search", params={"q": "equity income"})
    assert response.status_code == 200
    hits = response.json()
    assert 0 < len(hits) <= 10
    assert all(h["product_id"].startswith("SYN-") for h in hits)
    assert all(h["display_name"] and h["fund_name"] and h["share_class"] and h["asset_class"] for h in hits)
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(0 < s <= 1.0 for s in scores)
    assert "equity income" in hits[0]["fund_name"].lower()
    assert len({h["product_id"] for h in hits}) == len(hits)


def test_catalogue_search_finds_a_product_by_its_exact_display_name(client: TestClient) -> None:
    target = client.get(f"{API}/catalogue/search", params={"q": "equity income"}).json()[0]
    hits = client.get(f"{API}/catalogue/search", params={"q": target["display_name"], "top": 3}).json()
    assert target["product_id"] in [h["product_id"] for h in hits]


def test_catalogue_search_respects_top_and_validates_input(client: TestClient) -> None:
    assert len(client.get(f"{API}/catalogue/search", params={"q": "equity income", "top": 2}).json()) == 2
    assert client.get(f"{API}/catalogue/search", params={"q": "equity income", "top": 51}).status_code == 422
    assert client.get(f"{API}/catalogue/search").status_code == 422
    assert client.get(f"{API}/catalogue/search", params={"q": ""}).status_code == 422


# ---- learned aliases ----------------------------------------------------------------------------------------------
def test_learned_aliases_can_be_listed_and_cleared(client: TestClient, api_container: Container) -> None:
    assert client.delete(f"{API}/aliases").json() == {"deleted": 0}
    assert client.get(f"{API}/aliases").json() == []

    product_id = client.get(f"{API}/catalogue/search", params={"q": "equity income"}).json()[0]["product_id"]
    api_container.deps.store.upsert_alias(LearnedAlias(surface="blue chip fund c", product_id=product_id))
    try:
        aliases = client.get(f"{API}/aliases").json()
        assert [(a["surface"], a["product_id"]) for a in aliases] == [("blue chip fund c", product_id)]
        assert aliases[0]["count"] >= 1
        assert client.get(f"{API}/catalogue/stats").json()["learned_aliases"] == 1
    finally:
        cleared = client.delete(f"{API}/aliases")
    assert cleared.json() == {"deleted": 1}
    assert client.get(f"{API}/aliases").json() == []
    assert client.get(f"{API}/catalogue/stats").json()["learned_aliases"] == 0


# ---- CRM simulator view -------------------------------------------------------------------------------------------
def test_crm_health_and_seed_records_are_visible(client: TestClient) -> None:
    health = client.get(f"{API}/crm/health").json()
    assert health["ok"] is True
    assert health["adapter"] == "local_sim"
    assert health["synthetic"] is True
    assert health["counts"]["accounts"] >= 10
    assert health["counts"]["phonecalls"] == 0

    for entity in ("account", "contact", "opportunity"):
        rows = client.get(f"{API}/crm/records", params={"entity": entity}).json()
        assert rows, entity
    assert client.get(f"{API}/crm/records", params={"entity": "phonecall"}).json() == []


def test_crm_records_reject_unknown_entities_and_oversized_limits(client: TestClient) -> None:
    assert client.get(f"{API}/crm/records", params={"entity": "bogus"}).status_code == 422
    assert client.get(f"{API}/crm/records", params={"entity": "account", "limit": 501}).status_code == 422
    assert len(client.get(f"{API}/crm/records", params={"entity": "account", "limit": 2}).json()) == 2
