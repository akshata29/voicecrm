"""The phone line's wire formats: Event Grid and callback payloads, secrets, recording URLs, metadata and unmixed WAVs."""
from __future__ import annotations

import struct
from datetime import datetime, timezone

import pytest

from voicecrm.adapters.sources import phone as wire

from ..phone_support import (
    CALLER, LINE, PHONE, SELLER, SELLER_OID, extensible_wav, incoming_event, make_wav, phone_caller, recording_event, square)


# ---- identities ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("value,kind,phone", [
    ({"kind": "phoneNumber", "rawId": "4:+12065550111", "phoneNumber": {"value": "+12065550111"}}, "phone", "+12065550111"),
    ({"rawId": "4:+12065550111"}, "phone", "+12065550111"),
    ({"kind": "communicationUser", "communicationUser": {"id": CALLER}}, "acs", None),
    ({"rawId": SELLER}, "teams", None),
    ({"kind": "microsoftTeamsUser", "microsoftTeamsUser": {"userId": SELLER_OID}}, "teams", None),
    ({"rawId": "28:orgid:abcdef"}, "app", None),
    ("8:acs:xyz", "acs", None),
    ({"kind": "unknown", "rawId": "weird"}, "unknown", None),
    (None, "unknown", None),
])
def test_identifiers_are_classified_by_their_raw_id(value, kind, phone):
    party = wire.parse_party(value)
    assert party.kind == kind and party.phone == phone


@pytest.mark.parametrize("a,b,same", [
    ("4:+12065550111", "+1 (206) 555-0111", True),            # a phone number written two ways
    ("4:+12065550111", "4:+12065550112", False),
    (SELLER, SELLER_OID.upper(), True),                        # 8:orgid:<guid> and <guid> are one Teams user
    (CALLER, CALLER.upper(), True),
    (CALLER, LINE, False),
    ("", SELLER, False), (None, SELLER, False), ("", "", False),
])
def test_two_raw_ids_name_the_same_party(a, b, same):
    assert wire.same_party(a, b) is same


def test_numbers_are_masked_and_formatted_for_people():
    assert wire.mask("+12065550111") == "********0111" and wire.mask(None) == "-" and wire.mask("12") == "12"
    assert wire.display_number("+12065550111") == "+1 206 555 0111"
    assert wire.display_number("+442071838750") == "+442071838750" and wire.display_number("") is None


# ---- Event Grid ------------------------------------------------------------------------------------------------------
def test_event_grid_bodies_are_lists_of_objects():
    assert wire.as_events([{"a": 1}, "junk", 3]) == [{"a": 1}] and wire.as_events({"a": 1}) == [{"a": 1}]
    assert wire.as_events("text") == [] and wire.as_events(None) == []


def test_the_subscription_handshake_code_is_found():
    handshake = [{"eventType": wire.EVENT_VALIDATION, "data": {"validationCode": "abc123", "validationUrl": "https://x"}}]
    assert wire.validation_code(handshake) == "abc123"
    assert wire.validation_code([incoming_event()]) is None and wire.validation_code([]) is None


def test_an_incoming_call_event_is_parsed_with_both_parties():
    call = wire.parse_incoming_call(incoming_event())
    assert call is not None and call.correlation_id == "corr-1" and call.incoming_call_context == "ctx-1"
    assert call.server_call_id == "server-0" and call.caller_name == "Dana Whitfield"
    assert call.caller.kind == "acs" and call.caller.raw_id == CALLER and call.callee.raw_id == LINE
    pstn = wire.parse_incoming_call(incoming_event(caller=phone_caller(), name=PHONE))
    assert pstn is not None and pstn.caller.kind == "phone" and pstn.caller.phone == PHONE


def test_an_incoming_call_without_a_context_or_with_another_type_is_ignored():
    no_context = incoming_event()
    del no_context["data"]["incomingCallContext"]
    assert wire.parse_incoming_call(no_context) is None
    other = incoming_event()
    other["eventType"] = "Microsoft.Communication.SMSReceived"
    assert wire.parse_incoming_call(other) is None
    assert wire.parse_incoming_call({"eventType": wire.EVENT_INCOMING_CALL, "data": "nope"}) is None


def test_the_correlation_id_falls_back_to_the_event_id():
    event = incoming_event(event_id="evt-9")
    del event["data"]["correlationId"]
    assert wire.parse_incoming_call(event).correlation_id == "evt-9"
    del event["id"]
    assert wire.parse_incoming_call(event) is None


