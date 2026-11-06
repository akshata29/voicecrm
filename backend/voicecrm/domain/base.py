"""Shared Pydantic base classes."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class VModel(BaseModel):
    """Strict base: unknown keys raise, so typos in data files fail loudly."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
