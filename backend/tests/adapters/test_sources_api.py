"""Public API of the sources package: detect_and_parse, render_for_channel and the per-format round trips."""
from __future__ import annotations

import io
import json
import wave

import pytest

from voicecrm.adapters.sources import (
    ParsedSource, RenderedPayload, detect_and_parse, parse_mobile_upload, render_for_channel,
)
from voicecrm.adapters.sources.mobile import silent_wav
from voicecrm.domain.conversation import CanonicalConversation
from voicecrm.domain.enums import Channel, TranscriptOrigin

from .test_sources_fixtures import make_call, spanish_call

NAMES = {"seller": "Rodney Seller", "cust1": "Priya Raman", "cust2": "Marcus Lee", "int1": "Dana Colleague"}


def speaker_ids(call):
    return [t.speaker_id or {"seller": "seller", "customer": "cust1", "internal": "int1"}[t.speaker] for t in call.turns]


def expected_labels(call, channel):
    if channel == Channel.genesys:
        return ["agent" if t.speaker in ("seller", "internal") else "customer" for t in call.turns]
    if channel in (Channel.mobile, Channel.upload):
        return speaker_ids(call)
    return [NAMES[i] for i in speaker_ids(call)]


ROUND_TRIPS = [
    (Channel.teams, {}, TranscriptOrigin.teams_platform),
    (Channel.zoom, {}, TranscriptOrigin.zoom_platform),
    (Channel.zoom, {"zoom_format": "json"}, TranscriptOrigin.zoom_platform),
    (Channel.genesys, {}, TranscriptOrigin.genesys_platform),
    (Channel.mobile, {}, TranscriptOrigin.fixture),
    (Channel.upload, {}, TranscriptOrigin.fixture),
]
IDS = ["teams-vtt", "zoom-vtt", "zoom-json", "genesys-json", "mobile-canonical", "upload-canonical"]


@pytest.mark.parametrize("channel,options,origin", ROUND_TRIPS, ids=IDS)
@pytest.mark.parametrize("call", [make_call(), spanish_call()], ids=["english", "spanish"])
def test_parse_of_render_reproduces_speakers_order_and_text(call, channel, options, origin):
    rendered = render_for_channel(call, channel, **options)
    parsed = detect_and_parse(rendered.filename, rendered.content_type, rendered.data, channel=channel)

    assert isinstance(parsed, ParsedSource) and parsed.kind == "transcript" and parsed.channel == channel
    assert parsed.origin == origin
    assert [s.speaker_id for s in parsed.segments] == expected_labels(call, channel)
    assert [s.text for s in parsed.segments] == [t.text for t in call.turns]
    assert [s.id for s in parsed.segments] == list(range(len(call.turns)))
    assert parsed.speakers == list(dict.fromkeys(expected_labels(call, channel)))
    assert parsed.meta_hint["duration_seconds"] > 0
    assert parsed.audio_bytes is None and parsed.audio_filename is None
    if channel in (Channel.teams, Channel.genesys, Channel.mobile, Channel.upload) or options:
        assert parsed.source_interaction_id == rendered.source_interaction_id


def test_the_language_is_recovered_for_every_format():
    english, spanish = make_call(), spanish_call()
    for channel, options, _ in ROUND_TRIPS:
        for call, expected in ((english, "en"), (spanish, "es")):
            rendered = render_for_channel(call, channel, **options)
            parsed = detect_and_parse(rendered.filename, rendered.content_type, rendered.data, channel=channel)
            assert parsed.language is not None and parsed.language.startswith(expected)


def test_canonical_json_keeps_metadata_for_the_pipeline():
    call = make_call()
    rendered = render_for_channel(call, Channel.upload)
    conv = CanonicalConversation.model_validate_json(rendered.data)
    assert conv.meta.source_interaction_id == rendered.source_interaction_id == "upload-pb-teams-equity-class-c"
    parsed = detect_and_parse(rendered.filename, rendered.content_type, rendered.data, channel=Channel.upload)
    assert parsed.language == "en-US" and parsed.origin == TranscriptOrigin.fixture
    hint = parsed.meta_hint
    assert (hint["title"], hint["customer_org"], hint["call_type"], hint["market"]) == (
        call.title, "Meridian Pension Fund", "product_recommendation", "US")
    assert hint["seller_hint"] == {"name": "Rodney Seller", "email": "rodney.seller@contoso.example"}
    assert {"name": "Priya Raman", "email": "priya.raman@meridianpension.example"} in hint["participants"]
    assert hint["started_at"].startswith("2026-11-10T15:00:00") and hint["duration_seconds"] == 720.0


