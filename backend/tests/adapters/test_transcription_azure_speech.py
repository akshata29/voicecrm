"""Azure Speech fast transcription adapter: request shape, auth, phrase merging, retries and error handling (no network)."""
from __future__ import annotations

import json
import re
import time
import wave
from pathlib import Path

import httpx
import pytest
import respx
from azure.core.credentials import AccessToken

from voicecrm.adapters.transcription.azure_speech import (
    API_VERSION,
    SPEECH_SCOPE,
    AzureSpeechError,
    AzureSpeechTranscriber,
)
from voicecrm.domain.enums import TranscriptOrigin
from voicecrm.ports.transcription import AudioInput, TranscriptionPort, TranscriptionRequest

ENDPOINT = "https://voicecrm-speech.cognitiveservices.azure.com"
URL = f"{ENDPOINT}/speechtotext/transcriptions:transcribe"


class FakeCredential:
    def __init__(self) -> None:
        self.scopes: list[tuple[str, ...]] = []
        self.closed = False

    async def get_token(self, *scopes: str, **_kw: object) -> AccessToken:
        self.scopes.append(scopes)
        return AccessToken("fake-entra-token", int(time.time()) + 3600)

    async def close(self) -> None:
        self.closed = True


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _wav(path: Path, seconds: float = 1.0, *, channels: int = 1, rate: int = 8000) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x00" * channels * int(rate * seconds))
    return path


def _request(path: Path, **kw) -> TranscriptionRequest:
    return TranscriptionRequest(
        audio=AudioInput(filename="call-with-jane-doe.wav", path=str(path), sha256="0" * 64), **kw)


def _phrase(speaker: int | None, start: int, dur: int, text: str, *, locale: str = "en-US", conf: float = 0.9,
            channel: int | None = None) -> dict:
    out: dict = {"offsetMilliseconds": start, "durationMilliseconds": dur, "text": text, "locale": locale,
                 "confidence": conf, "words": []}
    if speaker is not None:
        out["speaker"] = speaker
    if channel is not None:
        out["channel"] = channel
    return out


BODY = {
    "durationMilliseconds": 12_480,
    "combinedPhrases": [{"channel": 0, "text": "ignored"}],
    "phrases": [
        _phrase(2, 1000, 1500, "Hello, this is Sam."),
        _phrase(2, 2600, 1200, "Thanks for joining."),                       # 100 ms gap -> merged
        _phrase(1, 4500, 1000, "Hi Sam, glad to be here."),
        _phrase(2, 8000, 1000, "Let's talk about the fund."),                # 4.2 s pause -> new segment
    ],
}


def _multipart(request: httpx.Request) -> dict[str, tuple[str | None, bytes]]:
    boundary = request.headers["content-type"].split("boundary=")[1].strip('"').encode()
    parts: dict[str, tuple[str | None, bytes]] = {}
    for raw in request.content.split(b"--" + boundary):
        raw = raw.removeprefix(b"\r\n")
        if not raw.strip() or raw.strip() == b"--":
            continue
        head, _, body = raw.partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]+)"', head).group(1).decode()
        filename = re.search(rb'filename="([^"]*)"', head)
        parts[name] = (filename.group(1).decode() if filename else None, body.removesuffix(b"\r\n"))
    return parts


def _definition(request: httpx.Request) -> dict:
    return json.loads(_multipart(request)["definition"][1])


@pytest.fixture()
def api():
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture()
def audio(tmp_path):
    return _wav(tmp_path / "call.wav")


async def _transcriber(**kw) -> AzureSpeechTranscriber:
    kw.setdefault("endpoint", ENDPOINT)
    kw.setdefault("key", "speech-key-123")
    kw.setdefault("auth", "key")
    kw.setdefault("http_client", httpx.AsyncClient(verify=False))     # no TLS in tests; skips the slow CA-bundle load
    kw.setdefault("sleep", Sleeps())
    return AzureSpeechTranscriber(**kw)


async def test_implements_the_port():
    assert isinstance(await _transcriber(), TranscriptionPort)


