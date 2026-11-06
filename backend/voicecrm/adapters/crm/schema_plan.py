"""Schema plan for the Dataverse "custom" field profile: the metadata Web API requests that create the table.

`build_schema_plan` is pure (profile + topics in, ordered requests out), so it can be inspected, diffed and unit tested
without an environment. `apply_plan` executes it with an existence check before every step (so re-running is safe).
`tools/dataverse_bootstrap.py` is the CLI around both.

Request shapes follow the Microsoft Learn Web API metadata articles (create table / column / relationship definitions;
EntityKeyMetadata for alternate keys; PublishXml). NOT yet exercised against a live Dataverse environment.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from .dataverse import DataverseError, DataverseHttp
from .field_profile import ColumnSpec, FieldProfile, LookupSpec, build_topic_option_map

logger = logging.getLogger(__name__)

LANGUAGE_CODE = 1033
_NO_CASCADE = {
    "Assign": "NoCascade", "Delete": "RemoveLink", "Merge": "NoCascade", "Reparent": "NoCascade",
    "Share": "NoCascade", "Unshare": "NoCascade",
}


@dataclass(frozen=True, slots=True)
class ExistsCheck:
    """GET that tells whether a step's target already exists: HTTP 200 (and, with `collection`, a matching item)."""

    path: str
    params: dict[str, str] = field(default_factory=dict)
    collection: str | None = None
    match_field: str | None = None
    match_value: str | None = None


@dataclass(frozen=True, slots=True)
class SchemaStep:
    name: str
    method: str
    path: str
    body: dict[str, Any] | None
    headers: dict[str, str] = field(default_factory=dict)
    check: ExistsCheck | None = None
    optional: bool = False


@dataclass(slots=True)
class SchemaPlan:
    steps: list[SchemaStep] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


StepStatus = Literal["planned", "created", "skipped", "failed_optional"]


@dataclass(frozen=True, slots=True)
class StepResult:
    step: SchemaStep
    status: StepStatus
    detail: str = ""


# ---- metadata fragments ----------------------------------------------------------------------------------------
def label(text: str) -> dict[str, Any]:
    return {
        "@odata.type": "Microsoft.Dynamics.CRM.Label",
        "LocalizedLabels": [
            {"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel", "Label": text, "LanguageCode": LANGUAGE_CODE}
        ],
    }


def _required(level: str = "None") -> dict[str, Any]:
    return {"Value": level, "CanBeChanged": True, "ManagedPropertyLogicalName": "canmodifyrequirementlevelsettings"}


def _common(column: ColumnSpec) -> dict[str, Any]:
    return {
        "SchemaName": column.schema_name,
        "DisplayName": label(column.display_name),
        "Description": label(column.description or column.display_name),
        "RequiredLevel": _required(),
    }


def _option_set(options: Sequence[tuple[int, str]]) -> dict[str, Any]:
    return {
        "@odata.type": "Microsoft.Dynamics.CRM.OptionSetMetadata",
        "Options": [{"Value": value, "Label": label(text)} for value, text in options],
        "IsGlobal": False,
        "OptionSetType": "Picklist",
    }


