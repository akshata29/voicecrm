"""DataverseCrm: every request is checked against a mocked Web API (no live environment, no network)."""
from __future__ import annotations

import json
import time
from datetime import date

import httpx
import pytest
import respx

from voicecrm.adapters.crm.dataverse import DataverseCrm, DataverseError
from voicecrm.adapters.crm.field_profile import load_field_profile
from voicecrm.domain.crm import CrmMatch, CrmRecordRef
from voicecrm.domain.enums import SpeakerRole
from voicecrm.domain.extraction import NoteSection
from voicecrm.domain.templates import SummaryTemplate, TemplateSection
from voicecrm.ports.crm import CrmPort

from .test_crm_fixtures import SELLER_EMAIL, customer, make_meta, make_package

ORG = "https://contoso-test.crm.dynamics.com"
API = f"{ORG}/api/data/v9.2"
ACC, CON, OPP, USER = (f"{n}{n}{n}{n}{n}{n}{n}{n}-{n}{n}{n}{n}-{n}{n}{n}{n}-{n}{n}{n}{n}-{n * 12}" for n in "1234")
CALL, TASK, INSIGHT = (f"{n}{n}{n}{n}{n}{n}{n}{n}-{n}{n}{n}{n}-{n}{n}{n}{n}-{n}{n}{n}{n}-{n * 12}" for n in "567")
TOPICS = ["fees_expenses", "performance", "esg"]


async def static_token() -> str:
    return "tok-123"


class Sleeper:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


@pytest.fixture()
def api():
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


@pytest.fixture()
def sleeper() -> Sleeper:
    return Sleeper()


_CLIENT = httpx.AsyncClient(verify=False)        # shared: building an SSL context per test is slow; respx mocks the transport
_PROFILE = load_field_profile()


def make_crm(sleeper: Sleeper | None = None, **kwargs) -> DataverseCrm:
    kwargs.setdefault("token_provider", static_token)
    kwargs.setdefault("sleep", sleeper or Sleeper())
    kwargs.setdefault("profile", _PROFILE)
    return DataverseCrm(ORG, client=_CLIENT, **kwargs)


def guid_match() -> CrmMatch:
    return CrmMatch(
        account=CrmRecordRef(entity="account", id=ACC, name="Meridian Pension Fund"),
        contact=CrmRecordRef(entity="contact", id=CON, name="Priya Raman"),
        opportunity=CrmRecordRef(entity="opportunity", id=OPP, name="Meridian - Global Equity Mandate"),
        confidence=0.98, method="email",
    )


def package(key: str = "teams:meeting-1:v1", **kwargs):
    kwargs.setdefault("match", guid_match())
    return make_package(key, **kwargs)


def body(call) -> dict:
    return json.loads(call.request.content)


def sequence(router) -> list[tuple[str, str]]:
    return [(c.request.method, c.request.url.path.removeprefix("/api/data/v9.2")) for c in router.calls]


def standard_routes(api, *, existing: bool = False, users: bool = True):
    api.get("/phonecalls").respond(json={"value": [{"activityid": CALL}] if existing else []})
    api.get("/systemusers").respond(json={"value": [{"systemuserid": USER}] if users else []})
    api.post("/phonecalls").respond(201, json={"activityid": CALL})
    api.patch(f"/phonecalls({CALL})").respond(204)
    api.post("/tasks").respond(201, json={"activityid": TASK})


def footer_of(description: str) -> tuple[str, dict]:
    header, payload = description.rsplit("\n", 2)[-2:]
    return header, json.loads(payload)


# ---- construction / plumbing -----------------------------------------------------------------------------
def test_implements_the_crm_port():
    crm = make_crm()
    assert isinstance(crm, CrmPort) and crm.name == "dataverse"


@pytest.mark.parametrize("url", ["", "contoso.crm.dynamics.com", "/api/data/v9.2"])
def test_base_url_must_be_absolute(url):
    with pytest.raises(ValueError, match="base_url"):
        DataverseCrm(url, token_provider=static_token)


async def test_every_request_carries_the_documented_headers(api):
    api.get("/WhoAmI").respond(json={"UserId": USER, "OrganizationId": "o", "BusinessUnitId": "b"})
    health = await make_crm().health()
    assert health["ok"] is True and health["user_id"] == USER and health["field_profile"] == "standard"
    headers = api.calls.last.request.headers
    assert headers["Authorization"] == "Bearer tok-123"
    assert headers["Accept"] == "application/json"
    assert headers["OData-Version"] == "4.0" and headers["OData-MaxVersion"] == "4.0"
    assert headers["If-None-Match"] == "null"


async def test_health_reports_failures_without_raising(api):
    api.get("/WhoAmI").respond(500, json={"error": {"code": "0x1", "message": "boom"}})
    health = await make_crm().health()
    assert health["ok"] is False and "500" in health["error"]


class FakeCredential:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.expires_on = time.time() + 3600

    async def get_token(self, *scopes, **kwargs):
        self.calls.append(scopes)
        return type("Token", (), {"token": f"cred-{len(self.calls)}", "expires_on": self.expires_on})()


