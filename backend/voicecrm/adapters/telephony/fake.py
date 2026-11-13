"""A scriptable telephony fake: records every request and serves canned downloads. Used by the tests."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from ...ports.telephony import AnsweredCall, IssuedToken, TelephonyError, Target
from ..sources.phone import is_recording_url


class FakeTelephony:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.answered: list[AnsweredCall] = []
        self.downloads: dict[str, bytes] = {}
        self.fail: dict[str, TelephonyError] = {}       # operation name -> the error it raises
        self.counter = 0
        self.closed = False

    def _record(self, op: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((op, args, kwargs))
        if op in self.fail:
            raise self.fail[op]

    def ops(self, name: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        return [(a, k) for op, a, k in self.calls if op == name]

    def _next(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}-{self.counter}"

    async def answer(self, incoming_call_context: str, callback_url: str, *,
                     cognitive_endpoint: str | None = None) -> AnsweredCall:
        self._record("answer", incoming_call_context, callback_url, cognitive_endpoint=cognitive_endpoint)
        answered = AnsweredCall(call_connection_id=self._next("conn"), server_call_id=self._next("server"),
                                correlation_id=self._next("corr"))
        self.answered.append(answered)
        return answered

    async def start_recording(self, call_connection_id: str, *, state_callback_url: str,
                              channels: Sequence[Target] = ()) -> str:
        self._record("start_recording", call_connection_id, state_callback_url=state_callback_url, channels=list(channels))
        return self._next("recording")

    async def stop_recording(self, recording_id: str) -> None:
        self._record("stop_recording", recording_id)

    async def play_text(self, call_connection_id: str, text: str, *, voice: str, locale: str, context: str) -> None:
        self._record("play_text", call_connection_id, text, voice=voice, locale=locale, context=context)

    async def add_participant(self, call_connection_id: str, target: Target, *, timeout_seconds: int,
                              display_name: str | None, context: str) -> str:
        self._record("add_participant", call_connection_id, target, timeout_seconds=timeout_seconds,
                     display_name=display_name, context=context)
        return self._next("invite")

    async def cancel_invite(self, call_connection_id: str, invitation_id: str) -> None:
        self._record("cancel_invite", call_connection_id, invitation_id)

    async def hang_up(self, call_connection_id: str) -> None:
        self._record("hang_up", call_connection_id)

    async def download(self, url: str) -> bytes:
        self._record("download", url)
        if not is_recording_url(url):
            raise TelephonyError("refusing to download from a URL that is not the recording service's own storage")
        if url not in self.downloads:
            raise TelephonyError(f"nothing to download at {url}", status=404)
        return self.downloads[url]

    async def delete_recording(self, url: str) -> None:
        self._record("delete_recording", url)

    async def issue_token(self, identity: str | None = None) -> IssuedToken:
        self._record("issue_token", identity)
        return IssuedToken(identity=identity or self._next("8:acs:fake-user"), token="fake-token",
                           expires_on=datetime.now(timezone.utc) + timedelta(hours=24))

    async def aclose(self) -> None:
        self.closed = True
