"""Transcription port. Adapters: mock (sidecar scripts), Azure Speech fast transcription."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from ..domain.base import VModel
from ..domain.conversation import Segment
from ..domain.enums import TranscriptOrigin


class AudioInput(VModel):
    filename: str
    content_type: str = "audio/wav"
    path: str                                  # local filesystem path (blob-backed vaults materialise to a temp file)
    sha256: str
    duration_seconds: float | None = None


class TranscriptionRequest(VModel):
    audio: AudioInput
    locales: list[str] = Field(default_factory=lambda: ["en-US", "es-US"])   # candidate locales (language identification)
    phrase_list: list[str] = Field(default_factory=list)                    # catalogue-derived boosts for product names
    diarize: bool = True
    max_speakers: int = 2
    separate_channels: bool = False            # dual-channel recording (one party per channel, e.g. Genesys): label by channel
    channel_speakers: list[str] = Field(default_factory=list)   # dual-channel only: the speaker id of each channel (index = channel)
    fixture_id: str | None = None              # lets the mock transcriber find the scripted call for a known audio file
    asr_noise: bool = False                    # mock only: inject realistic ASR mistakes (e.g. "class see")


class TranscriptionResult(VModel):
    segments: list[Segment]                    # speaker_id is "S1", "S2", ... (diarization labels) or a known participant id
    language: str
    origin: TranscriptOrigin
    audio_seconds: float
    provider: str
    raw: dict[str, Any] | None = None


NO_SPEECH_MESSAGE = ("No speech could be recognised in the recording. It may be silent, too short, or in a language "
                     "that was not expected.")


class NoSpeechError(Exception):
    """The audio holds nothing the transcriber could recognise: silence, a call hung up at once, or only noise.

    An adapter raises this instead of a raw service error when the service finds no language or no words. The pipeline fails
    the conversation with this plain message, which is also how a caller of the pipeline recognises the outcome.
    """

    def __init__(self, message: str = NO_SPEECH_MESSAGE) -> None:
        super().__init__(message)


@runtime_checkable
class TranscriptionPort(Protocol):
    name: str

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult: ...
