"""Telephony port: what the phone line needs from a call-control service. Adapters: Azure Communication Services, a fake."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable


class TelephonyError(RuntimeError):
    """A call-control request failed (bad configuration, an expired call, throttling or a service error)."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status, self.code = status, code


@dataclass(frozen=True)
class Target:
    """Someone the line can ring or record: a Teams user, an ACS identity (the browser console) or a phone number."""

    kind: Literal["teams", "acs", "phone"]
    id: str                                     # Teams object id, ACS raw id (8:acs:...) or an E.164 number
    display_name: str | None = None


@dataclass(frozen=True)
class AnsweredCall:
    call_connection_id: str
    server_call_id: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class IssuedToken:
    identity: str                               # the ACS identity (raw id) the token belongs to
    token: str
    expires_on: datetime


@runtime_checkable
class TelephonyPort(Protocol):
    name: str

    async def answer(self, incoming_call_context: str, callback_url: str, *,
                     cognitive_endpoint: str | None = None) -> AnsweredCall: ...

    async def start_recording(self, call_connection_id: str, *, state_callback_url: str,
                              channels: Sequence[Target] = ()) -> str:
        """Starts an unmixed audio recording; ``channels[i]`` is asked to take audio channel ``i``. Returns the recording id."""
        ...

    async def stop_recording(self, recording_id: str) -> None: ...

    async def play_text(self, call_connection_id: str, text: str, *, voice: str, locale: str, context: str) -> None: ...

    async def add_participant(self, call_connection_id: str, target: Target, *, timeout_seconds: int,
                              display_name: str | None, context: str) -> str:
        """Rings ``target`` into the call. Returns the invitation id."""
        ...

    async def cancel_invite(self, call_connection_id: str, invitation_id: str) -> None: ...

    async def hang_up(self, call_connection_id: str) -> None:
        """Ends the call for everyone."""
        ...

    async def download(self, url: str) -> bytes:
        """Downloads a recording or its metadata from the service's temporary storage (the URL must be the service's own)."""
        ...

    async def delete_recording(self, url: str) -> None: ...

    async def issue_token(self, identity: str | None = None) -> IssuedToken:
        """A VoIP access token for ``identity``; a new identity is created when it is None."""
        ...

    async def aclose(self) -> None: ...