def test_a_recording_event_lists_its_chunks_in_order_and_names_the_call():
    ready = wire.parse_recording_ready(recording_event(chunks=3))
    assert ready is not None and [c.index for c in ready.chunks] == [0, 1, 2]
    assert (ready.call_id, ready.server_call_id, ready.recording_id) == ("call-1", "srv-1", "rec-1")
    assert ready.duration_ms == 187_000 and ready.end_reason == "CallEnded"
    assert ready.started_at == datetime(2026, 11, 10, 15, 0, 0, 123456, tzinfo=timezone.utc)   # .NET's 7 digits are trimmed
    assert ready.key == "acs-doc-0"                                                             # the first chunk's document id


def test_a_recording_key_without_document_ids_is_still_stable():
    event = recording_event()
    for chunk in event["data"]["recordingStorageInfo"]["recordingChunks"]:
        del chunk["documentId"]
    first, second = wire.parse_recording_ready(event), wire.parse_recording_ready(event)
    assert first.key == second.key and first.key.startswith("acs-") and len(first.key) == 36


def test_a_recording_event_without_files_is_ignored():
    event = recording_event()
    event["data"]["recordingStorageInfo"]["recordingChunks"] = []
    assert wire.parse_recording_ready(event) is None
    assert wire.parse_recording_ready(incoming_event()) is None


# ---- Call Automation callbacks ---------------------------------------------------------------------------------------
def test_callbacks_are_parsed_from_cloud_events():
    body = [
        {"type": "Microsoft.Communication.CallConnected", "data": {
            "callConnectionId": "conn-1", "serverCallId": "srv-1", "correlationId": "corr-1"}},
        {"type": "Microsoft.Communication.ParticipantsUpdated", "data": {"callConnectionId": "conn-1", "participants": [
            {"identifier": {"rawId": CALLER, "kind": "communicationUser"}, "isMuted": False},
            {"identifier": {"rawId": SELLER, "kind": "microsoftTeamsUser"}}]}},
        {"type": "Microsoft.Communication.AddParticipantFailed", "data": {
            "callConnectionId": "conn-1", "operationContext": "seller", "participant": {"rawId": SELLER},
            "resultInformation": {"code": 400, "subCode": 7000, "message": "no answer"}}},
        {"type": "Microsoft.Communication.PlayCompleted", "data": {"callConnectionId": "conn-1", "operationContext": "notice"}},
        {"type": "Microsoft.Communication.RecordingStateChanged", "data": {"recordingId": "r1", "recordingState": "Active"}},
    ]
    connected, updated, failed, played, recording = wire.parse_callbacks(body)
    assert (connected.kind, connected.call_connection_id, connected.server_call_id) == ("CallConnected", "conn-1", "srv-1")
    assert [p.raw_id for p in updated.participants] == [CALLER, SELLER] and updated.participants[1].kind == "teams"
    assert failed.operation_context == "seller" and failed.result_code == 400 and failed.result_message == "no answer"
    assert failed.participant.raw_id == SELLER
    assert played.operation_context == "notice" and (recording.recording_state, recording.recording_id) == ("active", "r1")


def test_a_single_cloud_event_and_garbage_are_handled():
    assert [c.kind for c in wire.parse_callbacks({"type": "Microsoft.Communication.CallDisconnected", "data": {}})] == ["CallDisconnected"]
    assert wire.parse_callbacks([{"type": "x"}, "junk", {"data": {}}]) == [] and wire.parse_callbacks(None) == []


# ---- secrets ---------------------------------------------------------------------------------------------------------
def test_callback_tokens_belong_to_one_call_and_one_secret():
    token = wire.callback_token("s3cret-value", "call-1")
    assert token == wire.callback_token("s3cret-value", "call-1") and len(token) == 40
    assert wire.valid_callback_token("s3cret-value", "call-1", token)
    assert not wire.valid_callback_token("s3cret-value", "call-2", token)             # a token for another call
    assert not wire.valid_callback_token("other-secret", "call-1", token)
    assert not wire.valid_callback_token("s3cret-value", "call-1", None) and not wire.valid_callback_token("", "call-1", token)


