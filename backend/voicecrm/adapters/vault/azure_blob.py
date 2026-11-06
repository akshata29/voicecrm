"""Azure Blob vault: raw audio and transcripts in separate containers, authenticated with Entra ID (no keys in code).

Blob names:   <conversation_id>/<kind>/<filename>
Containers:   audio -> the audio container;  transcript + working -> the transcripts container
Metadata:     kind, conversation_id, created_at, expires_at, redacted, filename (percent-encoded), content_type
              (names are C# identifiers as Blob metadata requires; expires_at is absent when there is no expiry)

`sweep_expired` lists blobs with their metadata and deletes everything past `expires_at`. It is the application-level
retention enforcement; a storage lifecycle-management rule on the containers is a sensible second line of defence.

The containers are injected (anything with upload_blob / download_blob / delete_blob / list_blobs / url), so tests use
an in-memory fake; `from_account_url` builds the real async clients. Live use of the async SDK needs `aiohttp`.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal, Protocol
from urllib.parse import quote, unquote

from azure.core.exceptions import ResourceNotFoundError

from ...domain.conversation import CanonicalConversation, utcnow
from ...ports.vault import VaultItem, VaultKind
from .common import as_utc, parse_iso, safe_filename, validate_conversation_id

logger = logging.getLogger(__name__)

KINDS: tuple[VaultKind, ...] = ("audio", "transcript", "working")
CONVERSATION_FILE = "conversation.json"


class ContainerLike(Protocol):
    """The slice of azure.storage.blob.aio.ContainerClient this adapter uses."""

    url: str

    async def upload_blob(self, name: str, data: bytes, **kwargs: Any) -> Any: ...

    async def download_blob(self, blob: str, **kwargs: Any) -> Any: ...

    async def delete_blob(self, blob: str, **kwargs: Any) -> None: ...

    def list_blobs(self, name_starts_with: str | None = None, include: Any = None, **kwargs: Any) -> Any: ...


def _ascii(value: str) -> str:
    return quote(value, safe=" ._-~")


class BlobVault:
    name = "azure_blob"

    def __init__(
        self,
        audio_container: ContainerLike,
        transcript_container: ContainerLike,
        *,
        clock: Callable[[], datetime] = utcnow,
        closers: list[Callable[[], Any]] | None = None,
    ) -> None:
        self._audio = audio_container
        self._transcripts = transcript_container
        self._clock = clock
        self._closers = closers or []

    @classmethod
    def from_account_url(
        cls,
        account_url: str,
        *,
        audio_container: str = "voicecrm-audio",
        transcript_container: str = "voicecrm-transcripts",
        credential: Any = None,
    ) -> BlobVault:
        """Real async clients with Entra auth (DefaultAzureCredential unless a credential is passed)."""
        from azure.storage.blob.aio import BlobServiceClient    # imported late: the async transport needs aiohttp

        closers: list[Callable[[], Any]] = []
        if credential is None:
            from azure.identity.aio import DefaultAzureCredential

            credential = DefaultAzureCredential()
            closers.append(credential.close)
        service = BlobServiceClient(account_url, credential=credential)
        closers.insert(0, service.close)
        return cls(
            service.get_container_client(audio_container), service.get_container_client(transcript_container),
            closers=closers,
        )

    async def aclose(self) -> None:
        for closer in self._closers:
            await closer()

    async def ensure_containers(self) -> None:
        """Create the containers when they are missing (needs data-plane container write permission)."""
        from azure.core.exceptions import ResourceExistsError

        for container in (self._audio, self._transcripts):
            create = getattr(container, "create_container", None)
            if create is None:
                continue
            try:
                await create()
            except ResourceExistsError:
                pass

    # ---- helpers -----------------------------------------------------------------------------------------------
    def _container(self, kind: VaultKind) -> ContainerLike:
        return self._audio if kind == "audio" else self._transcripts

    @staticmethod
    def _blob_name(conversation_id: str, kind: VaultKind, filename: str) -> str:
        return f"{validate_conversation_id(conversation_id)}/{kind}/{filename}"

    @staticmethod
    def _prefix(conversation_id: str, kind: VaultKind | None = None) -> str:
        base = f"{validate_conversation_id(conversation_id)}/"
        return f"{base}{kind}/" if kind else base

    def _metadata(self, conversation_id: str, kind: VaultKind, filename: str, content_type: str, redacted: bool,
                  expires_at: datetime | None) -> dict[str, str]:
        meta = {
            "kind": kind, "conversation_id": conversation_id, "created_at": as_utc(self._clock()).isoformat(),
            "redacted": "true" if redacted else "false", "filename": _ascii(filename), "content_type": content_type,
        }
        if expires_at is not None:
            meta["expires_at"] = as_utc(expires_at).isoformat()
        return meta

    def _item(self, container: ContainerLike, blob: Any) -> VaultItem | None:
        meta = {str(k).lower(): v for k, v in (getattr(blob, "metadata", None) or {}).items()}
        parts = str(blob.name).split("/", 2)
        if len(parts) != 3 or parts[1] not in KINDS:
            return None
        created = parse_iso(meta.get("created_at")) or getattr(blob, "creation_time", None) or self._clock()
        return VaultItem(
            conversation_id=meta.get("conversation_id") or parts[0], kind=parts[1],     # type: ignore[arg-type]
            uri=f"{container.url.rstrip('/')}/{quote(str(blob.name))}", created_at=as_utc(created),
            expires_at=parse_iso(meta.get("expires_at")), size_bytes=int(getattr(blob, "size", 0) or 0),
            redacted=str(meta.get("redacted", "false")).lower() == "true",
            filename=unquote(meta["filename"]) if meta.get("filename") else parts[2],
        )

    async def _blobs(self, container: ContainerLike, prefix: str | None) -> list[Any]:
        return [b async for b in container.list_blobs(name_starts_with=prefix, include=["metadata"])]

    async def _upload(self, kind: VaultKind, conversation_id: str, filename: str, data: bytes, content_type: str,
                      redacted: bool, expires_at: datetime | None) -> VaultItem:
        from azure.storage.blob import ContentSettings

        container = self._container(kind)
        name = self._blob_name(conversation_id, kind, filename)
        meta = self._metadata(conversation_id, kind, filename, content_type, redacted, expires_at)
        await container.upload_blob(name, data, overwrite=True, metadata=meta,
                                    content_settings=ContentSettings(content_type=content_type))
        for stale in await self._blobs(container, self._prefix(conversation_id, kind)):
            if stale.name != name:                 # one item per kind: a new put replaces the old one
                await container.delete_blob(stale.name)
        logger.info("vault stored %s for conversation %s (%d bytes)", kind, conversation_id, len(data))
        return VaultItem(
            conversation_id=conversation_id, kind=kind, uri=f"{container.url.rstrip('/')}/{quote(name)}",
            created_at=parse_iso(meta["created_at"]) or self._clock(), expires_at=parse_iso(meta.get("expires_at")),
            size_bytes=len(data), redacted=redacted, filename=filename,
        )

    # ---- VaultPort ---------------------------------------------------------------------------------------------
    async def put_audio(
        self, conversation_id: str, data: bytes, *, filename: str, content_type: str, expires_at: datetime | None
    ) -> VaultItem:
        return await self._upload("audio", conversation_id, safe_filename(filename), data, content_type, False, expires_at)

    async def get_audio(self, conversation_id: str) -> tuple[bytes, str] | None:
        blobs = await self._blobs(self._audio, self._prefix(conversation_id, "audio"))
        if not blobs:
            return None
        blob = blobs[0]
        item = self._item(self._audio, blob)
        try:
            data = await (await self._audio.download_blob(blob.name)).readall()
        except ResourceNotFoundError:
            return None
        return bytes(data), (item.filename if item and item.filename else str(blob.name).rsplit("/", 1)[-1])

    async def put_conversation(
        self, conversation_id: str, conv: CanonicalConversation, *, kind: Literal["transcript", "working"],
        redacted: bool, expires_at: datetime | None,
    ) -> VaultItem:
        data = conv.model_dump_json(indent=2).encode("utf-8")
        return await self._upload(kind, conversation_id, CONVERSATION_FILE, data, "application/json", redacted, expires_at)

    async def get_conversation(
        self, conversation_id: str, kind: Literal["transcript", "working"]
    ) -> CanonicalConversation | None:
        name = self._blob_name(conversation_id, kind, CONVERSATION_FILE)
        try:
            data = await (await self._transcripts.download_blob(name)).readall()
        except ResourceNotFoundError:
            return None
        return CanonicalConversation.model_validate_json(bytes(data))

    async def delete(self, conversation_id: str, kinds: tuple[VaultKind, ...] = KINDS) -> int:
        deleted = 0
        for kind in kinds:
            container = self._container(kind)
            for blob in await self._blobs(container, self._prefix(conversation_id, kind)):
                try:
                    await container.delete_blob(blob.name)
                    deleted += 1
                except ResourceNotFoundError:
                    continue
        if deleted:
            logger.info("vault deleted %d item(s) for conversation %s", deleted, conversation_id)
        return deleted

    async def list_items(self, conversation_id: str | None = None) -> list[VaultItem]:
        prefix = self._prefix(conversation_id) if conversation_id is not None else None
        items: list[VaultItem] = []
        for container in (self._audio, self._transcripts):
            for blob in await self._blobs(container, prefix):
                item = self._item(container, blob)
                if item is not None:
                    items.append(item)
        return sorted(items, key=lambda i: (i.created_at, i.conversation_id, i.kind), reverse=True)

    async def sweep_expired(self, now: datetime) -> list[VaultItem]:
        cutoff = as_utc(now)
        expired: list[VaultItem] = []
        for container in (self._audio, self._transcripts):
            for blob in await self._blobs(container, None):
                item = self._item(container, blob)
                if item is None or item.expires_at is None or as_utc(item.expires_at) > cutoff:
                    continue
                try:
                    await container.delete_blob(blob.name)
                except ResourceNotFoundError:
                    continue
                expired.append(item)
        if expired:
            logger.info("vault retention sweep removed %d item(s)", len(expired))
        return expired
