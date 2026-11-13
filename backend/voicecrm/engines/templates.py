"""Template selection and note rendering.

A template is a data contract (sections + CRM field mapping). `select_template` picks one from the market/business line
and the processing policy; `MarkdownNoteRenderer` renders the note exactly as it will be written to the CRM description.
"""
from __future__ import annotations

from ..domain.catalogue import Catalogue
from ..domain.conversation import ConversationMeta
from ..domain.enums import Channel, Direction
from ..domain.extraction import ExtractionResult
from ..domain.policy import ProcessingPolicy
from ..domain.templates import SummaryTemplate

_CHANNEL_TEXT = {Channel.teams: "Teams meeting", Channel.zoom: "Zoom meeting", Channel.genesys: "Genesys call",
                 Channel.phone: "Phone call", Channel.mobile: "Mobile recording", Channel.upload: "Uploaded recording"}


def select_template(templates: dict[str, SummaryTemplate], meta: ConversationMeta, processing: ProcessingPolicy,
                    requested: str | None = None) -> SummaryTemplate:
    """Requested template, else the policy default for the business line, else the best fit, else the first template."""
    if requested:
        if requested not in templates:
            raise KeyError(f"unknown template '{requested}'")
        return templates[requested]
    preferred = processing.default_template_by_business_line.get(meta.business_line)
    if preferred in templates:
        return templates[preferred]
    fits = [t for t in templates.values() if meta.business_line in t.business_lines
            and (not t.call_types or meta.call_type is None or meta.call_type in t.call_types)]
    if fits:
        return fits[0]
    if not templates:
        raise KeyError("no templates configured")
    return next(iter(templates.values()))


class MarkdownNoteRenderer:
    """Implements engines.contracts.NoteRenderer."""

    def render(self, result: ExtractionResult, template: SummaryTemplate, meta: ConversationMeta, *,
               catalogue: Catalogue) -> str:
        lines = [f"# {template.name}", self._context_line(meta), ""]
        by_key = {s.key: s for s in result.sections}
        ordered = [by_key[t.key] for t in template.sections if t.key in by_key]
        ordered += [s for s in result.sections if s.key not in {t.key for t in template.sections}]
        for section in ordered:
            lines.append(f"## {section.title}")
            if section.text:
                lines.append(section.text.strip())
            if section.items:
                lines.extend(f"- {item.strip()}" for item in section.items)
            if not section.text and not section.items:
                lines.append("_None noted._")
            lines.append("")
        if result.topics:
            lines.append("**Topics:** " + " | ".join(t.label for t in result.topics))
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _context_line(meta: ConversationMeta) -> str:
        names = ", ".join(p.display_name for p in meta.participants) or "customer"
        org = f" ({meta.customer_org})" if meta.customer_org else ""
        channel = _CHANNEL_TEXT.get(meta.source, "Call")
        if meta.direction == Direction.inbound:
            channel = f"Inbound {channel.lower()}"
        return f"*{channel} - {meta.started_at.strftime('%Y-%m-%d')} - {meta.seller.display_name} with {names}{org}*"
