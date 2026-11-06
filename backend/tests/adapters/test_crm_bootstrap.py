"""Schema plan and bootstrap CLI for the Dataverse "custom" field profile (mocked HTTP only)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest
import respx

from voicecrm.adapters.crm.dataverse import DataverseError, DataverseHttp
from voicecrm.adapters.crm.field_profile import build_topic_option_map, load_field_profile
from voicecrm.adapters.crm.schema_plan import apply_plan, build_schema_plan
from voicecrm.config import REPO_ROOT

ORG = "https://sandbox-test.crm.dynamics.com"
API = f"{ORG}/api/data/v9.2"
TOPICS = [("fees_expenses", "Fees and expenses"), ("performance", "Performance"), ("esg", "ESG")]
ENTITY = "/EntityDefinitions(LogicalName='vcrm_conversationinsight')"

_CLIENT = httpx.AsyncClient(verify=False)
_PROFILE = load_field_profile()


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("dataverse_bootstrap", REPO_ROOT / "tools" / "dataverse_bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _token() -> str:
    return "tok"


async def _no_sleep(_: float) -> None:
    return None


def make_http() -> DataverseHttp:
    return DataverseHttp(API, _token, _CLIENT, max_retries=1, max_retry_wait=1, sleep=_no_sleep)


def steps_by_name(plan):
    return {s.name: s for s in plan.steps}


# ---- plan ---------------------------------------------------------------------------------------------------
def test_plan_creates_table_then_columns_then_lookups_keys_and_publishes():
    plan = build_schema_plan(_PROFILE, TOPICS)
    names = [s.name for s in plan.steps]
    assert names[0] == "table vcrm_conversationinsight"
    assert names[-1] == "publish customizations"
    kinds = [n.split(" ", 1)[0] for n in names]
    assert kinds == sorted(kinds, key=["table", "column", "lookup", "alternate", "publish"].index)
    assert plan.warnings == []


def test_table_definition_has_a_primary_name_column_and_the_solution_header():
    table = build_schema_plan(_PROFILE, TOPICS).steps[0]
    assert table.method == "POST" and table.path == "/EntityDefinitions"
    assert table.headers == {"MSCRM.SolutionUniqueName": "voicecrm_poc"}
    body = table.body
    assert body["@odata.type"] == "Microsoft.Dynamics.CRM.EntityMetadata"
    assert body["SchemaName"] == "vcrm_ConversationInsight" and body["OwnershipType"] == "UserOwned"
    assert (body["HasActivities"], body["HasNotes"], body["IsActivity"]) == (False, False, False)
    (primary,) = body["Attributes"]
    assert primary["IsPrimaryName"] is True and primary["SchemaName"] == "vcrm_Name"
    assert primary["@odata.type"].endswith("StringAttributeMetadata") and primary["FormatName"] == {"Value": "Text"}
    assert primary["DisplayName"]["LocalizedLabels"][0] == {
        "@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel", "Label": "Name", "LanguageCode": 1033}


def test_column_payloads_follow_the_metadata_api_shapes():
    steps = steps_by_name(build_schema_plan(_PROFILE, TOPICS))
    summary = steps["column vcrm_summary"]
    assert summary.path == f"{ENTITY}/Attributes"
    assert summary.body["@odata.type"].endswith("MemoAttributeMetadata")
    assert (summary.body["AttributeTypeName"], summary.body["Format"], summary.body["MaxLength"]) == (
        {"Value": "MemoType"}, "TextArea", 20000)
    confidence = steps["column vcrm_confidence"].body
    assert confidence["@odata.type"].endswith("DecimalAttributeMetadata")
    assert (confidence["MinValue"], confidence["MaxValue"], confidence["Precision"]) == (0, 1, 4)

    status = steps["column vcrm_reviewstatus"].body
    assert status["@odata.type"].endswith("PicklistAttributeMetadata") and status["AttributeType"] == "Picklist"
    assert [o["Value"] for o in status["OptionSet"]["Options"]] == [100000000, 100000001]
    assert status["OptionSet"]["IsGlobal"] is False

    topics = steps["column vcrm_topics"].body
    assert topics["@odata.type"].endswith("MultiSelectPicklistAttributeMetadata")
    assert (topics["AttributeType"], topics["AttributeTypeName"]) == ("Virtual", {"Value": "MultiSelectPicklistType"})
    assert [(o["Value"], o["Label"]["LocalizedLabels"][0]["Label"]) for o in topics["OptionSet"]["Options"]] == [
        (100000000, "Fees and expenses"), (100000001, "Performance"), (100000002, "ESG")]


def test_template_section_columns_exist_with_the_logical_names_the_templates_use(data_dir):
    from voicecrm import data_loader

    steps = steps_by_name(build_schema_plan(_PROFILE, TOPICS))
    wanted = {s.crm_field for t in data_loader.load_templates(data_dir).values() for s in t.sections if s.crm_field}
    assert {f"column {name}" for name in wanted} <= set(steps)


def test_lookups_are_one_to_many_relationships_with_non_cascading_deletes():
    steps = steps_by_name(build_schema_plan(_PROFILE, TOPICS))
    account = steps["lookup vcrm_accountid -> account"]
    assert account.path == "/RelationshipDefinitions" and account.optional is False
    body = account.body
    assert body["@odata.type"].endswith("OneToManyRelationshipMetadata")
    assert (body["ReferencedEntity"], body["ReferencedAttribute"], body["ReferencingEntity"]) == (
        "account", "accountid", "vcrm_conversationinsight")
    assert body["ReferencingEntityNavigationPropertyName"] == "vcrm_AccountId"
    assert body["CascadeConfiguration"]["Delete"] == "RemoveLink"
    assert body["Lookup"]["@odata.type"].endswith("LookupAttributeMetadata") and body["Lookup"]["SchemaName"] == "vcrm_AccountId"
    assert steps["lookup vcrm_phonecallid -> phonecall"].optional is True


def test_alternate_key_targets_the_idempotency_column():
    key = steps_by_name(build_schema_plan(_PROFILE, TOPICS))["alternate key vcrm_conversationinsight_idempotency"]
    assert key.path == f"{ENTITY}/Keys"
    assert key.body["@odata.type"].endswith("EntityKeyMetadata")
    assert key.body["KeyAttributes"] == ["vcrm_idempotencykey"] and key.optional is True


def test_publish_step_lists_the_table():
    publish = build_schema_plan(_PROFILE, TOPICS).steps[-1]
    assert publish.path == "/PublishXml"
    assert "<entity>vcrm_conversationinsight</entity>" in publish.body["ParameterXml"]


def test_everything_uses_the_publisher_prefix():
    plan = build_schema_plan(_PROFILE, TOPICS)
    for step in plan.steps:
        if step.name.startswith(("column", "lookup", "table")):
            assert "vcrm_" in step.name
    assert all(s.body is None or "vcrm_" in json.dumps(s.body) for s in plan.steps if s.path != "/PublishXml")


def test_without_a_taxonomy_the_topics_column_is_skipped_with_a_warning():
    plan = build_schema_plan(_PROFILE, [])
    assert "column vcrm_topics" not in steps_by_name(plan)
    assert plan.warnings and "vcrm_topics" in plan.warnings[0]


def test_pinned_topic_values_are_honoured_and_stable():
    profile = load_field_profile()
    profile.custom.topic_options.overrides["esg"] = 100000000
    values = build_topic_option_map([t for t, _ in TOPICS], base=100000000, overrides={"esg": 100000000})
    assert values == {"esg": 100000000, "fees_expenses": 100000001, "performance": 100000002}
    options = steps_by_name(build_schema_plan(profile, TOPICS))["column vcrm_topics"].body["OptionSet"]["Options"]
    assert {o["Label"]["LocalizedLabels"][0]["Label"]: o["Value"] for o in options}["ESG"] == 100000000


# ---- execution -------------------------------------------------------------------------------------------------
async def test_dry_run_sends_nothing():
    lines: list[str] = []
    with respx.mock(base_url=API, assert_all_called=False) as api:
        results = await apply_plan(build_schema_plan(_PROFILE, TOPICS), make_http(), apply=False, out=lines.append)
        assert len(api.calls) == 0
    assert {r.status for r in results} == {"planned"} and lines[0].startswith("[plan]    POST /EntityDefinitions")


async def test_apply_creates_missing_objects_and_skips_existing_ones():
    plan = build_schema_plan(_PROFILE, TOPICS)
    with respx.mock(base_url=API, assert_all_called=False) as api:
        api.get(ENTITY).respond(404, json={"error": {"message": "not found"}})
        api.get(f"{ENTITY}/Attributes(LogicalName='vcrm_summary')").respond(json={"LogicalName": "vcrm_summary"})
        api.get(url__regex=r".*/Attributes\(LogicalName='.*'\)").respond(404, json={"error": {"message": "nf"}})
        api.get(url__regex=r".*/RelationshipDefinitions\(SchemaName='.*'\)").respond(404, json={"error": {"message": "nf"}})
        api.post(url__regex=r".*/(EntityDefinitions|Attributes|RelationshipDefinitions|Keys|PublishXml)$").respond(204)
        lines: list[str] = []
        results = await apply_plan(plan, make_http(), apply=True, out=lines.append)
        calls = list(api.calls)
    by_name = {r.step.name: r.status for r in results}
    assert by_name["column vcrm_summary"] == "skipped"
    assert by_name["table vcrm_conversationinsight"] == "created" and by_name["column vcrm_engine"] == "created"
    assert by_name["publish customizations"] == "created"
    posts = [c for c in calls if c.request.method == "POST"]
    assert len(posts) == len(plan.steps) - 1                                  # all but the existing column
    first = posts[0].request
    assert first.headers["MSCRM.SolutionUniqueName"] == "voicecrm_poc"
    assert json.loads(first.content)["SchemaName"] == "vcrm_ConversationInsight"


async def test_an_existing_table_is_not_recreated():
    plan = build_schema_plan(_PROFILE, [])
    with respx.mock(base_url=API, assert_all_called=False) as api:
        api.get(url__regex=r".*").respond(json={"LogicalName": "x", "Keys": [
            {"SchemaName": "vcrm_conversationinsight_idempotency"}]})
        api.post("/PublishXml").respond(204)
        results = await apply_plan(plan, make_http(), apply=True, out=lambda _: None)
        posts = [c for c in api.calls if c.request.method == "POST"]
    assert [r.step.name for r in results if r.status == "created"] == ["publish customizations"]
    assert len(posts) == 1


async def test_an_optional_failure_is_reported_and_execution_continues():
    plan = build_schema_plan(_PROFILE, [])
    with respx.mock(base_url=API, assert_all_called=False) as api:
        api.get(url__regex=r".*").respond(404, json={"error": {"message": "nf"}})
        api.post(url__regex=r".*/RelationshipDefinitions$").mock(side_effect=lambda request: (
            httpx.Response(400, json={"error": {"message": "cannot reference phonecall"}})
            if b"phonecall" in request.content and b"vcrm_PhoneCallId" in request.content else httpx.Response(204)))
        api.post(url__regex=r".*").respond(204)
        results = await apply_plan(plan, make_http(), apply=True, out=lambda _: None)
    statuses = {r.step.name: r.status for r in results}
    assert statuses["lookup vcrm_phonecallid -> phonecall"] == "failed_optional"
    assert statuses["publish customizations"] == "created"


async def test_a_required_failure_stops_the_run():
    plan = build_schema_plan(_PROFILE, [])
    with respx.mock(base_url=API, assert_all_called=False) as api:
        api.get(url__regex=r".*").respond(404, json={"error": {"message": "nf"}})
        api.post("/EntityDefinitions").respond(403, json={"error": {"message": "prvCreateEntity missing"}})
        with pytest.raises(DataverseError) as caught:
            await apply_plan(plan, make_http(), apply=True, out=lambda _: None)
        posts = [c for c in api.calls if c.request.method == "POST"]
    assert caught.value.status == 403
    assert len(posts) == 1


# ---- CLI -------------------------------------------------------------------------------------------------------
def write_taxonomy(tmp_path: Path) -> Path:
    path = tmp_path / "topics.json"
    path.write_text(json.dumps({"version": "t", "topics": [
        {"topic_id": "fees_expenses", "label": "Fees and expenses"}, {"topic_id": "esg", "label": "ESG"}]}), encoding="utf-8")
    return path


def test_cli_dry_run_prints_the_plan_and_needs_no_credentials(tool, tmp_path, capsys):
    assert tool.main(["--taxonomy", str(write_taxonomy(tmp_path)), "--show-bodies"]) == 0
    out = capsys.readouterr().out
    assert "[plan]    POST /EntityDefinitions  # table vcrm_conversationinsight" in out
    assert "column vcrm_topics" in out and "Dry-run" in out and "nothing sent" in out
    assert '"Label": "Fees and expenses"' in out


def test_cli_lists_the_topic_choice_values(tool, tmp_path, capsys):
    assert tool.main(["--list-topic-values", "--taxonomy", str(write_taxonomy(tmp_path))]) == 0
    assert capsys.readouterr().out.splitlines() == ["100000000  fees_expenses", "100000001  esg"]


def test_cli_apply_needs_an_environment_url(tool, tmp_path, monkeypatch):
    monkeypatch.delenv("VOICECRM_DATAVERSE_URL", raising=False)
    monkeypatch.setattr(tool, "get_settings", lambda: type("S", (), {"dataverse_url": "", "data_dir": tmp_path})())
    with pytest.raises(SystemExit, match="no environment URL"):
        tool.main(["--apply", "--yes", "--taxonomy", str(write_taxonomy(tmp_path))])


def test_cli_refuses_msx(tool, tmp_path):
    with pytest.raises(SystemExit, match="MSX"):
        tool.main(["--apply", "--yes", "--url", "https://microsoftsales.crm.dynamics.com",
                   "--taxonomy", str(write_taxonomy(tmp_path))])


def test_cli_apply_runs_the_plan_with_the_credential(tool, tmp_path, capsys, monkeypatch):
    seen: dict[str, str] = {}

    def fake_token(credential, scope):
        seen["scope"] = scope

        async def provider() -> str:
            return "cli-token"

        async def aclose() -> None:
            return None

        provider.aclose = aclose   # type: ignore[attr-defined]
        return provider

    monkeypatch.setattr(tool, "CachedToken", fake_token)
    with respx.mock(base_url=API, assert_all_called=False) as api:
        api.get(url__regex=r".*").respond(404, json={"error": {"message": "nf"}})
        api.post(url__regex=r".*").respond(204)
        code = tool.main(["--apply", "--yes", "--url", f"{ORG}/", "--taxonomy", str(write_taxonomy(tmp_path))])
        last = api.calls.last.request
    assert code == 0 and seen["scope"] == f"{ORG}/.default"
    assert last.headers["Authorization"] == "Bearer cli-token"
    assert "created" in capsys.readouterr().out
