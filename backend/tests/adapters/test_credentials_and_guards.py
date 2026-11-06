"""Shared Entra credential: tenant pinning, and the MSX guard on the Dataverse organisation URL."""
from __future__ import annotations

import pytest

from voicecrm.adapters import credentials
from voicecrm.adapters.credentials import TenantPinnedCredential, build_credential
from voicecrm.adapters.crm.dataverse import DataverseCrm
from voicecrm.adapters.crm.guards import assert_not_msx


class SyncInner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict]] = []
        self.closed = False

    def get_token(self, *scopes: str, **kwargs):
        self.calls.append((scopes, kwargs))
        return "token"

    def close(self) -> None:
        self.closed = True


class AsyncInner(SyncInner):
    async def get_token(self, *scopes: str, **kwargs):          # type: ignore[override]
        self.calls.append((scopes, kwargs))
        return "async-token"

    async def close(self) -> None:                               # type: ignore[override]
        self.closed = True


async def test_every_token_request_names_the_tenant_sync_and_async_credentials():
    for inner, expected in ((SyncInner(), "token"), (AsyncInner(), "async-token")):
        pinned = TenantPinnedCredential(inner, "tenant-123")
        assert await pinned.get_token("scope-a") == expected
        assert inner.calls == [(("scope-a",), {"tenant_id": "tenant-123"})] and pinned.tenant_id == "tenant-123"
        await pinned.close()
        assert inner.closed


async def test_an_explicit_tenant_on_the_request_wins():
    inner = SyncInner()
    await TenantPinnedCredential(inner, "pinned").get_token("s", tenant_id="other")
    assert inner.calls[0][1] == {"tenant_id": "other"}


def test_build_credential_wraps_only_when_a_tenant_is_given(monkeypatch):
    import azure.identity.aio as aio

    monkeypatch.setattr(aio, "DefaultAzureCredential", SyncInner)
    assert isinstance(build_credential("t-1"), TenantPinnedCredential)
    assert isinstance(build_credential(""), SyncInner) and isinstance(build_credential(None), SyncInner)
    assert credentials.build_credential.__module__ == "voicecrm.adapters.credentials"


@pytest.mark.parametrize("url", [
    "https://microsoftsales.crm.dynamics.com", "https://MicrosoftSales.crm.dynamics.com/", "https://msx.crm.dynamics.com",
    "https://contoso-msx.crm4.dynamics.com", "microsoftsales.crm.dynamics.com",
])
def test_msx_organisations_are_refused(url):
    with pytest.raises(ValueError, match="MSX"):
        assert_not_msx(url)
    with pytest.raises(ValueError, match="MSX"):
        DataverseCrm(url if "://" in url else f"https://{url}", credential=object())


@pytest.mark.parametrize("url", ["https://org1a2b3c.crm.dynamics.com", "https://voicecrm-sandbox.crm4.dynamics.com",
                                  "https://demo-tenant.crm.dynamics.com/"])
def test_sandbox_organisations_are_accepted(url):
    assert_not_msx(url)
    DataverseCrm(url, credential=object())