async def test_credential_token_is_cached_and_scoped_to_the_organisation(api):
    api.get("/WhoAmI").respond(json={"UserId": USER})
    credential = FakeCredential()
    crm = make_crm(token_provider=None, credential=credential)
    await crm.health()
    await crm.health()
    assert credential.calls == [(f"{ORG}/.default",)]
    assert api.calls.last.request.headers["Authorization"] == "Bearer cred-1"


async def test_a_401_refreshes_the_token_once(api):
    route = api.get("/WhoAmI")
    route.side_effect = [httpx.Response(401, json={"error": {"message": "expired"}}), httpx.Response(200, json={"UserId": USER})]
    credential = FakeCredential()
    health = await make_crm(token_provider=None, credential=credential).health()
    assert health["ok"] is True and len(credential.calls) == 2
    assert [c.request.headers["Authorization"] for c in api.calls] == ["Bearer cred-1", "Bearer cred-2"]


async def test_429_retry_after_is_honoured(api, sleeper):
    route = api.get("/WhoAmI")
    route.side_effect = [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={"UserId": USER})]
    assert (await make_crm(sleeper).health())["ok"] is True
    assert sleeper.waits == [7.0]


async def test_retries_are_bounded(api, sleeper):
    api.get("/WhoAmI").respond(429, headers={"Retry-After": "2"}, json={"error": {"message": "Number of requests exceeded"}})
    health = await make_crm(sleeper, max_retries=2).health()
    assert health["ok"] is False and "429" in health["error"]
    assert sleeper.waits == [2.0, 2.0] and len(api.calls) == 3


async def test_retry_wait_is_capped(api, sleeper):
    route = api.get("/WhoAmI")
    route.side_effect = [httpx.Response(429, headers={"Retry-After": "900"}), httpx.Response(200, json={"UserId": USER})]
    await make_crm(sleeper, max_retry_wait=30).health()
    assert sleeper.waits == [30.0]


async def test_transport_errors_are_mapped(api):
    api.get("/WhoAmI").mock(side_effect=httpx.ConnectError("no route"))
    health = await make_crm().health()
    assert health["ok"] is False and "ConnectError" in health["error"]


# ---- match -----------------------------------------------------------------------------------------------
def contact_row(**extra):
    row = {
        "contactid": CON, "fullname": "Priya Raman", "emailaddress1": "priya.raman@meridianpension.example",
        "_parentcustomerid_value": ACC,
        "parentcustomerid_account": {"accountid": ACC, "name": "Meridian Pension Fund", "websiteurl": "https://www.meridianpension.example/"},
    }
    return {**row, **extra}


async def test_match_by_email_resolves_contact_account_and_the_single_open_opportunity(api):
    contacts = api.get("/contacts").respond(json={"value": [contact_row()]})
    opportunities = api.get("/opportunities").respond(json={"value": [
        {"opportunityid": OPP, "name": "Meridian - Global Equity Mandate", "_parentaccountid_value": ACC, "statecode": 0}]})
    match = await make_crm().match(make_meta(customer()))
    assert (match.account.id, match.contact.id, match.opportunity.id) == (ACC, CON, OPP)
    assert match.method == "email" and not match.ambiguous
    params = contacts.calls.last.request.url.params
    assert params["$filter"] == "emailaddress1 eq 'priya.raman@meridianpension.example'"
    assert "parentcustomerid_account" in params["$expand"]
    opp_filter = opportunities.calls.last.request.url.params["$filter"]
    assert f"_parentaccountid_value eq {ACC}" in opp_filter and "statecode eq 0" in opp_filter
    assert [m for m, _ in sequence(api)] == ["GET", "GET"]          # the account came expanded; no extra lookup


async def test_match_with_several_open_opportunities_is_ambiguous(api):
    api.get("/contacts").respond(json={"value": [contact_row()]})
    api.get("/opportunities").respond(json={"value": [
        {"opportunityid": OPP, "name": "Equity", "statecode": 0},
        {"opportunityid": "99999999-9999-9999-9999-999999999999", "name": "Bonds", "statecode": 0},
    ]})
    match = await make_crm().match(make_meta(customer()))
    assert match.ambiguous and match.opportunity is None and len(match.candidates) == 2
    assert match.account.id == ACC


async def test_match_survives_an_environment_without_opportunities(api):
    api.get("/contacts").respond(json={"value": [contact_row()]})
    api.get("/opportunities").respond(404, json={"error": {"message": "Resource not found for the segment 'opportunities'."}})
    match = await make_crm().match(make_meta(customer()))
    assert match.account.id == ACC and match.opportunity is None and not match.ambiguous


async def test_match_falls_back_to_organisation_name_and_domain(api):
    api.get("/contacts").respond(json={"value": []})
    accounts = api.get("/accounts").respond(json={"value": [
        {"accountid": ACC, "name": "Meridian Pension Fund", "websiteurl": "https://www.meridianpension.example/"}]})
    api.get("/opportunities").respond(json={"value": []})
    meta = make_meta(customer("c", "Unknown Person", "someone@meridianpension.example"), customer_org="Meridian Pension Fund")
    match = await make_crm().match(meta)
    assert match.method == "name_org" and match.account.id == ACC and match.contact is None
    flt = accounts.calls.last.request.url.params["$filter"]
    assert "name eq 'Meridian Pension Fund'" in flt and "contains(websiteurl,'meridianpension.example')" in flt


