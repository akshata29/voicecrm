"""Matching by caller phone number: normalisation, the rule's place in the order, reconciliation with name / organisation."""
from __future__ import annotations

import pytest

from voicecrm.adapters.crm.local_sim import LocalCrm
from voicecrm.adapters.crm.matching import normalize_phone, phone_owners, phone_variants, search_phones
from voicecrm.data_loader import load_crm_seed, load_scripted_calls
from voicecrm.domain.conversation import ConversationMeta, Participant
from voicecrm.domain.crm import CrmMatch, CrmSeed, SeedAccount, SeedContact, SeedOpportunity
from voicecrm.domain.enums import SpeakerRole
from voicecrm.domain.fixtures import ScriptedCall

from .test_crm_fixtures import customer, make_meta

NANP_SPELLINGS = [
    "+1 212 555 0171", "(212) 555-0171", "212.555.0171", "212-555-0171", "2125550171", "12125550171", "1-212-555-0171",
    "+1 (212) 555-0171", "001 212 555 0171", "  +1 212 555 0171  ", "tel:+12125550171", "tel:+1-212-555-0171;ext=22",
    "sip:+12125550171@gw.example.com;user=phone", "+1 212 555 0171 ext. 22", "212-555-0171 x22", "212-555-0171 #22",
]


# ---- normalize_phone / search_phones / phone_variants ----------------------------------------------------------
@pytest.mark.parametrize("written", NANP_SPELLINGS)
def test_every_spelling_of_a_us_number_normalises_to_the_same_digits(written):
    assert normalize_phone(written) == "2125550171"


@pytest.mark.parametrize("written", [
    "+44 20 7946 0191", "0044 20 7946 0191", "+44 (0)20 7946 0191", "+442079460191", "tel:+44-20-7946-0191",
])
def test_international_numbers_keep_their_country_code_and_lose_the_trunk_zero(written):
    assert normalize_phone(written) == "442079460191"


@pytest.mark.parametrize("written", [None, "", "   ", "anonymous", "unknown", "555-01", "123456", "ext 1234", "x1234567",
                                     "1234567890123456"])
def test_strings_that_are_not_phone_numbers_are_ignored(written):
    assert normalize_phone(written) is None


def test_seven_digits_is_the_shortest_usable_number():
    assert normalize_phone("555-0171") == "5550171"
    assert normalize_phone("555-017") is None


@pytest.mark.parametrize("written", NANP_SPELLINGS + ["+44 20 7946 0191", "555-0171"])
def test_normalisation_is_idempotent(written):
    once = normalize_phone(written)
    assert normalize_phone(once) == once


def test_search_phones_normalises_dedupes_and_skips_unusable_numbers():
    people = [
        customer("a", "A", None, phone="+1 212 555 0171"), customer("b", "B", None, phone="(212) 555-0171"),
        customer("c", "C", None, phone="x12"), customer("d", "D", None, phone=None),
        customer("e", "E", None, phone="+44 20 7946 0191"),
    ]
    assert search_phones(people) == ["2125550171", "442079460191"]


def test_phone_owners_groups_participants_and_drops_the_excluded_numbers():
    people = [
        customer("a", "A", None, phone="+1 212 555 0171"), customer("b", "B", None, phone="212.555.0171"),
        customer("c", "C", None, phone="312-555-0103"),
    ]
    owners = phone_owners(people, exclude=["(312) 555 0103", None, "junk"])
    assert list(owners) == ["2125550171"]
    assert [p.id for p in owners["2125550171"]] == ["a", "b"]


@pytest.mark.parametrize("number", ["2125550171", "3055550181", "442079460191", "493055500201"])
def test_every_variant_normalises_back_to_the_number(number):
    variants = phone_variants(number)
    assert variants and len(variants) == len(set(variants))
    assert {normalize_phone(v) for v in variants} == {number}


def test_us_variants_cover_the_usual_stored_formats():
    assert {"+1 212 555 0171", "+12125550171", "(212) 555-0171", "212-555-0171", "212.555.0171", "2125550171"} <= set(
        phone_variants("2125550171"))


def test_the_callers_own_spelling_comes_first_and_unsafe_or_foreign_spellings_are_dropped():
    variants = phone_variants(
        "442079460191",
        ["+44 20 7946 0191", "+44 20 7946 0191'; DROP", "+1 212 555 0171", "tel:+44 20 7946 0191"],
    )
    assert variants[0] == "+44 20 7946 0191" and variants.count("+44 20 7946 0191") == 1
    assert all("DROP" not in v and "212" not in v for v in variants)


