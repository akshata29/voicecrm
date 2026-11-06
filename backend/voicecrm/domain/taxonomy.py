"""Topic taxonomy: the controlled list that maps to CRM dropdown (choice) fields."""
from __future__ import annotations

from pydantic import Field

from .base import VModel


class Topic(VModel):
    topic_id: str                              # stable key, e.g. "fees_expenses"
    label: str                                 # English label shown in CRM
    description: str | None = None
    labels_i18n: dict[str, str] = Field(default_factory=dict)             # {"es": "Comisiones y gastos"}
    synonyms: list[str] = Field(default_factory=list)                     # English trigger phrases
    synonyms_i18n: dict[str, list[str]] = Field(default_factory=dict)     # {"es": ["comisiones", ...]}
    parent_id: str | None = None
    business_lines: list[str] = Field(default_factory=list)               # empty = all business lines


class Taxonomy(VModel):
    version: str
    synthetic: bool = True
    topics: list[Topic]

    def by_id(self) -> dict[str, Topic]:
        return {t.topic_id: t for t in self.topics}