async def test_match_escapes_single_quotes_in_odata_filters(api):
    contacts = api.get("/contacts").respond(json={"value": []})
    api.get("/accounts").respond(json={"value": []})
    await make_crm().match(make_meta(customer("c", "Una O'Brien", "una.o'brien@unknown.example"), customer_org="O'Hare Capital"))
    assert "emailaddress1 eq 'una.o''brien@unknown.example'" == contacts.calls[0].request.url.params["$filter"]
    assert "O''Hare Capital" in api.calls[1].request.url.params["$filter"]


async def test_match_with_no_evidence_makes_no_requests(api):
    match = await make_crm().match(make_meta(customer("c", "Unknown", None, role=SpeakerRole.unknown)))
    assert match == CrmMatch() and len(api.calls) == 0


async def test_match_errors_propagate(api):
    api.get("/contacts").respond(500, json={"error": {"message": "server error"}})
    with pytest.raises(DataverseError) as caught:
        await make_crm().match(make_meta(customer()))
    assert caught.value.status == 500


# ---- match by phone ------------------------------------------------------------------------------------------
def caller(phone: str | None = "+1 212 555 0171", name: str = "Alex Kim", **kwargs):
    return customer("cust1", name, None, phone=phone, **kwargs)


def contacts_filters(router) -> list[str]:
    return [c.request.url.params["$filter"] for c in router.calls if c.request.url.path.endswith("/contacts")]


async def test_match_by_phone_filters_the_three_phone_columns_on_exact_spellings(api):
    contacts = api.get("/contacts").respond(json={"value": [contact_row(fullname="Alex Kim", telephone1="+1 212 555 0171")]})
    api.get("/opportunities").respond(json={"value": [
        {"opportunityid": OPP, "name": "Redwood - Manager research approval", "_parentaccountid_value": ACC, "statecode": 0}]})
    match = await make_crm().match(make_meta(caller()))
    assert match.method == "phone" and not match.ambiguous and match.candidates == []
    assert (match.account.id, match.contact.id, match.opportunity.id) == (ACC, CON, OPP)
    assert match.confidence == pytest.approx(0.98)
    params = contacts.calls.last.request.url.params
    flt = params["$filter"]
    assert flt.startswith("(telephone1 eq '+1 212 555 0171' or telephone1 eq '+12125550171' or ")
    for column in ("telephone1", "mobilephone", "telephone2"):
        for spelling in ("+1 212 555 0171", "(212) 555-0171", "212-555-0171", "212.555.0171", "2125550171"):
            assert f"{column} eq '{spelling}'" in flt
    assert flt.endswith(") and statecode eq 0")
    assert params["$select"].split(",") == [
        "contactid", "fullname", "emailaddress1", "telephone1", "mobilephone", "telephone2", "_parentcustomerid_value"]
    assert "parentcustomerid_account" in params["$expand"] and params["$top"] == "50"
    assert [m for m, _ in sequence(api)] == ["GET", "GET"]


async def test_phone_rows_match_on_digits_whichever_column_holds_the_number(api):
    api.get("/contacts").respond(json={"value": [
        contact_row(fullname="Alex Kim", telephone1="+1 212 555 0100", mobilephone="(212) 555-0171")]})
    api.get("/opportunities").respond(json={"value": []})
    match = await make_crm().match(make_meta(caller("212.555.0171")))
    assert (match.method, match.contact.id, match.account.id) == ("phone", CON, ACC)


async def test_a_returned_row_that_does_not_hold_the_number_is_not_a_phone_match(api):
    api.get("/contacts").respond(json={"value": [contact_row(fullname="Alex Kim", telephone1="+1 999 555 0000")]})
    match = await make_crm().match(make_meta(caller(name="Unknown caller")))
    assert match == CrmMatch()


async def test_the_phone_is_not_looked_up_when_the_email_matched(api):
    contacts = api.get("/contacts").respond(json={"value": [contact_row()]})
    api.get("/opportunities").respond(json={"value": []})
    match = await make_crm().match(make_meta(customer(phone="+1 212 555 0171")))
    assert match.method == "email"
    assert len(contacts.calls) == 1 and "telephone1" not in contacts.calls[0].request.url.params["$filter"]


async def test_an_unknown_email_still_gets_the_phone_lookup(api):
    def handler(request: httpx.Request) -> httpx.Response:
        by_phone = "telephone1" in request.url.params["$filter"]
        rows = [contact_row(fullname="Alex Kim", telephone1="+1 212 555 0171")] if by_phone else []
        return httpx.Response(200, json={"value": rows})

    api.get("/contacts").mock(side_effect=handler)
    api.get("/opportunities").respond(json={"value": []})
    match = await make_crm().match(make_meta(customer("c", "Alex Kim", "alex@elsewhere.example", phone="+1 212 555 0171")))
    assert (match.method, match.contact.id) == ("phone", CON)
    first, second = contacts_filters(api)[:2]
    assert first.startswith("emailaddress1 eq") and second.startswith("(telephone1 eq")


async def test_unusable_phone_numbers_make_no_request(api):
    match = await make_crm().match(make_meta(caller("555-01", "Unknown caller")))
    assert match == CrmMatch() and len(api.calls) == 0