# ---- detection ------------------------------------------------------------------------------------------------------------
def test_vtt_flavour_is_decided_by_structure_not_by_the_declared_channel():
    teams_file = render_for_channel(make_call(), Channel.teams)
    as_upload = detect_and_parse(teams_file.filename, "text/vtt", teams_file.data, channel=Channel.upload)
    assert as_upload.channel == Channel.upload and as_upload.origin == TranscriptOrigin.teams_platform
    zoom_file = render_for_channel(make_call(), Channel.zoom)
    as_teams = detect_and_parse(zoom_file.filename, None, zoom_file.data, channel=Channel.teams)
    assert as_teams.channel == Channel.teams and as_teams.origin == TranscriptOrigin.zoom_platform


def test_content_wins_over_name_and_content_type():
    vtt = render_for_channel(make_call(), Channel.teams).data
    assert detect_and_parse("whatever.bin", "application/octet-stream", vtt, channel=Channel.upload).kind == "transcript"
    assert detect_and_parse("notes.vtt", "audio/wav", vtt, channel=Channel.upload).kind == "transcript"
    genesys = render_for_channel(make_call(), Channel.genesys).data
    assert detect_and_parse("transcript.txt", "text/plain", genesys, channel=Channel.genesys).origin == TranscriptOrigin.genesys_platform


def test_bom_and_crlf_are_tolerated():
    text = render_for_channel(make_call(), Channel.teams).data.decode().replace("\n", "\r\n")
    parsed = detect_and_parse("t.vtt", "text/vtt", b"\xef\xbb\xbf" + text.encode(), channel=Channel.teams)
    assert [s.text for s in parsed.segments] == [t.text for t in make_call().turns]


@pytest.mark.parametrize("filename,content_type,fmt", [
    ("call.wav", "audio/wav", "wav"), ("call.mp3", "audio/mpeg", "mp3"), ("call.m4a", "audio/mp4", "m4a"),
    ("call.ogg", "audio/ogg", "ogg"), ("call.webm", "audio/webm", "webm"),
])
def test_audio_is_recognised_from_its_bytes(filename, content_type, fmt):
    samples = {
        "wav": silent_wav(1.0), "mp3": b"ID3\x04\x00\x00\x00\x00\x00\x21" + b"\x00" * 16,
        "m4a": b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00M4A mp42isom" + b"\x00" * 16, "ogg": b"OggS\x00\x02" + b"\x00" * 16,
        "webm": b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01" + b"\x00" * 16,
    }
    parsed = detect_and_parse(filename, content_type, samples[fmt], channel=Channel.mobile)
    assert (parsed.kind, parsed.channel, parsed.audio_bytes, parsed.audio_filename) == ("audio", Channel.mobile, samples[fmt], filename)
    assert parsed.audio_content_type == content_type
    assert parsed.segments == [] and parsed.speakers == [] and parsed.origin is None and parsed.language is None


def test_audio_is_identified_by_content_even_when_the_name_lies():
    parsed = detect_and_parse("recording.mp3", "audio/mpeg", silent_wav(1.0), channel=Channel.upload)
    assert parsed.audio_content_type == "audio/wav" and parsed.audio_filename == "recording.mp3"


def test_unrecognised_bytes_with_an_audio_label_are_accepted_as_audio():
    parsed = detect_and_parse("voice.wav", "audio/wav", b"not really a wav, but declared as one", channel=Channel.mobile)
    assert parsed.kind == "audio" and parsed.audio_content_type == "audio/wav"
    assert detect_and_parse(None, "audio/webm", b"\x00\x01\x02\x03\x04", channel=Channel.mobile).audio_filename == "audio.webm"


def test_audio_filenames_lose_their_directories():
    parsed = detect_and_parse("C:\\Users\\rodney\\voice memos\\call.wav", "audio/wav", silent_wav(1.0), channel=Channel.mobile)
    assert parsed.audio_filename == "call.wav"


@pytest.mark.parametrize("filename,content_type,data,message", [
    ("a.wav", "audio/wav", b"", "empty"),
    ("a.txt", "text/plain", b"just some words", "unsupported"),
    ("a.pdf", "application/pdf", b"%PDF-1.7 ...", "unsupported"),
    ("a.json", "application/json", b"{not json", "valid JSON"),
    ("a.json", "application/json", b'{"hello": "world"}', "unrecognised JSON"),
    ("a.json", "application/json", b"[1, 2, 3]", "unrecognised JSON"),
    ("a.vtt", "text/vtt", b"WEBVTT\n\n", "no speech"),
    ("a.json", "application/json", b'{"transcripts": [{"phrases": []}]}', "no speech"),
    ("a.json", "application/json", b'{"timeline": []}', "no speech"),
])
def test_bad_input_is_a_value_error_with_a_reason(filename, content_type, data, message):
    with pytest.raises(ValueError, match=message):
        detect_and_parse(filename, content_type, data, channel=Channel.upload)


