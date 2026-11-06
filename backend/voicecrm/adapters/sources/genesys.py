"""Genesys Cloud: Speech & Text Analytics transcripts, notification payloads, and a small API client.

Transcript JSON (developer.genesys.cloud "Transcript URL"): `transcripts[].phrases[]`, each with `participantPurpose`
(`internal` = the agent side, `external` = the customer side), `text`, `confidence`, `offset.milliseconds` and/or an epoch
`startTimeMs` (relative to `conversationStartTime`), `duration.milliseconds`, and `words[]` with the same timing fields.
Segments get the raw side as speaker_id: `agent` (internal) or `customer` (external).

API flow: client-credentials OAuth (`POST https://login.<environment>/oauth/token`, Basic auth, `grant_type=
client_credentials`), then `GET /api/v2/speechandtextanalytics/conversations/{conversationId}/communications/
{communicationId}/transcripturl` -> `{"url": ...}`. The pre-signed URL expires after 10 minutes, so it is downloaded
immediately (and without the Authorization header, which a pre-signed URL rejects).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from ...domain.base import VModel
from ...domain.conversation import Segment
from ...domain.enums import Channel
from ...domain.fixtures import ScriptedCall
from .common import UNKNOWN_SPEAKER, communication_id, interaction_id, parse_started_at, turn_timings

logger = logging.getLogger(__name__)

_SIDE = {"internal": "agent", "agent": "agent", "external": "customer", "customer": "customer", "user": "customer"}
_UUID_RE = r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}"
_TOPIC_CONVERSATION_RE = re.compile(rf"conversations?\.({_UUID_RE})", re.IGNORECASE)
_EPOCH_MS_FLOOR = 10**11            # anything above is an absolute epoch timestamp, not an offset
_ENDED_STATES = frozenset({"disconnected", "terminated", "ended", "end"})


class GenesysError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Genesys {status}: {message}" if status else f"Genesys request failed: {message}")
        self.status = status


# ---- transcript ----------------------------------------------------------------------------------------------------
def _ms(value: Any) -> int | None:
    if isinstance(value, Mapping):
        value = value.get("milliseconds")
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _phrase_text(phrase: Mapping[str, Any]) -> str:
    text = phrase.get("text")
    if not text and isinstance(phrase.get("words"), list):
        text = " ".join(str(w.get("word", "")) for w in phrase["words"] if isinstance(w, Mapping))
    if not text and isinstance(phrase.get("alternatives"), list) and phrase["alternatives"]:
        first = phrase["alternatives"][0]
        text = first.get("transcript") if isinstance(first, Mapping) else None
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _epoch_origin(data: Mapping[str, Any]) -> int | None:
    for key in ("conversationStartTime", "startTime"):
        value = _ms(data.get(key))
        if value and value >= _EPOCH_MS_FLOOR:
            return value
    return None


def _phrase_times(phrase: Mapping[str, Any], origin: int | None) -> tuple[int, int]:
    start = _ms(phrase.get("offset")) if phrase.get("offset") is not None else _ms(phrase.get("offsetMs"))
    if start is None:
        raw = _ms(phrase.get("startTimeMs"))
        start = 0 if raw is None else (max(0, raw - origin) if origin and raw >= _EPOCH_MS_FLOOR else raw)
    duration = _ms(phrase.get("duration")) if phrase.get("duration") is not None else _ms(phrase.get("durationMs"))
    return start, start + (duration or 0)


def parse_genesys_transcript(data: Mapping[str, Any]) -> list[Segment]:
    """Speech & Text Analytics transcript JSON -> segments in time order (speaker_id 'agent' / 'customer')."""
    origin = _epoch_origin(data)
    collected: list[tuple[int, int, str, str]] = []
    for transcript in data.get("transcripts") or []:
        if not isinstance(transcript, Mapping):
            continue
        for phrase in transcript.get("phrases") or []:
            if not isinstance(phrase, Mapping):
                continue
            text = _phrase_text(phrase)
            if not text:
                continue
            purpose = str(phrase.get("participantPurpose") or transcript.get("participantPurpose") or "").lower()
            side = _SIDE.get(purpose, purpose or UNKNOWN_SPEAKER)
            start, end = _phrase_times(phrase, origin)
            collected.append((start, end, side, text))
    collected.sort(key=lambda item: item[0])                      # stable: equal starts keep their order
    return [
        Segment(id=index, speaker_id=side, start_ms=start, end_ms=end, text=text)
        for index, (start, end, side, text) in enumerate(collected)
    ]


def genesys_meta(data: Mapping[str, Any]) -> dict[str, Any]:
    """Interaction id and call facts in a transcript JSON, for the pipeline's metadata hint."""
    meta: dict[str, Any] = {}
    if data.get("conversationId"):
        meta["source_interaction_id"] = str(data["conversationId"])
    if data.get("communicationId"):
        meta["communication_id"] = str(data["communicationId"])
    origin = _epoch_origin(data)
    if origin:
        meta["started_at"] = datetime.fromtimestamp(origin / 1000, tz=timezone.utc).isoformat()
    duration = _ms(data.get("duration"))
    if duration:
        meta["duration_seconds"] = duration / 1000
    for participant in data.get("participants") or []:
        if isinstance(participant, Mapping) and str(participant.get("participantPurpose", "")).lower() == "customer":
            direction = str(participant.get("initialDirection") or "").lower()
            if direction in ("inbound", "outbound"):
                meta["direction"] = direction
    return meta


