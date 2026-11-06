"""Session-scoped fixtures for the rules-engine tests (loading data and building the catalogue index happen once)."""
from __future__ import annotations

import pytest

from voicecrm.data_loader import load_catalogue, load_policies, load_pricing, load_taxonomy, load_templates
from voicecrm.engines.catalogue_index import CatalogueIndex
from voicecrm.engines.extractor import RulesExtractor
from voicecrm.engines.product_resolver import RulesProductResolver
from voicecrm.engines.summarizer import RulesSummaryEngine
from voicecrm.engines.tasks import RulesTaskEngine
from voicecrm.engines.topics import RulesTopicEngine
from voicecrm.engines.validator import RulesValidator


@pytest.fixture(scope="session")
def catalogue():
    return load_catalogue()


@pytest.fixture(scope="session")
def taxonomy():
    return load_taxonomy()


@pytest.fixture(scope="session")
def templates():
    return load_templates()


@pytest.fixture(scope="session")
def policies():
    return load_policies()


@pytest.fixture(scope="session")
def pricing():
    return load_pricing()


@pytest.fixture(scope="session")
def cindex(catalogue) -> CatalogueIndex:
    return CatalogueIndex(catalogue)


@pytest.fixture(scope="session")
def resolver(catalogue, cindex) -> RulesProductResolver:
    """Shared and read-only: tests that load learned aliases build their own resolver on the shared index."""
    return RulesProductResolver(catalogue, index=cindex)


@pytest.fixture(scope="session")
def topic_engine(taxonomy) -> RulesTopicEngine:
    return RulesTopicEngine(taxonomy)


@pytest.fixture(scope="session")
def rules_extractor(resolver, topic_engine) -> RulesExtractor:
    return RulesExtractor(resolver, topic_engine, RulesTaskEngine(), RulesSummaryEngine())


@pytest.fixture(scope="session")
def validator(catalogue, taxonomy) -> RulesValidator:
    return RulesValidator(catalogue, taxonomy)