def test_a_malformed_canonical_conversation_is_a_value_error():
    with pytest.raises(ValueError):
        detect_and_parse("c.json", "application/json", json.dumps({"meta": {}, "segments": []}).encode(), channel=Channel.upload)


# ---- rendering --------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("channel,options,filename,content_type", [
    (Channel.teams, {}, "teams-pb-teams-equity-class-c.vtt", "text/vtt"),
    (Channel.zoom, {}, "zoom-pb-teams-equity-class-c.vtt", "text/vtt"),
    (Channel.zoom, {"zoom_format": "json"}, "zoom-pb-teams-equity-class-c.json", "application/json"),
    (Channel.genesys, {}, "genesys-pb-teams-equity-class-c.json", "application/json"),
    (Channel.mobile, {}, "mobile-pb-teams-equity-class-c.json", "application/json"),
    (Channel.upload, {}, "upload-pb-teams-equity-class-c.json", "application/json"),
])
def test_each_channel_gets_its_native_payload(channel, options, filename, content_type):
    call = make_call()
    rendered = render_for_channel(call, channel, **options)
    assert isinstance(rendered, RenderedPayload) and rendered.channel == channel
    assert (rendered.filename, rendered.content_type) == (filename, content_type)
    assert isinstance(rendered.data, bytes) and rendered.data
    assert rendered.source_interaction_id
    assert render_for_channel(call, channel, **options) == rendered                      # deterministic


def test_the_channel_defaults_to_the_fixtures_and_can_be_overridden():
    call = make_call(channel=Channel.zoom)
    assert render_for_channel(call).channel == Channel.zoom and render_for_channel(call).filename.endswith(".vtt")
    assert render_for_channel(call, Channel.genesys).channel == Channel.genesys


def test_interaction_ids_are_stable_per_fixture_and_channel():
    call = make_call()
    ids = {c: render_for_channel(call, c).source_interaction_id for c in Channel}
    assert len(set(ids.values())) == len(ids)
    assert render_for_channel(make_call("other-fixture"), Channel.teams).source_interaction_id != ids[Channel.teams]
    assert ids[Channel.zoom] == "zoom-pb-teams-equity-class-c" and ids[Channel.mobile] == "mobile-pb-teams-equity-class-c"


@pytest.mark.parametrize("channel", [Channel.teams, Channel.zoom, Channel.genesys, Channel.mobile, None])
def test_as_audio_returns_a_tiny_valid_silent_wav_named_after_the_fixture(channel):
    call = make_call()
    rendered = render_for_channel(call, channel, as_audio=True)
    assert rendered.filename == "fixture-pb-teams-equity-class-c.wav" and rendered.content_type == "audio/wav"
    assert rendered.channel == (channel or call.channel)
    with wave.open(io.BytesIO(rendered.data)) as handle:
        assert handle.getnframes() / handle.getframerate() == pytest.approx(30.0)
        assert handle.readframes(100) == b"\x00" * 200
    parsed = detect_and_parse(rendered.filename, rendered.content_type, rendered.data, channel=rendered.channel)
    assert parsed.kind == "audio" and parsed.audio_content_type == "audio/wav"


def test_short_calls_get_proportionally_shorter_audio():
    rendered = render_for_channel(make_call(duration_minutes=0), as_audio=True)
    with wave.open(io.BytesIO(rendered.data)) as handle:
        assert handle.getnframes() / handle.getframerate() == pytest.approx(1.0)


def test_spanish_text_survives_as_utf8():
    call = spanish_call()
    for channel, options, _ in ROUND_TRIPS:
        data = render_for_channel(call, channel, **options).data
        assert "Quería hablar con usted" in data.decode("utf-8")


def test_mobile_upload_is_exported_from_the_package():
    audio = render_for_channel(make_call(), Channel.mobile, as_audio=True)
    parsed = parse_mobile_upload(audio.data, audio.filename, audio.content_type, {"capture_id": "cap-1", "offline": True})
    assert parsed.kind == "audio" and parsed.source_interaction_id == "cap-1" and parsed.meta_hint["offline"] is True