def render_genesys_transcript(call: ScriptedCall) -> dict[str, Any]:
    """The call as a Speech & Text Analytics transcript: phrases with offsets, absolute times and word timings."""
    started = parse_started_at(call)
    origin = int(started.timestamp() * 1000)
    phrases = []
    for turn, (start, end) in zip(call.turns, turn_timings(call), strict=True):
        purpose = "internal" if turn.speaker in ("seller", "internal") else "external"
        words = turn.text.split()
        step = (end - start) / max(len(words), 1)
        phrases.append({
            "participantPurpose": purpose, "text": turn.text, "stability": 1.0, "confidence": 0.95,
            "offset": {"milliseconds": start}, "startTimeMs": origin + start, "duration": {"milliseconds": end - start},
            "words": [
                {"word": word, "confidence": 0.95, "startTimeMs": origin + start + int(i * step),
                 "duration": {"milliseconds": int(step)}}
                for i, word in enumerate(words)
            ],
            "alternatives": [],
        })
    direction = "inbound" if call.direction.value == "inbound" else "outbound"
    return {
        "conversationId": interaction_id(call, Channel.genesys), "communicationId": communication_id(call),
        "mediaType": "call", "conversationStartTime": origin, "startTime": origin,
        "duration": {"milliseconds": call.duration_minutes * 60_000},
        "participants": [
            {"participantPurpose": "customer", "initialDirection": direction},
            {"participantPurpose": "agent", "initialDirection": direction},
        ],
        "transcripts": [{"phrases": phrases, "analytics": {"sentiment": [], "topics": []}}],
    }


# ---- notifications ---------------------------------------------------------------------------------------------------
class GenesysNotification(VModel):
    conversation_id: str
    communication_id: str | None = None
    topic: str | None = None
    event_time: str | None = None
    ended: bool = False                            # the payload says the conversation / call ended


def _envelope(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], str | None]:
    """(event body, topic) for Notification API, EventBridge and plain Architect / data action payloads."""
    detail = payload.get("detail") if isinstance(payload.get("detail"), Mapping) else None
    source = detail or payload
    body = source.get("eventBody")
    topic = source.get("topicName") or payload.get("topicName") or payload.get("detail-type")
    return (body if isinstance(body, Mapping) else source), (str(topic) if topic else None)


def _find_communication(body: Mapping[str, Any]) -> tuple[str | None, bool]:
    ended = False
    found: str | None = None
    for participant in body.get("participants") or []:
        if not isinstance(participant, Mapping):
            continue
        purpose = str(participant.get("purpose") or "").lower()
        for call in participant.get("calls") or []:
            if not isinstance(call, Mapping):
                continue
            ended = ended or str(call.get("state") or "").lower() in _ENDED_STATES or bool(call.get("disconnectedTime"))
            if call.get("id") and (found is None or purpose == "customer"):
                found = str(call["id"])
    return found, ended


def _looks_like_conversation(body: Mapping[str, Any]) -> bool:
    """Notification bodies that are the conversation itself: a UUID `id` plus participants."""
    identifier = body.get("id")
    return isinstance(identifier, str) and re.fullmatch(_UUID_RE, identifier) is not None and bool(body.get("participants"))


