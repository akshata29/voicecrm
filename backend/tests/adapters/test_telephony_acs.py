"""The ACS adapter against stand-ins for the Call Automation and Identity clients: what it asks for and how it reports errors."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from azure.core.exceptions import HttpResponseError

from voicecrm.adapters.telephony.acs import AcsTelephony
from voicecrm.ports.telephony import Target, TelephonyError

from ..phone_support import CALLER, CONSOLE, RECORDING_HOST, SELLER_OID

ENDPOINT = "https://acs-test.unitedstates.communication.azure.com"
EXPIRES = datetime(2026, 11, 11, tzinfo=timezone.utc) + timedelta(hours=24)


def http_error(status: int, message: str = "the service said no") -> HttpResponseError:
    error = HttpResponseError(message=message)
    error.status_code = status
    return error


class Connection:
    """One call connection: records what it was asked and can be told to fail."""

    def __init__(self, log: list, fail: dict[str, Exception]) -> None:
        self.log, self.fail = log, fail

    async def _do(self, op: str, *args: Any, **kwargs: Any) -> Any:
        self.log.append((op, args, kwargs))
        if op in self.fail:
            raise self.fail[op]

    async def play_media_to_all(self, source, **kw):
        await self._do("play", source, **kw)

    async def add_participant(self, target, **kw):
        await self._do("add_participant", target, **kw)
        return SimpleNamespace(invitation_id="invite-7")

    async def cancel_add_participant_operation(self, invitation_id):
        await self._do("cancel_invite", invitation_id)

    async def hang_up(self, **kw):
        await self._do("hang_up", **kw)


class SdkResponse:
    """The azure-core response the async client hands back instead of an iterator; it has to be closed."""

    def __init__(self, stream) -> None:
        self._stream, self.closed = stream, False

    def iter_bytes(self):
        return self._stream()

    async def close(self) -> None:
        self.closed = True


class Calls:
    def __init__(self) -> None:
        self.log: list[tuple[str, tuple, dict]] = []
        self.fail: dict[str, Exception] = {}
        self.fail_once: dict[str, Exception] = {}
        self.response_shape = "response"
        self.closed = False

    def ops(self, name: str) -> list[tuple[tuple, dict]]:
        return [(a, k) for op, a, k in self.log if op == name]

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail_once:
            raise self.fail_once.pop(op)
        if op in self.fail:
            raise self.fail[op]

    async def answer_call(self, context, callback, **kw):
        self.log.append(("answer", (context, callback), kw))
        self._maybe_fail("answer")
        return SimpleNamespace(call_connection_id="conn-1", server_call_id="srv-1", correlation_id="corr-1")

    async def start_recording(self, **kw):
        self.log.append(("start_recording", (), kw))
        self._maybe_fail("start_recording")
        return SimpleNamespace(recording_id="rec-1")

    async def stop_recording(self, recording_id):
        self.log.append(("stop_recording", (recording_id,), {}))
        self._maybe_fail("stop_recording")

    def get_call_connection(self, conn):
        self.log.append(("connection", (conn,), {}))
        return Connection(self.log, self.fail)

    async def download_recording(self, url):
        self.log.append(("download", (url,), {}))
        self._maybe_fail("download")
        parts = (b"RIFF", b"-data", b"-end")

        async def stream():
            for part in parts:
                yield part

        if self.response_shape == "iterable":                    # what the SDK documents
            return stream()
        return SdkResponse(stream)                              # what the SDK actually returns (issue 42835)

    async def delete_recording(self, url):
        self.log.append(("delete", (url,), {}))
        self._maybe_fail("delete")

    async def close(self):
        self.closed = True


class Identity:
    def __init__(self) -> None:
        self.log: list[tuple[str, tuple]] = []
        self.fail: Exception | None = None
        self.closed = False

    async def create_user_and_token(self, scopes):
        self.log.append(("create", (scopes,)))
        if self.fail:
            raise self.fail
        return SimpleNamespace(properties={"id": "8:acs:new-user"}), SimpleNamespace(token="jwt-1", expires_on=EXPIRES)

    async def get_token(self, user, scopes):
        self.log.append(("token", (user.properties["id"], scopes)))
        if self.fail:
            raise self.fail
        return SimpleNamespace(token="jwt-2", expires_on=EXPIRES)

    async def close(self):
        self.closed = True


@pytest.fixture()
def acs():
    adapter = AcsTelephony(endpoint=ENDPOINT, credential=object())
    adapter._calls, adapter._identity = Calls(), Identity()
    return adapter


def test_the_adapter_needs_a_way_to_sign_in():
    for kw in ({}, {"endpoint": ENDPOINT}, {"credential": object()}):
        with pytest.raises(ValueError, match="VOICECRM_PHONE_ACS"):
            AcsTelephony(**kw)
    AcsTelephony(connection_string="endpoint=https://x.communication.azure.com/;accesskey=a2V5")
    AcsTelephony(endpoint=ENDPOINT, credential=object())


# ---- the call ---------------------------------------------------------------------------------------------------------
async def test_answering_passes_the_context_the_callback_and_the_voice_connection(acs):
    answered = await acs.answer("ctx", "https://x.example/cb", cognitive_endpoint="https://ai.example/")
    assert (answered.call_connection_id, answered.server_call_id, answered.correlation_id) == ("conn-1", "srv-1", "corr-1")
    ((args, kw),) = acs._calls.ops("answer")
    assert args == ("ctx", "https://x.example/cb") and kw == {"cognitive_services_endpoint": "https://ai.example/"}
    await acs.answer("ctx", "https://x.example/cb")
    assert acs._calls.ops("answer")[1][1] == {"cognitive_services_endpoint": None}


async def test_an_expired_call_is_reported_with_what_it_means(acs):
    acs._calls.fail["answer"] = http_error(404, "call not found")
    with pytest.raises(TelephonyError) as info:
        await acs.answer("ctx", "https://x.example/cb")
    assert info.value.status == 404 and "call not found" in str(info.value) and "30 seconds" in str(info.value)


@pytest.mark.parametrize("status,hint", [(401, "Entra roles"), (403, "Entra roles"), (500, "")])
async def test_access_errors_point_at_the_roles(acs, status, hint):
    acs._calls.fail["answer"] = http_error(status)
    with pytest.raises(TelephonyError) as info:
        await acs.answer("ctx", "https://x.example/cb")
    assert (hint in str(info.value)) if hint else ("Entra" not in str(info.value))
    assert info.value.status == status


async def test_recording_is_unmixed_wav_with_one_channel_per_participant(acs):
    recording = await acs.start_recording("conn-1", state_callback_url="https://x.example/cb", channels=[
        Target("acs", CALLER), Target("teams", SELLER_OID, "Aisha"), Target("phone", "+12065550111")])
    assert recording == "rec-1"
    ((_, kw),) = acs._calls.ops("start_recording")
    assert (kw["call_connection_id"], kw["recording_state_callback_url"]) == ("conn-1", "https://x.example/cb")
    assert (str(kw["recording_channel_type"]), str(kw["recording_format_type"]), str(kw["recording_content_type"])) == (
        "RecordingChannel.UNMIXED", "RecordingFormat.WAV", "RecordingContent.AUDIO")
    assert [(a.channel, a.target_participant.raw_id) for a in kw["channel_affinity"]] == [
        (0, CALLER), (1, f"8:orgid:{SELLER_OID}"), (2, "4:+12065550111")]


async def test_a_recording_without_channels_is_still_started(acs):
    await acs.start_recording("conn-1", state_callback_url="https://x.example/cb")
    ((_, kw),) = acs._calls.ops("start_recording")
    assert kw["channel_affinity"] is None


async def test_when_channel_affinity_is_refused_the_recording_starts_without_it(acs):
    acs._calls.fail_once["start_recording"] = http_error(400, "participant not in the call yet")
    assert await acs.start_recording("conn-1", state_callback_url="cb", channels=[Target("acs", CALLER)]) == "rec-1"
    first, second = acs._calls.ops("start_recording")
    assert "channel_affinity" in first[1] and "channel_affinity" not in second[1]


@pytest.mark.parametrize("status,channels,calls", [(403, [Target("acs", CALLER)], 1), (400, [], 1)])
async def test_other_recording_errors_are_not_retried(acs, status, channels, calls):
    acs._calls.fail["start_recording"] = http_error(status, "nope")
    with pytest.raises(TelephonyError, match="starting the recording failed"):
        await acs.start_recording("conn-1", state_callback_url="cb", channels=channels)
    assert len(acs._calls.ops("start_recording")) == calls


async def test_a_refusal_that_repeats_without_affinity_is_reported(acs):
    acs._calls.fail["start_recording"] = http_error(400, "recording is not allowed")
    with pytest.raises(TelephonyError, match="recording is not allowed"):
        await acs.start_recording("conn-1", state_callback_url="cb", channels=[Target("acs", CALLER)])
    assert len(acs._calls.ops("start_recording")) == 2


async def test_the_notice_is_text_to_speech_to_everyone_on_the_call(acs):
    await acs.play_text("conn-1", "This call is recorded.", voice="en-US-JennyNeural", locale="en-US", context="notice")
    ((args, kw),) = acs._calls.ops("play")
    source = args[0]
    assert (source.text, source.voice_name, source.source_locale) == ("This call is recorded.", "en-US-JennyNeural", "en-US")
    assert kw == {"operation_context": "notice"} and acs._calls.ops("connection")[0][0] == ("conn-1",)


@pytest.mark.parametrize("target,raw", [
    (Target("teams", SELLER_OID), f"8:orgid:{SELLER_OID}"), (Target("acs", CONSOLE), CONSOLE), (Target("phone", "+12065550111"), "4:+12065550111")])
async def test_ringing_a_participant_uses_the_identifier_for_that_kind(acs, target, raw):
    invitation = await acs.add_participant("conn-1", target, timeout_seconds=25, display_name="Dana Whitfield", context="seller")
    assert invitation == "invite-7"
    ((args, kw),) = acs._calls.ops("add_participant")
    assert args[0].raw_id == raw
    assert kw == {"invitation_timeout": 25, "operation_context": "seller", "source_display_name": "Dana Whitfield"}


async def test_cancelling_an_invite_and_hanging_up_for_everyone(acs):
    await acs.cancel_invite("conn-1", "invite-7")
    await acs.hang_up("conn-1")
    assert acs._calls.ops("cancel_invite") == [(("invite-7",), {})]
    assert acs._calls.ops("hang_up") == [((), {"is_for_everyone": True})]


@pytest.mark.parametrize("call,args", [
    ("cancel_invite", ("conn-1", "invite-7")), ("hang_up", ("conn-1",)), ("stop_recording", ("rec-1",))])
async def test_call_control_errors_name_the_action(acs, call, args):
    acs._calls.fail[call] = http_error(404, "gone")                             # the stub's operation names match the adapter's
    with pytest.raises(TelephonyError, match="failed: HTTP 404 gone"):
        await getattr(acs, call)(*args)


# ---- the recording files ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("shape", ["response", "iterable"])
async def test_a_recording_is_downloaded_in_full_and_then_deleted(acs, shape):
    acs._calls.response_shape = shape                                # the SDK returns the response; the docs promise an iterator
    url = f"{RECORDING_HOST}/doc-0/content/audio"
    assert await acs.download(url) == b"RIFF-data-end"
    await acs.delete_recording(f"{RECORDING_HOST}/doc-0")
    assert acs._calls.ops("download") == [((url,), {})] and acs._calls.ops("delete") == [((f"{RECORDING_HOST}/doc-0",), {})]


async def test_the_download_response_is_closed_so_its_connection_is_reused(acs):
    seen = []
    original = SdkResponse.close

    async def close(self):
        seen.append(self)
        await original(self)

    SdkResponse.close = close
    try:
        await acs.download(f"{RECORDING_HOST}/doc-0/content/audio")
    finally:
        SdkResponse.close = original
    assert len(seen) == 1 and seen[0].closed


@pytest.mark.parametrize("url", ["https://attacker.example/x", "http://storage.asm.skype.com/x", "https://asm.skype.com.evil.example/x", ""])
async def test_nothing_is_downloaded_or_deleted_outside_the_recording_service(acs, url):
    with pytest.raises(TelephonyError, match="not the recording service's own"):
        await acs.download(url)
    with pytest.raises(TelephonyError, match="not the recording service's own"):
        await acs.delete_recording(url)
    assert not acs._calls.log                                                  # the credential never left the app


async def test_download_errors_say_what_happened(acs):
    acs._calls.fail["download"] = http_error(404, "expired")
    with pytest.raises(TelephonyError) as info:
        await acs.download(f"{RECORDING_HOST}/doc-0/content/audio")
    assert info.value.status == 404 and "24 hours" in str(info.value)


# ---- tokens for the browser ---------------------------------------------------------------------------------------------
async def test_a_new_identity_is_created_with_a_voip_token(acs):
    issued = await acs.issue_token()
    assert (issued.identity, issued.token, issued.expires_on) == ("8:acs:new-user", "jwt-1", EXPIRES)
    assert acs._identity.log == [("create", (["voip"],))]


async def test_an_existing_identity_gets_a_fresh_token(acs):
    issued = await acs.issue_token(CALLER)
    assert (issued.identity, issued.token) == (CALLER, "jwt-2") and acs._identity.log == [("token", (CALLER, ["voip"]))]


async def test_token_errors_are_reported(acs):
    acs._identity.fail = http_error(403, "no data-plane access")
    with pytest.raises(TelephonyError, match="issuing the access token failed: HTTP 403"):
        await acs.issue_token()


# ---- shutdown ------------------------------------------------------------------------------------------------------------
async def test_closing_releases_the_clients_and_only_a_credential_it_owns(acs):
    closed = []

    class Credential:
        async def close(self):
            closed.append(True)

    calls, identity = acs._calls, acs._identity
    acs._credential = Credential()
    await acs.aclose()
    assert calls.closed and identity.closed and closed == []                    # the shared credential is left to its owner
    owner = AcsTelephony(endpoint=ENDPOINT, credential=Credential(), owns_credential=True)
    await owner.aclose()
    assert closed == [True]
    await owner.aclose()                                                        # closing twice is harmless