async def test_request_shape_with_key_auth(api, audio):
    route = api.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    phrases = ["Contoso Equity Fund Class C", "contoso equity fund class c", "  Contoso   Income Fund ", ""]
    t = await _transcriber()
    await t.transcribe(_request(audio, locales=["en-US", "es-US", "en-GB"], phrase_list=phrases, max_speakers=3))
    request = route.calls.last.request
    assert request.method == "POST" and request.url.path == "/speechtotext/transcriptions:transcribe"
    assert request.url.params["api-version"] == API_VERSION == "2025-10-15"
    assert request.headers["Ocp-Apim-Subscription-Key"] == "speech-key-123"
    assert "authorization" not in request.headers
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    parts = _multipart(request)
    assert parts["audio"][0] == "audio.wav"                          # original (customer-identifying) name is not sent
    assert parts["audio"][1] == audio.read_bytes()
    assert _definition(request) == {
        "locales": ["en-US", "es-US"],                              # one locale per language
        "diarization": {"enabled": True, "maxSpeakers": 3},
        "phraseList": {"phrases": ["Contoso Equity Fund Class C", "Contoso Income Fund"]},
        "profanityFilterMode": "None",
    }


async def test_entra_auth_sends_bearer_token_and_caches_it(api, audio):
    route = api.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    cred = FakeCredential()
    t = await _transcriber(auth="entra", key=None, credential=cred)
    await t.transcribe(_request(audio))
    await t.transcribe(_request(audio))
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer fake-entra-token"
    assert "Ocp-Apim-Subscription-Key" not in request.headers
    assert cred.scopes == [(SPEECH_SCOPE,)]                          # token fetched once for both calls
    await t.aclose()
    assert cred.closed is False                                      # injected credentials are not ours to close


async def test_entra_rejects_regional_endpoint(audio):
    t = await _transcriber(endpoint="https://westus.api.cognitive.microsoft.com", auth="entra", key=None,
                           credential=FakeCredential())
    with pytest.raises(AzureSpeechError, match="custom-domain endpoint"):
        await t.transcribe(_request(audio))


async def test_key_auth_accepts_regional_endpoint(api, audio):
    regional = "https://westus.api.cognitive.microsoft.com"
    api.post(f"{regional}/speechtotext/transcriptions:transcribe").mock(return_value=httpx.Response(200, json=BODY))
    t = await _transcriber(endpoint=regional)
    assert (await t.transcribe(_request(audio))).segments


def test_key_auth_requires_a_key():
    with pytest.raises(ValueError, match="requires an API key"):
        AzureSpeechTranscriber(endpoint=ENDPOINT, auth="key", key="")


async def test_phrases_become_labelled_merged_segments(api, audio):
    api.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    result = await (await _transcriber()).transcribe(_request(audio))
    assert result.origin == TranscriptOrigin.azure_speech and result.provider == "azure_speech"
    assert result.audio_seconds == pytest.approx(12.48)
    assert result.language == "en-US"
    assert [(s.id, s.speaker_id, s.start_ms, s.end_ms, s.text) for s in result.segments] == [
        (0, "S1", 1000, 3800, "Hello, this is Sam. Thanks for joining."),   # speaker 2 spoke first -> S1
        (1, "S2", 4500, 5500, "Hi Sam, glad to be here."),
        (2, "S1", 8000, 9000, "Let's talk about the fund."),
    ]
    assert all(s.locale == "en-US" and s.confidence == pytest.approx(0.9) for s in result.segments)
    assert result.raw["speakers"] == 2 and result.raw["phrases"] == 4 and result.raw["segments"] == 3
    assert "Sam" not in json.dumps(result.raw)                       # raw diagnostics are transcript-free


async def test_merging_honours_locale_pause_and_max_segment_length(api, audio):
    body = {"durationMilliseconds": 60_000, "phrases": [
        _phrase(1, 0, 1000, "Hello there."),
        _phrase(1, 1200, 1000, "Hola, buenas tardes.", locale="es-US"),     # locale change splits
        _phrase(1, 2400, 1000, "Gracias por llamar.", locale="es-US"),
        _phrase(1, 3600, 25_000, "A very long monologue that keeps going.", locale="es-US"),
        _phrase(1, 29_000, 2500, "Still the same speaker.", locale="es-US"),   # would push the run past max_segment_ms
    ]}
    api.post(URL).mock(return_value=httpx.Response(200, json=body))
    result = await (await _transcriber(max_segment_ms=30_000)).transcribe(_request(audio))
    assert [s.text for s in result.segments] == [
        "Hello there.", "Hola, buenas tardes. Gracias por llamar. A very long monologue that keeps going.",
        "Still the same speaker.",
    ]
    assert result.language == "es-US"                                       # by characters spoken


