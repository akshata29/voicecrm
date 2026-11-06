"""Hard guards that keep transcript text out of CRM writes (requirement RET-2).

A CrmPackage carries a summary note, structured fields and at most a vault *pointer*. Both CRM adapters run
`assert_no_transcript` before anything is persisted, so a pointer that is really text, or a field that looks like a
raw transcript dump (WebVTT cues, voice tags, timestamped speaker lines), is rejected instead of written.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterator

from ...domain.crm import CrmPackage

logger = logging.getLogger(__name__)

MAX_POINTER_LENGTH = 2048

_POINTER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]{1,31}://\S+$")
_VTT_CUE_RE = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3}\s*-->\s*\d{1,2}:\d{2}")
_VOICE_TAG_RE = re.compile(r"<v[\s.][^>]*>", re.IGNORECASE)
_TIMESTAMPED_LINE_RE = re.compile(r"^\s*[\[(]?\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?[\])]?\s+\S", re.MULTILINE)
_MIN_TIMESTAMPED_LINES = 3


class TranscriptLeakError(ValueError):
    """The package contains something that looks like transcript text."""


_FORBIDDEN_ORG_HOST = re.compile(r"(?:^|\.)(?:microsoftsales|msx)(?:[.\-]|$)|(?:^|[.\-])msx(?:[.\-]|$)", re.IGNORECASE)


def assert_not_msx(base_url: str) -> None:
    """Refuse the MSX Dataverse organisation (``microsoftsales.crm.dynamics.com``): the demo writes to a sandbox only."""
    host = re.sub(r"^[a-z]+://", "", base_url.strip().lower()).split("/")[0]
    if _FORBIDDEN_ORG_HOST.search(host):
        raise ValueError(f"{host} looks like the MSX organisation; VoiceCRM must only write to a dedicated "
                         "non-production Dataverse environment")


def validate_transcript_pointer(pointer: str | None) -> None:
    """A pointer must be a URI (scheme://...), never prose. Raises TranscriptLeakError otherwise."""
    if pointer is None:
        return
    if len(pointer) > MAX_POINTER_LENGTH:
        raise TranscriptLeakError(f"transcript_pointer is {len(pointer)} characters long; a pointer is a short URI")
    if not _POINTER_RE.match(pointer):
        raise TranscriptLeakError("transcript_pointer must be a URI such as https://... or file:///..., not text")


def transcript_artifact(text: str | None) -> str | None:
    """Name the transcript-dump pattern found in `text`, or None when it looks like an ordinary note."""
    if not text:
        return None
    if text.lstrip().upper().startswith("WEBVTT"):
        return "WebVTT header"
    if _VTT_CUE_RE.search(text):
        return "WebVTT cue timing"
    if _VOICE_TAG_RE.search(text):
        return "voice tag"
    if len(_TIMESTAMPED_LINE_RE.findall(text)) >= _MIN_TIMESTAMPED_LINES:
        return "timestamped speaker lines"
    return None


def _text_fields(package: CrmPackage) -> Iterator[tuple[str, str | None]]:
    yield "phone_call.subject", package.phone_call.subject
    yield "phone_call.description", package.phone_call.description
    for task in package.tasks:
        yield f"task[{task.source_task_id}].subject", task.subject
        yield f"task[{task.source_task_id}].description", task.description
    insight = package.insight
    yield "insight.summary_markdown", insight.summary_markdown
    for section in insight.sections:
        yield f"insight.sections[{section.key}].text", section.text
        for index, item in enumerate(section.items):
            yield f"insight.sections[{section.key}].items[{index}]", item
    for index, fact in enumerate(insight.relationship_facts):
        yield f"insight.relationship_facts[{index}]", fact


def assert_no_transcript(package: CrmPackage) -> None:
    """Raise TranscriptLeakError when the package could carry transcript text. Never echoes the offending text."""
    try:
        validate_transcript_pointer(package.insight.transcript_pointer)
        for where, value in _text_fields(package):
            artifact = transcript_artifact(value)
            if artifact:
                raise TranscriptLeakError(f"{where} looks like a transcript dump ({artifact})")
    except TranscriptLeakError as exc:
        logger.warning("CRM write rejected for conversation %s: %s", package.conversation_id, exc)
        raise
