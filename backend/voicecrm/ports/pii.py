"""PII detection port. Adapters: local rules (regex + Luhn + lexicons), Azure AI Language."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.base import VModel

# Normalised categories shared with NotesPolicy.omit_categories:
#   payment_card | bank_account | government_id | credentials | personal_contact | medical | family_health
PII_CATEGORIES = (
    "payment_card", "bank_account", "government_id", "credentials", "personal_contact", "medical", "family_health",
)


class PiiSpan(VModel):
    start: int                                 # character offsets within the text that was submitted
    end: int
    category: str                              # one of PII_CATEGORIES
    text: str
    confidence: float = 1.0
    source: str = "local"                      # local | azure_language


@runtime_checkable
class PiiPort(Protocol):
    name: str

    async def detect_many(self, texts: list[str], *, language: str) -> list[list[PiiSpan]]:
        """Detect spans in each text. Returns one list of spans per input text (same order)."""
        ...
