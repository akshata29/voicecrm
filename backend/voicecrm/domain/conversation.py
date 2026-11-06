"""Canonical conversation: the source-agnostic shape every channel is normalised into."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import Field

from .base import VModel
from .enums import Channel, Direction, SpeakerRole, TranscriptOrigin


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Participant(VModel):
    id: str                                   # referenced by Segment.speaker_id
    display_name: str
    email: str | None = None
    phone: str | None = None
    role: SpeakerRole = SpeakerRole.unknown
    org: str | None = None


class ConsentInfo(VModel):
    status: str = "unknown"                   # recorded_with_consent | unknown | not_required | withheld
    basis: str | None = None


class Segment(VModel):
    id: int
    speaker_id: str
    start_ms: int = 0
    end_ms: int = 0
    text: str
    locale: str | None = None                 # BCP-47 per segment when known (e.g. es-US)
    confidence: float | None = None


class ConversationMeta(VModel):
    conversation_id: str = Field(default_factory=lambda: uuid4().hex)
    source: Channel
    source_interaction_id: str                # stable ID in the source system; basis of CRM idempotency
    direction: Direction = Direction.outbound
    title: str | None = None
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None
    duration_seconds: float | None = None
    seller: Participant
    participants: list[Participant] = Field(default_factory=list)   # everyone except the seller
    customer_org: str | None = None
    market: str = "US"                        # key into data/policies/markets.yaml
    business_line: str = "professional_buyer"
    call_type: str | None = None              # discovery | service | product_recommendation | follow_up | relationship
    language_hint: str | None = None          # BCP-47, e.g. en-US / es-US
    consent: ConsentInfo = Field(default_factory=ConsentInfo)


class ArtifactRefs(VModel):
    audio_uri: str | None = None
    raw_transcript_uri: str | None = None
    source_summary: str | None = None         # summary produced by the source platform (benchmark only, never used as input)


class CanonicalConversation(VModel):
    meta: ConversationMeta
    segments: list[Segment]
    language: str = "en-US"                   # dominant language of the conversation
    transcript_origin: TranscriptOrigin = TranscriptOrigin.fixture
    artifacts: ArtifactRefs = Field(default_factory=ArtifactRefs)

    def participant(self, speaker_id: str) -> Participant | None:
        if self.meta.seller.id == speaker_id:
            return self.meta.seller
        return next((p for p in self.meta.participants if p.id == speaker_id), None)

    def text(self) -> str:
        return "\n".join(s.text for s in self.segments)
