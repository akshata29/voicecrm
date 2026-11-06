"""Enumerations shared across the system.

Code-bound concepts are Enums. Data-driven concepts (market, business_line, call_type, share class,
topic ids, template ids) are plain strings whose valid values come from the files under data/.
"""
from __future__ import annotations

from enum import Enum


class Channel(str, Enum):
    teams = "teams"
    zoom = "zoom"
    genesys = "genesys"
    mobile = "mobile"
    upload = "upload"


class Direction(str, Enum):
    inbound = "inbound"
    outbound = "outbound"
    meeting = "meeting"
    in_person = "in_person"


class SpeakerRole(str, Enum):
    seller = "seller"
    customer = "customer"
    internal = "internal"
    unknown = "unknown"


class TranscriptOrigin(str, Enum):
    azure_speech = "azure_speech"
    teams_platform = "teams_platform"
    zoom_platform = "zoom_platform"
    genesys_platform = "genesys_platform"
    fixture = "fixture"
    manual = "manual"


class ConversationStatus(str, Enum):
    received = "received"
    processing = "processing"
    needs_review = "needs_review"
    approved = "approved"      # approved by a human or the auto-approve policy; CRM write pending
    written = "written"        # CRM write succeeded
    rejected = "rejected"
    failed = "failed"


class Stage(str, Enum):
    ingest = "ingest"
    transcribe = "transcribe"
    normalize = "normalize"
    policy = "policy"          # PII redaction + notes policy (sensitive / small talk / relationship)
    extract = "extract"
    resolve = "resolve"        # product + topic resolution, CRM matching
    validate = "validate"
    review = "review"
    write = "write"
    retention = "retention"


class StageStatus(str, Enum):
    started = "started"
    completed = "completed"
    skipped = "skipped"
    failed = "failed"


class MentionType(str, Enum):
    interest = "interest"
    existing_holding = "existing_holding"
    comparison = "comparison"
    objection = "objection"
    info_only = "info_only"


class OwnerRole(str, Enum):
    seller = "seller"
    customer = "customer"
    internal = "internal"
    unknown = "unknown"


class Engine(str, Enum):
    rules = "rules"            # no LLM
    llm = "llm"                # Foundry model does extraction
    hybrid = "hybrid"          # rules generate candidates, LLM summarises/types, rules validate
