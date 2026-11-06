"""CRM port. Adapters: local SQLite simulator, Dataverse Web API."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..domain.conversation import ConversationMeta
from ..domain.crm import CrmMatch, CrmPackage, CrmWriteResult


@runtime_checkable
class CrmPort(Protocol):
    name: str

    async def match(self, meta: ConversationMeta) -> CrmMatch:
        """Resolve participants / org to account, contact and opportunity. Never guess: ambiguous -> ambiguous=True."""
        ...

    async def write(self, package: CrmPackage) -> CrmWriteResult:
        """Idempotent on package.idempotency_key: a repeat returns skipped_duplicate=True and writes nothing."""
        ...

    async def list_records(self, entity: str, limit: int = 100) -> list[dict[str, Any]]:
        """For the UI 'CRM view'. entity: account | contact | opportunity | phonecall | task | insight."""
        ...

    async def health(self) -> dict[str, Any]: ...