def test_the_event_key_is_compared_in_constant_time_and_never_empty():
    assert wire.valid_event_key("abc", "abc") and not wire.valid_event_key("abc", "abd")
    assert not wire.valid_event_key("abc", None) and not wire.valid_event_key("", "") and not wire.valid_event_key("", None)


@pytest.mark.parametrize("url,ok", [
    ("https://storage.asm.skype.com/v1/objects/0-eus-d1/content/audio", True),
    ("https://us-storage.asm.skype.com/v1/objects/x/content/acsmetadata", True),
    ("https://eu.asyncgw.teams.microsoft.com/v1/objects/x", True),
    ("http://storage.asm.skype.com/v1/objects/x", False),                          # not https
    ("https://evil.example.com/asm.skype.com", False),
    ("https://asm.skype.com.evil.example.com/x", False),                           # a look-alike host
    ("https://notasm.skype.com/x", False),
    ("https://user@storage.asm.skype.com/x", False),                               # credentials in the URL
    ("https://storage.asm.skype.com:8443/x", False),
    ("https://stor\u00e4ge.asm.skype.com/x", False),                               # non-ASCII host
    ("ftp://storage.asm.skype.com/x", False), ("", False), (None, False), ("not a url", False)])
def test_only_the_recording_services_own_hosts_are_downloaded_from(url, ok):
    assert wire.is_recording_url(url) is ok


# ---- recording metadata ----------------------------------------------------------------------------------------------
METADATA = {
    "resourceId": "res", "callId": "call-1", "chunkDocumentId": "doc-0", "chunkIndex": 0,
    "chunkStartTime": "2026-11-10T15:00:00Z", "chunkDuration": 187000,
    "recordingInfo": {"contentType": "audio", "channelType": "unmixed", "format": "wav",
                      "audioConfiguration": {"sampleRate": 16000, "bitRate": 256000, "channels": 3}},
    "participants": [{"participantId": LINE, "channel": 0}, {"participantId": CALLER, "channel": 1},
                     {"participantId": SELLER, "channel": 2}, {"participantId": "ghost"}],
}


def test_recording_metadata_names_the_participant_on_each_channel():
    import json

    for raw in (METADATA, json.dumps(METADATA), json.dumps(METADATA).encode("utf-8-sig")):
        meta = wire.parse_recording_metadata(raw)
        assert (meta.call_id, meta.channel_type, meta.audio_format, meta.sample_rate, meta.channels) == ("call-1", "unmixed", "wav", 16000, 3)
        assert [(p.participant_id, p.channel) for p in meta.participants] == [(LINE, 0), (CALLER, 1), (SELLER, 2), ("ghost", None)]
        assert meta.chunk_duration_ms == 187000 and meta.chunk_start.year == 2026
    with pytest.raises(ValueError):
        wire.parse_recording_metadata("[1, 2]")


# ---- audio -----------------------------------------------------------------------------------------------------------
def test_a_wav_round_trips_through_the_reader_and_writer():
    original = [square(400, 3000), square(400, 1500)]
    pcm = wire.read_pcm16(make_wav(original))
    assert (pcm.channels, pcm.rate, pcm.frames) == (2, 16000, 400)
    assert list(pcm.channel(0)) == original[0] and list(pcm.channel(1)) == original[1]


def test_multi_channel_extensible_wavs_and_unfinalised_sizes_are_read():
    original = [square(200, 100), square(200, 2000), square(200, 3000)]
    for size in (None, 0, 0xFFFFFFFF):                                                         # streamed files have no real size
        pcm = wire.read_pcm16(extensible_wav(original, data_size=size))
        assert (pcm.channels, pcm.frames) == (3, 200)
        assert [list(pcm.channel(i)) for i in range(3)] == original


def test_unsupported_or_broken_audio_is_refused_with_a_reason():
    with pytest.raises(ValueError, match="not a WAV"):
        wire.read_pcm16(b"ID3\x03\x00not audio")
    eight_bit = bytearray(make_wav([square(40, 100)]))
    struct.pack_into("<H", eight_bit, 34, 8)                                                  # bits per sample -> 8
    with pytest.raises(ValueError, match="16-bit"):
        wire.read_pcm16(bytes(eight_bit))
    with pytest.raises(ValueError, match="no audio"):
        wire.read_pcm16(b"RIFF\x04\x00\x00\x00WAVE")


