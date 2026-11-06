"""Shared builders for the CRM adapter tests (synthetic data only; no tests live here)."""
from __future__ import annotations

from datetime import date, datetime, timezone

from voicecrm.domain.conversation import ConversationMeta, Participant
from voicecrm.domain.crm import (
    CrmMatch, CrmPackage, CrmRecordRef, CrmSeed, InsightPayload, PhoneCallPayload, SeedAccount, SeedContact,
    SeedOpportunity, SeedUser, TaskPayload,
)
from voicecrm.domain.enums import Channel, SpeakerRole
from voicecrm.domain.extraction import NoteSection

SELLER_EMAIL = "rodney.seller@contoso.example"
STARTED = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)


def make_seed() -> CrmSeed:
    return CrmSeed(
        users=[SeedUser(id="user-rodney", name="Rodney Seller", email=SELLER_EMAIL)],
        accounts=[
            SeedAccount(id="acc-meridian", name="Meridian Pension Fund", domain="meridianpension.example"),
            SeedAccount(id="acc-northwind", name="Northwind Capital Partners", domain="northwindcap.example"),
            SeedAccount(id="acc-solis", name="Fondo de Pensiones Solís", domain="fondosolis.example"),
            SeedAccount(id="acc-harbor", name="Harborview Endowment", domain="harborview.example"),
            SeedAccount(id="acc-orchid", name="Orchid Wealth Advisors", domain="orchidwealth.example"),
            SeedAccount(id="acc-cedar", name="Cedar Foundation", domain="cedarfoundation.example"),
        ],
        contacts=[
            SeedContact(id="con-priya", name="Priya Raman", email="priya.raman@meridianpension.example",
                        account_id="acc-meridian", title="Head of Fixed Income"),
            SeedContact(id="con-marcus", name="Marcus Lee", email="marcus.lee@northwindcap.example",
                        account_id="acc-northwind"),
            SeedContact(id="con-lucia", name="Lucía Fernández", email="lucia.fernandez@fondosolis.example",
                        account_id="acc-solis"),
            SeedContact(id="con-dana", name="Dana Whitfield", email="dana.whitfield@harborview.example",
                        account_id="acc-harbor"),
            SeedContact(id="con-orchid-a", name="Sam Okafor", email="shared@orchidwealth.example",
                        account_id="acc-orchid"),
            SeedContact(id="con-orchid-b", name="Samuel Okafor", email="shared@orchidwealth.example",
                        account_id="acc-orchid"),
            SeedContact(id="con-cedar", name="Ingrid Halvorsen", email="ingrid@cedarfoundation.example",
                        account_id="acc-cedar"),
        ],
        opportunities=[
            SeedOpportunity(id="opp-meridian", name="Meridian - Global Equity Mandate", account_id="acc-meridian",
                            contact_id="con-priya", stage="Develop"),
            SeedOpportunity(id="opp-northwind-won", name="Northwind - Core Bond (won)", account_id="acc-northwind",
                            stage="Closed Won"),
            SeedOpportunity(id="opp-northwind-open", name="Northwind - Multi-Asset Pilot", account_id="acc-northwind",
                            stage="Propose"),
            SeedOpportunity(id="opp-solis", name="Solís - Renta Fija", account_id="acc-solis", stage="Qualify"),
            SeedOpportunity(id="opp-harbor-1", name="Harborview - Equity Income", account_id="acc-harbor"),
            SeedOpportunity(id="opp-harbor-2", name="Harborview - Alternatives Sleeve", account_id="acc-harbor",
                            stage="Develop"),
            SeedOpportunity(id="opp-cedar-lost", name="Cedar - Short Duration", account_id="acc-cedar",
                            stage="Closed Lost"),
        ],
    )


def customer(
    pid: str = "cust1", name: str = "Priya Raman", email: str | None = "priya.raman@meridianpension.example",
    org: str | None = None, role: SpeakerRole = SpeakerRole.customer, phone: str | None = None,
) -> Participant:
    return Participant(id=pid, display_name=name, email=email, role=role, org=org, phone=phone)


def make_meta(*participants: Participant, customer_org: str | None = None, title: str | None = None) -> ConversationMeta:
    return ConversationMeta(
        conversation_id="conv-1", source=Channel.teams, source_interaction_id="meeting-1", title=title,
        started_at=STARTED,
        seller=Participant(id="seller", display_name="Rodney Seller", email=SELLER_EMAIL, role=SpeakerRole.seller),
        participants=list(participants), customer_org=customer_org,
    )


def make_package(
    key: str = "teams:meeting-1:v1",
    *,
    match: CrmMatch | None = None,
    pointer: str | None = None,
    description: str = "Overview\nMeridian reviewed a global equity mandate.",
    tasks: list[TaskPayload] | None = None,
    products: list[str] | None = None,
    topics: list[str] | None = None,
) -> CrmPackage:
    match = match or CrmMatch(
        account=CrmRecordRef(entity="account", id="acc-meridian", name="Meridian Pension Fund"),
        contact=CrmRecordRef(entity="contact", id="con-priya", name="Priya Raman"),
        opportunity=CrmRecordRef(entity="opportunity", id="opp-meridian", name="Meridian - Global Equity Mandate"),
        confidence=0.98, method="email",
    )
    return CrmPackage(
        idempotency_key=key,
        conversation_id="conv-1",
        regarding=match,
        phone_call=PhoneCallPayload(
            subject="Call with Priya Raman - Meridian Pension Fund",
            description=description,
            direction="outbound",
            start=STARTED,
            end=datetime(2026, 11, 10, 15, 12, tzinfo=timezone.utc),
            duration_minutes=12,
            owner_email=SELLER_EMAIL,
        ),
        tasks=[
            TaskPayload(
                source_task_id="t1", subject="Send fee schedule for Class I", description="Evidence: 'I'll send it Friday'",
                due=date(2026, 11, 13), owner_email=SELLER_EMAIL, priority="high",
            )
        ] if tasks is None else tasks,
        insight=InsightPayload(
            source=Channel.teams, source_interaction_id="meeting-1", template_id="pb_institutional", language="en",
            summary_markdown=description,
            sections=[NoteSection(key="overview", title="Overview", text="Meridian reviewed a global equity mandate.")],
            product_ids=["SYN-000101", "SYN-000102"] if products is None else products,
            topic_ids=["fees_expenses", "performance"] if topics is None else topics,
            engine="rules", model=None, confidence=0.93, review_status="approved", transcript_pointer=pointer,
        ),
    )
