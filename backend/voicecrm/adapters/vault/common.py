"""Shared helpers for the vault adapters: id and file-name validation, timestamps."""
from __future__ import annotations

import re
from datetime import datetime, timezone

_CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = frozenset({
    "con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10)),
})
MAX_FILENAME_LENGTH = 120


def validate_conversation_id(conversation_id: str) -> str:
    """Ids become directory / blob prefixes, so anything that could escape them (or is not a valid Windows name) is refused."""
    if (
        not _CONVERSATION_ID_RE.match(conversation_id)
        or ".." in conversation_id
        or conversation_id.endswith(".")
        or conversation_id.split(".", 1)[0].lower() in _WINDOWS_RESERVED
    ):
        raise ValueError(f"unsafe conversation id: {conversation_id!r}")
    return conversation_id


def safe_filename(filename: str | None, default: str = "audio.bin") -> str:
    """A base name that is valid on Windows and in blob names: no separators, reserved names or trailing dots."""
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_FILENAME_RE.sub("_", name).strip(" .")
    stem, dot, ext = name.partition(".")
    if stem.lower() in _WINDOWS_RESERVED:
        name = f"_{name}"
    if len(name) > MAX_FILENAME_LENGTH:
        stem, dot, ext = name.rpartition(".")
        keep = max(1, MAX_FILENAME_LENGTH - len(ext) - 1) if dot else MAX_FILENAME_LENGTH
        name = f"{stem[:keep]}.{ext}" if dot else name[:MAX_FILENAME_LENGTH]
    return name or default


def as_utc(value: datetime) -> datetime:
    """Naive datetimes are taken to be UTC."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None
