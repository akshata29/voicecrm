"""Retention decisions per market: what is kept, where, and for how long (the CRM never holds the transcript)."""
from __future__ import annotations

from datetime import datetime, timedelta

from ..domain.pipeline import RetentionDecision
from ..domain.policy import MarketPolicy


class PolicyRetentionEngine:
    """Implements engines.contracts.RetentionEngine."""

    def decide(self, policy: MarketPolicy, *, now: datetime) -> RetentionDecision:
        keep = policy.transcript_retention == "on"
        days = policy.transcript_retention_days or 0
        expires = now + timedelta(days=days) if keep and days else None
        audio_expires = now + timedelta(days=policy.audio_retention_days) if policy.audio_retention_days > 0 else None
        audio = ("deleted right after transcription" if audio_expires is None
                 else f"kept for {policy.audio_retention_days} days")
        if keep:
            horizon = f"for {days} days (until {expires.date().isoformat()})" if expires else "until deleted by policy"
            explanation = (f"{policy.display_name}: transcript retention is ON - the {'redacted ' if policy.store_redacted_only else ''}"
                           f"transcript is kept in the vault {horizon}; audio is {audio}. The CRM receives the summary, "
                           f"structured fields and tasks plus a pointer to the vault copy - never the transcript.")
        else:
            explanation = (f"{policy.display_name}: transcript retention is OFF - the transcript is discarded after review; audio is "
                           f"{audio}. The CRM receives the summary, structured fields and tasks only.")
        return RetentionDecision(
            market=policy.market, transcript_stored=keep, transcript_redacted=policy.store_redacted_only,
            transcript_expires_at=expires, audio_expires_at=audio_expires, crm_contains_transcript=False,
            explanation=explanation)
