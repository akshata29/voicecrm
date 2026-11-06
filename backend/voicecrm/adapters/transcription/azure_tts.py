"""Azure Speech text-to-speech (REST), used to render scripted calls into real speech audio for live demos and checks.

``POST {endpoint}/tts/cognitiveservices/v1`` with an SSML body returns the audio in the requested ``X-Microsoft-OutputFormat``.
It is not part of the pipeline: the pipeline only *transcribes*. This client exists so that a demo (or the preflight check)
can produce genuine speech without a recording, which then goes through the real speech-to-text path.

Auth, retry and error handling are shared with the other live adapters (Entra bearer with the custom-domain endpoint, or
the resource key). Neural voices are rate limited per resource, so callers should keep concurrency modest; a 429 is retried
with the service's Retry-After.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal, Self
from xml.sax.saxutils import escape, quoteattr

import httpx

from ._azure_common import (
    RETRYABLE_STATUS,
    AzureAuth,
    AzureServiceError,
    Sleep,
    endpoint_host,
    error_details,
    send_with_retry,
)

logger = logging.getLogger(__name__)

SPEECH_SCOPE = "https://cognitiveservices.azure.com/.default"
_REGIONAL_HOST_SUFFIXES = (".cognitive.microsoft.com", ".speech.microsoft.com")
WAV_16K = "riff-16khz-16bit-mono-pcm"
WAV_8K = "riff-8khz-16bit-mono-pcm"

_STATUS_HINTS = {
    400: "the SSML or voice name was rejected - check the voice is available in the resource's region",
    401: "authentication failed - with Entra assign 'Cognitive Services Speech User' and use the custom-domain endpoint; "
         "with a key check VOICECRM_SPEECH_KEY",
    403: "access denied - check the identity's role and the resource's network rules",
    429: "throttled after retries - text-to-speech is limited per resource; lower the concurrency",
}


class AzureTtsError(AzureServiceError):
    """Text-to-speech failed (bad voice, auth, throttling or service error). The message never contains the text."""


def voice_locale(voice: str) -> str:
    """``en-US-JennyNeural`` -> ``en-US``; falls back to en-US for names that do not follow the pattern."""
    parts = voice.split("-")
    return "-".join(parts[:2]) if len(parts) >= 3 else "en-US"


def build_ssml(text: str, voice: str, *, rate: str | None = None, style: str | None = None) -> str:
    """SSML for one utterance. ``rate`` is e.g. ``"-5%"``; ``style`` an ``mstts:express-as`` style the voice supports."""
    body = escape(text)
    if rate:
        body = f"<prosody rate={quoteattr(rate)}>{body}</prosody>"
    if style:
        body = f"<mstts:express-as style={quoteattr(style)}>{body}</mstts:express-as>"
    return (
        f"<speak version='1.0' xml:lang={quoteattr(voice_locale(voice))} xmlns='http://www.w3.org/2001/10/synthesis' "
        f"xmlns:mstts='https://www.w3.org/2001/mstts'><voice name={quoteattr(voice)}>{body}</voice></speak>"
    )


class AzureSpeechSynthesizer:
    name = "azure_tts"

    def __init__(
        self,
        *,
        endpoint: str,
        key: str | None = None,
        auth: Literal["entra", "key"] = "entra",
        credential: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 60.0,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        jitter: float = 0.1,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not endpoint:
            raise ValueError("AzureSpeechSynthesizer needs the Speech resource endpoint (VOICECRM_SPEECH_ENDPOINT)")
        self._endpoint = endpoint.rstrip("/")
        self._auth = AzureAuth(service="Azure Speech", mode=auth, key=key, scope=SPEECH_SCOPE,
                               key_header="Ocp-Apim-Subscription-Key", credential=credential)
        self._client = http_client
        self._owns_client = http_client is None
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._jitter = jitter
        self._sleep = sleep

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
        await self._auth.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def synthesize(
        self, text: str, *, voice: str = "en-US-JennyNeural", output_format: str = WAV_16K, rate: str | None = None,
        style: str | None = None,
    ) -> bytes:
        """Audio bytes (a WAV container for the ``riff-*`` formats). Raises ``AzureTtsError``."""
        if not text.strip():
            raise AzureTtsError("nothing to synthesize: the text is empty", service="Azure Speech")
        if self._auth.mode == "entra" and endpoint_host(self._endpoint).endswith(_REGIONAL_HOST_SUFFIXES):
            raise AzureTtsError(
                "Entra authentication needs the Speech resource's custom-domain endpoint "
                "(https://<name>.cognitiveservices.azure.com), not a regional endpoint", service="Azure Speech")
        ssml = build_ssml(text, voice, rate=rate, style=style)
        response = await send_with_retry(
            lambda: self._post(ssml, output_format), service="Azure Speech", max_retries=self._max_retries,
            backoff_base=self._backoff_base, backoff_cap=self._backoff_cap, sleep=self._sleep, jitter=self._jitter)
        if response.status_code != 200:
            code, message = error_details(response)
            hint = _STATUS_HINTS.get(response.status_code)
            detail = f"Azure text-to-speech failed: HTTP {response.status_code}" + (f" {code}" if code else "")
            detail += f": {message}" + (f" ({hint})" if hint else "")
            raise AzureTtsError(detail, service="Azure Speech", status_code=response.status_code, code=code,
                                retryable=response.status_code in RETRYABLE_STATUS)
        if not response.content:
            raise AzureTtsError("Azure text-to-speech returned no audio", service="Azure Speech")
        return response.content

    async def _post(self, ssml: str, output_format: str) -> httpx.Response:
        headers = await self._auth.headers()
        headers.update({"Content-Type": "application/ssml+xml", "X-Microsoft-OutputFormat": output_format,
                        "User-Agent": "voicecrm"})
        return await self._http().post(f"{self._endpoint}/tts/cognitiveservices/v1", headers=headers,
                                       content=ssml.encode("utf-8"))

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout, connect=15.0))
        return self._client
