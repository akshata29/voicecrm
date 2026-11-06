"""BlobVault against an in-memory fake container client (no Azure, no network)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ResourceNotFoundError

from voicecrm.adapters.vault.azure_blob import BlobVault
from voicecrm.domain.conversation import CanonicalConversation
from voicecrm.ports.vault import VaultPort

from .test_vault_local import make_conv

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
METADATA_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class FakeContainer:
    """Implements the part of ContainerClient the vault uses, with Blob metadata semantics."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.url = f"https://acct.blob.core.windows.net/{name}"
        self.blobs: dict[str, dict] = {}
        self.upload_calls: list[dict] = []

    async def upload_blob(self, name, data, *, overwrite=False, metadata=None, content_settings=None, **kwargs):
        if name in self.blobs and not overwrite:
            raise RuntimeError("BlobAlreadyExists")
        assert isinstance(data, bytes)
        self.upload_calls.append({"name": name, "metadata": dict(metadata or {}), "content_settings": content_settings})
        self.blobs[name] = {
            "data": data, "metadata": dict(metadata or {}), "created": NOW,
            "content_type": getattr(content_settings, "content_type", None),
        }

    async def download_blob(self, blob, **kwargs):
        if blob not in self.blobs:
            raise ResourceNotFoundError("The specified blob does not exist.")
        data = self.blobs[blob]["data"]

        async def readall():
            return data

        return SimpleNamespace(readall=readall)

    async def delete_blob(self, blob, **kwargs):
        if blob not in self.blobs:
            raise ResourceNotFoundError("The specified blob does not exist.")
        del self.blobs[blob]

    def list_blobs(self, name_starts_with=None, include=None, **kwargs):
        include = [include] if isinstance(include, str) else (include or [])

        async def generate():
            for name in sorted(self.blobs):
                if name_starts_with and not name.startswith(name_starts_with):
                    continue
                blob = self.blobs[name]
                yield SimpleNamespace(
                    name=name, size=len(blob["data"]), creation_time=blob["created"],
                    metadata=dict(blob["metadata"] or {}) if "metadata" in include else None,
                )

        return generate()


@pytest.fixture()
def containers():
    return FakeContainer("voicecrm-audio"), FakeContainer("voicecrm-transcripts")


@pytest.fixture()
def vault(containers) -> BlobVault:
    audio, transcripts = containers
    return BlobVault(audio, transcripts, clock=lambda: NOW)


def test_implements_the_vault_port(vault):
    assert isinstance(vault, VaultPort) and vault.name == "azure_blob"


async def test_audio_goes_to_the_audio_container_with_metadata(vault, containers):
    audio, transcripts = containers
    item = await vault.put_audio("conv-1", b"RIFFdata", filename="call.wav", content_type="audio/wav",
                                 expires_at=NOW + timedelta(days=30))
    assert list(audio.blobs) == ["conv-1/audio/call.wav"] and transcripts.blobs == {}
    assert audio.blobs["conv-1/audio/call.wav"]["metadata"] == {
        "kind": "audio", "conversation_id": "conv-1", "created_at": NOW.isoformat(), "redacted": "false",
        "filename": "call.wav", "content_type": "audio/wav", "expires_at": (NOW + timedelta(days=30)).isoformat(),
    }
    assert audio.blobs["conv-1/audio/call.wav"]["content_type"] == "audio/wav"
    assert item.uri == "https://acct.blob.core.windows.net/voicecrm-audio/conv-1/audio/call.wav"
    assert (item.kind, item.size_bytes, item.redacted, item.expires_at) == ("audio", 8, False, NOW + timedelta(days=30))
    assert await vault.get_audio("conv-1") == (b"RIFFdata", "call.wav")
    assert await vault.get_audio("nope") is None


