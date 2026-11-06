"""Minimal RIFF/WAV header reader (channels, sample rate, duration) that never decodes audio.

Works for PCM and for the compressed WAV variants Azure Speech accepts (A-law, mu-law), because the duration is
derived from the byte rate and the size of the ``data`` chunk rather than from a codec.
"""
from __future__ import annotations

import logging
import os
import struct
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_HEADER_SCAN_BYTES = 1 << 20          # headers live in the first few hundred bytes; stop scanning past 1 MB


@dataclass(frozen=True)
class WavInfo:
    channels: int
    sample_rate: int
    duration_seconds: float

    @property
    def is_stereo(self) -> bool:
        return self.channels == 2


def read_wav_info(path: str | os.PathLike[str]) -> WavInfo | None:
    """Returns header facts for a WAV file, or None when the file is missing, not WAV, or unparseable."""
    try:
        file_size = Path(path).stat().st_size
        with open(path, "rb") as fh:
            riff = fh.read(12)
            if len(riff) < 12 or riff[:4] not in (b"RIFF", b"RF64") or riff[8:12] != b"WAVE":
                return None
            fmt: tuple[int, int, int, int] | None = None     # channels, sample_rate, byte_rate, block_align
            while fh.tell() < _MAX_HEADER_SCAN_BYTES:
                head = fh.read(8)
                if len(head) < 8:
                    return None
                chunk_id, size = struct.unpack("<4sI", head)
                if chunk_id == b"fmt ":
                    body = fh.read(size)
                    if len(body) < 16:
                        return None
                    _tag, channels, rate, byte_rate, block_align, _bits = struct.unpack("<HHIIHH", body[:16])
                    fmt = (channels, rate, byte_rate, block_align)
                    if size & 1:
                        fh.read(1)
                elif chunk_id == b"data":
                    if fmt is None:
                        return None
                    channels, rate, byte_rate, block_align = fmt
                    remaining = file_size - fh.tell()
                    # streamed/unfinalised WAVs write 0 or 0xFFFFFFFF as the data size
                    data_size = remaining if size in (0, 0xFFFFFFFF) or size > remaining else size
                    bytes_per_second = byte_rate or (rate * block_align)
                    if channels < 1 or bytes_per_second <= 0:
                        return None
                    return WavInfo(channels=channels, sample_rate=rate, duration_seconds=data_size / bytes_per_second)
                else:
                    fh.seek(size + (size & 1), os.SEEK_CUR)
        return None
    except (OSError, struct.error, ValueError):
        logger.debug("not a parseable WAV file: %s", os.fspath(path))
        return None