def attribute_body(column: ColumnSpec, options: Sequence[tuple[int, str]] = ()) -> dict[str, Any]:
    """AttributeMetadata JSON for one column (also used, with IsPrimaryName, inside the table definition)."""
    body = _common(column)
    if column.type == "string":
        body.update({
            "@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata", "AttributeType": "String",
            "AttributeTypeName": {"Value": "StringType"}, "FormatName": {"Value": "Text"},
            "MaxLength": column.max_length or 200,
        })
    elif column.type == "memo":
        body.update({
            "@odata.type": "Microsoft.Dynamics.CRM.MemoAttributeMetadata", "AttributeType": "Memo",
            "AttributeTypeName": {"Value": "MemoType"}, "Format": "TextArea", "ImeMode": "Disabled",
            "MaxLength": column.max_length or 2000,
        })
    elif column.type == "decimal":
        body.update({
            "@odata.type": "Microsoft.Dynamics.CRM.DecimalAttributeMetadata", "AttributeType": "Decimal",
            "AttributeTypeName": {"Value": "DecimalType"}, "MinValue": column.min_value if column.min_value is not None else 0,
            "MaxValue": column.max_value if column.max_value is not None else 1, "Precision": column.precision or 2,
        })
    elif column.type == "choice":
        body.update({
            "@odata.type": "Microsoft.Dynamics.CRM.PicklistAttributeMetadata", "AttributeType": "Picklist",
            "AttributeTypeName": {"Value": "PicklistType"}, "SourceTypeMask": 0,
            "OptionSet": _option_set(options),
        })
    else:  # multiselect
        body.update({
            "@odata.type": "Microsoft.Dynamics.CRM.MultiSelectPicklistAttributeMetadata", "AttributeType": "Virtual",
            "AttributeTypeName": {"Value": "MultiSelectPicklistType"}, "SourceTypeMask": 0,
            "OptionSet": _option_set(options),
        })
    return body


def relationship_body(profile: FieldProfile, lookup: LookupSpec) -> dict[str, Any]:
    table = profile.custom.table
    return {
        "@odata.type": "Microsoft.Dynamics.CRM.OneToManyRelationshipMetadata",
        "SchemaName": lookup.relationship_schema_name,
        "ReferencedEntity": lookup.referenced_entity,
        "ReferencedAttribute": lookup.referenced_attribute,
        "ReferencingEntity": table.logical_name,
        "ReferencingEntityNavigationPropertyName": lookup.schema_name,
        "AssociatedMenuConfiguration": {
            "Behavior": "UseCollectionName", "Group": "Details", "Label": label(table.display_collection_name),
            "Order": 10000,
        },
        "CascadeConfiguration": dict(_NO_CASCADE),
        "Lookup": {
            "@odata.type": "Microsoft.Dynamics.CRM.LookupAttributeMetadata", "AttributeType": "Lookup",
            "AttributeTypeName": {"Value": "LookupType"}, "SchemaName": lookup.schema_name,
            "DisplayName": label(lookup.display_name),
            "Description": label(f"{lookup.display_name} the conversation concerned"),
            "RequiredLevel": _required(),
        },
    }