def handle_notification(payload: Mapping[str, Any]) -> GenesysNotification | None:
    """Conversation / communication ids from a Genesys notification; None when the payload names no conversation."""
    if not isinstance(payload, Mapping):
        return None
    body, topic = _envelope(payload)
    conversation_id = body.get("conversationId") or body.get("conversation_id")
    if not conversation_id and topic and (match := _TOPIC_CONVERSATION_RE.search(topic)):
        conversation_id = match.group(1)
    if not conversation_id and _looks_like_conversation(body):
        conversation_id = body["id"]
    if not conversation_id:
        return None
    communication, ended = _find_communication(body)
    communication = body.get("communicationId") or body.get("communication_id") or communication
    return GenesysNotification(
        conversation_id=str(conversation_id), communication_id=str(communication) if communication else None,
        topic=topic, event_time=str(body.get("eventTime") or payload.get("time") or "") or None,
        ended=ended or bool(body.get("endTime") or body.get("conversationEnd")),
    )


# ---- client ----------------------------------------------------------------------------------------------------------
class GenesysClient:
    """Client-credentials client for the Speech & Text Analytics transcript API.

    Secrets are passed in, never stored in code.
    """

    def __init__(
        self, client_id: str, client_secret: str, *, environment: str = "mypurecloud.com",
        http: httpx.AsyncClient | None = None, clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, max_retries: int = 3, timeout: float = 30.0,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._login = f"https://login.{environment}"
        self._api = f"https://api.{environment}"
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout)
        self._clock = clock
        self._sleep = sleep
        self._max_retries = max_retries
        self._token: str | None = None
        self._expires_at = 0.0

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def access_token(self) -> str:
        if self._token and self._clock() < self._expires_at - 60:
            return self._token
        basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        response = await self._http.post(
            f"{self._login}/oauth/token", data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code != 200:
            raise GenesysError(response.status_code, "OAuth token request failed")
        body = response.json()
        self._token = str(body["access_token"])
        self._expires_at = self._clock() + float(body.get("expires_in", 3600))
        return self._token

    async def _get_json(self, path: str) -> dict[str, Any]:
        attempt = 0
        while True:
            token = await self.access_token()
            response = await self._http.get(f"{self._api}{path}", headers={"Authorization": f"Bearer {token}"})
            if response.status_code == 429 and attempt < self._max_retries:
                try:
                    wait = float(response.headers.get("Retry-After", ""))
                except ValueError:
                    wait = float(2 ** attempt)
                await self._sleep(min(wait, 30.0))
                attempt += 1
                continue
            if response.status_code == 401 and attempt < 1:
                self._token = None
                attempt += 1
                continue
            if response.status_code != 200:
                raise GenesysError(response.status_code, _error_message(response))
            return response.json()

    async def transcript_url(self, conversation_id: str, communication_id: str) -> str:
        path = (f"/api/v2/speechandtextanalytics/conversations/{quote(conversation_id, safe='')}"
                f"/communications/{quote(communication_id, safe='')}/transcripturl")
        url = (await self._get_json(path)).get("url")
        if not url:
            raise GenesysError(200, "transcripturl response had no url")
        return str(url)

    async def download_transcript(self, conversation_id: str, communication_id: str) -> dict[str, Any]:
        """Fetch the transcript URL and download it immediately (the URL expires after 10 minutes)."""
        for attempt in range(2):
            url = await self.transcript_url(conversation_id, communication_id)
            response = await self._http.get(url)             # pre-signed: no Authorization header
            if response.status_code == 200:
                return response.json()
            if attempt == 0 and response.status_code in (403, 404):
                continue                                    # expired or not yet visible: ask for a fresh URL once
            raise GenesysError(response.status_code, "transcript download failed")
        raise GenesysError(0, "transcript download failed")

    async def fetch_segments(self, conversation_id: str, communication_id: str) -> list[Segment]:
        return parse_genesys_transcript(await self.download_transcript(conversation_id, communication_id))

    async def communication_ids(self, conversation_id: str) -> list[str]:
        """Voice communication ids of a conversation (`participants[].calls[].id`), customer side first."""
        data = await self._get_json(f"/api/v2/conversations/{quote(conversation_id, safe='')}")
        ordered = sorted(
            (p for p in data.get("participants") or [] if isinstance(p, Mapping)),
            key=lambda p: str(p.get("purpose", "")).lower() != "customer",
        )
        ids = [str(c["id"]) for p in ordered for c in p.get("calls") or [] if isinstance(c, Mapping) and c.get("id")]
        return list(dict.fromkeys(ids))


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
        return str(body.get("message") or body.get("error_description") or response.reason_phrase)
    except (ValueError, AttributeError):
        return response.reason_phrase or "request failed"


__all__ = [
    "GenesysClient", "GenesysError", "GenesysNotification", "genesys_meta", "handle_notification",
    "parse_genesys_transcript", "render_genesys_transcript",
]