async def test_unsorted_phrases_missing_speakers_and_blank_text(api, audio):
    body = {"durationMilliseconds": 9_000, "phrases": [
        _phrase(7, 5000, 800, "Second."),
        _phrase(None, 5900, 500, "Unlabelled continues the speaker."),
        _phrase(3, 0, 800, "First."),
        _phrase(3, 900, 500, "   "),
    ]}
    api.post(URL).mock(return_value=httpx.Response(200, json=body))
    result = await (await _transcriber()).transcribe(_request(audio))
    assert [(s.speaker_id, s.text) for s in result.segments] == [
        ("S1", "First."), ("S2", "Second. Unlabelled continues the speaker.")]


async def test_empty_result(api, audio):
    api.post(URL).mock(return_value=httpx.Response(200, json={"durationMilliseconds": 2000, "phrases": []}))
    result = await (await _transcriber()).transcribe(_request(audio, locales=["es-US"]))
    assert result.segments == [] and result.language == "es-US" and result.audio_seconds == 2.0


async def test_throttling_is_retried_honouring_retry_after(api, audio):
    sleeps = Sleeps()
    route = api.post(URL).mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "2"}, json={"code": "TooManyRequests", "message": "slow down"}),
        httpx.Response(503, json={"error": {"code": "ServiceUnavailable", "message": "busy"}}),
        httpx.Response(200, json=BODY),
    ])
    t = await _transcriber(sleep=sleeps, jitter=0, backoff_base=1.0)
    result = await t.transcribe(_request(audio))
    assert route.call_count == 3 and result.segments
    assert sleeps.delays == [2.0, 2.0]                               # Retry-After, then base * 2**1


async def test_transport_errors_are_retried(api, audio):
    route = api.post(URL).mock(side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json=BODY)])
    sleeps = Sleeps()
    await (await _transcriber(sleep=sleeps, jitter=0)).transcribe(_request(audio))
    assert route.call_count == 2 and sleeps.delays == [1.0]


async def test_gives_up_with_a_clear_error_after_max_retries(api, audio):
    route = api.post(URL).mock(return_value=httpx.Response(500, json={"error": {"code": "InternalServerError",
                                                                                "message": "kaboom"}}))
    t = await _transcriber(max_retries=2, jitter=0)
    with pytest.raises(AzureSpeechError, match="HTTP 500 InternalServerError: kaboom") as info:
        await t.transcribe(_request(audio))
    assert route.call_count == 3 and info.value.status_code == 500 and info.value.retryable


async def test_retry_after_longer_than_the_cap_fails_fast(api, audio):
    route = api.post(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "3600"}, json={}))
    with pytest.raises(AzureSpeechError, match="HTTP 429"):
        await (await _transcriber()).transcribe(_request(audio))
    assert route.call_count == 1


async def test_language_identification_error_is_actionable(api, audio):
    error = {"code": "InvalidRequest", "message": "bad request",
             "innerError": {"code": "NoLanguageIdentified",
                            "message": "Language Identification did not recognize any language."}}
    api.post(URL).mock(return_value=httpx.Response(400, json=error))
    with pytest.raises(AzureSpeechError, match="NoLanguageIdentified.*multilingual") as info:
        await (await _transcriber()).transcribe(_request(audio))
    assert info.value.status_code == 400 and not info.value.retryable


@pytest.mark.parametrize("status,needle", [(401, "Cognitive Services Speech User"), (403, "access denied"),
                                           (413, "250 MB"), (415, "media type")])
async def test_auth_and_payload_errors_name_the_fix(api, audio, status, needle):
    api.post(URL).mock(return_value=httpx.Response(status, json={"error": {"message": "nope"}}))
    with pytest.raises(AzureSpeechError, match=needle):
        await (await _transcriber()).transcribe(_request(audio))


async def test_stereo_is_merged_for_diarization_by_default(api, tmp_path):
    stereo = _wav(tmp_path / "stereo.wav", channels=2)
    route = api.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    result = await (await _transcriber()).transcribe(_request(stereo))
    definition = _definition(route.calls.last.request)
    assert "channels" not in definition and definition["diarization"]["enabled"] is True
    assert result.raw["audio_channels"] == 2 and result.raw["stereo_merged_to_mono"] is True


