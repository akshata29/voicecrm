"""Fixtures for the HTTP API tests: one isolated app (in-memory state and CRM, temp vault) per test module."""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from voicecrm.api.main import create_app
from voicecrm.config import Settings
from voicecrm.container import Container, build_container


@pytest.fixture(scope="module")
def api_container(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Container]:
    """Real engines, catalogue, taxonomy, templates and policies; mock transcriber; nothing shared between modules."""
    settings = Settings(
        _env_file=None, state_dir=tmp_path_factory.mktemp("api-state"), demo_mode=True, transcription_mode="mock",
        llm_mode="rules", search_mode="local", pii_mode="local", crm_mode="local", vault_mode="local",
        zoom_webhook_secret="")
    parent = build_container(settings)
    try:
        yield parent.isolated()
    finally:
        parent.close()


@pytest.fixture(scope="module")
def client(api_container: Container) -> Iterator[TestClient]:
    # The context manager keeps one event loop alive so the background ingest tasks survive between requests.
    with TestClient(create_app(api_container), raise_server_exceptions=False) as test_client:
        yield test_client
