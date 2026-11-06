"""One Entra credential shared by every live Azure adapter, optionally pinned to the tenant that owns the resources.

Why pin: on a developer machine ``az login`` points at one default tenant, while the Azure resources may live in another.
Speech and the model deployments then reject the token with "Tenant provided in token does not match resource token".
Naming the tenant on every token request (``az account get-access-token --tenant ...``) fixes that, provided the user has
signed in to that tenant (``az login --tenant <id>``). In Azure (managed identity) no pin is needed.

Key authentication never touches this module.
"""
from __future__ import annotations

import inspect
from typing import Any


class TenantPinnedCredential:
    """Adds ``tenant_id`` to every ``get_token`` call of the wrapped azure-identity credential (sync or async)."""

    def __init__(self, inner: Any, tenant_id: str) -> None:
        self._inner = inner
        self._tenant_id = tenant_id

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    async def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        kwargs.setdefault("tenant_id", self._tenant_id)
        result = self._inner.get_token(*scopes, **kwargs)
        return await result if inspect.isawaitable(result) else result

    async def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result


def build_credential(tenant_id: str | None = None) -> Any:
    """``DefaultAzureCredential`` (async), wrapped to name ``tenant_id`` on every request when one is given."""
    from azure.identity.aio import DefaultAzureCredential     # imported late: the async credentials need aiohttp

    inner = DefaultAzureCredential()
    tenant = (tenant_id or "").strip()
    return TenantPinnedCredential(inner, tenant) if tenant else inner
