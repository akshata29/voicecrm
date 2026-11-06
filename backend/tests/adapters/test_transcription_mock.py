"""Mock transcriber: fixture lookup, speaker ids, timings, language, WAV duration, noise and failure modes."""
from __future__ import annotations

import wave
from pathlib import Path

import pytest

from voicecrm.adapters.transcription.mock import MockTranscriber, TranscriptionUnavailable
from voicecrm.domain.conversation import Participant
from voicecrm.domain.enums import Channel, SpeakerRole, TranscriptOrigin
from voicecrm.domain.fixtures import GoldLabels, ScriptedCall, ScriptedTurn
from voicecrm.ports.transcription import AudioInput, TranscriptionPort, TranscriptionRequest


def _call(fixture_id: str = "demo-call", turns: list[ScriptedTurn] | None = None, **kw) -> ScriptedCall:
    turns = turns or [
        ScriptedTurn(speaker="seller", text="Hello Cora, thanks for joining the call today and for your time."),
        ScriptedTurn(speaker="customer", text="Happy to be here. I wanted to ask about the Contoso Equity Fund class C."),
        ScriptedTurn(speaker="internal", text="I can pull the fee schedule for you."),
        ScriptedTurn(speaker="seller", text="I will follow up with you on Friday."),
    ]
    return ScriptedCall(
        fixture_id=fixture_id, title="Demo", description="in-memory demo call", channel=Channel.teams,
        seller=Participant(id="seller", display_name="Sam Seller", role=SpeakerRole.seller),
        customers=[Participant(id="cust1", display_name="Cora Customer", role=SpeakerRole.customer)],
        internal=[Participant(id="int1", display_name="Ivy Internal", role=SpeakerRole.internal)],
        turns=turns, gold=GoldLabels(template_id="pb_institutional"), **kw,
    )


def _request(filename: str = "upload.wav", path: str = "does-not-exist.wav", **kw) -> TranscriptionRequest:
    return TranscriptionRequest(audio=AudioInput(filename=filename, path=path, sha256="0" * 64), **kw)


def _write_wav(path: Path, seconds: float, *, channels: int = 1, rate: int = 8000) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * channels * int(rate * seconds))


def test_implements_the_port():
    assert isinstance(MockTranscriber([]), TranscriptionPort)


async def test_resolves_by_fixture_id_and_maps_default_speakers():
    result = await MockTranscriber([_call()]).transcribe(_request(fixture_id="demo-call"))
    assert [s.speaker_id for s in result.segments] == ["seller", "cust1", "int1", "seller"]
    assert [s.id for s in result.segments] == [0, 1, 2, 3]
    assert result.origin == TranscriptOrigin.fixture and result.provider == "mock"
    assert all(s.locale == "en-US" for s in result.segments) and result.language == "en-US"
    assert "Contoso Equity Fund class C" in result.segments[1].text
    assert result.raw is not None and "Contoso" not in str(result.raw)         # diagnostics carry no transcript text


async def test_explicit_turn_speaker_id_overrides_the_default():
    call = _call(turns=[
        ScriptedTurn(speaker="customer", text="First customer here.", speaker_id="cust1"),
        ScriptedTurn(speaker="customer", text="Second customer here.", speaker_id="cust2"),
    ])
    result = await MockTranscriber([call]).transcribe(_request(fixture_id="demo-call"))
    assert [s.speaker_id for s in result.segments] == ["cust1", "cust2"]


async def test_resolves_by_filename_and_prefers_the_longest_fixture_id():
    mock = MockTranscriber([_call("pb-teams-demo"), _call("pb-teams-demo-es", turns=[
        ScriptedTurn(speaker="seller", text="Hola, gracias por su tiempo.", locale="es-US")])])
    short = await mock.transcribe(_request(filename="fixture-pb-teams-demo.wav"))
    assert short.language == "en-US" and len(short.segments) == 4
    long = await mock.transcribe(_request(filename="fixture-pb-teams-demo-es.wav"))
    assert long.language == "es-US" and len(long.segments) == 1
    suffixed = await mock.transcribe(_request(filename="fixture-pb-teams-demo_v2.mp3"))
    assert len(suffixed.segments) == 4
    by_path = await mock.transcribe(_request(filename="upload.wav", path="C:/tmp/fixture-pb-teams-demo.wav"))
    assert len(by_path.segments) == 4