async def test_transcripts_and_working_copies_share_the_transcripts_container(vault, containers):
    audio, transcripts = containers
    conv = make_conv()
    await vault.put_conversation("conv-1", conv, kind="transcript", redacted=True, expires_at=NOW + timedelta(days=365))
    await vault.put_conversation("conv-1", conv, kind="working", redacted=True, expires_at=NOW + timedelta(hours=4))
    assert audio.blobs == {}
    assert sorted(transcripts.blobs) == ["conv-1/transcript/conversation.json", "conv-1/working/conversation.json"]
    meta = transcripts.blobs["conv-1/working/conversation.json"]["metadata"]
    assert meta["kind"] == "working" and meta["redacted"] == "true"
    assert transcripts.blobs["conv-1/working/conversation.json"]["content_type"] == "application/json"
    assert await vault.get_conversation("conv-1", "transcript") == conv
    assert isinstance(await vault.get_conversation("conv-1", "working"), CanonicalConversation)
    assert await vault.get_conversation("conv-2", "working") is None


async def test_metadata_names_are_valid_identifiers_and_values_are_ascii(vault, containers):
    audio, _ = containers
    await vault.put_audio("conv-1", b"x", filename="Rodrígo & Priya – call.m4a", content_type="audio/mp4", expires_at=NOW)
    (blob,) = audio.blobs.values()
    for key, value in blob["metadata"].items():
        assert METADATA_NAME.match(key)
        value.encode("ascii")
    assert "%" in blob["metadata"]["filename"]
    # the original (sanitised) name survives the round trip
    assert (await vault.get_audio("conv-1"))[1] == "Rodrígo & Priya – call.m4a"
    assert (await vault.list_items("conv-1"))[0].filename == "Rodrígo & Priya – call.m4a"


async def test_a_new_put_replaces_the_previous_item_of_that_kind(vault, containers):
    audio, _ = containers
    await vault.put_audio("conv-1", b"one", filename="a.wav", content_type="audio/wav", expires_at=None)
    await vault.put_audio("conv-1", b"two", filename="b.mp3", content_type="audio/mpeg", expires_at=None)
    assert list(audio.blobs) == ["conv-1/audio/b.mp3"]
    assert "expires_at" not in audio.blobs["conv-1/audio/b.mp3"]["metadata"]


async def test_list_items_by_prefix_and_across_containers(vault):
    await vault.put_audio("conv-1", b"1", filename="a.wav", content_type="audio/wav", expires_at=None)
    await vault.put_conversation("conv-1", make_conv(), kind="working", redacted=True, expires_at=NOW + timedelta(hours=4))
    await vault.put_conversation("conv-10", make_conv("conv-10"), kind="transcript", redacted=False, expires_at=None)
    one = await vault.list_items("conv-1")
    assert {(i.conversation_id, i.kind) for i in one} == {("conv-1", "audio"), ("conv-1", "working")}   # not conv-10
    everything = await vault.list_items()
    assert len(everything) == 3
    transcript = next(i for i in everything if i.kind == "transcript")
    assert transcript.redacted is False and transcript.expires_at is None and transcript.size_bytes > 0
    assert transcript.uri.startswith("https://acct.blob.core.windows.net/voicecrm-transcripts/conv-10/transcript/")


async def test_delete_removes_only_the_requested_kinds(vault, containers):
    audio, transcripts = containers
    await vault.put_audio("conv-1", b"1", filename="a.wav", content_type="audio/wav", expires_at=None)
    await vault.put_conversation("conv-1", make_conv(), kind="working", redacted=True, expires_at=None)
    await vault.put_conversation("conv-1", make_conv(), kind="transcript", redacted=True, expires_at=None)
    assert await vault.delete("conv-1", ("audio", "working")) == 2
    assert audio.blobs == {} and list(transcripts.blobs) == ["conv-1/transcript/conversation.json"]
    assert await vault.delete("conv-1") == 1
    assert await vault.delete("conv-1") == 0


