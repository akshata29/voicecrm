"""Microsoft Teams: WebVTT transcripts and Microsoft Graph change-notification helpers.

Transcript format (Graph `.../content?$format=text/vtt`): WebVTT whose cues carry `<v Speaker Name>text</v>` voice tags;
the cue identifiers look like `<guid>/<n>-<m>` and share one guid per transcript. The speaker label is kept raw as the
segment's speaker_id; mapping labels to participants is the pipeline's job.

Graph flow (all verified against Microsoft Learn; application permission OnlineMeetingTranscript.Read.All plus a
tenant application access policy):
  1. subscribe to `communications/onlineMeetings/getAllTranscripts` (changeType created) *before* the meeting starts;
  2. Graph validates the endpoint with `?validationToken=` -> answer 200 text/plain with the decoded token;
  3. each notification's `resource` is `users/{organizer}/onlineMeetings('{meeting}')/transcripts('{transcript}')`;
  4. GET `/users/{organizer}/onlineMeetings/{meeting}/transcripts/{transcript}/content?$format=text/vtt`.
`fetch_transcript_vtt` uses step 4's path when the organizer id is known (it is in every notification). Without it
the path falls back to `/communications/onlineMeetings/...`, which Learn documents only for subscriptions.
"""
from __future__ import annotations

import hmac
import html
import logging
import re
from typing import Any, Literal
from urllib.parse import quote

import httpx

from ...domain.base import VModel
from ...domain.conversation import Segment
from ...domain.enums import Channel
from ...domain.fixtures import ScriptedCall
from .common import UNKNOWN_SPEAKER, interaction_id, participants_by_id, speaker_label, turn_timings
from .vtt import format_timestamp, iter_cues

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
VALIDATION_CONTENT_TYPE = "text/plain"

_VOICE_RE = re.compile(r"<v(?:\.[^\s>]*)*\s+([^>]*)>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>\n]*>")
_CUE_ID_RE = re.compile(r"^([0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})/\d+-\d+$")


# ---- WebVTT ------------------------------------------------------------------------------------------------------
def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub("", fragment))).strip()


def _split_voices(payload: str) -> list[tuple[str, str]]:
    matches = list(_VOICE_RE.finditer(payload))
    if not matches:
        return [(UNKNOWN_SPEAKER, _clean(payload))]
    voices: list[tuple[str, str]] = []
    leading = _clean(payload[: matches[0].start()])
    if leading:
        voices.append((UNKNOWN_SPEAKER, leading))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(payload)
        label = html.unescape(match.group(1)).strip() or UNKNOWN_SPEAKER
        voices.append((label, _clean(payload[match.end():end])))
    return voices


def looks_like_teams_vtt(text: str) -> bool:
    return bool(_VOICE_RE.search(text))


def parse_teams_vtt(text: str) -> list[Segment]:
    """Teams transcript WebVTT -> segments; speaker_id is the raw label from the `<v ...>` voice tag."""
    segments: list[Segment] = []
    for cue in iter_cues(text):
        for speaker, spoken in _split_voices(cue.payload):
            if spoken:
                segments.append(Segment(id=len(segments), speaker_id=speaker, start_ms=cue.start_ms,
                                        end_ms=cue.end_ms, text=spoken))
    return segments


def transcript_guid(text: str) -> str | None:
    """The guid every cue identifier of a real Teams transcript starts with, when they all agree."""
    guids = {m.group(1).lower() for cue in iter_cues(text) if cue.identifier and (m := _CUE_ID_RE.match(cue.identifier))}
    return guids.pop() if len(guids) == 1 else None


def render_teams_vtt(call: ScriptedCall) -> str:
    """The call as Teams would export it: voice-tagged cues with `<guid>/<n>-0` identifiers."""
    guid = interaction_id(call, Channel.teams)
    people = participants_by_id(call)
    lines = ["WEBVTT", ""]
    for number, (turn, (start, end)) in enumerate(zip(call.turns, turn_timings(call), strict=True)):
        label = html.escape(speaker_label(call, turn, people), quote=False)
        body = re.sub(r"\n\s*\n", "\n", html.escape(turn.text, quote=False))
        lines += [f"{guid}/{number}-0", f"{format_timestamp(start)} --> {format_timestamp(end)}",
                  f"<v {label}>{body}</v>", ""]
    return "\n".join(lines)


# ---- Graph change notifications --------------------------------------------------------------------------------------
class TranscriptRef(VModel):
    """Where a newly available transcript lives, taken from a Graph changeNotification."""

    tenant_id: str | None = None
    meeting_id: str                               # online meeting id (for ad hoc calls: the call id)
    transcript_id: str
    organizer_id: str | None = None               # the `users/{id}` segment; needed for the documented content path
    subscription_id: str | None = None
    change_type: str = "created"
    resource: str
    kind: Literal["online_meeting", "adhoc_call"] = "online_meeting"


_MEETING_RES = (
    re.compile(
        r"(?:^|/)users/(?P<organizer>[^/]+)/onlineMeetings(?:\('(?P<m1>[^']+)'\)|/(?P<m2>[^/]+))"
        r"/transcripts(?:\('(?P<t1>[^']+)'\)|/(?P<t2>[^/?]+))", re.IGNORECASE),
    re.compile(
        r"(?:^|/)onlineMeetings(?:\('(?P<m1>[^']+)'\)|/(?P<m2>[^/]+))"
        r"/transcripts(?:\('(?P<t1>[^']+)'\)|/(?P<t2>[^/?]+))", re.IGNORECASE),
)
_ADHOC_RE = re.compile(
    r"(?:^|/)users/(?P<organizer>[^/]+)/adhocCalls/(?P<call>[^/(]+)/transcripts(?:\('(?P<t1>[^']+)'\)|/(?P<t2>[^/?]+))",
    re.IGNORECASE)