# ---- LocalCrm with a purpose-built seed ------------------------------------------------------------------------
def phone_seed() -> CrmSeed:
    return CrmSeed(
        users=[],
        accounts=[
            SeedAccount(id="acc-a", name="Alder Pension Fund", domain="alderpension.example"),
            SeedAccount(id="acc-b", name="Birch Capital Partners", domain="birchcap.example"),
            SeedAccount(id="acc-c", name="Cypress Trust Company", domain="cypresstrust.example"),
        ],
        contacts=[
            SeedContact(id="con-ann", name="Ann Archer", email="ann.archer@alderpension.example",
                        phone="+1 212 555 0101", account_id="acc-a"),
            SeedContact(id="con-ben", name="Ben Brook", email="ben.brook@alderpension.example",
                        phone="(212) 555-0102", account_id="acc-a"),
            SeedContact(id="con-cyrus", name="Cyrus Cole", email="cyrus.cole@birchcap.example",
                        phone="312.555.0103", account_id="acc-b"),
            SeedContact(id="con-dee", name="Dee Dunn", phone="+1 646 555 0104", account_id="acc-c"),
            SeedContact(id="con-eli", name="Eli Eaton", phone="+1 646 555 0104", account_id="acc-c"),
            SeedContact(id="con-fay", name="Fay Frost", phone="+1 917 555 0105", account_id="acc-b"),
            SeedContact(id="con-gus", name="Gus Gray", phone="+1 917 555 0105", account_id="acc-c"),
            SeedContact(id="con-una", name="Una Kent", phone="+44 20 7946 0191", account_id="acc-b"),
            SeedContact(id="con-nina", name="Nina Nophone", account_id="acc-a"),
        ],
        opportunities=[
            SeedOpportunity(id="opp-a", name="Alder - Core Bond", account_id="acc-a", stage="Develop"),
            SeedOpportunity(id="opp-b", name="Birch - Multi-Asset", account_id="acc-b", stage="Propose"),
        ],
    )


@pytest.fixture()
def crm() -> LocalCrm:
    return LocalCrm(":memory:", phone_seed())


def caller(phone: str | None, name: str = "Unknown caller", org: str | None = None) -> Participant:
    return customer("cust1", name, None, org=org, phone=phone)


async def test_a_phone_only_caller_resolves_contact_account_and_the_single_open_opportunity(crm):
    match = await crm.match(make_meta(caller("+1 212 555 0101", "Ann Archer")))
    assert match.method == "phone" and match.ambiguous is False and match.candidates == []
    assert (match.account.id, match.contact.id, match.opportunity.id) == ("acc-a", "con-ann", "opp-a")
    assert match.confidence == pytest.approx(0.98)


async def test_a_bare_number_is_trusted_less_than_a_number_the_name_confirms(crm):
    bare = await crm.match(make_meta(caller("+1 212 555 0101")))
    named = await crm.match(make_meta(caller("+1 212 555 0101", "Ann Archer")))
    assert bare.contact.id == named.contact.id == "con-ann"
    assert bare.confidence == pytest.approx(0.94) and named.confidence == pytest.approx(0.98)


async def test_a_confirming_organisation_counts_like_a_confirming_name(crm):
    match = await crm.match(make_meta(caller("+1 212 555 0101"), customer_org="Alder Pension Fund Inc."))
    assert (match.method, match.contact.id) == ("phone", "con-ann")
    assert match.confidence == pytest.approx(0.98)


@pytest.mark.parametrize("written", [
    "+1 212 555 0101", "(212) 555-0101", "212.555.0101", "212-555-0101", "2125550101", "12125550101",
    "+1 (212) 555-0101", "001 212 555 0101", "tel:+12125550101", "212-555-0101 x14",
])
async def test_every_common_spelling_of_a_number_matches(crm, written):
    match = await crm.match(make_meta(caller(written)))
    assert (match.method, match.contact.id) == ("phone", "con-ann")


@pytest.mark.parametrize("written, contact", [("+1 212 555 0102", "con-ben"), ("+1 312 555 0103", "con-cyrus")])
async def test_stored_formats_do_not_matter(crm, written, contact):
    assert (await crm.match(make_meta(caller(written)))).contact.id == contact


@pytest.mark.parametrize("written", ["+44 20 7946 0191", "0044 20 7946 0191", "+44 (0)20 7946 0191", "+442079460191"])
async def test_international_numbers_match_when_written_internationally(crm, written):
    match = await crm.match(make_meta(caller(written)))
    assert (match.method, match.contact.id, match.account.id) == ("phone", "con-una", "acc-b")


