"""Mobile uploads: audio sniffing, the silent fixture WAV and the JSON sidecar."""
from __future__ import annotations

import io
import json
import wave
from datetime import datetime, timezone

import pytest

from voicecrm.adapters.sources import parse_mobile_upload
from voicecrm.adapters.sources.mobile import (
    M4A, MP3, OGG, WAV, WEBM, MobileSidecar, audio_from_hints, audio_seconds, parse_mobile_sidecar, render_mobile_sidecar,
    silent_wav, sniff_audio,
)
from voicecrm.domain.enums import Channel

from .test_sources_fixtures import make_call

FIXTURES = {
    "wav": silent_wav(1.0),
    "mp3-id3": b"ID3\x04\x00\x00\x00\x00\x00\x21" + b"\x00" * 32,
    "mp3-frame": b"\xff\xfb\x90\x64" + b"\x00" * 32,
    "m4a": b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00M4A mp42isom" + b"\x00" * 16,
    "mp4-audio": b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom",
    "ogg": b"OggS\x00\x02" + b"\x00" * 32,
    "webm": b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01" + b"\x00" * 32,
}


@pytest.mark.parametrize("name,expected", [
    ("wav", WAV), ("mp3-id3", MP3), ("mp3-frame", MP3), ("m4a", M4A), ("mp4-audio", M4A), ("ogg", OGG), ("webm", WEBM),
])
def test_sniffing_recognises_the_supported_formats_from_their_bytes(name, expected):
    assert sniff_audio(FIXTURES[name]) == expected


def test_sniffing_ignores_a_misleading_filename():
    assert sniff_audio(FIXTURES["wav"], "recording.mp3") == WAV
    assert sniff_audio(FIXTURES["webm"], "note.wav").format == "webm"


@pytest.mark.parametrize("data", [b"", b"abc", b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi", b'{"json": true}', b"\x00" * 32,
                                  b"\xff\xff\xff\xff"])
def test_unrecognised_bytes_are_not_audio(data):
    assert sniff_audio(data) is None


def test_format_details():
    assert (WAV.content_type, WAV.extension) == ("audio/wav", ".wav")
    assert (MP3.content_type, M4A.content_type, OGG.content_type, WEBM.content_type) == (
        "audio/mpeg", "audio/mp4", "audio/ogg", "audio/webm")


@pytest.mark.parametrize("filename,content_type,expected", [
    ("a.wav", None, WAV), ("a.MP3", None, MP3), ("a.m4a", "", M4A), ("a.mp4", None, M4A), ("a.opus", None, OGG),
    ("a.bin", "audio/webm;codecs=opus", WEBM), ("a.bin", "audio/x-wav", WAV), ("a.bin", "video/webm", WEBM),
    ("a.txt", "text/plain", None), (None, None, None),
])
def test_declared_type_and_extension_are_the_fallback(filename, content_type, expected):
    assert audio_from_hints(filename, content_type) == expected


# ---- silent WAV --------------------------------------------------------------------------------------------------------
def test_silent_wav_is_a_valid_16_bit_mono_pcm_file():
    data = silent_wav(2.5)
    with wave.open(io.BytesIO(data)) as handle:
        assert (handle.getnchannels(), handle.getsampwidth(), handle.getframerate()) == (1, 2, 8000)
        assert handle.getnframes() == 20000
        assert handle.readframes(handle.getnframes()) == b"\x00\x00" * 20000
    assert sniff_audio(data) == WAV


@pytest.mark.parametrize("minutes,expected", [(12, 30.0), (1, 30.0), (0, 1.0)])
def test_fixture_audio_length_is_capped_at_30_seconds(minutes, expected):
    assert audio_seconds(make_call(duration_minutes=minutes)) == expected


# ---- sidecar -----------------------------------------------------------------------------------------------------------------
NESTED = {
    "capture_id": "cap-42", "seller": {"name": "Rodney Seller", "email": "rodney.seller@contoso.example"},
    "client": {"name": "Priya Raman", "org": "Meridian Pension Fund", "email": "priya.raman@meridianpension.example",
               "phone": "+1-555-0100"},
    "captured_at": "2026-11-10T15:00:00Z", "duration_seconds": 312.5, "offline": True, "language_hint": "es-US",
    "title": "Visit", "unknown_key": "ignored",
}