def test_channel_levels_tell_speech_from_silence():
    pcm = wire.read_pcm16(make_wav([[0] * 800, square(800, 2000), square(800, 10)]))
    quiet, speech, hiss = wire.channel_levels(pcm)
    assert quiet == 0 and speech > 1000 and 0 < hiss < wire.SILENCE_RMS


def meta_for(**channels: int | None) -> wire.RecordingMetadata:
    ids = {"bot": LINE, "caller": CALLER, "seller": SELLER}
    return wire.RecordingMetadata(participants=[wire.MetaParticipant(participant_id=ids[n], channel=c) for n, c in channels.items()])


def test_caller_and_seller_are_found_by_identity_whatever_the_channel_order():
    plan = wire.plan_channels(meta_for(bot=0, caller=2, seller=1), [0.0, 900.0, 800.0], caller_id=CALLER, seller_ids=[SELLER, SELLER_OID])
    assert (plan.mode, plan.caller_channel, plan.seller_channel) == ("stereo", 2, 1)


def test_a_caller_alone_gives_the_caller_channel_only():
    plan = wire.plan_channels(meta_for(bot=0, caller=1), [0.0, 900.0], caller_id=CALLER, seller_ids=[SELLER])
    assert (plan.mode, plan.caller_channel, plan.seller_channel) == ("caller_only", 1, None)


def test_an_unmatched_caller_is_the_one_other_channel_with_speech():
    plan = wire.plan_channels(meta_for(seller=2), [0.0, 900.0, 800.0], caller_id="8:acs:someone-else", seller_ids=[SELLER])
    assert (plan.mode, plan.caller_channel, plan.seller_channel) == ("stereo", 1, 2)


def test_when_nobody_can_be_matched_the_speech_is_mixed_instead_of_guessing():
    ambiguous = wire.plan_channels(meta_for(bot=0), [900.0, 800.0, 700.0], caller_id="x", seller_ids=["y"])
    assert ambiguous.mode == "mono_mix" and "mixed" in ambiguous.note
    single = wire.plan_channels(wire.RecordingMetadata(), [0.0, 900.0], caller_id=None, seller_ids=[])
    assert single.mode == "mono_mix"                                    # one voice, but whose it is was never established


def test_the_caller_and_the_seller_cannot_share_a_channel():
    plan = wire.plan_channels(meta_for(caller=1, seller=1), [0.0, 900.0], caller_id=CALLER, seller_ids=[SELLER])
    assert plan.mode != "stereo"


def test_the_lines_own_channel_is_never_part_of_the_conversation():
    meta = meta_for(caller=0, bot=1)                                      # the line's spoken prompts land on its own channel
    told = wire.plan_channels(meta, [900.0, 800.0], caller_id=CALLER, seller_ids=[SELLER], line_ids=[LINE])
    assert (told.mode, told.caller_channel, told.seller_channel) == ("caller_only", 0, None)
    untold = wire.plan_channels(meta, [900.0, 800.0], caller_id=CALLER, seller_ids=[SELLER])
    assert untold.mode == "mono_mix"                                      # not told which channel is the line: it is one more voice
    with_seller = wire.plan_channels(meta_for(caller=0, seller=2, bot=1), [900.0, 800.0, 700.0], caller_id=CALLER,
                                     seller_ids=[SELLER], line_ids=[LINE])
    assert (with_seller.mode, with_seller.caller_channel, with_seller.seller_channel) == ("stereo", 0, 2)


def test_the_lines_voice_is_left_out_of_a_mix():
    plan = wire.plan_channels(meta_for(bot=0), [900.0, 800.0, 0.0], caller_id="x", seller_ids=["y"], line_ids=[LINE])
    assert plan.mode == "mono_mix" and plan.mix_channels == (1,)
    channels = [square(160, 2500), square(160, 1000), [0] * 160]
    mixed = wire.read_pcm16(wire.render_plan(wire.read_pcm16(make_wav(channels)), plan))
    assert mixed.channels == 1 and list(mixed.channel(0)) == channels[1]


def test_a_stereo_plan_puts_the_caller_on_the_left_and_the_seller_on_the_right():
    channels = [[0] * 160, square(160, 2000), square(160, 700)]
    pcm = wire.read_pcm16(make_wav(channels))
    out = wire.read_pcm16(wire.render_plan(pcm, wire.ChannelPlan("stereo", caller_channel=2, seller_channel=1)))
    assert out.channels == 2 and list(out.channel(0)) == channels[2] and list(out.channel(1)) == channels[1]