async def test_stereo_separate_mode_labels_speakers_by_channel(api, tmp_path):
    stereo = _wav(tmp_path / "stereo.wav", channels=2)
    body = {"durationMilliseconds": 5000, "phrases": [
        _phrase(None, 100, 900, "Agent greeting.", channel=1),
        _phrase(None, 1200, 900, "Customer reply.", channel=0),
        _phrase(None, 1300, 400, "Overlap.", channel=1),
    ]}
    route = api.post(URL).mock(return_value=httpx.Response(200, json=body))
    result = await (await _transcriber(stereo="separate")).transcribe(_request(stereo))
    definition = _definition(route.calls.last.request)
    assert definition["channels"] == [0, 1] and "diarization" not in definition
    assert [(s.speaker_id, s.text) for s in result.segments] == [
        ("S1", "Agent greeting."), ("S2", "Customer reply."), ("S1", "Overlap.")]
    assert result.raw["mode"] == "channels" and result.raw["stereo_merged_to_mono"] is False


async def test_mono_with_stereo_separate_still_uses_diarization(api, audio):
    route = api.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await (await _transcriber(stereo="separate")).transcribe(_request(audio))
    assert "diarization" in _definition(route.calls.last.request)


async def test_multilingual_mode_omits_candidate_locales(api, audio):
    route = api.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    t = await _transcriber(multilingual=True)
    await t.transcribe(_request(audio, locales=["en-US", "es-US"]))
    assert "locales" not in _definition(route.calls.last.request)
    await t.transcribe(_request(audio, locales=["es-US"]))               # a single locale is still sent
    assert _definition(route.calls.last.request)["locales"] == ["es-US"]


async def test_diarization_off_and_phrase_list_limits_and_biasing(api, audio):
    route = api.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    t = await _transcriber(max_phrases=3, max_phrase_chars=40, phrase_biasing_weight=1.5, profanity_filter_mode=None)
    names = ["Alpha Fund", "Beta Fund", "Gamma Fund", "Delta Fund"]
    await t.transcribe(_request(audio, diarize=False, phrase_list=names, locales=[]))
    definition = _definition(route.calls.last.request)
    assert definition == {"phraseList": {"phrases": ["Alpha Fund", "Beta Fund", "Gamma Fund"], "biasingWeight": 1.5}}


async def test_max_speakers_is_clamped_to_the_service_range(api, audio):
    route = api.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    t = await _transcriber()
    await t.transcribe(_request(audio, max_speakers=1))
    assert _definition(route.calls.last.request)["diarization"]["maxSpeakers"] == 2
    await t.transcribe(_request(audio, max_speakers=99))
    assert _definition(route.calls.last.request)["diarization"]["maxSpeakers"] == 35


async def test_audio_file_problems_are_reported_before_any_request(api, tmp_path):
    route = api.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    t = await _transcriber(max_audio_bytes=100)
    with pytest.raises(AzureSpeechError, match="not found"):
        await t.transcribe(_request(tmp_path / "missing.wav"))
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    with pytest.raises(AzureSpeechError, match="empty"):
        await t.transcribe(_request(empty))
    big = _wav(tmp_path / "big.wav", seconds=1.0)
    with pytest.raises(AzureSpeechError, match="at most"):
        await t.transcribe(_request(big))
    assert route.call_count == 0


async def test_non_json_success_body_is_an_error(api, audio):
    api.post(URL).mock(return_value=httpx.Response(200, text="<html>gateway</html>"))
    with pytest.raises(AzureSpeechError, match="non-JSON"):
        await (await _transcriber()).transcribe(_request(audio))


async def test_missing_duration_falls_back_to_wav_header(api, tmp_path):
    wav = _wav(tmp_path / "three.wav", seconds=3.0)
    api.post(URL).mock(return_value=httpx.Response(200, json={"phrases": []}))
    result = await (await _transcriber()).transcribe(_request(wav))
    assert result.audio_seconds == pytest.approx(3.0, abs=0.01)


async def test_owned_http_client_is_created_lazily_and_closed(api, audio):
    api.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    async with AzureSpeechTranscriber(endpoint=ENDPOINT, key="k", auth="key") as t:
        assert (await t.transcribe(_request(audio))).segments
        client = t._client
        assert client is not None and not client.is_closed
    assert client.is_closed
