"""Loaders for the data/ directory: catalogue, taxonomy, templates, policies, CRM seed, scripted calls, pricing."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from .config import get_settings
from .domain.catalogue import Catalogue
from .domain.crm import CrmSeed
from .domain.fixtures import ScriptedCall
from .domain.metering import PricingTable
from .domain.policy import MarketPolicy, NotesPolicy, PolicyBundle, ProcessingPolicy
from .domain.taxonomy import Taxonomy
from .domain.templates import SummaryTemplate


def _dir(data_dir: Path | None) -> Path:
    return Path(data_dir) if data_dir else get_settings().data_dir


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_catalogue(data_dir: Path | None = None) -> Catalogue:
    return Catalogue.model_validate(_json(_dir(data_dir) / "catalogue" / "products.json"))


def load_taxonomy(data_dir: Path | None = None) -> Taxonomy:
    return Taxonomy.model_validate(_json(_dir(data_dir) / "taxonomy" / "topics.json"))


def load_templates(data_dir: Path | None = None) -> dict[str, SummaryTemplate]:
    out: dict[str, SummaryTemplate] = {}
    for path in sorted((_dir(data_dir) / "templates").glob("*.yaml")):
        tpl = SummaryTemplate.model_validate(_yaml(path))
        out[tpl.template_id] = tpl
    return out


def parse_policies(raw: dict[str, Any]) -> PolicyBundle:
    markets = {key: MarketPolicy.model_validate({**value, "market": key}) for key, value in raw["markets"].items()}
    return PolicyBundle(
        markets=markets,
        notes=NotesPolicy.model_validate(raw.get("notes", {})),
        processing=ProcessingPolicy.model_validate(raw.get("processing", {})),
    )


def load_policies(data_dir: Path | None = None) -> PolicyBundle:
    return parse_policies(_yaml(_dir(data_dir) / "policies" / "policies.yaml"))


def load_crm_seed(data_dir: Path | None = None) -> CrmSeed:
    return CrmSeed.model_validate(_json(_dir(data_dir) / "crm" / "seed.json"))


def load_scripted_calls(data_dir: Path | None = None) -> list[ScriptedCall]:
    return [ScriptedCall.model_validate(_json(p)) for p in sorted((_dir(data_dir) / "calls").glob("*.json"))]


def load_pricing(data_dir: Path | None = None) -> PricingTable:
    return PricingTable.model_validate(_yaml(_dir(data_dir) / "pricing.yaml"))