def test_nested_sidecar_is_flattened():
    sidecar = parse_mobile_sidecar(NESTED)
    assert isinstance(sidecar, MobileSidecar)
    assert (sidecar.seller_name, sidecar.client_org, sidecar.offline, sidecar.language_hint) == (
        "Rodney Seller", "Meridian Pension Fund", True, "es-US")
    assert sidecar.captured_at == datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)


def test_flat_sidecar_and_all_input_types():
    flat = {"seller_email": "r@contoso.example", "client_name": "Priya", "offline": False}
    assert parse_mobile_sidecar(flat).client_name == "Priya"
    assert parse_mobile_sidecar(json.dumps(flat)).seller_email == "r@contoso.example"
    assert parse_mobile_sidecar(json.dumps(flat).encode("utf-8-sig")).client_name == "Priya"
    for empty in (None, "", b""):
        assert parse_mobile_sidecar(empty) == MobileSidecar()


def test_meta_hint_shape():
    hint = parse_mobile_sidecar(NESTED).to_meta_hint()
    assert hint == {
        "offline": True, "title": "Visit", "started_at": "2026-11-10T15:00:00+00:00", "duration_seconds": 312.5,
        "source_interaction_id": "cap-42", "language_hint": "es-US",
        "seller_hint": {"name": "Rodney Seller", "email": "rodney.seller@contoso.example"},
        "client_hint": {"name": "Priya Raman", "org": "Meridian Pension Fund",
                        "email": "priya.raman@meridianpension.example", "phone": "+1-555-0100"},
        "participants": [{"name": "Priya Raman", "email": "priya.raman@meridianpension.example"}],
    }
    assert parse_mobile_sidecar({}).to_meta_hint() == {"offline": False}


def test_bad_sidecars():
    with pytest.raises(ValueError):
        parse_mobile_sidecar("{not json")
    with pytest.raises(ValueError, match="object"):
        parse_mobile_sidecar("[1, 2]")
    assert parse_mobile_sidecar({"captured_at": "last tuesday"}).captured_at is None


def test_rendered_sidecar_round_trips():
    call = make_call()
    raw = render_mobile_sidecar(call)
    sidecar = parse_mobile_sidecar(json.loads(json.dumps(raw)))
    assert (sidecar.capture_id, sidecar.seller_name, sidecar.client_name) == (
        f"mobile-{call.fixture_id}", "Rodney Seller", "Priya Raman")
    assert (sidecar.client_org, sidecar.offline, sidecar.language_hint) == ("Meridian Pension Fund", True, "en-US")
    assert sidecar.captured_at == datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
    assert sidecar.duration_seconds == 720.0


# ---- upload ----------------------------------------------------------------------------------------------------------------------
def test_mobile_upload_carries_the_audio_and_the_capture_hints():
    source = parse_mobile_upload(FIXTURES["webm"], "visit.webm", "audio/webm;codecs=opus", NESTED)
    assert (source.kind, source.channel, source.audio_filename, source.audio_content_type) == (
        "audio", Channel.mobile, "visit.webm", "audio/webm")
    assert source.audio_bytes == FIXTURES["webm"] and source.segments == [] and source.origin is None
    assert source.source_interaction_id == "cap-42"
    assert "source_interaction_id" not in source.meta_hint and source.meta_hint["offline"] is True
    assert source.meta_hint["client_hint"]["org"] == "Meridian Pension Fund"


def test_mobile_upload_without_a_sidecar_and_with_bad_input():
    source = parse_mobile_upload(FIXTURES["wav"], "x.wav", "audio/wav")
    assert source.meta_hint == {"offline": False} and source.source_interaction_id is None
    with pytest.raises(ValueError, match="empty"):
        parse_mobile_upload(b"", "x.wav", "audio/wav")
    with pytest.raises(ValueError, match="supported audio"):
        parse_mobile_upload(b"just text", "x.txt", "text/plain")
