"""A recording nobody spoke in: the pipeline fails the call in plain words, never with a raw service response."""
from __future__ import annotations

import logging

from voicecrm.adapters.llm.fake import FakeLlm
from voicecrm.adapters.transcription.azure_speech import AzureSpeechError
from voicecrm.domain.enums import ConversationStatus, Stage, StageStatus, TranscriptOrigin
from voicecrm.pipeline.fixtures import ingest_from_scripted
from voicecrm.ports.transcription import NO_SPEECH_MESSAGE, NoSpeechError, TranscriptionRequest, TranscriptionResult

from .test_llm_pipeline import _service


class Transcriber:
    """Stands in for the speech service: it raises ``error``, or answers with ``segments`` (none by default)."""

    name = "stub"

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        if self.error is not None:
            raise self.error
        return TranscriptionResult(segments=[], language="en-US", origin=TranscriptOrigin.azure_speech, audio_seconds=12.0,
                                   provider="stub")


async def run(tmp_path, transcriber: Transcriber):
    h = _service(tmp_path, FakeLlm(lambda call: {}, model="x"))
    h.service.deps.transcription = transcriber
    rec = await h.service.run_to_completion(ingest_from_scripted(h.call, via="audio", nonce="silent"))
    return h, rec


async def test_a_transcriber_that_hears_no_speech_fails_the_call_in_plain_words(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="voicecrm.pipeline.service")
    h, rec = await run(tmp_path, Transcriber(error=NoSpeechError()))
    assert rec.status == ConversationStatus.failed and rec.error == NO_SPEECH_MESSAGE        # no class name, no service response
    events = h.store.list_events(rec.meta.conversation_id)
    failed = [e for e in events if e.status == StageStatus.failed]
    assert [(e.stage, e.detail["error"]) for e in failed] == [(Stage.transcribe, NO_SPEECH_MESSAGE)]
    assert Stage.normalize not in {e.stage for e in events}                                  # nothing was made of it
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]                   # an expected outcome, not a fault


async def test_a_transcript_with_no_words_is_the_same_outcome_and_is_still_billed(tmp_path):
    h, rec = await run(tmp_path, Transcriber())
    assert rec.status == ConversationStatus.failed and rec.error == NO_SPEECH_MESSAGE
    (speech,) = [u for u in h.store.list_usage(rec.meta.conversation_id) if u.service == "speech"]
    assert speech.audio_seconds == 12.0                                                      # the service did process the audio


async def test_other_transcription_failures_keep_their_class_and_still_warn(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="voicecrm.pipeline.service")
    _, rec = await run(tmp_path, Transcriber(error=AzureSpeechError("Azure Speech fast transcription failed: HTTP 500",
                                                                    service="Azure Speech")))
    assert rec.status == ConversationStatus.failed and rec.error.startswith("AzureSpeechError: ")
    assert [r for r in caplog.records if r.levelno == logging.WARNING and "pipeline failed" in r.getMessage()]