def test_a_caller_alone_keeps_the_stereo_layout_with_a_silent_seller_channel():
    channels = [[0] * 160, square(160, 2000), square(160, 1000)]
    pcm = wire.read_pcm16(make_wav(channels))
    alone = wire.read_pcm16(wire.render_plan(pcm, wire.ChannelPlan("caller_only", caller_channel=1)))
    assert alone.channels == 2 and list(alone.channel(0)) == channels[1] and set(alone.channel(1)) == {0}


def test_a_mix_skips_the_silent_channels():
    channels = [[0] * 160, square(160, 2000), square(160, 1000)]
    pcm = wire.read_pcm16(make_wav(channels))
    mixed = wire.read_pcm16(wire.render_plan(pcm, wire.ChannelPlan("mono_mix")))
    assert mixed.channels == 1 and mixed.frames == 160
    assert list(mixed.channel(0)) == [(a + b) // 2 for a, b in zip(channels[1], channels[2], strict=True)]   # the bot's silence is not averaged in


# ---- how much speech a recording holds -------------------------------------------------------------------------------
SECOND = 16000


def talk(*, before: float = 0.0, speaking: float, after: float = 0.0, amplitude: int = 2000) -> list[int]:
    """One channel: silence, then speech-level audio, then silence (in seconds)."""
    return [0] * round(before * SECOND) + square(round(speaking * SECOND), amplitude) + [0] * round(after * SECOND)


def test_speech_seconds_counts_what_is_louder_than_room_noise():
    room_noise = square(4 * SECOND, 12)                                   # what a quiet, open microphone sounds like
    pcm = wire.read_pcm16(make_wav([room_noise, talk(before=1, speaking=2, after=1)]))
    assert wire.speech_seconds(pcm, [0]) == 0.0
    assert wire.speech_seconds(pcm, [1]) == pytest.approx(2.0, abs=0.15)


def test_two_people_talking_at_once_are_counted_once():
    pcm = wire.read_pcm16(make_wav([talk(speaking=2, after=2), talk(before=1, speaking=2, after=1)]))
    assert wire.speech_seconds(pcm, [0]) == pytest.approx(2.0, abs=0.15)
    assert wire.speech_seconds(pcm, [0, 1]) == pytest.approx(3.0, abs=0.15)         # 0-2 s and 1-3 s overlap: three seconds of talking


def test_counting_stops_once_enough_speech_has_been_found():
    pcm = wire.read_pcm16(make_wav([talk(speaking=10)]))
    assert wire.speech_seconds(pcm, [0], stop_at=1.5) == pytest.approx(1.5, abs=0.15)
    assert wire.speech_seconds(pcm, [0]) == pytest.approx(10.0, abs=0.15)


def test_channels_that_are_not_there_hold_no_speech():
    pcm = wire.read_pcm16(make_wav([talk(speaking=2)]))
    assert wire.speech_seconds(pcm, []) == 0.0 and wire.speech_seconds(pcm, [3]) == 0.0


def test_a_plan_draws_on_the_channels_that_carry_the_conversation():
    pcm = wire.read_pcm16(make_wav([[0] * 160, square(160, 2000), square(160, 700)]))
    assert wire.plan_sources(pcm, wire.ChannelPlan("stereo", caller_channel=2, seller_channel=1)) == [2, 1]
    assert wire.plan_sources(pcm, wire.ChannelPlan("caller_only", caller_channel=1)) == [1]
    assert wire.plan_sources(pcm, wire.ChannelPlan("mono_mix", mix_channels=(2,))) == [2]
    assert wire.plan_sources(pcm, wire.ChannelPlan("mono_mix")) == [1, 2]            # no list given: whatever is not silent
    silent = wire.read_pcm16(make_wav([[0] * 160, [0] * 160]))
    assert wire.plan_sources(silent, wire.ChannelPlan("mono_mix")) == [0]


def test_recording_chunks_join_end_to_end_when_they_share_a_format():
    one, two = make_wav([square(100, 500)]), make_wav([square(60, 700)])
    joined = wire.read_pcm16(wire.concat_wavs([one, two]))
    assert joined.frames == 160 and list(joined.channel(0)) == square(100, 500) + square(60, 700)
    with pytest.raises(ValueError, match="differ"):
        wire.concat_wavs([one, make_wav([square(10, 1), square(10, 1)])])
