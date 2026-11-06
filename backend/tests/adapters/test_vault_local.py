"""LocalVault: layout, manifests, expiry sweep and path safety."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from voicecrm.adapters.vault.common import safe_filename, validate_conversation_id
from voicecrm.adapters.vault.local_fs import LocalVault
from voicecrm.domain.conversation import CanonicalConversation, ConversationMeta, Participant, Segment
from voicecrm.domain.enums import Channel, SpeakerRole
from voicecrm.ports.vault import VaultPort

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)


def make_conv(conversation_id: str = "conv-1", text: str = "Hello Priya, thanks for joining. Ünïcode ok?") -> CanonicalConversation:
    return CanonicalConversation(
        meta=ConversationMeta(
            conversation_id=conversation_id, source=Channel.teams, source_interaction_id="meeting-1", started_at=NOW,
            seller=Participant(id="seller", display_name="Rodney Seller", role=SpeakerRole.seller),
            participants=[Participant(id="cust1", display_name="Priya Raman", role=SpeakerRole.customer)],
        ),
        segments=[Segment(id=0, speaker_id="seller", start_ms=0, end_ms=1500, text=text),
                  Segment(id=1, speaker_id="cust1", start_ms=1800, end_ms=2600, text="Happy to be here.")],
    )


@pytest.fixture()
def vault(tmp_path: Path) -> LocalVault:
    return LocalVault(tmp_path / "vault", clock=lambda: NOW)


def test_implements_the_vault_port(vault):
    assert isinstance(vault, VaultPort) and vault.name == "local_fs"


async def test_audio_round_trip_and_layout(vault, tmp_path):
    item = await vault.put_audio("conv-1", b"RIFFdata", filename="call.wav", content_type="audio/wav",
                                 expires_at=NOW + timedelta(days=30))
    assert (item.conversation_id, item.kind, item.size_bytes, item.redacted, item.filename) == ("conv-1", "audio", 8, False, "call.wav")
    assert item.created_at == NOW and item.expires_at == NOW + timedelta(days=30)
    assert item.uri.startswith("file:///")

    directory = tmp_path / "vault" / "conv-1" / "audio"
    assert (directory / "call.wav").read_bytes() == b"RIFFdata"
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == {
        "kind": "audio", "conversation_id": "conv-1", "created_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(days=30)).isoformat(), "redacted": False, "size": 8, "filename": "call.wav",
        "content_type": "audio/wav",
    }
    assert await vault.get_audio("conv-1") == (b"RIFFdata", "call.wav")
    assert await vault.get_audio("nope") is None


async def test_a_new_audio_put_replaces_the_old_file(vault, tmp_path):
    await vault.put_audio("conv-1", b"one", filename="a.wav", content_type="audio/wav", expires_at=None)
    await vault.put_audio("conv-1", b"two", filename="b.mp3", content_type="audio/mpeg", expires_at=None)
    assert sorted(p.name for p in (tmp_path / "vault" / "conv-1" / "audio").iterdir()) == ["b.mp3", "manifest.json"]
    assert await vault.get_audio("conv-1") == (b"two", "b.mp3")


@pytest.mark.parametrize("kind,redacted", [("transcript", True), ("working", False)])
async def test_conversations_round_trip_as_pydantic_json(vault, tmp_path, kind, redacted):
    conv = make_conv()
    item = await vault.put_conversation("conv-1", conv, kind=kind, redacted=redacted, expires_at=NOW + timedelta(hours=4))
    assert item.kind == kind and item.redacted is redacted and item.filename == "conversation.json"
    stored = (tmp_path / "vault" / "conv-1" / kind / "conversation.json").read_text(encoding="utf-8")
    assert CanonicalConversation.model_validate_json(stored) == conv
    assert await vault.get_conversation("conv-1", kind) == conv
    assert (await vault.get_conversation("conv-1", "working" if kind == "transcript" else "transcript")) is None


async def test_no_expiry_is_stored_as_null(vault, tmp_path):
    item = await vault.put_conversation("conv-1", make_conv(), kind="transcript", redacted=True, expires_at=None)
    assert item.expires_at is None
    assert json.loads((tmp_path / "vault" / "conv-1" / "transcript" / "manifest.json").read_text())["expires_at"] is None


async def test_delete_removes_only_the_requested_kinds_and_tidies_up(vault, tmp_path):
    await vault.put_audio("conv-1", b"a", filename="a.wav", content_type="audio/wav", expires_at=None)
    await vault.put_conversation("conv-1", make_conv(), kind="transcript", redacted=True, expires_at=None)
    await vault.put_conversation("conv-1", make_conv(), kind="working", redacted=True, expires_at=None)
    assert await vault.delete("conv-1", ("audio", "working")) == 2
    assert [i.kind for i in await vault.list_items("conv-1")] == ["transcript"]
    assert await vault.delete("conv-1") == 1
    assert not (tmp_path / "vault" / "conv-1").exists()
    assert await vault.delete("conv-1") == 0


async def test_list_items_is_newest_first_and_filterable(tmp_path):
    clock = iter(NOW + timedelta(minutes=i) for i in range(10))
    vault = LocalVault(tmp_path / "v", clock=lambda: next(clock))
    await vault.put_audio("conv-a", b"1", filename="a.wav", content_type="audio/wav", expires_at=None)
    await vault.put_conversation("conv-b", make_conv("conv-b"), kind="transcript", redacted=True, expires_at=None)
    await vault.put_conversation("conv-a", make_conv("conv-a"), kind="working", redacted=True, expires_at=None)
    assert [(i.conversation_id, i.kind) for i in await vault.list_items()] == [
        ("conv-a", "working"), ("conv-b", "transcript"), ("conv-a", "audio")]
    assert {i.kind for i in await vault.list_items("conv-a")} == {"audio", "working"}
    assert await vault.list_items("unknown") == []


async def test_sweep_deletes_everything_past_its_expiry(vault):
    await vault.put_audio("conv-1", b"a", filename="a.wav", content_type="audio/wav", expires_at=NOW - timedelta(seconds=1))
    await vault.put_conversation("conv-1", make_conv(), kind="working", redacted=True, expires_at=NOW)
    await vault.put_conversation("conv-1", make_conv(), kind="transcript", redacted=True, expires_at=NOW + timedelta(days=365))
    await vault.put_conversation("conv-2", make_conv("conv-2"), kind="transcript", redacted=True, expires_at=None)

    swept = await vault.sweep_expired(NOW)
    assert sorted((i.conversation_id, i.kind) for i in swept) == [("conv-1", "audio"), ("conv-1", "working")]
    remaining = {(i.conversation_id, i.kind) for i in await vault.list_items()}
    assert remaining == {("conv-1", "transcript"), ("conv-2", "transcript")}
    assert await vault.sweep_expired(NOW) == []
    assert [i.kind for i in await vault.sweep_expired(NOW + timedelta(days=366))] == ["transcript"]


async def test_sweep_treats_a_naive_now_as_utc(vault):
    await vault.put_audio("conv-1", b"a", filename="a.wav", content_type="audio/wav", expires_at=datetime(2026, 11, 10, 14, 0))
    assert len(await vault.sweep_expired(datetime(2026, 11, 10, 15, 0))) == 1


@pytest.mark.parametrize("bad", ["../evil", "a/b", "a\\b", "C:\\x", "", "con", "nul.txt", "trailing.", "..", ".hidden/..", "x" * 200])
async def test_unsafe_conversation_ids_are_refused(vault, bad):
    with pytest.raises(ValueError, match="unsafe conversation id"):
        await vault.put_audio(bad, b"x", filename="a.wav", content_type="audio/wav", expires_at=None)
    with pytest.raises(ValueError):
        validate_conversation_id(bad)


@pytest.mark.parametrize("name,expected", [
    ("..\\..\\evil.wav", "evil.wav"), ("a/b/c.wav", "c.wav"), ('we:ird*na?me".wav', "we_ird_na_me_.wav"),
    ("con.wav", "_con.wav"), ("NUL", "_NUL"), ("call. ", "call"), ("", "audio.bin"), (None, "audio.bin"),
    ("tab\tname.wav", "tab_name.wav"),
])
def test_filenames_are_windows_safe(name, expected):
    assert safe_filename(name) == expected


def test_overlong_filenames_keep_their_extension():
    name = safe_filename("x" * 300 + ".m4a")
    assert len(name) <= 120 and name.endswith(".m4a")


async def test_hostile_filenames_stay_inside_the_kind_directory(vault, tmp_path):
    await vault.put_audio("conv-1", b"x", filename="..\\..\\..\\escape.wav", content_type="audio/wav", expires_at=None)
    assert (tmp_path / "vault" / "conv-1" / "audio" / "escape.wav").read_bytes() == b"x"
    assert not (tmp_path / "escape.wav").exists()


async def test_corrupt_manifests_are_ignored(vault, tmp_path):
    await vault.put_audio("conv-1", b"x", filename="a.wav", content_type="audio/wav", expires_at=None)
    (tmp_path / "vault" / "conv-1" / "audio" / "manifest.json").write_text("{not json", encoding="utf-8")
    assert await vault.list_items() == []
    assert await vault.get_audio("conv-1") is None


async def test_manifest_without_its_payload_is_not_listed(vault, tmp_path):
    await vault.put_audio("conv-1", b"x", filename="a.wav", content_type="audio/wav", expires_at=None)
    (tmp_path / "vault" / "conv-1" / "audio" / "a.wav").unlink()
    assert await vault.list_items() == []


async def test_concurrent_puts_do_not_interfere(vault):
    await asyncio.gather(*[
        vault.put_conversation(f"conv-{n}", make_conv(f"conv-{n}"), kind="working", redacted=True, expires_at=None)
        for n in range(8)
    ])
    assert len(await vault.list_items()) == 8
    assert (await vault.get_conversation("conv-3", "working")).meta.conversation_id == "conv-3"


async def test_reopening_the_vault_sees_existing_items(tmp_path):
    first = LocalVault(tmp_path / "v", clock=lambda: NOW)
    await first.put_audio("conv-1", b"x", filename="a.wav", content_type="audio/wav", expires_at=None)
    assert [i.conversation_id for i in await LocalVault(tmp_path / "v").list_items()] == ["conv-1"]
