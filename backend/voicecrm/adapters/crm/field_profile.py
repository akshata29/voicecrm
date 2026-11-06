"""Field profile for the Dataverse adapter and the bootstrap tool: which columns hold the structured insight.

`data/crm/field_profile.yaml` describes two profiles:
  standard  no custom columns; the structured fields travel as a compact footer inside the phone call description
  custom    a `vcrm_conversationinsight` table with typed columns, a multi-select topic column and lookups

The same model drives the adapter (what to write) and `tools/dataverse_bootstrap.py` (what to create), so the two
cannot drift apart. Column keys are the adapter's vocabulary (`summary`, `topic_ids`, ...); schema names are Dataverse's.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from ...config import get_settings
from ...domain.base import VModel

logger = logging.getLogger(__name__)

DEFAULT_TOPIC_OPTION_BASE = 100_000_000
INSIGHT_KEYS = frozenset({
    "name", "summary", "sections", "relationship_facts", "product_ids", "topic_ids", "template_id", "engine", "model",
    "confidence", "review_status", "language", "source_system", "source_interaction_id", "idempotency_key",
    "conversation_id", "transcript_pointer",
})


class StandardSpec(VModel):
    footer_marker: str = "voicecrm-insight"
    footer_version: int = 1
    description_max_chars: int = 2000          # phonecall.description MaxLength
    subject_max_chars: int = 200               # phonecall.subject / task.subject MaxLength
    task_description_max_chars: int = 2000


class TableSpec(VModel):
    schema_name: str
    logical_name: str
    entity_set: str
    display_name: str
    display_collection_name: str
    description: str
    ownership: Literal["UserOwned", "OrganizationOwned"] = "UserOwned"


class ColumnSpec(VModel):
    key: str                                   # adapter vocabulary, see INSIGHT_KEYS
    schema_name: str
    type: Literal["string", "memo", "decimal", "choice", "multiselect"]
    display_name: str
    description: str | None = None
    max_length: int | None = None              # string (<= 4000) and memo
    min_value: float | None = None             # decimal
    max_value: float | None = None
    precision: int | None = None
    options: dict[str, int] = Field(default_factory=dict)   # choice: option key -> value
    option_source: Literal["taxonomy"] | None = None        # multiselect: options come from data/taxonomy/topics.json
    primary_name: bool = False

    @property
    def logical_name(self) -> str:
        return self.schema_name.lower()


class LookupSpec(VModel):
    key: Literal["account", "contact", "opportunity", "phonecall"]
    schema_name: str                           # also the single-valued navigation property used in @odata.bind
    referenced_entity: str
    referenced_attribute: str
    entity_set: str
    display_name: str
    relationship_schema_name: str
    optional: bool = False                     # the bootstrap tool continues when an optional relationship fails

    @property
    def logical_name(self) -> str:
        return self.schema_name.lower()


class AlternateKeySpec(VModel):
    schema_name: str
    display_name: str
    columns: list[str]                         # column keys


class TopicOptions(VModel):
    base: int = DEFAULT_TOPIC_OPTION_BASE
    overrides: dict[str, int] = Field(default_factory=dict)   # topic_id -> option value, pin once a column exists


class CustomSpec(VModel):
    table: TableSpec
    columns: list[ColumnSpec]
    section_columns: list[ColumnSpec] = Field(default_factory=list)   # targets of SummaryTemplate sections[].crm_field
    lookups: list[LookupSpec] = Field(default_factory=list)
    alternate_keys: list[AlternateKeySpec] = Field(default_factory=list)
    topic_options: TopicOptions = Field(default_factory=TopicOptions)

    @model_validator(mode="after")
    def _check(self) -> CustomSpec:
        keys = [c.key for c in self.columns]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate column keys in the custom field profile")
        unknown = sorted(set(keys) - INSIGHT_KEYS)
        if unknown:
            raise ValueError(f"unknown insight column keys: {unknown}")
        if "idempotency_key" not in keys:
            raise ValueError("the custom profile needs an idempotency_key column (duplicate protection)")
        if sum(1 for c in self.columns if c.primary_name) != 1:
            raise ValueError("exactly one column must be the primary name column")
        names = [c.logical_name for c in [*self.columns, *self.section_columns]]
        if len(set(names)) != len(names):
            raise ValueError("duplicate column schema names in the custom field profile")
        for key in self.alternate_key_columns():
            if key not in keys:
                raise ValueError(f"alternate key references unknown column {key!r}")
        return self

    def alternate_key_columns(self) -> list[str]:
        return [c for key in self.alternate_keys for c in key.columns]

    def column(self, key: str) -> ColumnSpec | None:
        return next((c for c in self.columns if c.key == key), None)

    def section_column(self, logical_name: str) -> ColumnSpec | None:
        return next((c for c in self.section_columns if c.logical_name == logical_name), None)

    def lookup(self, key: str) -> LookupSpec | None:
        return next((lk for lk in self.lookups if lk.key == key), None)


class FieldProfile(VModel):
    version: int = 1
    publisher_prefix: str = "vcrm"
    solution_unique_name: str | None = None
    standard: StandardSpec = Field(default_factory=StandardSpec)
    custom: CustomSpec


def default_profile_path(data_dir: Path | None = None) -> Path:
    return Path(data_dir or get_settings().data_dir) / "crm" / "field_profile.yaml"


def load_field_profile(path: Path | None = None) -> FieldProfile:
    target = Path(path) if path else default_profile_path()
    return FieldProfile.model_validate(yaml.safe_load(target.read_text(encoding="utf-8")))


def build_topic_option_map(
    topic_ids: Sequence[str], *, base: int = DEFAULT_TOPIC_OPTION_BASE, overrides: Mapping[str, int] | None = None
) -> dict[str, int]:
    """topic_id -> choice value. Pinned overrides win; the rest are numbered from `base` in the given (taxonomy) order.

    Numbering by position is only stable while topics are appended: pin values in `topic_options.overrides` before
    reordering or removing topics in an environment where the choice column already exists.
    """
    mapping = dict(overrides or {})
    used = set(mapping.values())
    if len(used) != len(mapping):
        raise ValueError("topic option overrides must be unique")
    next_value = base
    for topic_id in topic_ids:
        if topic_id in mapping:
            continue
        while next_value in used:
            next_value += 1
        mapping[topic_id] = next_value
        used.add(next_value)
        next_value += 1
    return mapping
