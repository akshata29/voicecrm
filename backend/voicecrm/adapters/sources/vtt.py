"""Small WebVTT reader/writer shared by the Teams and Zoom adapters.

Tolerant on input (BOM, CRLF, missing hours, comma decimals, negative offsets, and the Graph "unattributed" format
that puts a blank line between the timing line and the text); strict on output.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_TIMESTAMP = r"-?(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3}"
_TIMING_RE = re.compile(rf"^\s*({_TIMESTAMP})\s*-->\s*({_TIMESTAMP})(?:\s.*)?$")


@dataclass(frozen=True, slots=True)
class Cue:
    identifier: str | None
    start_ms: int
    end_ms: int
    lines: tuple[str, ...]

    @property
    def payload(self) -> str:
        return "\n".join(self.lines)


def parse_timestamp(value: str) -> int:
    """'HH:MM:SS.mmm' / 'MM:SS.mmm' (comma allowed) -> milliseconds; negative offsets clamp to 0."""
    negative = value.startswith("-")
    parts = value.lstrip("-").replace(",", ".").split(":")
    seconds_part = parts[-1]
    whole, _, fraction = seconds_part.partition(".")
    millis = int((fraction + "00")[:3]) if fraction else 0
    hours, minutes = (int(parts[0]), int(parts[1])) if len(parts) == 3 else (0, int(parts[0]))
    total = (hours * 3600 + minutes * 60 + int(whole)) * 1000 + millis
    return 0 if negative else total


def format_timestamp(milliseconds: int) -> str:
    milliseconds = max(0, int(milliseconds))
    seconds, millis = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def iter_cues(text: str) -> list[Cue]:
    lines = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    count = len(lines)
    cues: list[Cue] = []
    consumed = 0
    index = 0
    while index < count:
        timing = _TIMING_RE.match(lines[index])
        if not timing:
            index += 1
            continue
        identifier = None
        if index > consumed and lines[index - 1].strip() and not _TIMING_RE.match(lines[index - 1]):
            identifier = lines[index - 1].strip()
        cursor = index + 1
        while cursor < count and not lines[cursor].strip():
            cursor += 1
        payload: list[str] = []
        starts_next_cue = cursor < count and (
            _TIMING_RE.match(lines[cursor]) or (cursor + 1 < count and _TIMING_RE.match(lines[cursor + 1]))
        )
        if not starts_next_cue:
            while cursor < count and lines[cursor].strip() and not _TIMING_RE.match(lines[cursor]):
                payload.append(lines[cursor].rstrip())
                cursor += 1
            consumed = cursor
        else:
            consumed = index + 1
        cues.append(Cue(identifier, parse_timestamp(timing.group(1)), parse_timestamp(timing.group(2)), tuple(payload)))
        index = max(cursor, index + 1) if payload else index + 1
    return cues