async def test_unknown_audio_raises_actionable_error():
    mock = MockTranscriber([_call()])
    with pytest.raises(TranscriptionUnavailable, match="VOICECRM_TRANSCRIPTION_MODE=azure_speech"):
        await mock.transcribe(_request(filename="my-real-call.wav"))
    with pytest.raises(TranscriptionUnavailable, match="not-a-fixture"):
        await mock.transcribe(_request(fixture_id="not-a-fixture"))
    with pytest.raises(TranscriptionUnavailable):                       # prefix alone, or a longer alnum run, is no match
        await mock.transcribe(_request(filename="fixture-demo-callx.wav"))


async def test_timings_are_plausible_monotonic_and_deterministic():
    long_text = " ".join(["word"] * 25)
    call = _call(turns=[ScriptedTurn(speaker="seller", text=long_text), ScriptedTurn(speaker="customer", text="Yes.")])
    result = await MockTranscriber([call]).transcribe(_request(fixture_id="demo-call"))
    first, second = result.segments
    assert 8_000 <= first.end_ms - first.start_ms <= 12_000               # ~2.5 words/s
    assert second.start_ms - first.end_ms >= 250                          # a pause between turns
    assert second.end_ms - second.start_ms >= 700                         # even "Yes." takes time
    again = await MockTranscriber([call]).transcribe(_request(fixture_id="demo-call"))
    assert [(s.start_ms, s.end_ms) for s in again.segments] == [(s.start_ms, s.end_ms) for s in result.segments]


async def test_audio_seconds_prefers_the_wav_header(tmp_path):
    wav = tmp_path / "fixture-demo-call.wav"
    _write_wav(wav, 3.0)
    result = await MockTranscriber([_call()]).transcribe(_request(filename=wav.name, path=str(wav)))
    assert result.audio_seconds == pytest.approx(3.0, abs=0.01)


async def test_audio_seconds_falls_back_to_declared_duration_then_timings(tmp_path):
    mock = MockTranscriber([_call()])
    declared = TranscriptionRequest(
        audio=AudioInput(filename="x.wav", path="nope.wav", sha256="0" * 64, duration_seconds=42.5), fixture_id="demo-call")
    assert (await mock.transcribe(declared)).audio_seconds == 42.5
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"not a wav file at all")
    result = await mock.transcribe(_request(path=str(junk), fixture_id="demo-call"))
    assert result.audio_seconds == pytest.approx(result.segments[-1].end_ms / 1000 + 0.8)


async def test_dominant_language_is_by_words_and_locale_is_per_turn():
    call = _call(turns=[
        ScriptedTurn(speaker="seller", text="Hello.", locale="en-US"),
        ScriptedTurn(speaker="customer", text="Quiero saber más sobre el fondo de renta variable clase C.", locale="es-US"),
        ScriptedTurn(speaker="seller", text="Claro que sí, le envío la información hoy mismo.", locale="es-US"),
    ])
    result = await MockTranscriber([call]).transcribe(_request(fixture_id="demo-call"))
    assert result.language == "es-US"
    assert [s.locale for s in result.segments] == ["en-US", "es-US", "es-US"]


async def test_asr_noise_changes_text_only_and_is_deterministic():
    turns = [ScriptedTurn(speaker="seller" if i % 2 == 0 else "customer",
                          text="We looked at the Contoso Equity Fund class C last week.") for i in range(12)]
    mock = MockTranscriber([_call(turns=turns)])
    clean = await mock.transcribe(_request(fixture_id="demo-call"))
    noisy = await mock.transcribe(_request(fixture_id="demo-call", asr_noise=True))
    noisy_again = await mock.transcribe(_request(fixture_id="demo-call", asr_noise=True))
    assert [s.text for s in noisy.segments] == [s.text for s in noisy_again.segments]
    assert any(a.text != b.text for a, b in zip(clean.segments, noisy.segments, strict=True))
    for a, b in zip(clean.segments, noisy.segments, strict=True):
        assert (a.id, a.speaker_id, a.start_ms, a.end_ms, a.locale) == (b.id, b.speaker_id, b.start_ms, b.end_ms, b.locale)


async def test_loads_scripted_calls_lazily_from_data_dir_and_caches(tmp_path):
    calls_dir = tmp_path / "calls"
    calls_dir.mkdir()
    path = calls_dir / "demo-call.json"
    path.write_text(_call().model_dump_json(), encoding="utf-8")
    mock = MockTranscriber(data_dir=tmp_path)
    first = await mock.transcribe(_request(fixture_id="demo-call"))
    path.unlink()                                                       # a second read would now find nothing
    second = await mock.transcribe(_request(filename="fixture-demo-call.wav"))
    assert len(first.segments) == len(second.segments) == 4