async def test_the_callers_own_spelling_is_tried_for_numbers_without_a_known_grouping(api):
    contacts = api.get("/contacts").respond(json={"value": []})
    await make_crm().match(make_meta(caller("tel:+44 20 7946 0191", "Unknown caller")))
    flt = contacts.calls.last.request.url.params["$filter"]
    assert flt.startswith("(telephone1 eq '+44 20 7946 0191' or ")
    assert "mobilephone eq '+442079460191'" in flt and "telephone2 eq '00442079460191'" in flt


async def test_a_hostile_phone_string_is_never_sent_to_dataverse_as_written(api):
    contacts = api.get("/contacts").respond(json={"value": []})
    await make_crm().match(make_meta(caller("212-555-0171'; DROP", "Unknown caller")))
    flt = contacts.calls.last.request.url.params["$filter"]
    assert "DROP" not in flt and "';" not in flt and "telephone1 eq '(212) 555-0171'" in flt


async def test_a_number_held_by_two_contacts_is_ambiguous(api):
    other = "99999999-9999-9999-9999-999999999999"
    api.get("/contacts").respond(json={"value": [
        contact_row(fullname="Alex Kim", telephone1="+1 212 555 0171"),
        contact_row(contactid=other, fullname="Alexa Kimball", mobilephone="(212) 555-0171"),
    ]})
    api.get("/opportunities").respond(json={"value": []})
    match = await make_crm().match(make_meta(caller(name="Unknown caller")))
    assert match.method == "phone" and match.ambiguous is True and match.contact is None
    assert match.account.id == ACC and {c.id for c in match.candidates} == {CON, other}


async def test_the_name_picks_between_contacts_sharing_a_number(api):
    other = "99999999-9999-9999-9999-999999999999"
    api.get("/contacts").respond(json={"value": [
        contact_row(fullname="Alex Kim", telephone1="+1 212 555 0171"),
        contact_row(contactid=other, fullname="Nora Fitzgerald", telephone1="+1 212 555 0171"),
    ]})
    api.get("/opportunities").respond(json={"value": []})
    match = await make_crm().match(make_meta(caller(name="Nora Fitzgerald")))
    assert (match.method, match.contact.id, match.ambiguous) == ("phone", other, False)


async def test_a_name_that_contradicts_the_number_withholds_the_pick_in_dataverse(api):
    api.get("/contacts").respond(json={"value": [contact_row(fullname="Alex Kim", telephone1="+1 212 555 0171")]})
    match = await make_crm().match(make_meta(caller(name="Zed Quill")))
    assert match.ambiguous is True and match.contact is None and match.account is None
    assert [c.id for c in match.candidates] == [CON]
    assert len(api.calls) == 1


# ---- write: standard profile ---------------------------------------------------------------------------------
async def test_standard_profile_write_sequence_and_payloads(api):
    standard_routes(api)
    result = await make_crm().write(package(pointer="https://vault.example/conv-1/transcript/conversation.json"))

    assert result.ok and not result.skipped_duplicate and result.error is None
    assert [(r.entity, r.id) for r in result.created] == [("phonecall", CALL), ("task", TASK)]
    assert {"idempotency_key", "template_id", "engine", "confidence", "product_ids", "topic_ids"} <= set(result.degraded_fields)
    assert sequence(api) == [
        ("GET", "/phonecalls"), ("GET", "/systemusers"), ("POST", "/phonecalls"), ("PATCH", f"/phonecalls({CALL})"),
        ("POST", "/tasks"),
    ]

    lookup = api.calls[0].request.url.params
    assert lookup["$filter"] == "contains(description,'voicecrm-insight v1 key=teams:meeting-1:v1]')"
    assert api.calls[1].request.url.params["$filter"] == f"internalemailaddress eq '{SELLER_EMAIL}'"

    create = api.calls[2].request
    assert create.url.params["$select"] == "activityid"
    assert create.headers["Prefer"] == "return=representation"
    assert create.headers["Content-Type"] == "application/json; charset=utf-8"
    call = json.loads(create.content)
    assert call["subject"] == "Call with Priya Raman - Meridian Pension Fund"
    assert call["directioncode"] is True
    assert call["actualstart"] == "2026-11-10T15:00:00Z" and call["actualend"] == "2026-11-10T15:12:00Z"
    assert call["actualdurationminutes"] == 12
    assert call["regardingobjectid_opportunity_phonecall@odata.bind"] == f"/opportunities({OPP})"
    assert call["ownerid@odata.bind"] == f"/systemusers({USER})"
    assert call["phonecall_activity_parties"] == [
        {"partyid_systemuser@odata.bind": f"/systemusers({USER})", "participationtypemask": 1},
        {"partyid_contact@odata.bind": f"/contacts({CON})", "participationtypemask": 2},
    ]
    assert not any(k.startswith("vcrm_") for k in call)

    header, footer = footer_of(call["description"])
    assert call["description"].startswith("Overview\nMeridian reviewed a global equity mandate.\n\n---\n[voicecrm-insight v1")
    assert header == "[voicecrm-insight v1 key=teams:meeting-1:v1]"
    assert footer == {
        "tpl": "pb_institutional", "eng": "rules", "conf": 0.93, "products": ["SYN-000101", "SYN-000102"],
        "topics": ["fees_expenses", "performance"], "rev": "approved", "lang": "en", "src": "teams", "iid": "meeting-1",
        "tp": "https://vault.example/conv-1/transcript/conversation.json",
    }

    patch = api.calls[3].request
    assert json.loads(patch.content) == {"statecode": 1, "statuscode": 2}
    assert patch.headers["If-Match"] == "*"

    task = json.loads(api.calls[4].request.content)
    assert task == {
        "subject": "Send fee schedule for Class I", "prioritycode": 2, "description": "Evidence: 'I'll send it Friday'",
        "scheduledend": "2026-11-13T12:00:00Z", "regardingobjectid_opportunity_task@odata.bind": f"/opportunities({OPP})",
        "ownerid@odata.bind": f"/systemusers({USER})",
    }


