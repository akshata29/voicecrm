"""Azure Communication Services adapter for the phone line (Call Automation, Call Recording and the Identity API).

Answering, playing the notice, ringing a participant, recording and hanging up are Call Automation requests; the recording
files are fetched from the service's temporary storage (kept 24 hours); browser callers get VoIP tokens from the Identity API.

Auth: Microsoft Entra (the default; the identity needs data-plane access to the resource) or the resource's connection
string. The SDK validates recording download URLs against the service's own hosts before it attaches credentials, and
`adapters/sources/phone.py` checks them again, so a forged event cannot make the app send its token elsewhere.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from azure.core.exceptions import AzureError, HttpResponseError

from ...ports.telephony import AnsweredCall, IssuedToken, TelephonyError, Target
from ..sources.phone import is_recording_url

logger = logging.getLogger(__name__)

_TOKEN_SCOPES = ["voip"]


def _identifier(target: Target) -> Any:
    from azure.communication.callautomation import (
        CommunicationUserIdentifier, MicrosoftTeamsUserIdentifier, PhoneNumberIdentifier,
    )

    if target.kind == "teams":
        return MicrosoftTeamsUserIdentifier(user_id=target.id)
    if target.kind == "phone":
        return PhoneNumberIdentifier(target.id)
    return CommunicationUserIdentifier(target.id)


async def _read_all(stream: Any) -> bytes:
    """The bytes of a recording download.

    The async SDK client is documented to return an ``AsyncIterable[bytes]`` but returns the azure-core response itself
    (SDK issue 42835), so both shapes are read; the response is closed so its connection goes back to the pool.
    """
    iter_bytes = getattr(stream, "iter_bytes", None)
    if iter_bytes is None:
        return b"".join([chunk async for chunk in stream])
    try:
        return b"".join([chunk async for chunk in iter_bytes()])
    finally:
        closed = getattr(stream, "close", None)
        if closed is not None:
            result = closed()
            if hasattr(result, "__await__"):
                await result


def _error(exc: Exception, action: str) -> TelephonyError:
    status = getattr(exc, "status_code", None)
    message = getattr(exc, "message", None) or str(exc)
    first_line = " ".join(str(message).split())[:300]
    hint = ""
    if status in (401, 403):
        hint = " - check the access to the Communication Services resource (Entra roles or the connection string)"
    elif status == 404:
        hint = " - the call or recording no longer exists (a ringing call expires after 30 seconds; recordings after 24 hours)"
    error = getattr(exc, "error", None)
    return TelephonyError(f"{action} failed: HTTP {status or '?'} {first_line}{hint}", status=status,
                          code=getattr(error, "code", None))


class AcsTelephony:
    name = "acs"

    def __init__(self, *, endpoint: str | None = None, connection_string: str | None = None,
                 credential: Any | None = None, owns_credential: bool = False) -> None:
        if not connection_string and not (endpoint and credential is not None):
            raise ValueError("the phone line needs VOICECRM_PHONE_ACS_ENDPOINT with Entra sign-in, or "
                             "VOICECRM_PHONE_ACS_CONNECTION_STRING")
        self._endpoint, self._connection_string, self._credential = endpoint, connection_string, credential
        self._owns_credential = owns_credential
        self._calls: Any = None
        self._identity: Any = None

    # ---- clients (created on first use, inside the running event loop) -------------------------------------------
    def _call_client(self) -> Any:
        if self._calls is None:
            from azure.communication.callautomation.aio import CallAutomationClient

            self._calls = (CallAutomationClient.from_connection_string(self._connection_string) if self._connection_string
                           else CallAutomationClient(self._endpoint, self._credential))
        return self._calls

    def _identity_client(self) -> Any:
        if self._identity is None:
            from azure.communication.identity.aio import CommunicationIdentityClient

            self._identity = (CommunicationIdentityClient.from_connection_string(self._connection_string)
                              if self._connection_string else CommunicationIdentityClient(self._endpoint, self._credential))
        return self._identity

    # ---- call control --------------------------------------------------------------------------------------------
    async def answer(self, incoming_call_context: str, callback_url: str, *,
                     cognitive_endpoint: str | None = None) -> AnsweredCall:
        try:
            props = await self._call_client().answer_call(
                incoming_call_context, callback_url, cognitive_services_endpoint=cognitive_endpoint or None)
        except (AzureError, ValueError) as exc:
            raise _error(exc, "answering the call") from exc
        return AnsweredCall(call_connection_id=props.call_connection_id, server_call_id=props.server_call_id,
                            correlation_id=props.correlation_id)

    async def start_recording(self, call_connection_id: str, *, state_callback_url: str,
                              channels: Sequence[Target] = ()) -> str:
        from azure.communication.callautomation import (
            ChannelAffinity, RecordingChannel, RecordingContent, RecordingFormat,
        )

        affinity = [ChannelAffinity(target_participant=_identifier(t), channel=i) for i, t in enumerate(channels)]
        options: dict[str, Any] = dict(
            call_connection_id=call_connection_id, recording_content_type=RecordingContent.AUDIO,
            recording_channel_type=RecordingChannel.UNMIXED, recording_format_type=RecordingFormat.WAV,
            recording_state_callback_url=state_callback_url)
        try:
            props = await self._call_client().start_recording(channel_affinity=affinity or None, **options)
        except HttpResponseError as exc:
            if not affinity or exc.status_code != 400:
                raise _error(exc, "starting the recording") from exc
            logger.info("recording channel affinity was refused (%s); starting without it, channels are mapped from the metadata",
                        exc.status_code)
            try:                                  # the metadata names the participant on every channel, so this still works
                props = await self._call_client().start_recording(**options)
            except AzureError as retry:
                raise _error(retry, "starting the recording") from retry
        except (AzureError, ValueError) as exc:
            raise _error(exc, "starting the recording") from exc
        return props.recording_id

    async def stop_recording(self, recording_id: str) -> None:
        try:
            await self._call_client().stop_recording(recording_id)
        except (AzureError, ValueError) as exc:
            raise _error(exc, "stopping the recording") from exc

    async def play_text(self, call_connection_id: str, text: str, *, voice: str, locale: str, context: str) -> None:
        from azure.communication.callautomation import TextSource

        source = TextSource(text=text, voice_name=voice, source_locale=locale)
        try:
            await self._call_client().get_call_connection(call_connection_id).play_media_to_all(
                source, operation_context=context)
        except (AzureError, ValueError) as exc:
            raise _error(exc, "playing the prompt") from exc

    async def add_participant(self, call_connection_id: str, target: Target, *, timeout_seconds: int,
                              display_name: str | None, context: str) -> str:
        try:
            result = await self._call_client().get_call_connection(call_connection_id).add_participant(
                _identifier(target), invitation_timeout=timeout_seconds, operation_context=context,
                source_display_name=display_name)
        except (AzureError, ValueError) as exc:
            raise _error(exc, "ringing the seller") from exc
        return str(getattr(result, "invitation_id", "") or "")

    async def cancel_invite(self, call_connection_id: str, invitation_id: str) -> None:
        try:
            await self._call_client().get_call_connection(call_connection_id).cancel_add_participant_operation(invitation_id)
        except (AzureError, ValueError) as exc:
            raise _error(exc, "cancelling the invitation") from exc

    async def hang_up(self, call_connection_id: str) -> None:
        try:
            await self._call_client().get_call_connection(call_connection_id).hang_up(is_for_everyone=True)
        except (AzureError, ValueError) as exc:
            raise _error(exc, "ending the call") from exc

    # ---- recordings ----------------------------------------------------------------------------------------------
    async def download(self, url: str) -> bytes:
        if not is_recording_url(url):
            raise TelephonyError("refusing to download from a URL that is not the recording service's own storage")
        try:
            stream = await self._call_client().download_recording(url)
            return await _read_all(stream)
        except (AzureError, ValueError) as exc:
            raise _error(exc, "downloading the recording") from exc

    async def delete_recording(self, url: str) -> None:
        if not is_recording_url(url):
            raise TelephonyError("refusing to delete at a URL that is not the recording service's own storage")
        try:
            await self._call_client().delete_recording(url)
        except (AzureError, ValueError) as exc:
            raise _error(exc, "deleting the recording") from exc

    # ---- browser callers -----------------------------------------------------------------------------------------
    async def issue_token(self, identity: str | None = None) -> IssuedToken:
        from azure.communication.identity import CommunicationUserIdentifier

        client = self._identity_client()
        try:
            if identity:
                token = await client.get_token(CommunicationUserIdentifier(identity), _TOKEN_SCOPES)
                return IssuedToken(identity=identity, token=token.token, expires_on=token.expires_on)
            user, token = await client.create_user_and_token(_TOKEN_SCOPES)
            return IssuedToken(identity=user.properties["id"], token=token.token, expires_on=token.expires_on)
        except (AzureError, ValueError) as exc:
            raise _error(exc, "issuing the access token") from exc

    async def aclose(self) -> None:
        for client in (self._calls, self._identity):
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    logger.debug("ACS client did not close cleanly", exc_info=True)
        self._calls = self._identity = None
        close = getattr(self._credential, "close", None) if self._owns_credential else None
        if close is not None:
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                logger.debug("the phone line's credential did not close cleanly", exc_info=True)