# ---- plan --------------------------------------------------------------------------------------------------------
def build_schema_plan(profile: FieldProfile, topics: Sequence[tuple[str, str]]) -> SchemaPlan:
    """Ordered metadata requests for the custom table. `topics` = (topic_id, English label) in taxonomy order."""
    spec = profile.custom
    table = spec.table
    solution = {"MSCRM.SolutionUniqueName": profile.solution_unique_name} if profile.solution_unique_name else {}
    plan = SchemaPlan()
    entity_path = f"/EntityDefinitions(LogicalName='{table.logical_name}')"

    primary = next(c for c in spec.columns if c.primary_name)
    primary_body = attribute_body(primary)
    primary_body["IsPrimaryName"] = True
    plan.steps.append(SchemaStep(
        name=f"table {table.logical_name}",
        method="POST",
        path="/EntityDefinitions",
        body={
            "@odata.type": "Microsoft.Dynamics.CRM.EntityMetadata",
            "SchemaName": table.schema_name,
            "DisplayName": label(table.display_name),
            "DisplayCollectionName": label(table.display_collection_name),
            "Description": label(table.description),
            "OwnershipType": table.ownership,
            "HasActivities": False, "HasNotes": False, "IsActivity": False,
            "Attributes": [primary_body],
        },
        headers=dict(solution),
        check=ExistsCheck(entity_path, {"$select": "LogicalName"}),
    ))

    topic_ids = [t for t, _ in topics]
    topic_values = build_topic_option_map(topic_ids, base=spec.topic_options.base, overrides=spec.topic_options.overrides)
    topic_options = [(topic_values[t], text) for t, text in topics]

    def add_column(column: ColumnSpec, options: Sequence[tuple[int, str]] = ()) -> None:
        plan.steps.append(SchemaStep(
            name=f"column {column.logical_name}", method="POST", path=f"{entity_path}/Attributes",
            body=attribute_body(column, options), headers=dict(solution),
            check=ExistsCheck(f"{entity_path}/Attributes(LogicalName='{column.logical_name}')", {"$select": "LogicalName"}),
        ))

    for column in spec.columns:
        if column.primary_name:
            continue
        if column.type == "multiselect":
            if not topic_options:
                plan.warnings.append(
                    f"column {column.logical_name} skipped: no topics (data/taxonomy/topics.json); re-run once it exists"
                )
                continue
            add_column(column, topic_options)
        elif column.type == "choice":
            add_column(column, [(value, key.replace("_", " ").capitalize()) for key, value in column.options.items()])
        else:
            add_column(column)
    for column in spec.section_columns:
        add_column(column)

    for lookup in spec.lookups:
        plan.steps.append(SchemaStep(
            name=f"lookup {lookup.logical_name} -> {lookup.referenced_entity}", method="POST",
            path="/RelationshipDefinitions", body=relationship_body(profile, lookup), headers=dict(solution),
            check=ExistsCheck(f"/RelationshipDefinitions(SchemaName='{lookup.relationship_schema_name}')",
                              {"$select": "SchemaName"}),
            optional=lookup.optional,
        ))

    by_key = {c.key: c for c in spec.columns}
    for key in spec.alternate_keys:
        plan.steps.append(SchemaStep(
            name=f"alternate key {key.schema_name}", method="POST", path=f"{entity_path}/Keys",
            body={
                "@odata.type": "Microsoft.Dynamics.CRM.EntityKeyMetadata", "SchemaName": key.schema_name,
                "DisplayName": label(key.display_name), "KeyAttributes": [by_key[c].logical_name for c in key.columns],
            },
            headers=dict(solution),
            check=ExistsCheck(entity_path, {"$select": "LogicalName", "$expand": "Keys($select=SchemaName)"},
                              collection="Keys", match_field="SchemaName", match_value=key.schema_name),
            optional=True,    # key index creation is asynchronous and can fail on existing data; the adapter filters anyway
        ))

    publish_xml = f"<importexportxml><entities><entity>{table.logical_name}</entity></entities></importexportxml>"
    plan.steps.append(SchemaStep(
        name="publish customizations", method="POST", path="/PublishXml",
        body={"ParameterXml": publish_xml},
    ))
    return plan


# ---- execution ---------------------------------------------------------------------------------------------------
async def _exists(http: DataverseHttp, check: ExistsCheck) -> bool:
    try:
        data = await http.get_json(check.path, check.params)
    except DataverseError as exc:
        if exc.status == 404:
            return False
        raise
    if check.collection is None:
        return True
    items = data.get(check.collection)
    return isinstance(items, list) and any(
        isinstance(i, dict) and i.get(check.match_field or "") == check.match_value for i in items
    )


async def apply_plan(
    plan: SchemaPlan, http: DataverseHttp, *, apply: bool, out: Callable[[str], None] = print
) -> list[StepResult]:
    """Dry-run (apply=False) only lists the steps. With apply=True each step is skipped when its target exists."""
    results: list[StepResult] = []
    for step in plan.steps:
        if not apply:
            out(f"[plan]    {step.method} {step.path}  # {step.name}")
            results.append(StepResult(step, "planned"))
            continue
        if step.check is not None and await _exists(http, step.check):
            out(f"[skip]    {step.name} (already exists)")
            results.append(StepResult(step, "skipped", "already exists"))
            continue
        try:
            await http.request(step.method, step.path, body=step.body, headers=step.headers)
        except DataverseError as exc:
            if not step.optional:
                out(f"[FAILED]  {step.name}: {exc}")
                raise
            out(f"[warn]    {step.name} failed (optional): {exc}")
            results.append(StepResult(step, "failed_optional", str(exc)))
            continue
        out(f"[created] {step.name}")
        results.append(StepResult(step, "created"))
    return results