async def test_inbound_call_swaps_the_activity_parties_and_is_marked_received(api):
    standard_routes(api)
    pkg = package()
    pkg.phone_call.direction = "inbound"
    assert (await make_crm().write(pkg)).ok
    call = body(api.calls[2])
    assert call["directioncode"] is False
    assert call["phonecall_activity_parties"] == [
        {"partyid_contact@odata.bind": f"/contacts({CON})", "participationtypemask": 1},
        {"partyid_systemuser@odata.bind": f"/systemusers({USER})", "participationtypemask": 2},
    ]
    assert body(api.calls[3]) == {"statecode": 1, "statuscode": 4}


async def test_regarding_falls_back_to_the_account_and_honours_an_explicit_target(api):
    standard_routes(api)
    account_only = package(match=CrmMatch(account=CrmRecordRef(entity="account", id=ACC)), tasks=[])
    assert (await make_crm().write(account_only)).ok
    assert body(api.calls[2])["regardingobjectid_account_phonecall@odata.bind"] == f"/accounts({ACC})"

    start = len(api.calls)
    explicit = package("teams:meeting-2:v1", tasks=[])
    explicit.phone_call.regarding = CrmRecordRef(entity="contact", id=CON)
    assert (await make_crm().write(explicit)).ok
    assert body(api.calls[start + 2])["regardingobjectid_contact_phonecall@odata.bind"] == f"/contacts({CON})"


async def test_unresolved_regarding_and_missing_owner_are_left_out(api):
    standard_routes(api, users=False)
    result = await make_crm().write(package(match=CrmMatch(), tasks=[]))
    assert result.ok
    call = body(api.calls[2])
    assert not [k for k in call if k.startswith("regardingobjectid") or k == "ownerid@odata.bind"]
    assert "phonecall_activity_parties" not in call


async def test_note_is_truncated_to_fit_the_description_limit_with_the_footer(api):
    standard_routes(api)
    long_note = "Overview. " + "Meridian discussed mandates in detail. " * 120
    result = await make_crm().write(package(description=long_note, tasks=[]))
    description = body(api.calls[2])["description"]
    assert len(description) <= 2000 and "…" in description
    assert footer_of(description)[1]["products"] == ["SYN-000101", "SYN-000102"]
    assert "description_truncated" in result.degraded_fields


async def test_failing_to_complete_the_call_is_not_fatal(api):
    standard_routes(api)
    api.patch(f"/phonecalls({CALL})").respond(403, json={"error": {"message": "no privilege"}})
    result = await make_crm().write(package(tasks=[]))
    assert result.ok and "phonecall_status" in result.degraded_fields


async def test_completion_can_be_disabled(api):
    standard_routes(api)
    assert (await make_crm(complete_phonecalls=False).write(package(tasks=[]))).ok
    assert ("PATCH", f"/phonecalls({CALL})") not in sequence(api)


async def test_created_id_can_come_from_the_odata_entityid_header(api):
    api.get("/phonecalls").respond(json={"value": []})
    api.get("/systemusers").respond(json={"value": []})
    api.post("/phonecalls").respond(204, headers={"OData-EntityId": f"{API}/phonecalls({CALL})"})
    api.patch(f"/phonecalls({CALL})").respond(204)
    result = await make_crm().write(package(tasks=[]))
    assert result.ok and result.created[0].id == CALL


async def test_standard_duplicate_is_skipped_without_writing(api):
    standard_routes(api, existing=True)
    result = await make_crm().write(package())
    assert result.ok and result.skipped_duplicate and result.created == []
    assert sequence(api) == [("GET", "/phonecalls")]


# ---- write: failures -----------------------------------------------------------------------------------------
async def test_failure_after_the_call_is_rolled_back(api):
    standard_routes(api)
    api.post("/tasks").respond(400, json={"error": {"code": "0x80040203", "message": "Invalid property 'foo'"}})
    api.delete(f"/phonecalls({CALL})").respond(204)
    result = await make_crm().write(package())
    assert result.ok is False and "400" in result.error and "Invalid property" in result.error
    assert result.created == []
    assert ("DELETE", f"/phonecalls({CALL})") in sequence(api)


async def test_incomplete_rollback_is_reported(api):
    standard_routes(api)
    api.post("/tasks").respond(400, json={"error": {"message": "bad task"}})
    api.delete(f"/phonecalls({CALL})").respond(500, json={"error": {"message": "cannot delete"}})
    result = await make_crm().write(package())
    assert result.ok is False and "rollback incomplete" in result.error
    assert [r.id for r in result.created] == [CALL]


