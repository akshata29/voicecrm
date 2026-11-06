"""Mock transcriber: "transcribes" the scripted demo recordings by replaying their script with plausible timings.

It only knows audio that belongs to a ``ScriptedCall`` (matched by ``TranscriptionRequest.fixture_id`` or by an
audio filename starting ``fixture-<fixture_id>``). Anything else raises ``TranscriptionUnavailable``: the mock never
pretends to recognise real speech. Timings are synthetic (about 2.5 words per second plus short pauses).
"""
from __future__ import annotations

import hashlib
import logging
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from ...data_loader import load_scripted_calls
from ...domain.conversation import Segment
from ...domain.enums import TranscriptOrigin
from ...domain.fixtures import ScriptedCall
from ...ports.transcription import TranscriptionPort, TranscriptionRequest, TranscriptionResult
from .noise import apply_asr_noise
from .wav import read_wav_info

logger = logging.getLogger(__name__)

_FIXTURE_PREFIX = "fixture-"
_LEAD_IN_MS = 800
_MIN_TURN_MS = 700
_REAL_ASR_HINT = (
    "To transcribe real audio set VOICECRM_TRANSCRIPTION_MODE=azure_speech and configure VOICECRM_SPEECH_ENDPOINT "
    "(plus VOICECRM_SPEECH_KEY when VOICECRM_AZURE_AUTH=key)."
)


class TranscriptionUnavailable(RuntimeError):
    """The configured transcriber cannot transcribe this audio (the mock only knows the scripted demo recordings)."""


class MockTranscriber(TranscriptionPort):
    name = "mock"

    def __init__(
        self,
        scripted_calls: Sequence[ScriptedCall] | None = None,
        *,
        data_dir: Path | None = None,
        words_per_second: float = 2.5,
        noise_seed: int = 7,
        noise_level: float = 0.5,
    ) -> None:
        """``scripted_calls`` injects fixtures (tests); otherwise they load lazily from ``data_dir`` (or the settings)."""
        self._injected = list(scripted_calls) if scripted_calls is not None else None
        self._data_dir = data_dir
        self._words_per_second = words_per_second
        self._noise_seed = noise_seed
        self._noise_level = noise_level
        self._by_id: dict[str, ScriptedCall] | None = None

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        call = self._resolve(request)
        segments = self._render(call)
        if request.asr_noise:
            segments = apply_asr_noise(segments, seed=self._noise_seed, level=self._noise_level)
        logger.info("mock transcription of fixture %s: %d segments", call.fixture_id, len(segments))
        return TranscriptionResult(
            segments=segments,
            language=self._dominant_language(call),
            origin=TranscriptOrigin.fixture,
            audio_seconds=self._audio_seconds(request, segments),
            provider=self.name,
            raw={"fixture_id": call.fixture_id, "turns": len(call.turns), "asr_noise": request.asr_noise,
                 "timings": "synthetic"},
        )

    # ---- fixture lookup ---------------------------------------------------------------------------------------
    def _fixtures(self) -> dict[str, ScriptedCall]:
        if self._by_id is None:
            calls = self._injected if self._injected is not None else load_scripted_calls(self._data_dir)
            self._by_id = {c.fixture_id: c for c in calls}
        return self._by_id

    def _resolve(self, request: TranscriptionRequest) -> ScriptedCall:
        fixtures = self._fixtures()
        if request.fixture_id:
            call = fixtures.get(request.fixture_id)
            if call is None:
                raise TranscriptionUnavailable(
                    f"Unknown demo fixture_id {request.fixture_id!r} ({len(fixtures)} scripted calls are loaded). "
                    + _REAL_ASR_HINT)
            return call
        for name in (request.audio.filename, Path(request.audio.path).name):
            call = _match_filename(name, fixtures)
            if call is not None:
                return call
        raise TranscriptionUnavailable(
            f"{request.audio.filename!r} is not a known demo recording: the mock transcriber only handles audio named "
            f"'{_FIXTURE_PREFIX}<fixture_id>.<ext>' or uploaded with a fixture_id. " + _REAL_ASR_HINT)

    # ---- rendering --------------------------------------------------------------------------------------------
    def _render(self, call: ScriptedCall) -> list[Segment]:
        defaults = {
            "seller": call.seller.id or "seller",
            "customer": call.customers[0].id if call.customers else "cust1",
            "internal": call.internal[0].id if call.internal else "int1",
        }
        segments: list[Segment] = []
        clock = _LEAD_IN_MS
        previous_speaker: str | None = None
        for index, turn in enumerate(call.turns):
            speaker = turn.speaker_id or defaults[turn.speaker]
            if index:
                clock += _pause_ms(call.fixture_id, index, speaker_changed=speaker != previous_speaker)
            duration = max(_MIN_TURN_MS, round(len(turn.text.split()) / self._words_per_second * 1000))
            segments.append(Segment(
                id=index, speaker_id=speaker, start_ms=clock, end_ms=clock + duration, text=turn.text,
                locale=turn.locale,
            ))
            clock += duration
            previous_speaker = speaker
        return segments

    @staticmethod
    def _dominant_language(call: ScriptedCall) -> str:
        words: Counter[str] = Counter()
        for turn in call.turns:
            words[turn.locale] += len(turn.text.split())
        return words.most_common(1)[0][0] if words else call.language

    @staticmethod
    def _audio_seconds(request: TranscriptionRequest, segments: list[Segment]) -> float:
        info = read_wav_info(request.audio.path)
        if info is not None and info.duration_seconds > 0:
            return round(info.duration_seconds, 3)
        if request.audio.duration_seconds:
            return float(request.audio.duration_seconds)
        return round(((segments[-1].end_ms if segments else 0) + _LEAD_IN_MS) / 1000, 3)


def _match_filename(filename: str, fixtures: dict[str, ScriptedCall]) -> ScriptedCall | None:
    """``fixture-<fixture_id>[-suffix].ext`` -> the call with the longest matching fixture id."""
    stem = Path(filename).stem
    if not stem.lower().startswith(_FIXTURE_PREFIX):
        return None
    rest = stem[len(_FIXTURE_PREFIX):].lower()
    best: ScriptedCall | None = None
    for fixture_id, call in fixtures.items():
        fid = fixture_id.lower()
        exact = rest == fid
        with_suffix = len(rest) > len(fid) and rest.startswith(fid) and not rest[len(fid)].isalnum()
        if (exact or with_suffix) and (best is None or len(fid) > len(best.fixture_id)):
            best = call
    return best


def _pause_ms(fixture_id: str, index: int, *, speaker_changed: bool) -> int:
    """Deterministic 250-900 ms pause (longer when the speaker changes) so reruns give identical timings."""
    digest = hashlib.sha256(f"{fixture_id}:{index}".encode()).digest()
    return 250 + int.from_bytes(digest[:4], "big") % 650 + (150 if speaker_changed else 0)