async def test_sweep_uses_the_expiry_metadata(vault, containers):
    audio, transcripts = containers
    await vault.put_audio("conv-1", b"1", filename="a.wav", content_type="audio/wav", expires_at=NOW - timedelta(minutes=1))
    await vault.put_conversation("conv-1", make_conv(), kind="working", redacted=True, expires_at=NOW)
    await vault.put_conversation("conv-1", make_conv(), kind="transcript", redacted=True, expires_at=NOW + timedelta(days=365))
    await vault.put_conversation("conv-2", make_conv("conv-2"), kind="transcript", redacted=True, expires_at=None)

    swept = await vault.sweep_expired(NOW)
    assert sorted((i.conversation_id, i.kind) for i in swept) == [("conv-1", "audio"), ("conv-1", "working")]
    assert audio.blobs == {} and sorted(transcripts.blobs) == [
        "conv-1/transcript/conversation.json", "conv-2/transcript/conversation.json"]
    assert [i.kind for i in await vault.sweep_expired(NOW + timedelta(days=366))] == ["transcript"]


async def test_blobs_without_metadata_are_listed_but_never_swept(vault, containers):
    _, transcripts = containers
    transcripts.blobs["conv-9/transcript/conversation.json"] = {"data": b"{}", "metadata": None, "created": NOW, "content_type": None}
    transcripts.blobs["stray.txt"] = {"data": b"x", "metadata": {}, "created": NOW, "content_type": None}
    items = await vault.list_items()
    assert [(i.conversation_id, i.kind, i.expires_at) for i in items] == [("conv-9", "transcript", None)]
    assert await vault.sweep_expired(NOW + timedelta(days=9999)) == []


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "con"])
async def test_unsafe_ids_are_refused(vault, bad):
    with pytest.raises(ValueError):
        await vault.put_audio(bad, b"x", filename="a.wav", content_type="audio/wav", expires_at=None)
    with pytest.raises(ValueError):
        await vault.list_items(bad)


async def test_hostile_filenames_cannot_add_path_segments(vault, containers):
    audio, _ = containers
    await vault.put_audio("conv-1", b"x", filename="../../other-conv/audio/evil.wav", content_type="audio/wav", expires_at=None)
    assert list(audio.blobs) == ["conv-1/audio/evil.wav"]


async def test_from_account_url_builds_async_clients_with_entra_auth(monkeypatch):
    import azure.identity.aio as identity_aio
    import azure.storage.blob.aio as blob_aio

    made: dict = {}

    class FakeService:
        def __init__(self, account_url, credential=None):
            made.update(account_url=account_url, credential=credential)
            self.closed = False

        def get_container_client(self, name):
            return FakeContainer(name)

        async def close(self):
            made["service_closed"] = True

    class FakeCredential:
        async def close(self):
            made["credential_closed"] = True

    monkeypatch.setattr(blob_aio, "BlobServiceClient", FakeService)
    monkeypatch.setattr(identity_aio, "DefaultAzureCredential", FakeCredential)
    vault = BlobVault.from_account_url("https://acct.blob.core.windows.net", audio_container="a", transcript_container="t")
    assert made["account_url"] == "https://acct.blob.core.windows.net" and isinstance(made["credential"], FakeCredential)
    item = await vault.put_conversation("conv-1", make_conv(), kind="working", redacted=True, expires_at=None)
    assert item.uri.startswith("https://acct.blob.core.windows.net/t/")
    await vault.aclose()
    assert made["service_closed"] and made["credential_closed"]


async def test_ensure_containers_ignores_existing_ones(containers):
    from azure.core.exceptions import ResourceExistsError

    audio, transcripts = containers
    calls: list[str] = []

    async def create_audio():
        calls.append("audio")

    async def create_transcripts():
        calls.append("transcripts")
        raise ResourceExistsError("exists")

    audio.create_container, transcripts.create_container = create_audio, create_transcripts
    await BlobVault(audio, transcripts).ensure_containers()
    assert calls == ["audio", "transcripts"]
