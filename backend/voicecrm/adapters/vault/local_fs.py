"""Local filesystem vault: raw audio, retained transcripts and short-lived working copies, kept OUT of the CRM.

Layout (every directory name is validated, so a conversation id can never escape the root):

    <root>\\<conversation_id>\\audio\\<filename>            + manifest.json
    <root>\\<conversation_id>\\transcript\\conversation.json + manifest.json
    <root>\\<conversation_id>\\working\\conversation.json    + manifest.json

manifest.json records kind, conversation_id, created_at, expires_at, redacted, size, filename (and content type).
Conversations are serialised with pydantic JSON. `sweep_expired` is the retention enforcement point.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from ...domain.conversation import CanonicalConversation, utcnow
from ...ports.vault import VaultItem, VaultKind
from .common import as_utc, parse_iso, safe_filename, validate_conversation_id

logger = logging.getLogger(__name__)

KINDS: tuple[VaultKind, ...] = ("audio", "transcript", "working")
MANIFEST = "manifest.json"
CONVERSATION_FILE = "conversation.json"


class LocalVault:
    name = "local_fs"

    def __init__(self, root: str | Path, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()

    # ---- paths -----------------------------------------------------------------------------------------------
    def _kind_dir(self, conversation_id: str, kind: VaultKind) -> Path:
        return self._root / validate_conversation_id(conversation_id) / kind

    def _write_atomic(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def _store(
        self, conversation_id: str, kind: VaultKind, data: bytes, *, filename: str, content_type: str,
        redacted: bool, expires_at: datetime | None,
    ) -> VaultItem:
        with self._lock:
            directory = self._kind_dir(conversation_id, kind)
            if directory.exists():
                shutil.rmtree(directory)          # one item per kind: a new put replaces the old one
            target = directory / filename
            self._write_atomic(target, data)
            manifest = {
                "kind": kind, "conversation_id": conversation_id, "created_at": as_utc(self._clock()).isoformat(),
                "expires_at": as_utc(expires_at).isoformat() if expires_at else None, "redacted": redacted,
                "size": len(data), "filename": filename, "content_type": content_type,
            }
            self._write_atomic(directory / MANIFEST, json.dumps(manifest, indent=2).encode("utf-8"))
        logger.info("vault stored %s for conversation %s (%d bytes)", kind, conversation_id, len(data))
        return self._item(manifest, target)

    def _item(self, manifest: dict[str, Any], payload: Path) -> VaultItem:
        return VaultItem(
            conversation_id=manifest["conversation_id"], kind=manifest["kind"], uri=payload.resolve().as_uri(),
            created_at=parse_iso(manifest.get("created_at")) or self._clock(),
            expires_at=parse_iso(manifest.get("expires_at")), size_bytes=int(manifest.get("size") or 0),
            redacted=bool(manifest.get("redacted")), filename=manifest.get("filename"),
        )

    def _read(self, directory: Path) -> tuple[dict[str, Any], Path] | None:
        manifest_path = directory / MANIFEST
        if not manifest_path.is_file():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload = directory / manifest["filename"]
        except (OSError, ValueError, KeyError):
            logger.warning("vault manifest %s is unreadable; ignoring it", manifest_path.parent.name)
            return None
        return (manifest, payload) if payload.is_file() else None

    # ---- sync implementations (run in a worker thread) ---------------------------------------------------------
    def _put_audio(self, conversation_id: str, data: bytes, filename: str, content_type: str,
                   expires_at: datetime | None) -> VaultItem:
        return self._store(conversation_id, "audio", data, filename=safe_filename(filename), content_type=content_type,
                           redacted=False, expires_at=expires_at)

    def _get_audio(self, conversation_id: str) -> tuple[bytes, str] | None:
        with self._lock:
            found = self._read(self._kind_dir(conversation_id, "audio"))
            return (found[1].read_bytes(), found[0]["filename"]) if found else None

    def _put_conversation(self, conversation_id: str, conv: CanonicalConversation, kind: Literal["transcript", "working"],
                          redacted: bool, expires_at: datetime | None) -> VaultItem:
        data = conv.model_dump_json(indent=2).encode("utf-8")
        return self._store(conversation_id, kind, data, filename=CONVERSATION_FILE, content_type="application/json",
                           redacted=redacted, expires_at=expires_at)

    def _get_conversation(self, conversation_id: str, kind: Literal["transcript", "working"]) -> CanonicalConversation | None:
        with self._lock:
            found = self._read(self._kind_dir(conversation_id, kind))
            if not found:
                return None
            return CanonicalConversation.model_validate_json(found[1].read_text(encoding="utf-8"))

    def _delete(self, conversation_id: str, kinds: tuple[VaultKind, ...]) -> int:
        deleted = 0
        with self._lock:
            for kind in kinds:
                directory = self._kind_dir(conversation_id, kind)
                if directory.exists():
                    shutil.rmtree(directory, ignore_errors=True)
                    deleted += 1
            conversation_dir = self._root / validate_conversation_id(conversation_id)
            if conversation_dir.is_dir() and not any(conversation_dir.iterdir()):
                conversation_dir.rmdir()
        if deleted:
            logger.info("vault deleted %d item(s) for conversation %s", deleted, conversation_id)
        return deleted

    def _list(self, conversation_id: str | None) -> list[VaultItem]:
        with self._lock:
            if conversation_id is not None:
                directories = [self._root / validate_conversation_id(conversation_id)]
            else:
                directories = [d for d in self._root.iterdir() if d.is_dir()]
            items: list[VaultItem] = []
            for conversation_dir in directories:
                for kind in KINDS:
                    found = self._read(conversation_dir / kind)
                    if found:
                        items.append(self._item(*found))
        return sorted(items, key=lambda i: (i.created_at, i.conversation_id, i.kind), reverse=True)

    def _sweep(self, now: datetime) -> list[VaultItem]:
        cutoff = as_utc(now)
        expired = [i for i in self._list(None) if i.expires_at is not None and as_utc(i.expires_at) <= cutoff]
        for item in expired:
            self._delete(item.conversation_id, (item.kind,))
        if expired:
            logger.info("vault retention sweep removed %d item(s)", len(expired))
        return expired

    # ---- VaultPort ---------------------------------------------------------------------------------------------
    async def put_audio(
        self, conversation_id: str, data: bytes, *, filename: str, content_type: str, expires_at: datetime | None
    ) -> VaultItem:
        return await asyncio.to_thread(self._put_audio, conversation_id, data, filename, content_type, expires_at)

    async def get_audio(self, conversation_id: str) -> tuple[bytes, str] | None:
        return await asyncio.to_thread(self._get_audio, conversation_id)

    async def put_conversation(
        self, conversation_id: str, conv: CanonicalConversation, *, kind: Literal["transcript", "working"],
        redacted: bool, expires_at: datetime | None,
    ) -> VaultItem:
        return await asyncio.to_thread(self._put_conversation, conversation_id, conv, kind, redacted, expires_at)

    async def get_conversation(
        self, conversation_id: str, kind: Literal["transcript", "working"]
    ) -> CanonicalConversation | None:
        return await asyncio.to_thread(self._get_conversation, conversation_id, kind)

    async def delete(self, conversation_id: str, kinds: tuple[VaultKind, ...] = KINDS) -> int:
        return await asyncio.to_thread(self._delete, conversation_id, kinds)

    async def list_items(self, conversation_id: str | None = None) -> list[VaultItem]:
        return await asyncio.to_thread(self._list, conversation_id)

    async def sweep_expired(self, now: datetime) -> list[VaultItem]:
        return await asyncio.to_thread(self._sweep, now)