async def test_first_request_failure_maps_to_a_failed_result(api):
    standard_routes(api)
    api.post("/phonecalls").respond(403, json={"error": {"message": "prvCreatePhoneCall missing"}})
    result = await make_crm().write(package())
    assert result.ok is False and "403" in result.error and result.created == []


async def test_non_guid_record_ids_are_refused_before_any_write(api):
    standard_routes(api)
    pkg = package(match=CrmMatch(opportunity=CrmRecordRef(entity="opportunity", id="opp-meridian")))
    result = await make_crm().write(pkg)
    assert result.ok is False and "GUID" in result.error
    assert ("POST", "/phonecalls") not in sequence(api)


@pytest.mark.parametrize("pointer", ["Rodney: Hello Priya, thanks for joining the call today.", "just words"])
async def test_the_transcript_guard_runs_before_any_http_call(api, pointer):
    result = await make_crm().write(package(pointer=pointer))
    assert result.ok is False and "transcript_pointer" in result.error
    assert len(api.calls) == 0


async def test_transcript_dumps_never_reach_dataverse(api):
    result = await make_crm().write(package(description="Overview\n<v Rodney Seller>Hello Priya</v>"))
    assert result.ok is False and len(api.calls) == 0


async def test_missing_idempotency_key_is_rejected(api):
    assert (await make_crm().write(package(" "))).ok is False and len(api.calls) == 0


# ---- write: custom profile -----------------------------------------------------------------------------------
def template() -> SummaryTemplate:
    return SummaryTemplate(
        template_id="pb_institutional", name="PB", description="test", business_lines=["professional_buyer"],
        sections=[
            TemplateSection(key="overview", title="Overview", kind="text", instruction="x", crm_field="vcrm_overview"),
            TemplateSection(key="decisions_next_steps", title="Next steps", kind="list", instruction="x",
                            crm_field="vcrm_nextsteps"),
            TemplateSection(key="free", title="Free", kind="text", instruction="x", crm_field="vcrm_not_declared"),
        ],
    )


def custom_crm(sleeper: Sleeper | None = None, **kwargs) -> DataverseCrm:
    kwargs.setdefault("topic_ids", TOPICS)
    kwargs.setdefault("templates", {"pb_institutional": template()})
    return make_crm(sleeper, field_profile="custom", **kwargs)


def custom_routes(api, *, existing: bool = False):
    api.get("/vcrm_conversationinsights").respond(json={"value": [{"vcrm_conversationinsightid": INSIGHT}] if existing else []})
    api.get("/systemusers").respond(json={"value": [{"systemuserid": USER}]})
    api.post("/phonecalls").respond(201, json={"activityid": CALL})
    api.patch(f"/phonecalls({CALL})").respond(204)
    api.post("/tasks").respond(201, json={"activityid": TASK})
    api.post("/vcrm_conversationinsights").respond(201, json={"vcrm_conversationinsightid": INSIGHT})


async def test_custom_profile_writes_the_insight_row(api):
    custom_routes(api)
    pkg = package(pointer="https://vault.example/conv-1/transcript/conversation.json")
    pkg.insight.sections = [
        NoteSection(key="overview", title="Overview", text="Meridian reviewed a global equity mandate."),
        NoteSection(key="decisions_next_steps", title="Next steps", items=["Send fee schedule (Rodney, Fri)", "Book follow-up"]),
        NoteSection(key="free", title="Free", text="not mapped to a declared column"),
    ]
    result = await custom_crm().write(pkg)

    assert result.ok and result.degraded_fields == []
    assert [(r.entity, r.id) for r in result.created] == [("phonecall", CALL), ("task", TASK), ("insight", INSIGHT)]
    assert sequence(api) == [
        ("GET", "/vcrm_conversationinsights"), ("GET", "/systemusers"), ("POST", "/phonecalls"),
        ("PATCH", f"/phonecalls({CALL})"), ("POST", "/tasks"), ("POST", "/vcrm_conversationinsights"),
    ]
    assert api.calls[0].request.url.params["$filter"] == "vcrm_idempotencykey eq 'teams:meeting-1:v1'"

    call = body(api.calls[2])
    assert "voicecrm-insight" not in call["description"]            # no footer when custom columns exist
    insight_request = api.calls[5].request
    assert insight_request.url.params["$select"] == "vcrm_conversationinsightid"
    assert insight_request.headers["Prefer"] == "return=representation"
    row = json.loads(insight_request.content)
    assert row["vcrm_name"] == "Call with Priya Raman - Meridian Pension Fund"
    assert row["vcrm_summary"].startswith("Overview")
    assert [s["key"] for s in json.loads(row["vcrm_sectionsjson"])] == ["overview", "decisions_next_steps", "free"]
    assert row["vcrm_productids"] == "SYN-000101,SYN-000102"
    assert row["vcrm_topics"] == "100000000, 100000001"                  # multi-select: comma separated option values
    assert (row["vcrm_templateid"], row["vcrm_engine"], row["vcrm_language"]) == ("pb_institutional", "rules", "en")
    assert row["vcrm_confidence"] == 0.93
    assert row["vcrm_reviewstatus"] == 100000000
    assert (row["vcrm_sourcesystem"], row["vcrm_sourceinteractionid"]) == ("teams", "meeting-1")
    assert (row["vcrm_idempotencykey"], row["vcrm_conversationid"]) == ("teams:meeting-1:v1", "conv-1")
    assert row["vcrm_transcriptpointer"] == "https://vault.example/conv-1/transcript/conversation.json"
    assert row["vcrm_overview"] == "Meridian reviewed a global equity mandate."
    assert row["vcrm_nextsteps"] == "- Send fee schedule (Rodney, Fri)\n- Book follow-up"
    assert "vcrm_not_declared" not in row and "vcrm_model" not in row
    assert row["vcrm_AccountId@odata.bind"] == f"/accounts({ACC})"
    assert row["vcrm_ContactId@odata.bind"] == f"/contacts({CON})"
    assert row["vcrm_OpportunityId@odata.bind"] == f"/opportunities({OPP})"
    assert row["vcrm_PhoneCallId@odata.bind"] == f"/phonecalls({CALL})"


