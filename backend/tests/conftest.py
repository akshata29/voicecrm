"""Shared pytest fixtures."""
from __future__ import annotations

from pathlib import Path

import pytest

from voicecrm.config import REPO_ROOT, Settings


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return REPO_ROOT / "data"


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """Isolated settings: temp state dir, everything mock/local, demo mode on."""
    return Settings(
        _env_file=None,
        state_dir=tmp_path / "state",
        demo_mode=True,
        transcription_mode="mock",
        llm_mode="rules",
        search_mode="local",
        pii_mode="local",
        crm_mode="local",
        vault_mode="local",
    )
