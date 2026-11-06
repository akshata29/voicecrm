"""Catalogue search port. Adapters: local in-process index, Azure AI Search."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from ..domain.base import VModel
from ..domain.catalogue import Catalogue


class SearchHit(VModel):
    id: str                                    # product_id
    score: float                               # adapter-specific relevance, higher is better
    fields: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class CatalogueSearchPort(Protocol):
    name: str

    async def index(self, catalogue: Catalogue) -> int:
        """(Re)build the index; returns the number of documents indexed."""
        ...

    async def search(self, query: str, *, top: int = 10, business_line: str | None = None) -> list[SearchHit]: ...