async def test_custom_profile_duplicate_is_skipped(api):
    custom_routes(api, existing=True)
    result = await custom_crm().write(package())
    assert result.skipped_duplicate and result.created == []
    assert sequence(api) == [("GET", "/vcrm_conversationinsights")]


async def test_custom_profile_with_a_missing_table_degrades_to_the_footer(api):
    custom_routes(api)
    api.get("/vcrm_conversationinsights").respond(404, json={"error": {"message": "Resource not found for the segment"}})
    api.get("/phonecalls").respond(json={"value": []})
    result = await custom_crm().write(package(tasks=[]))
    assert result.ok and "custom_table_unavailable" in result.degraded_fields and "product_ids" in result.degraded_fields
    assert ("POST", "/vcrm_conversationinsights") not in sequence(api)
    assert "[voicecrm-insight v1 key=teams:meeting-1:v1]" in body(next(c for c in api.calls if c.request.method == "POST"))["description"]


async def test_topics_without_a_choice_value_are_not_written_and_flagged(api):
    custom_routes(api)
    result = await custom_crm(topic_ids=["performance"]).write(package(topics=["performance", "brand_new_topic"]))
    assert result.ok and "topic_ids" in result.degraded_fields
    assert json.loads(api.calls[-1].request.content)["vcrm_topics"] == "100000000"


async def test_custom_profile_failure_deletes_everything_it_created(api):
    custom_routes(api)
    api.post("/vcrm_conversationinsights").respond(400, json={"error": {"message": "Invalid property 'vcrm_foo'"}})
    for entity_set, record in (("tasks", TASK), ("phonecalls", CALL)):
        api.delete(f"/{entity_set}({record})").respond(204)
    result = await custom_crm().write(package())
    assert result.ok is False and "vcrm_foo" in result.error and result.created == []
    deletes = [p for m, p in sequence(api) if m == "DELETE"]
    assert deletes == [f"/tasks({TASK})", f"/phonecalls({CALL})"]


async def test_custom_profile_needs_a_profile_file():
    with pytest.raises(FileNotFoundError):
        make_crm(field_profile="custom", profile=None, profile_path="does-not-exist.yaml")


async def test_an_unexpected_error_still_rolls_back_what_was_created(api, monkeypatch):
    standard_routes(api)
    api.delete(f"/phonecalls({CALL})").respond(204)

    async def boom(*args, **kwargs):
        raise KeyError("surprise")

    monkeypatch.setattr(DataverseCrm, "_task_body", boom)
    result = await make_crm().write(package())
    assert result.ok is False and "unexpected KeyError" in result.error and result.created == []
    assert ("DELETE", f"/phonecalls({CALL})") in sequence(api)


def test_topic_choice_values_default_to_the_taxonomy_order(monkeypatch):
    from voicecrm import data_loader
    from voicecrm.domain.taxonomy import Taxonomy, Topic

    monkeypatch.setattr(data_loader, "load_taxonomy", lambda *a, **k: Taxonomy(
        version="t", topics=[Topic(topic_id="a", label="A"), Topic(topic_id="b", label="B")]))
    crm = make_crm(field_profile="custom", templates={})
    assert crm._topic_options == {"a": 100000000, "b": 100000001}


def test_a_missing_taxonomy_leaves_topics_unmapped(monkeypatch):
    from voicecrm import data_loader

    def missing(*args, **kwargs):
        raise FileNotFoundError("topics.json")

    monkeypatch.setattr(data_loader, "load_taxonomy", missing)
    assert make_crm(field_profile="custom", templates={})._topic_options == {}


async def test_standard_profile_works_without_a_profile_file(api):
    standard_routes(api)
    crm = make_crm(profile=None, profile_path="does-not-exist.yaml")
    assert (await crm.write(package(tasks=[]))).ok


async def test_aclose_closes_only_a_client_the_adapter_created():
    owned = DataverseCrm(ORG, token_provider=static_token, profile=_PROFILE)
    await owned.aclose()
    assert owned._http.client.is_closed
    await make_crm().aclose()
    assert not _CLIENT.is_closed


