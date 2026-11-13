"""Azure Speech fast transcription adapter (REST, GA api-version 2025-10-15).

``POST {endpoint}/speechtotext/transcriptions:transcribe?api-version=2025-10-15`` as multipart/form-data:

* ``audio``      the recording (WAV, MP3, OGG/OPUS, FLAC, ...; the REST reference caps it at 2 h / 250 MB)
* ``definition`` JSON: ``locales`` (candidate locales for language identification), ``diarization``
  (``enabled``, ``maxSpeakers`` 2-35), ``channels``, ``phraseList`` (``phrases``, ``biasingWeight``) and
  ``profanityFilterMode``

Auth: Entra bearer token (scope ``https://cognitiveservices.azure.com/.default``; needs the resource's custom-domain
endpoint, never a regional one) or the ``Ocp-Apim-Subscription-Key`` header.

Mono vs stereo: diarization separates speakers inside ONE channel, so it works on mono audio; the service merges
stereo to mono before diarizing. For dual-channel recordings (one party per channel) set ``stereo="separate"`` to
transcribe channels independently and label speakers by channel instead.

Language identification picks ONE main language per file; for code-switched calls pass ``multilingual=True`` to omit
the candidate locales and use the service's multilingual model (see its supported-locale list).

``TranscriptionResult.raw`` holds text-free diagnostics only (counts, durations, request id) so it is safe to persist.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

import httpx

from ...domain.conversation import Segment
from ...domain.enums import TranscriptOrigin
from ...ports.transcription import NoSpeechError, TranscriptionPort, TranscriptionRequest, TranscriptionResult
from ._azure_common import (
    RETRYABLE_STATUS,
    AzureAuth,
    AzureServiceError,
    Sleep,
    endpoint_host,
    error_details,
    send_with_retry,
)
from .wav import WavInfo, read_wav_info

logger = logging.getLogger(__name__)

API_VERSION = "2025-10-15"
SPEECH_SCOPE = "https://cognitiveservices.azure.com/.default"
MAX_AUDIO_BYTES = 250 * 1024 * 1024
_REGIONAL_HOST_SUFFIXES = (".cognitive.microsoft.com", ".speech.microsoft.com")

_ERROR_HINTS = {
    "NoLanguageIdentified": "pass the expected locale(s) explicitly, or enable multilingual mode",
    "MultipleLanguagesIdentified": "pass a single locale, or enable multilingual mode for code-switched audio",
    "InvalidAudioFormat": "use WAV, MP3, OGG/OPUS, FLAC or another supported codec",
    "BadChannelConfiguration": "diarization needs mono audio; use stereo='separate' for dual-channel recordings",
    "InvalidChannelSpecification": "diarization needs mono audio; use stereo='separate' for dual-channel recordings",
    "EmptyAudioFile": "the audio file contains no audio",
    "AudioLengthLimitExceeded": "the recording is longer than the 2 hour limit; split it first",
}
_STATUS_HINTS = {
    401: "authentication failed - with Entra the identity needs the 'Cognitive Services Speech User' role on the "
         "resource and the custom-domain endpoint; with a key check VOICECRM_SPEECH_KEY",
    403: "access denied - assign 'Cognitive Services Speech User' (or 'Cognitive Services User') to the identity",
    413: "audio file too large (limit 250 MB)",
    415: "unsupported audio media type",
}


class AzureSpeechError(AzureServiceError):
    """Speech fast transcription failed (bad config, rejected audio, throttling or service error)."""


@dataclass
class _Phrase:
    start: int
    end: int
    text: str
    locale: str | None
    confidence: float | None
    speaker_key: Any


@dataclass
class _Run:
    speaker: str
    start: int
    end: int
    texts: list[str]
    locale: str | None
    weighted_conf: float = 0.0
    conf_ms: int = 0

    @classmethod
    def begin(cls, speaker: str, p: _Phrase) -> _Run:
        run = cls(speaker=speaker, start=p.start, end=p.end, texts=[], locale=p.locale)
        run.add(p)
        return run

    def add(self, p: _Phrase) -> None:
        self.end = max(self.end, p.end)
        self.texts.append(p.text)
        if p.confidence is not None:                      # duration-weighted mean confidence
            weight = max(1, p.end - p.start)
            self.weighted_conf += p.confidence * weight
            self.conf_ms += weight


class AzureSpeechTranscriber(TranscriptionPort):
    name = "azure_speech"

    def __init__(
        self,
        *,
        endpoint: str,
        key: str | None = None,
        auth: Literal["entra", "key"] = "entra",
        credential: Any | None = None,
        api_version: str = API_VERSION,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 300.0,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        jitter: float = 0.1,
        sleep: Sleep = asyncio.sleep,
        pause_threshold_ms: int = 1500,
        max_segment_ms: int = 30_000,
        max_phrases: int = 200,
        max_phrase_chars: int = 3200,
        phrase_biasing_weight: float | None = None,
        profanity_filter_mode: str | None = "None",
        multilingual: bool = False,
        stereo: Literal["merge", "separate"] = "merge",
        max_audio_bytes: int = MAX_AUDIO_BYTES,
    ) -> None:
        if not endpoint:
            raise ValueError("AzureSpeechTranscriber needs the Speech resource endpoint (VOICECRM_SPEECH_ENDPOINT)")
        self._endpoint = endpoint.rstrip("/")
        self._auth = AzureAuth(service="Azure Speech", mode=auth, key=key, scope=SPEECH_SCOPE,
                               key_header="Ocp-Apim-Subscription-Key", credential=credential)
        self._api_version = api_version
        self._client = http_client
        self._owns_client = http_client is None
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._jitter = jitter
        self._sleep = sleep
        self._pause_ms = pause_threshold_ms
        self._max_segment_ms = max_segment_ms
        self._max_phrases = max_phrases
        self._max_phrase_chars = max_phrase_chars
        self._biasing_weight = phrase_biasing_weight
        self._profanity = profanity_filter_mode
        self._multilingual = multilingual
        self._stereo = stereo
        self._max_audio_bytes = max_audio_bytes
        if self._regional_endpoint_with_entra():
            logger.warning("Speech endpoint %s is a regional endpoint; Entra auth needs the custom-domain endpoint",
                           endpoint_host(self._endpoint))

    # ---- public API -------------------------------------------------------------------------------------------
    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        if self._regional_endpoint_with_entra():
            raise AzureSpeechError(
                "Entra authentication needs the Speech resource's custom-domain endpoint "
                "(https://<name>.cognitiveservices.azure.com), not a regional endpoint; fix VOICECRM_SPEECH_ENDPOINT "
                "or set VOICECRM_AZURE_AUTH=key", service="Azure Speech")
        path = Path(request.audio.path)
        self._check_audio_file(path)
        wav = read_wav_info(path)
        definition, mode = self._build_definition(request, wav)
        logger.info("azure speech transcription: locales=%s mode=%s phrases=%d audio_channels=%s",
                    definition.get("locales", "auto"), mode, len(definition.get("phraseList", {}).get("phrases", [])),
                    wav.channels if wav else "?")
        started = time.perf_counter()
        response = await send_with_retry(
            lambda: self._post(path, request, definition), service="Azure Speech", max_retries=self._max_retries,
            backoff_base=self._backoff_base, backoff_cap=self._backoff_cap, sleep=self._sleep, jitter=self._jitter)
        latency_ms = round((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            error = self._error(response)
            if error.code == "NoLanguageIdentified":
                # Silence, a hang-up or noise. Do not retry with a forced locale: that makes the service invent words
                # ("Yes.", "A.") out of noise, which would become a note about a call nobody spoke in.
                logger.info("azure speech identified no language in %.1f s of audio: the recording has no speech",
                            wav.duration_seconds if wav else 0.0)
                raise NoSpeechError() from error
            raise error
        try:
            body = response.json()
        except ValueError as exc:
            raise AzureSpeechError("Azure Speech returned a non-JSON response", service="Azure Speech",
                                   status_code=response.status_code) from exc
        result = self._to_result(body, request, wav, mode, definition, latency_ms, response)
        logger.info("azure speech transcription done: %d segments, %.1fs audio, %d ms", len(result.segments),
                    result.audio_seconds, latency_ms)
        return result

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
        await self._auth.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ---- request ----------------------------------------------------------------------------------------------
    def _regional_endpoint_with_entra(self) -> bool:
        return self._auth.mode == "entra" and endpoint_host(self._endpoint).endswith(_REGIONAL_HOST_SUFFIXES)

    def _check_audio_file(self, path: Path) -> None:
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise AzureSpeechError(f"Audio file not found or unreadable: {path.name}", service="Azure Speech") from exc
        if size == 0:
            raise AzureSpeechError(f"Audio file is empty: {path.name}", service="Azure Speech")
        if size > self._max_audio_bytes:
            raise AzureSpeechError(
                f"Audio file is {size / 1_048_576:.0f} MB; fast transcription accepts at most "
                f"{self._max_audio_bytes / 1_048_576:.0f} MB - split or compress the recording", service="Azure Speech")

    def _build_definition(self, request: TranscriptionRequest, wav: WavInfo | None) -> tuple[dict[str, Any], str]:
        definition: dict[str, Any] = {}
        locales = _one_locale_per_language(request.locales)
        if locales and not (self._multilingual and len(locales) > 1):
            definition["locales"] = locales
        mode = "single"
        if wav is not None and wav.is_stereo and (self._stereo == "separate" or request.separate_channels):
            definition["channels"] = [0, 1]
            mode = "channels"
        elif request.diarize:
            definition["diarization"] = {"enabled": True, "maxSpeakers": min(35, max(2, request.max_speakers))}
            mode = "diarization"
        phrases = _clean_phrases(request.phrase_list, self._max_phrases, self._max_phrase_chars)
        if phrases:
            definition["phraseList"] = {"phrases": phrases}
            if self._biasing_weight is not None:
                definition["phraseList"]["biasingWeight"] = self._biasing_weight
        if self._profanity is not None:
            definition["profanityFilterMode"] = self._profanity
        return definition, mode

    async def _post(self, path: Path, request: TranscriptionRequest, definition: dict[str, Any]) -> httpx.Response:
        client = self._http()
        headers = await self._auth.headers()          # fresh header per attempt (tokens refresh)
        # never send the original file name: it may identify the customer
        upload_name = "audio" + (path.suffix.lower() or ".wav")
        with path.open("rb") as audio:
            return await client.post(
                f"{self._endpoint}/speechtotext/transcriptions:transcribe",
                params={"api-version": self._api_version},
                headers=headers,
                data={"definition": json.dumps(definition, ensure_ascii=False)},
                files={"audio": (upload_name, audio, request.audio.content_type)},
            )

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout, connect=15.0))
        return self._client

    def _error(self, response: httpx.Response) -> AzureSpeechError:
        code, message = error_details(response)
        status = response.status_code
        hint = _ERROR_HINTS.get(code or "") or _STATUS_HINTS.get(status)
        if status == 429:
            hint = "throttled after retries; see the Speech throttling guidance and Retry-After"
        text = f"Azure Speech fast transcription failed: HTTP {status}" + (f" {code}" if code else "") + f": {message}"
        if hint:
            text += f" ({hint})"
        return AzureSpeechError(text, service="Azure Speech", status_code=status, code=code,
                                retryable=status in RETRYABLE_STATUS)

    # ---- response ---------------------------------------------------------------------------------------------
    def _to_result(
        self, body: dict[str, Any], request: TranscriptionRequest, wav: WavInfo | None, mode: str,
        definition: dict[str, Any], latency_ms: int, response: httpx.Response,
    ) -> TranscriptionResult:
        phrases = _parse_phrases(body.get("phrases") or [], by_channel=(mode == "channels"))
        labels: dict[Any, str] = {}
        if mode == "channels":                   # the caller knows who is on which channel: use their ids, not "S1"/"S2"
            for p in phrases:
                key = p.speaker_key
                if isinstance(key, int) and 0 <= key < len(request.channel_speakers) and request.channel_speakers[key]:
                    labels.setdefault(key, request.channel_speakers[key])
        for p in phrases:
            labels.setdefault(p.speaker_key, f"S{len(labels) + 1}")
        segments = self._merge(phrases, labels)
        duration_ms = body.get("durationMilliseconds")
        if isinstance(duration_ms, (int, float)) and duration_ms > 0:
            audio_seconds = duration_ms / 1000.0
        elif wav is not None:
            audio_seconds = wav.duration_seconds
        else:
            audio_seconds = request.audio.duration_seconds or (segments[-1].end_ms / 1000.0 if segments else 0.0)
        raw = {
            "api_version": self._api_version,
            "mode": mode,
            "audio_channels": wav.channels if wav else None,
            "stereo_merged_to_mono": bool(wav and wav.is_stereo and mode == "diarization"),
            "duration_ms": duration_ms,
            "phrases": len(phrases),
            "segments": len(segments),
            "speakers": len(labels),
            "locales": dict(Counter(p.locale for p in phrases if p.locale)),
            "phrase_list_size": len(definition.get("phraseList", {}).get("phrases", [])),
            "latency_ms": latency_ms,
            "request_id": response.headers.get("apim-request-id") or response.headers.get("x-request-id"),
        }
        return TranscriptionResult(
            segments=segments, language=_dominant_locale(segments, request), origin=TranscriptOrigin.azure_speech,
            audio_seconds=float(audio_seconds), provider=self.name, raw=raw)

    def _merge(self, phrases: list[_Phrase], labels: dict[Any, str]) -> list[Segment]:
        """Joins consecutive same-speaker, same-locale phrases separated by <= the pause threshold into segments."""
        runs: list[_Run] = []
        for p in phrases:
            speaker = labels[p.speaker_key]
            last = runs[-1] if runs else None
            if (last and last.speaker == speaker and last.locale == p.locale
                    and p.start - last.end <= self._pause_ms and max(last.end, p.end) - last.start <= self._max_segment_ms):
                last.add(p)
                continue
            runs.append(_Run.begin(speaker, p))
        return [
            Segment(id=i, speaker_id=r.speaker, start_ms=r.start, end_ms=r.end, text=" ".join(r.texts), locale=r.locale,
                    confidence=round(r.weighted_conf / r.conf_ms, 4) if r.conf_ms else None)
            for i, r in enumerate(runs)
        ]


def _parse_phrases(raw_phrases: list[dict[str, Any]], *, by_channel: bool) -> list[_Phrase]:
    parsed: list[_Phrase] = []
    for raw in raw_phrases:
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        start = int(raw.get("offsetMilliseconds") or 0)
        end = start + int(raw.get("durationMilliseconds") or 0)
        key = raw.get("channel") if by_channel else raw.get("speaker")
        parsed.append(_Phrase(start, end, text, raw.get("locale"), raw.get("confidence"), key))
    parsed.sort(key=lambda p: (p.start, p.end))
    for i, p in enumerate(parsed):                         # a phrase without a label continues the previous speaker
        if p.speaker_key is None and i:
            p.speaker_key = parsed[i - 1].speaker_key
    return parsed


def _dominant_locale(segments: list[Segment], request: TranscriptionRequest) -> str:
    weight: Counter[str] = Counter()
    for s in segments:
        if s.locale:
            weight[s.locale] += len(s.text)
    if weight:
        return weight.most_common(1)[0][0]
    return request.locales[0] if request.locales else "en-US"


def _one_locale_per_language(locales: list[str]) -> list[str]:
    """Language identification must not get two locales of one language (en-US and en-GB): keep the first."""
    seen: set[str] = set()
    out: list[str] = []
    for locale in (loc.strip() for loc in locales):
        base = locale.split("-")[0].lower()
        if locale and base not in seen:
            seen.add(base)
            out.append(locale)
    return out


def _clean_phrases(phrases: list[str], max_phrases: int, max_chars: int) -> list[str]:
    """Dedupes and caps the phrase list to the service limits (200 phrases / 3,200 characters)."""
    out: list[str] = []
    seen: set[str] = set()
    total = 0
    for phrase in phrases:
        cleaned = " ".join(phrase.split())
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        if len(out) >= max_phrases or total + len(cleaned) + 1 > max_chars:
            break
        seen.add(key)
        out.append(cleaned)
        total += len(cleaned) + 1
    return out