@pytest.mark.parametrize("written", ["555-01", "anonymous", "ext 1234", "", None])
async def test_unusable_numbers_match_nothing(crm, written):
    assert await crm.match(make_meta(caller(written))) == CrmMatch()


async def test_a_number_nobody_holds_matches_nothing(crm):
    assert await crm.match(make_meta(caller("+1 202 555 0199"))) == CrmMatch()


async def test_a_known_email_is_never_overridden_by_the_phone_number(crm):
    person = customer("c", "Ann Archer", "ann.archer@alderpension.example", phone="+1 312 555 0103")   # Cyrus's number
    match = await crm.match(make_meta(person))
    assert (match.method, match.contact.id, match.account.id) == ("email", "con-ann", "acc-a")
    assert match.confidence == pytest.approx(0.99)


async def test_an_email_the_crm_does_not_know_does_not_block_the_phone_match(crm):
    person = customer("c", "Ann Archer", "ann@elsewhere.example", phone="212-555-0101")
    match = await crm.match(make_meta(person))
    assert (match.method, match.contact.id) == ("phone", "con-ann")


async def test_an_unknown_number_falls_back_to_the_organisation_and_the_name(crm):
    match = await crm.match(make_meta(caller("+1 202 555 0199", "Ann Archer"), customer_org="Alder Pension Fund"))
    assert (match.method, match.account.id, match.contact.id) == ("name_org", "acc-a", "con-ann")


# ---- sharing and conflicts: never guess ---------------------------------------------------------------------------
async def test_a_number_shared_by_colleagues_is_ambiguous_and_lists_them(crm):
    match = await crm.match(make_meta(caller("+1 646 555 0104")))
    assert match.method == "phone" and match.ambiguous is True
    assert match.contact is None and match.account.id == "acc-c"           # one company, so the account is safe
    assert {c.id for c in match.candidates} == {"con-dee", "con-eli"}
    assert all(c.entity == "contact" for c in match.candidates)
    assert match.confidence < 0.6


async def test_the_name_singles_out_one_of_the_contacts_sharing_a_number(crm):
    match = await crm.match(make_meta(caller("+1 646 555 0104", "Eli Eaton")))
    assert (match.method, match.contact.id, match.account.id) == ("phone", "con-eli", "acc-c")
    assert match.ambiguous is False and match.candidates == []
    assert match.confidence == pytest.approx(0.88)


async def test_a_number_shared_across_companies_needs_the_organisation(crm):
    bare = await crm.match(make_meta(caller("+1 917 555 0105")))
    assert bare.ambiguous is True and bare.account is None and bare.contact is None
    assert {c.id for c in bare.candidates} == {"con-fay", "con-gus"}

    hinted = await crm.match(make_meta(caller("+1 917 555 0105"), customer_org="Birch Capital Partners"))
    assert (hinted.contact.id, hinted.account.id, hinted.ambiguous) == ("con-fay", "acc-b", False)


async def test_hints_that_single_out_different_contacts_leave_the_number_ambiguous(crm):
    match = await crm.match(make_meta(caller("+1 917 555 0105", "Fay Frost"), customer_org="Cypress Trust Company"))
    assert match.ambiguous is True and match.contact is None
    assert {c.id for c in match.candidates} == {"con-fay", "con-gus"}


async def test_a_name_that_contradicts_the_number_withholds_the_pick(crm):
    match = await crm.match(make_meta(caller("+1 212 555 0101", "Zed Quill")))
    assert match.method == "phone" and match.ambiguous is True
    assert match.contact is None and match.account is None and match.opportunity is None
    assert [c.id for c in match.candidates] == ["con-ann"]
    assert match.confidence < 0.5


async def test_an_organisation_that_contradicts_the_number_withholds_the_pick(crm):
    match = await crm.match(make_meta(caller("+1 212 555 0101", "Ann Archer"), customer_org="Birch Capital Partners"))
    assert match.ambiguous is True and match.contact is None and match.account is None
    assert [c.id for c in match.candidates] == ["con-ann"]


async def test_a_name_in_between_is_inconclusive_not_a_contradiction(crm):
    match = await crm.match(make_meta(caller("+1 212 555 0101", "Anne Archer-Lowe")))
    assert (match.method, match.contact.id, match.ambiguous) == ("phone", "con-ann", False)
    assert match.confidence == pytest.approx(0.94)


