"""Azure text-to-speech client: SSML, auth, errors and retries (HTTP mocked)."""
from __future__ import annotations

import httpx
import pytest
import respx

from voicecrm.adapters.transcription.azure_tts import (
    WAV_8K,
    AzureSpeechSynthesizer,
    AzureTtsError,
    build_ssml,
    voice_locale,
)

ENDPOINT = "https://voicecrm-speech.cognitiveservices.azure.com"
URL = f"{ENDPOINT}/tts/cognitiveservices/v1"
WAV = b"RIFF....WAVEfmt "


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _tts(**kw) -> AzureSpeechSynthesizer:
    kw.setdefault("endpoint", ENDPOINT)
    kw.setdefault("auth", "key")
    kw.setdefault("key", "speech-key-123")
    kw.setdefault("http_client", httpx.AsyncClient())
    kw.setdefault("sleep", Sleeps())
    kw.setdefault("jitter", 0)
    return AzureSpeechSynthesizer(**kw)


@pytest.fixture()
def api():
    with respx.mock(assert_all_called=False) as router:
        yield router


def test_voice_locale_comes_from_the_voice_name():
    assert voice_locale("en-US-JennyNeural") == "en-US"
    assert voice_locale("es-US-PalomaNeural") == "es-US"
    assert voice_locale("custom") == "en-US"


def test_ssml_escapes_text_and_names_the_voice_and_language():
    ssml = build_ssml('Fees < 1% & "class C"', "es-US-PalomaNeural", rate="-5%", style="friendly")
    assert "Fees &lt; 1% &amp; \"class C\"" in ssml
    assert "xml:lang=\"es-US\"" in ssml and "<voice name=\"es-US-PalomaNeural\">" in ssml
    assert "<prosody rate=\"-5%\">" in ssml and "<mstts:express-as style=\"friendly\">" in ssml
    assert "<prosody" not in build_ssml("hi", "en-US-GuyNeural")


async def test_request_shape_key_auth_and_format(api):
    route = api.post(URL).mock(return_value=httpx.Response(200, content=WAV))
    audio = await _tts().synthesize("Hello Dana", voice="en-US-GuyNeural", output_format=WAV_8K)
    assert audio == WAV
    request = route.calls[0].request
    assert request.headers["Ocp-Apim-Subscription-Key"] == "speech-key-123"
    assert request.headers["X-Microsoft-OutputFormat"] == WAV_8K
    assert request.headers["Content-Type"] == "application/ssml+xml"
    assert b"Hello Dana" in request.content and b"en-US-GuyNeural" in request.content


async def test_throttling_is_retried_honouring_retry_after(api):
    route = api.post(URL).mock(side_effect=[httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200, content=WAV)])
    sleeps = Sleeps()
    assert await _tts(sleep=sleeps).synthesize("hi") == WAV
    assert route.call_count == 2 and sleeps.delays == [3.0]


@pytest.mark.parametrize("status, needle", [(401, "VOICECRM_SPEECH_KEY"), (403, "access denied"), (400, "voice")])
async def test_failures_name_the_fix_and_never_echo_the_text(api, status, needle):
    api.post(URL).mock(return_value=httpx.Response(status, json={"error": {"code": "Bad", "message": "nope"}}))
    with pytest.raises(AzureTtsError) as info:
        await _tts().synthesize("a confidential sentence about a client")
    assert str(status) in str(info.value) and needle in str(info.value)
    assert "confidential" not in str(info.value) and info.value.status_code == status


async def test_empty_text_and_empty_audio_are_errors(api):
    with pytest.raises(AzureTtsError, match="empty"):
        await _tts().synthesize("   ")
    api.post(URL).mock(return_value=httpx.Response(200, content=b""))
    with pytest.raises(AzureTtsError, match="no audio"):
        await _tts().synthesize("hi")


async def test_entra_needs_the_custom_domain_endpoint():
    tts = AzureSpeechSynthesizer(endpoint="https://eastus.api.cognitive.microsoft.com", auth="entra",
                                 credential=object(), http_client=httpx.AsyncClient())
    with pytest.raises(AzureTtsError, match="custom-domain"):
        await tts.synthesize("hi")


def test_configuration_errors_are_clear():
    with pytest.raises(ValueError, match="VOICECRM_SPEECH_ENDPOINT"):
        AzureSpeechSynthesizer(endpoint="", auth="key", key="k")
    with pytest.raises(ValueError, match="key"):
        AzureSpeechSynthesizer(endpoint=ENDPOINT, auth="key", key=None)