# ---- list_records ---------------------------------------------------------------------------------------------
async def test_list_phonecalls_only_returns_records_created_by_the_app(api):
    api.get("/WhoAmI").respond(json={"UserId": USER})
    route = api.get("/phonecalls").respond(json={"value": [{
        "activityid": CALL, "subject": "Call with Priya", "description": "note", "directioncode": True,
        "actualstart": "2026-11-10T15:00:00Z", "actualend": "2026-11-10T15:12:00Z", "actualdurationminutes": 12,
        "createdon": "2026-11-10T15:13:00Z", "_regardingobjectid_value": OPP,
        "_regardingobjectid_value@Microsoft.Dynamics.CRM.lookuplogicalname": "opportunity",
        "_regardingobjectid_value@OData.Community.Display.V1.FormattedValue": "Meridian - Global Equity Mandate",
        "_ownerid_value": USER, "_ownerid_value@OData.Community.Display.V1.FormattedValue": "Rodney Seller",
    }]})
    (row,) = await make_crm().list_records("phonecall", limit=5)
    params = route.calls.last.request.url.params
    assert params["$filter"] == f"_createdby_value eq {USER}"
    assert params["$orderby"] == "createdon desc" and params["$top"] == "5"
    assert "FormattedValue" in route.calls.last.request.headers["Prefer"]
    assert row["regarding_entity"] == "opportunity" and row["regarding_name"] == "Meridian - Global Equity Mandate"
    assert row["owner_name"] == "Rodney Seller" and row["direction"] == "outbound" and row["duration_minutes"] == 12


async def test_list_tasks_maps_due_date_and_priority(api):
    api.get("/WhoAmI").respond(json={"UserId": USER})
    api.get("/tasks").respond(json={"value": [{"activityid": TASK, "subject": "Send fees", "scheduledend": "2026-11-13T12:00:00Z",
                                               "prioritycode": 2, "createdon": "2026-11-10T15:13:00Z"}]})
    (row,) = await make_crm().list_records("task")
    assert row["due"] == "2026-11-13" and row["priority"] == "high" and row["subject"] == "Send fees"


async def test_list_insights_parses_footers_in_the_standard_profile(api):
    description = "Note text\n\n---\n[voicecrm-insight v1 key=teams:m1:v1]\n" + json.dumps(
        {"tpl": "pb_institutional", "eng": "rules", "conf": 0.9, "products": ["SYN-000101"], "topics": ["esg"], "src": "teams"})
    route = api.get("/phonecalls").respond(json={"value": [
        {"activityid": CALL, "subject": "Call", "description": description, "createdon": "2026-11-10T15:13:00Z"},
        {"activityid": TASK, "subject": "Other", "description": "no footer here", "createdon": "2026-11-10T15:00:00Z"},
    ]})
    (row,) = await make_crm().list_records("insight")
    assert "voicecrm-insight" in route.calls.last.request.url.params["$filter"]
    assert (row["idempotency_key"], row["template_id"], row["product_ids"], row["topic_ids"]) == (
        "teams:m1:v1", "pb_institutional", ["SYN-000101"], ["esg"])


async def test_list_insights_maps_custom_columns_back(api):
    api.get("/vcrm_conversationinsights").respond(json={"value": [{
        "vcrm_conversationinsightid": INSIGHT, "createdon": "2026-11-10T15:13:00Z", "vcrm_productids": "SYN-000101,SYN-000102",
        "vcrm_topics": "100000000,100000002", "vcrm_templateid": "pb_institutional", "vcrm_idempotencykey": "teams:m1:v1",
    }]})
    (row,) = await custom_crm().list_records("insight")
    assert row["id"] == INSIGHT and row["product_ids"] == ["SYN-000101", "SYN-000102"]
    assert row["topic_ids"] == ["fees_expenses", "esg"] and row["template_id"] == "pb_institutional"


async def test_list_reference_entities_map_to_the_simulator_shape(api):
    api.get("/accounts").respond(json={"value": [{"accountid": ACC, "name": "Meridian Pension Fund", "websiteurl": "https://m.example"}]})
    api.get("/contacts").respond(json={"value": [{
        "contactid": CON, "fullname": "Priya Raman", "emailaddress1": "p@m.example", "_parentcustomerid_value": ACC,
        "_parentcustomerid_value@OData.Community.Display.V1.FormattedValue": "Meridian Pension Fund"}]})
    api.get("/opportunities").respond(json={"value": [{"opportunityid": OPP, "name": "Equity", "statecode": 1, "_parentaccountid_value": ACC}]})
    crm = make_crm()
    assert (await crm.list_records("account"))[0] == {"id": ACC, "name": "Meridian Pension Fund", "domain": "https://m.example"}
    assert (await crm.list_records("contact"))[0]["account_name"] == "Meridian Pension Fund"
    assert (await crm.list_records("opportunity"))[0]["stage"] == "Won"


async def test_unknown_entity_is_a_value_error():
    with pytest.raises(ValueError, match="unknown entity"):
        await make_crm().list_records("lead")


# ---- contract with the shipped data -------------------------------------------------------------------------
def test_the_shipped_profile_declares_every_template_crm_field(data_dir):
    from voicecrm import data_loader

    profile = load_field_profile(data_dir / "crm" / "field_profile.yaml")
    declared = {c.logical_name for c in profile.custom.section_columns}
    needed = {s.crm_field for t in data_loader.load_templates(data_dir).values() for s in t.sections if s.crm_field}
    assert needed <= declared
    assert profile.custom.column("idempotency_key") is not None and date.today()