def _parse_resource(resource: str) -> dict[str, Any] | None:
    for pattern in _MEETING_RES:
        match = pattern.search(resource)
        if match:
            groups = match.groupdict()
            return {"meeting_id": groups["m1"] or groups["m2"], "transcript_id": groups["t1"] or groups["t2"],
                    "organizer_id": groups.get("organizer"), "kind": "online_meeting"}
    match = _ADHOC_RE.search(resource)
    if match:
        groups = match.groupdict()
        return {"meeting_id": groups["call"], "transcript_id": groups["t1"] or groups["t2"],
                "organizer_id": groups["organizer"], "kind": "adhoc_call"}
    return None


def handle_graph_notification(payload: dict[str, Any], *, expected_client_state: str | None = None) -> list[TranscriptRef]:
    """Transcript references in a Graph changeNotificationCollection (only `created` notifications).

    With `expected_client_state`, notifications whose clientState differs are dropped (constant-time compare).
    Lifecycle notifications and resources that are not transcripts yield nothing.
    """
    items = payload.get("value") if isinstance(payload, dict) else None
    refs: list[TranscriptRef] = []
    seen: set[tuple[str | None, str, str]] = set()
    rejected = 0
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        if expected_client_state is not None and not hmac.compare_digest(
            str(item.get("clientState") or "").encode(), expected_client_state.encode()
        ):
            rejected += 1
            continue
        change_type = str(item.get("changeType") or "created").lower()
        resource = str(item.get("resource") or (item.get("resourceData") or {}).get("@odata.id") or "")
        parsed = _parse_resource(resource)
        if change_type != "created" or parsed is None or not parsed["meeting_id"] or not parsed["transcript_id"]:
            continue
        key = (item.get("tenantId"), parsed["meeting_id"], parsed["transcript_id"])
        if key in seen:
            continue
        seen.add(key)
        refs.append(TranscriptRef(
            tenant_id=item.get("tenantId"), meeting_id=parsed["meeting_id"], transcript_id=parsed["transcript_id"],
            organizer_id=parsed["organizer_id"], subscription_id=item.get("subscriptionId"), change_type=change_type,
            resource=resource, kind=parsed["kind"],
        ))
    if rejected:
        logger.warning("dropped %d Graph notification(s) with an unexpected clientState", rejected)
    return refs


def validation_response(token: str) -> str:
    """Body for the subscription validation request: 200 OK, Content-Type text/plain, the decoded validationToken.

    Pass the value as the web framework hands it over (already URL-decoded). Graph allows 10 seconds to answer.
    """
    return token


# ---- Graph transcript download ---------------------------------------------------------------------------------------
class TeamsGraphError(RuntimeError):
    def __init__(self, status: int, message: str, *, code: str | None = None) -> None:
        super().__init__(f"Graph {status}{f' {code}' if code else ''}: {message}")
        self.status = status
        self.code = code


async def acquire_graph_token(credential: Any) -> str:
    """App-only token from an async azure.identity credential (DefaultAzureCredential)."""
    return str((await credential.get_token(GRAPH_SCOPE)).token)


def _content_path(meeting_id: str, transcript_id: str, organizer_id: str | None, kind: str) -> str:
    meeting, transcript = quote(meeting_id, safe=""), quote(transcript_id, safe="")
    if kind == "adhoc_call":
        if not organizer_id:
            raise ValueError("ad hoc call transcripts need the organizer (user) id")
        return f"/users/{quote(organizer_id, safe='')}/adhocCalls/{meeting}/transcripts/{transcript}/content"
    if organizer_id:
        return f"/users/{quote(organizer_id, safe='')}/onlineMeetings/{meeting}/transcripts/{transcript}/content"
    return f"/communications/onlineMeetings/{meeting}/transcripts/{transcript}/content"


async def fetch_transcript_vtt(
    client: httpx.AsyncClient, token: str, meeting_id: str, transcript_id: str, *,
    organizer_id: str | None = None, kind: Literal["online_meeting", "adhoc_call"] = "online_meeting",
    base_url: str = GRAPH_BASE,
) -> str:
    """Download the speaker-attributed WebVTT transcript (`$format=text/vtt`) with an app-only bearer token."""
    url = f"{base_url.rstrip('/')}{_content_path(meeting_id, transcript_id, organizer_id, kind)}?$format=text/vtt"
    response = await client.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "text/vtt"})
    if response.status_code != 200:
        code = message = None
        try:
            error = response.json().get("error", {})
            message = error.get("message")
            code = (error.get("innerError") or {}).get("code") or error.get("code")
        except (ValueError, AttributeError):
            pass
        raise TeamsGraphError(response.status_code, message or response.reason_phrase or "request failed", code=code)
    return response.content.decode("utf-8-sig")


async def fetch_transcript_for(
    client: httpx.AsyncClient, token: str, ref: TranscriptRef, *, base_url: str = GRAPH_BASE
) -> str:
    return await fetch_transcript_vtt(
        client, token, ref.meeting_id, ref.transcript_id, organizer_id=ref.organizer_id, kind=ref.kind, base_url=base_url
    )
