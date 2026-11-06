"""Vault port: raw audio, retained transcripts and short-lived working copies. Never the CRM (requirement RET-2).

kinds:
  audio       raw recording (retention per market policy; 0 days = deleted right after transcription)
  transcript  durable transcript copy, only when the market policy says transcript_retention == "on"
  working     redacted conversation kept while a proposal awaits review; always short-lived (hours)
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from ..domain.base import VModel
from ..domain.conversation import CanonicalConversation

VaultKind = Literal["audio", "transcript", "working"]


class VaultItem(VModel):
    conversation_id: str
    kind: VaultKind
    uri: str
    created_at: datetime
    expires_at: datetime | None = None         # None = no automatic expiry
    size_bytes: int = 0
    redacted: bool = False
    filename: str | None = None


@runtime_checkable
class VaultPort(Protocol):
    name: str

    async def put_audio(
        self, conversation_id: str, data: bytes, *, filename: str, content_type: str, expires_at: datetime | None
    ) -> VaultItem: ...

    async def get_audio(self, conversation_id: str) -> tuple[bytes, str] | None:
        """Returns (bytes, filename) or None."""
        ...

    async def put_conversation(
        self, conversation_id: str, conv: CanonicalConversation, *, kind: Literal["transcript", "working"],
        redacted: bool, expires_at: datetime | None,
    ) -> VaultItem: ...

    async def get_conversation(
        self, conversation_id: str, kind: Literal["transcript", "working"]
    ) -> CanonicalConversation | None: ...

    async def delete(self, conversation_id: str, kinds: tuple[VaultKind, ...] = ("audio", "transcript", "working")) -> int:
        """Delete items of the given kinds; returns the number deleted."""
        ...

    async def list_items(self, conversation_id: str | None = None) -> list[VaultItem]: ...

    async def sweep_expired(self, now: datetime) -> list[VaultItem]:
        """Delete every item whose expires_at <= now; return what was deleted (retention enforcement)."""
        ...
