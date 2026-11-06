"""Referential-integrity and contract checks for the synthetic data files in data/."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from voicecrm.data_loader import (
    load_catalogue,
    load_crm_seed,
    load_policies,
    load_pricing,
    load_scripted_calls,
    load_taxonomy,
    load_templates,
)
from voicecrm.domain.catalogue import Catalogue
from voicecrm.domain.crm import CrmSeed
from voicecrm.domain.fixtures import ScriptedCall
from voicecrm.domain.policy import PolicyBundle
from voicecrm.domain.taxonomy import Taxonomy
from voicecrm.domain.templates import SummaryTemplate
from voicecrm.eval.data_checks import BUSINESS_LINES, check_all

CHANNELS = {"teams", "zoom", "genesys", "mobile", "upload"}


@pytest.fixture(scope="module")
def catalogue(data_dir: Path) -> Catalogue:
    return load_catalogue(data_dir)


@pytest.fixture(scope="module")
def taxonomy(data_dir: Path) -> Taxonomy:
    return load_taxonomy(data_dir)


@pytest.fixture(scope="module")
def templates(data_dir: Path) -> dict[str, SummaryTemplate]:
    return load_templates(data_dir)


@pytest.fixture(scope="module")
def policies(data_dir: Path) -> PolicyBundle:
    return load_policies(data_dir)


@pytest.fixture(scope="module")
def seed(data_dir: Path) -> CrmSeed:
    return load_crm_seed(data_dir)


@pytest.fixture(scope="module")
def calls(data_dir: Path) -> list[ScriptedCall]:
    return load_scripted_calls(data_dir)


# ---- the umbrella check -------------------------------------------------------------------------------------------------
def test_all_data_files_pass_the_referential_integrity_checks(data_dir: Path) -> None:
    assert check_all(data_dir) == []


# ---- catalogue ----------------------------------------------------------------------------------------------------------------
def test_catalogue_is_large_unique_and_clearly_synthetic(catalogue: Catalogue) -> None:
    assert len(catalogue.products) >= 1000
    assert len({p.family_id for p in catalogue.products}) >= 300
    assert len({p.product_id for p in catalogue.products}) == len(catalogue.products)
    assert catalogue.synthetic is True and "synthetic" in catalogue.note.lower()
    assert all(p.synthetic and p.product_id.startswith("SYN-") for p in catalogue.products)


def test_every_declared_share_class_is_used_and_every_used_class_is_declared(catalogue: Catalogue) -> None:
    used = {p.share_class for p in catalogue.products}
    assert set(catalogue.share_classes) == used
    assert len(used) >= 3


def test_share_class_spoken_variants_are_lower_case(catalogue: Catalogue) -> None:
    for share_class, variants in catalogue.share_classes.items():
        assert variants, share_class
        assert all(v == v.lower() and v.strip() for v in variants), share_class


def test_catalogue_products_carry_names_and_aliases_the_resolver_needs(catalogue: Catalogue) -> None:
    for product in catalogue.products:
        assert product.display_name and product.fund_name and product.asset_class and product.region, product.product_id
        assert product.fund_name.lower() in product.display_name.lower(), product.product_id


# ---- taxonomy -----------------------------------------------------------------------------------------------------------------
def test_taxonomy_topics_are_unique_and_have_english_and_spanish_synonyms(taxonomy: Taxonomy) -> None:
    ids = [t.topic_id for t in taxonomy.topics]
    assert len(taxonomy.topics) >= 15 and len(ids) == len(set(ids))
    assert taxonomy.synthetic is True
    for topic in taxonomy.topics:
        assert topic.label, topic.topic_id
        assert topic.synonyms and all(s.strip() for s in topic.synonyms), topic.topic_id
        assert topic.synonyms_i18n.get("es") and all(s.strip() for s in topic.synonyms_i18n["es"]), topic.topic_id
        assert topic.labels_i18n.get("es"), topic.topic_id


def test_taxonomy_parents_and_business_lines_are_valid(taxonomy: Taxonomy) -> None:
    ids = {t.topic_id for t in taxonomy.topics}
    for topic in taxonomy.topics:
        assert topic.parent_id is None or topic.parent_id in ids, topic.topic_id
        assert set(topic.business_lines) <= BUSINESS_LINES, topic.topic_id


# ---- templates ------------------------------------------------------------------------------------------------------------------
def test_template_ids_match_their_file_names(data_dir: Path, templates: dict[str, SummaryTemplate]) -> None:
    stems = {path.stem for path in (data_dir / "templates").glob("*.yaml")}
    assert set(templates) == stems
    assert len(templates) >= 2


def test_every_template_section_crm_field_is_unique_within_its_template(templates: dict[str, SummaryTemplate]) -> None:
    for template in templates.values():
        fields = [s.crm_field for s in template.sections if s.crm_field]
        assert fields, template.template_id
        assert len(fields) == len(set(fields)), f"{template.template_id}: duplicate crm_field in {fields}"
        keys = [s.key for s in template.sections]
        assert len(keys) == len(set(keys)), f"{template.template_id}: duplicate section key"
        assert all(re.fullmatch(r"vcrm_[a-z]+", f) for f in fields), fields
        assert all(s.title and s.instruction for s in template.sections), template.template_id


def test_templates_cover_every_business_line(templates: dict[str, SummaryTemplate]) -> None:
    covered = {line for template in templates.values() for line in template.business_lines}
    assert covered == BUSINESS_LINES


# ---- policies + pricing ------------------------------------------------------------------------------------------------------
def test_market_retention_policies(policies: PolicyBundle) -> None:
    assert set(policies.markets) == {"US", "UK", "EU"}
    us, uk, eu = (policies.markets[m] for m in ("US", "UK", "EU"))
    assert (us.transcript_retention, us.transcript_retention_days) == ("off", None)
    assert (uk.transcript_retention, uk.transcript_retention_days) == ("on", 365)
    assert (eu.transcript_retention, eu.transcript_retention_days) == ("on", 1825)
    for market in policies.markets.values():
        assert market.market in policies.markets and market.display_name
        assert market.store_redacted_only is True
        assert market.audio_retention_days == 0
        assert market.notes and "example" in market.notes.lower()


def test_retention_on_always_declares_its_days(policies: PolicyBundle) -> None:
    for market in policies.markets.values():
        if market.transcript_retention == "on":
            assert market.transcript_retention_days and market.transcript_retention_days > 0, market.market


def test_policy_file_quotes_on_off_so_yaml_does_not_turn_them_into_booleans(data_dir: Path) -> None:
    raw = yaml.safe_load((data_dir / "policies" / "policies.yaml").read_text(encoding="utf-8"))
    assert {k: v["transcript_retention"] for k, v in raw["markets"].items()} == {"US": "off", "UK": "on", "EU": "on"}


def test_processing_policy_defaults_are_conservative_and_complete(policies: PolicyBundle,
                                                                 templates: dict[str, SummaryTemplate]) -> None:
    processing = policies.processing
    assert (processing.engine, processing.auto_approve_enabled) == ("rules", False)
    assert set(processing.default_template_by_business_line) == BUSINESS_LINES
    assert set(processing.default_template_by_business_line.values()) <= set(templates)
    assert set(processing.transcription_source_preference) == CHANNELS
    for channel, sources in processing.transcription_source_preference.items():
        assert sources and set(sources) <= {"platform", "azure_speech"}, channel
    assert policies.notes.keep_relationship_context is True
    assert {"payment_card", "government_id", "credentials", "medical"} <= set(policies.notes.omit_categories)


def test_pricing_is_flagged_illustrative(data_dir: Path) -> None:
    pricing = load_pricing(data_dir)
    assert pricing.illustrative is True
    assert pricing.currency == "USD"
    assert "illustrative" in pricing.note.lower()
    raw = (data_dir / "pricing.yaml").read_text(encoding="utf-8")
    assert "illustrative" in raw.splitlines()[0].lower()
    assert pricing.speech_per_audio_hour >= 0 and pricing.language_pii_per_1k_records >= 0
    assert all(v.input_per_1m_tokens >= 0 and v.output_per_1m_tokens >= 0 for v in pricing.llm.values())


# ---- CRM seed ---------------------------------------------------------------------------------------------------------------
def test_crm_seed_references_are_consistent(seed: CrmSeed) -> None:
    assert seed.synthetic is True
    for group in (seed.users, seed.accounts, seed.contacts, seed.opportunities):
        ids = [item.id for item in group]
        assert len(ids) == len(set(ids))
    emails = [u.email.lower() for u in seed.users]
    assert len(emails) == len(set(emails))
    accounts = {a.id: a for a in seed.accounts}
    contacts = {c.id: c for c in seed.contacts}
    assert all(c.account_id in accounts for c in seed.contacts)
    for opportunity in seed.opportunities:
        assert opportunity.account_id in accounts
        if opportunity.contact_id:
            assert contacts[opportunity.contact_id].account_id == opportunity.account_id, opportunity.id


# ---- scripted calls ---------------------------------------------------------------------------------------------------------
def test_every_scripted_call_seller_exists_in_the_crm_seed(calls: list[ScriptedCall], seed: CrmSeed) -> None:
    users = {u.email.lower() for u in seed.users}
    for call in calls:
        assert call.seller.email and call.seller.email.lower() in users, f"{call.fixture_id}: {call.seller.email}"


def test_scripted_call_files_are_named_after_their_fixture_id(data_dir: Path) -> None:
    paths = sorted((data_dir / "calls").glob("*.json"))
    assert paths
    for path in paths:
        assert json.loads(path.read_text(encoding="utf-8"))["fixture_id"] == path.stem


def test_scripted_calls_cover_channels_languages_markets_and_business_lines(calls: list[ScriptedCall],
                                                                            templates: dict[str, SummaryTemplate]) -> None:
    assert len(calls) >= 12
    assert {c.channel.value for c in calls} >= {"teams", "zoom", "genesys", "mobile"}
    assert {c.gold.expected_language for c in calls} >= {"en", "es"}
    assert {c.language.split("-")[0] for c in calls} >= {"en", "es"}
    assert {c.market for c in calls} >= {"US", "UK", "EU"}
    assert {c.business_line for c in calls} == BUSINESS_LINES
    assert {c.gold.template_id for c in calls} == set(templates)


def test_spanish_calls_have_spanish_turns(calls: list[ScriptedCall]) -> None:
    spanish = [c for c in calls if c.gold.expected_language == "es"]
    assert spanish
    for call in spanish:
        assert sum(t.locale.startswith("es") for t in call.turns) >= len(call.turns) // 2, call.fixture_id


def test_golden_set_exercises_the_hard_cases(calls: list[ScriptedCall]) -> None:
    assert any(c.gold.must_not_leak for c in calls), "privacy case"
    assert any(c.gold.must_keep for c in calls), "relationship-context case"
    assert any(g.ambiguous for c in calls for g in c.gold.products), "ambiguous share class case"
    assert any(not c.gold.tasks for c in calls), "call with no follow-ups"
    assert any(len(c.gold.products) >= 2 for c in calls), "multi-product case"
    assert any(c.direction.value == "inbound" for c in calls), "inbound call"
    assert all(c.gold.expect_crm_account for c in calls)
