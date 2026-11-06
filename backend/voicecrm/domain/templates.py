"""Summary templates. A template is a DATA CONTRACT (sections + CRM field mapping), not just a writing style."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import VModel


class TemplateSection(VModel):
    key: str                                   # stable key, e.g. "client_context"
    title: str
    kind: Literal["text", "list"] = "text"
    instruction: str                           # what belongs in this section (also used as LLM guidance)
    max_items: int | None = None               # for kind == "list"
    required: bool = True
    crm_field: str | None = None               # optional CRM column that receives this section


class SummaryTemplate(VModel):
    template_id: str
    name: str
    description: str
    business_lines: list[str]
    call_types: list[str] = Field(default_factory=list)   # empty = any
    language_out: str = "en"                   # language of the generated note
    include_relationship_context: bool = False
    tone: str = "concise, factual, third person; no speculation"
    max_words: int = 220
    sections: list[TemplateSection]
