"""LocalCrm: matching rules, idempotent writes, the transcript guard and list_records."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from voicecrm.adapters.crm.guards import TranscriptLeakError, assert_no_transcript, transcript_artifact
from voicecrm.adapters.crm.local_sim import LocalCrm
from voicecrm.domain.crm import CrmMatch, CrmRecordRef, CrmSeed, SeedAccount, SeedOpportunity
from voicecrm.domain.enums import SpeakerRole
from voicecrm.ports.crm import CrmPort

from .test_crm_fixtures import SELLER_EMAIL, customer, make_meta, make_package, make_seed


@pytest.fixture()
def crm() -> LocalCrm:
    return LocalCrm(":memory:", make_seed())


def test_implements_the_crm_port(crm):
    assert isinstance(crm, CrmPort)
    assert crm.name == "local_sim"


# ---- match -----------------------------------------------------------------------------------------------
async def test_email_match_returns_contact_account_and_the_single_open_opportunity(crm):
    match = await crm.match(make_meta(customer()))
    assert match.method == "email"
    assert (match.account.id, match.contact.id, match.opportunity.id) == ("acc-meridian", "con-priya", "opp-meridian")
    assert not match.ambiguous and match.candidates == []
    assert match.confidence == pytest.approx(0.99)


async def test_email_match_is_case_insensitive(crm):
    match = await crm.match(make_meta(customer(email="  PRIYA.Raman@MeridianPension.EXAMPLE ")))
    assert match.contact.id == "con-priya"


async def test_closed_opportunities_are_not_candidates(crm):
    match = await crm.match(make_meta(customer("c", "Marcus Lee", "marcus.lee@northwindcap.example")))
    assert match.account.id == "acc-northwind"
    assert match.opportunity.id == "opp-northwind-open"
    assert not match.ambiguous


async def test_several_open_opportunities_are_ambiguous_and_never_guessed(crm):
    match = await crm.match(make_meta(customer("c", "Dana Whitfield", "dana.whitfield@harborview.example")))
    assert match.account.id == "acc-harbor" and match.contact.id == "con-dana"
    assert match.opportunity is None
    assert match.ambiguous is True
    assert {c.id for c in match.candidates} == {"opp-harbor-1", "opp-harbor-2"}
    assert all(c.entity == "opportunity" for c in match.candidates)
    assert match.confidence < 0.7


async def test_no_open_opportunity_gives_account_and_contact_only(crm):
    match = await crm.match(make_meta(customer("c", "Ingrid Halvorsen", "ingrid@cedarfoundation.example")))
    assert (match.account.id, match.contact.id) == ("acc-cedar", "con-cedar")
    assert match.opportunity is None and not match.ambiguous and match.candidates == []


async def test_unknown_email_falls_back_to_domain_and_organisation_name(crm):
    meta = make_meta(customer("c", "Unknown Person", "someone@meridianpension.example"), customer_org="Meridian Pension Fund")
    match = await crm.match(meta)
    assert match.method == "name_org"
    assert match.account.id == "acc-meridian" and match.contact is None
    assert match.opportunity.id == "opp-meridian"
    assert match.confidence == pytest.approx(0.95)


async def test_organisation_name_alone_matches_the_account_and_the_contact_by_name(crm):
    meta = make_meta(customer("c", "Priya Raman", None), customer_org="meridian pension fund inc.")
    match = await crm.match(meta)
    assert match.method == "name_org"
    assert match.account.id == "acc-meridian"
    assert match.contact.id == "con-priya"
    assert match.confidence == pytest.approx(0.90)


async def test_fuzzy_organisation_name_matches_when_unique(crm):
    match = await crm.match(make_meta(customer("c", "Someone", None), customer_org="Meridian Pensions Fund"))
    assert match.account.id == "acc-meridian"
    assert 0.6 <= match.confidence < 0.9


async def test_fuzzy_organisation_with_two_close_accounts_is_ambiguous():
    seed = CrmSeed(
        users=[],
        accounts=[SeedAccount(id="a1", name="Aurora Pension Plan"), SeedAccount(id="a2", name="Aurora Pension Plans")],
        contacts=[], opportunities=[SeedOpportunity(id="o1", name="o", account_id="a1")],
    )
    crm = LocalCrm(":memory:", seed)
    match = await crm.match(make_meta(customer("c", "Someone", None), customer_org="Aurora Pensions Plan"))
    assert match.ambiguous is True and match.account is None and match.opportunity is None
    assert {c.id for c in match.candidates} == {"a1", "a2"}


async def test_unknown_customer_returns_no_match(crm):
    match = await crm.match(make_meta(customer("c", "Nobody Known", "nobody@unknown-capital.example"),
                                      customer_org="Totally Unknown Capital"))
    assert match == CrmMatch()
    assert match.method == "none" and match.confidence == 0.0


async def test_free_mail_domains_are_not_evidence(crm):
    seed = make_seed()
    seed.accounts.append(SeedAccount(id="acc-mail", name="Mailbox Holdings", domain="gmail.com"))
    crm = LocalCrm(":memory:", seed)
    match = await crm.match(make_meta(customer("c", "Zed Quill", "zed.quill@gmail.com")))
    assert match == CrmMatch()


async def test_internal_participants_never_drive_matching(crm):
    colleague = customer("int1", "Priya Raman", "priya.raman@meridianpension.example", role=SpeakerRole.internal)
    assert (await crm.match(make_meta(colleague))) == CrmMatch()


async def test_duplicate_contacts_for_one_email_are_ambiguous(crm):
    match = await crm.match(make_meta(customer("c", "Sam Okafor", "shared@orchidwealth.example")))
    assert match.ambiguous is True and match.contact is None
    assert match.account.id == "acc-orchid"
    assert {c.id for c in match.candidates} == {"con-orchid-a", "con-orchid-b"}


async def test_customers_from_different_accounts_are_ambiguous(crm):
    meta = make_meta(customer(), customer("c2", "Marcus Lee", "marcus.lee@northwindcap.example"))
    match = await crm.match(meta)
    assert match.ambiguous is True and match.account is None
    assert {c.id for c in match.candidates} == {"acc-meridian", "acc-northwind"}


async def test_name_without_organisation_needs_an_exact_unique_contact_and_stays_low_confidence(crm):
    match = await crm.match(make_meta(customer("c", "Lucia Fernandez", None)))
    assert match.contact.id == "con-lucia" and match.account.id == "acc-solis"
    assert match.confidence <= 0.75


# ---- write -----------------------------------------------------------------------------------------------
async def test_write_stores_call_task_and_insight(crm):
    result = await crm.write(make_package(pointer="https://vault.example/blob/conv-1/transcript/conversation.json"))
    assert result.ok and not result.skipped_duplicate and result.error is None
    assert [r.entity for r in result.created] == ["phonecall", "task", "insight"]

    (call,) = await crm.list_records("phonecall")
    assert call["subject"].startswith("Call with Priya Raman")
    assert call["regarding_entity"] == "opportunity"
    assert call["regarding_name"] == "Meridian - Global Equity Mandate"
    assert call["contact_name"] == "Priya Raman" and call["owner_name"] == "Rodney Seller"
    assert call["start"].startswith("2026-11-10T15:00") and call["duration_minutes"] == 12

    (task,) = await crm.list_records("task")
    assert task["subject"] == "Send fee schedule for Class I"
    assert task["due"] == "2026-11-13" and task["priority"] == "high"
    assert task["owner_name"] == "Rodney Seller" and task["regarding_name"] == "Meridian - Global Equity Mandate"
    assert task["phonecall_id"] == call["id"]

    (insight,) = await crm.list_records("insight")
    assert insight["product_ids"] == ["SYN-000101", "SYN-000102"]
    assert insight["topic_ids"] == ["fees_expenses", "performance"]
    assert insight["template_id"] == "pb_institutional" and insight["engine"] == "rules"
    assert insight["review_status"] == "approved" and insight["confidence"] == pytest.approx(0.93)
    assert insight["transcript_pointer"].startswith("https://")
    assert insight["sections"][0]["key"] == "overview"
    assert insight["phonecall_id"] == call["id"]


async def test_write_is_idempotent_on_the_idempotency_key(crm):
    first = await crm.write(make_package())
    again = await crm.write(make_package())
    assert first.ok and again.ok
    assert again.skipped_duplicate is True and again.created == []
    for entity in ("phonecall", "task", "insight"):
        assert len(await crm.list_records(entity)) == 1


async def test_a_new_version_key_writes_again(crm):
    await crm.write(make_package("teams:meeting-1:v1"))
    result = await crm.write(make_package("teams:meeting-1:v2"))
    assert result.ok and not result.skipped_duplicate
    assert len(await crm.list_records("phonecall")) == 2


async def test_idempotency_survives_reopening_a_file_database(tmp_path: Path):
    path = tmp_path / "state" / "crm_sim.sqlite"
    first = LocalCrm(path, make_seed())
    assert (await first.write(make_package())).ok
    first.close()
    reopened = LocalCrm(path, make_seed())
    assert (await reopened.write(make_package())).skipped_duplicate is True
    assert len(await reopened.list_records("phonecall")) == 1
    reopened.close()


async def _stored(crm: LocalCrm) -> int:
    return sum([len(await crm.list_records(e)) for e in ("phonecall", "task", "insight")])


@pytest.mark.parametrize("pointer", [
    "Rodney: Hello Priya, thanks for joining. Priya: Of course, happy to be here.",
    "see the transcript",
    "D:\\vault\\conv-1\\transcript\\conversation.json",
    "https://vault.example/with space",
    "https://vault.example/" + "x" * 3000,
])
async def test_prose_or_path_is_rejected_as_a_transcript_pointer(crm, pointer):
    result = await crm.write(make_package(pointer=pointer))
    assert result.ok is False and "transcript_pointer" in (result.error or "")
    assert await _stored(crm) == 0


@pytest.mark.parametrize("pointer", [
    None,
    "https://acct.blob.core.windows.net/voicecrm-transcripts/conv-1/transcript/conversation.json",
    "file:///D:/repos/voicecrm/.state/vault/conv-1/transcript/conversation.json",
])
async def test_uri_pointers_are_accepted(crm, pointer):
    assert (await crm.write(make_package(pointer=pointer))).ok


@pytest.mark.parametrize("dump", [
    "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nHello",
    "00:00:01.000 --> 00:00:03.000 hello there",
    "<v Rodney Seller>Hello Priya</v>",
    "[00:01] Rodney: hi\n[00:05] Priya: hello\n[00:09] Rodney: thanks",
])
async def test_transcript_dumps_in_note_fields_are_rejected(crm, dump):
    result = await crm.write(make_package(description=f"Overview\n{dump}"))
    assert result.ok is False and "transcript" in (result.error or "")
    assert len(await crm.list_records("phonecall")) == 0


def test_guard_allows_normal_notes_and_names_the_artifact_otherwise():
    assert transcript_artifact("Overview\n- Next steps: send the fee schedule by 3:30 PM Friday\n- Decision: pilot") is None
    assert transcript_artifact("WEBVTT\n") == "WebVTT header"
    assert_no_transcript(make_package(pointer="https://vault.example/x"))
    with pytest.raises(TranscriptLeakError):
        assert_no_transcript(make_package(pointer="not a uri"))


async def test_regarding_prefers_the_explicit_target_then_opportunity_then_account(crm):
    package = make_package()
    package.phone_call.regarding = CrmRecordRef(entity="account", id="acc-meridian", name="Meridian Pension Fund")
    await crm.write(package)
    account_only = make_package("teams:meeting-2:v1", match=CrmMatch(
        account=CrmRecordRef(entity="account", id="acc-northwind", name="Northwind Capital Partners")))
    await crm.write(account_only)
    unmatched = make_package("teams:meeting-3:v1", match=CrmMatch(), tasks=[])
    assert (await crm.write(unmatched)).ok

    by_key = {r["idempotency_key"]: r for r in await crm.list_records("phonecall")}
    assert by_key["teams:meeting-1:v1"]["regarding_entity"] == "account"
    assert by_key["teams:meeting-2:v1"]["regarding_name"] == "Northwind Capital Partners"
    assert by_key["teams:meeting-3:v1"]["regarding_entity"] is None


async def test_task_owner_defaults_to_the_call_owner(crm):
    package = make_package()
    package.tasks[0].owner_email = None
    await crm.write(package)
    (task,) = await crm.list_records("task")
    assert task["owner_email"] == SELLER_EMAIL


async def test_missing_idempotency_key_is_rejected(crm):
    result = await crm.write(make_package(" "))
    assert result.ok is False and "idempotency_key" in result.error


# ---- list_records / health -------------------------------------------------------------------------------
async def test_list_records_is_newest_first_and_honours_limit():
    clock = iter(datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc) + timedelta(minutes=i) for i in range(100))
    crm = LocalCrm(":memory:", make_seed(), clock=lambda: next(clock))
    for n in range(1, 4):
        await crm.write(make_package(f"teams:m{n}:v1"))
    rows = await crm.list_records("phonecall")
    assert [r["idempotency_key"] for r in rows] == ["teams:m3:v1", "teams:m2:v1", "teams:m1:v1"]
    assert [r["idempotency_key"] for r in await crm.list_records("phonecall", limit=2)] == ["teams:m3:v1", "teams:m2:v1"]


async def test_seed_entities_are_listed_with_joined_names(crm):
    contacts = {c["id"]: c for c in await crm.list_records("contact")}
    assert contacts["con-priya"]["account_name"] == "Meridian Pension Fund"
    opportunities = {o["id"]: o for o in await crm.list_records("opportunity")}
    assert opportunities["opp-meridian"]["account_name"] == "Meridian Pension Fund"
    assert opportunities["opp-meridian"]["contact_name"] == "Priya Raman"
    assert len(await crm.list_records("account")) == 6
    assert (await crm.list_records("systemuser"))[0]["email"] == SELLER_EMAIL


async def test_unknown_entity_is_a_value_error(crm):
    with pytest.raises(ValueError, match="unknown entity"):
        await crm.list_records("lead")


async def test_health_reports_counts(crm):
    await crm.write(make_package())
    health = await crm.health()
    assert health["ok"] is True and health["synthetic"] is True
    assert health["counts"]["accounts"] == 6 and health["counts"]["phonecalls"] == 1
    assert health["counts"]["tasks"] == 1 and health["counts"]["insights"] == 1


async def test_seed_can_be_reloaded_without_duplicates(crm):
    crm.load_seed(make_seed())
    assert len(await crm.list_records("account")) == 6


async def test_delete_conversations_removes_everything_a_conversation_wrote_and_nothing_else(crm):
    first, second = make_package(), make_package()
    first.conversation_id, first.idempotency_key = "conv-a", "key-a"
    second.conversation_id, second.idempotency_key = "conv-b", "key-b"
    assert (await crm.write(first)).ok and (await crm.write(second)).ok
    assert (await crm.health())["counts"]["phonecalls"] == 2
    assert await crm.delete_conversations(["conv-a"]) == 1
    counts = (await crm.health())["counts"]
    assert (counts["phonecalls"], counts["tasks"], counts["insights"]) == (1, 1, 1)
    assert [r["conversation_id"] for r in await crm.list_records("phonecall")] == ["conv-b"]
    leftovers = crm._query("SELECT (SELECT COUNT(*) FROM insight_products) AS p, (SELECT COUNT(*) FROM insight_topics) AS t,"
                           " (SELECT COUNT(*) FROM idempotency) AS i")[0]
    assert (leftovers["p"], leftovers["t"], leftovers["i"]) == (len(second.insight.product_ids), len(second.insight.topic_ids), 1)
    assert (await crm.write(first)).created                              # the key is free again, so the call can be written anew
    assert await crm.delete_conversations([]) == 0


async def test_clear_written_removes_what_the_pipeline_wrote_and_keeps_the_seed(crm):
    first, second = make_package(), make_package()
    first.conversation_id, first.idempotency_key = "conv-a", "key-a"
    second.conversation_id, second.idempotency_key = "conv-b", "key-b"
    assert (await crm.write(first)).ok and (await crm.write(second)).ok
    seeded = {entity: len(await crm.list_records(entity)) for entity in ("account", "contact", "opportunity")}
    assert await crm.clear_written() == {"phonecalls": 2, "tasks": 2, "insights": 2}
    counts = (await crm.health())["counts"]
    assert (counts["phonecalls"], counts["tasks"], counts["insights"]) == (0, 0, 0)
    assert {entity: len(await crm.list_records(entity)) for entity in seeded} == seeded       # accounts, contacts, opportunities stay
    leftovers = crm._query("SELECT (SELECT COUNT(*) FROM insight_products) AS p, (SELECT COUNT(*) FROM insight_topics) AS t,"
                           " (SELECT COUNT(*) FROM idempotency) AS i")[0]
    assert (leftovers["p"], leftovers["t"], leftovers["i"]) == (0, 0, 0)
    assert (await crm.write(first)).created                              # the keys are free again, so the calls can be written anew
    assert await crm.clear_written() == {"phonecalls": 1, "tasks": 1, "insights": 1}
    assert await crm.clear_written() == {"phonecalls": 0, "tasks": 0, "insights": 0}