async def test_a_colleagues_line_keeps_the_confirmed_company_and_finds_the_person_by_name(crm):
    match = await crm.match(make_meta(caller("+1 212 555 0101", "Ben Brook"), customer_org="Alder Pension Fund"))
    assert (match.method, match.account.id, match.contact.id) == ("phone", "acc-a", "con-ben")
    assert match.ambiguous is False
    assert match.confidence == pytest.approx(0.90)


async def test_callers_whose_numbers_belong_to_different_companies_are_ambiguous(crm):
    meta = make_meta(caller("+1 212 555 0101", "Ann Archer"), customer("c2", "Cyrus Cole", None, phone="312-555-0103"))
    match = await crm.match(meta)
    assert match.method == "phone" and match.ambiguous is True and match.account is None
    assert {c.id for c in match.candidates} == {"acc-a", "acc-b"}


async def test_callers_from_one_company_resolve_to_the_first_contact(crm):
    meta = make_meta(caller("+1 212 555 0101", "Ann Archer"), customer("c2", "Ben Brook", None, phone="(212) 555-0102"))
    match = await crm.match(meta)
    assert (match.contact.id, match.account.id, match.ambiguous) == ("con-ann", "acc-a", False)


async def test_the_sellers_own_number_is_never_evidence(crm):
    meta = make_meta(caller("+1 212 555 0101"))
    meta = meta.model_copy(update={"seller": meta.seller.model_copy(update={"phone": "(212) 555-0101"})})
    assert await crm.match(meta) == CrmMatch()


async def test_internal_participants_phone_numbers_never_drive_matching(crm):
    colleague = customer("int1", "Ann Archer", None, role=SpeakerRole.internal, phone="+1 212 555 0101")
    assert await crm.match(make_meta(colleague)) == CrmMatch()


# ---- the shipped synthetic CRM and calls ---------------------------------------------------------------------------
_CALLS = {c.fixture_id: c for c in load_scripted_calls()}


def meta_from_call(call: ScriptedCall) -> ConversationMeta:
    """The metadata a source adapter would hand to the pipeline for this call."""
    return ConversationMeta(
        source=call.channel, source_interaction_id=call.fixture_id, direction=call.direction,
        seller=call.seller.model_copy(update={"role": SpeakerRole.seller}),
        participants=[
            *(p.model_copy(update={"role": SpeakerRole.customer}) for p in call.customers),
            *(p.model_copy(update={"role": SpeakerRole.internal}) for p in call.internal),
        ],
        customer_org=call.customer_org,
    )


@pytest.fixture(scope="module")
def real_crm() -> LocalCrm:
    return LocalCrm(":memory:", load_crm_seed())


async def test_genesys_inbound_call_resolves_the_right_alex_kim_by_phone(real_crm):
    match = await real_crm.match(meta_from_call(_CALLS["pb-genesys-inbound-service"]))
    assert (match.account.id, match.contact.id, match.method) == ("acc-007", "con-013", "phone")
    assert match.opportunity.id == "opp-007"
    assert match.ambiguous is False and match.candidates == []
    assert match.confidence >= 0.9


async def test_genesys_advisor_call_resolves_by_phone(real_crm):
    match = await real_crm.match(meta_from_call(_CALLS["retail-genesys-class-conversion"]))
    assert (match.account.id, match.contact.id, match.method) == ("acc-008", "con-015", "phone")
    assert match.opportunity.id == "opp-008"
    assert match.ambiguous is False and match.confidence >= 0.9


async def test_without_the_number_the_two_alex_kims_stay_ambiguous(real_crm):
    meta = meta_from_call(_CALLS["pb-genesys-inbound-service"])
    no_number = meta.model_copy(update={"participants": [p.model_copy(update={"phone": None}) for p in meta.participants]})
    match = await real_crm.match(no_number)
    assert match.ambiguous is True and {c.id for c in match.candidates} == {"con-002", "con-013"}


async def test_the_other_alex_kim_resolves_from_the_other_number(real_crm):
    match = await real_crm.match(make_meta(caller("(206) 555-0112", "Alex Kim")))
    assert (match.contact.id, match.account.id, match.method) == ("con-002", "acc-001", "phone")


@pytest.mark.parametrize("fixture_id", sorted(i for i, c in _CALLS.items() if c.gold.expect_crm_account))
async def test_every_scripted_call_resolves_to_its_expected_account(real_crm, fixture_id):
    call = _CALLS[fixture_id]
    match = await real_crm.match(meta_from_call(call))
    assert match.account is not None and match.account.id == call.gold.expect_crm_account
