"""Dataverse Web API (v9.2) CRM adapter.

Writes the same CrmPackage the local simulator takes, but into a Dynamics 365 / Dataverse environment:

  phonecall  subject, description (the note), directioncode, actualstart/end/duration, regarding bound to the
             opportunity (else account, else contact) and activity parties for the seller and the contact
  task       subject, description, scheduledend (noon UTC so the date is stable for user-local columns), priority,
             regarding, owner
  insight    field profile "custom": a row in the vcrm_conversationinsight table (data/crm/field_profile.yaml);
             field profile "standard": no custom columns, so a compact footer is appended to the phone call
             description and the footer fields are reported as `degraded_fields`

Never written: transcripts. `guards.assert_no_transcript` runs first, and the CRM only receives the note, structured
fields and an optional vault pointer.

Idempotency: custom profile -> lookup on the idempotency-key column; standard profile -> `contains(description, ...)`
on the footer marker. If a later step fails, records created so far are deleted again so a retry starts clean.

Authentication: an async azure.identity credential (DefaultAzureCredential by default, no secrets in code) or any
`async () -> str` token provider. HTTP 429/503 are retried honouring Retry-After, with a bounded number of attempts.

NOT yet exercised against a live environment: request shapes follow the Microsoft Learn Web API reference and are unit
tested with mocked HTTP only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date, datetime, time as dt_time, timezone
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx

from ...domain.conversation import ConversationMeta
from ...domain.crm import CrmMatch, CrmPackage, CrmRecordRef, CrmWriteResult, TaskPayload
from ...domain.extraction import NoteSection
from ...domain.templates import SummaryTemplate
from .field_profile import FieldProfile, StandardSpec, build_topic_option_map, load_field_profile
from .guards import TranscriptLeakError, assert_no_transcript, assert_not_msx
from .matching import (
    AccountRow, ContactRow, OpportunityRow, normalize_org, normalize_phone, phone_variants, resolve_match,
)

logger = logging.getLogger(__name__)

TokenProvider = Callable[[], Awaitable[str]]

_BASE_HEADERS = {
    "Accept": "application/json",
    "OData-MaxVersion": "4.0",
    "OData-Version": "4.0",
    "If-None-Match": "null",
}
_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
_ENTITY_ID_RE = re.compile(r"\(([0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})\)\s*$")
_FOOTER_RE = re.compile(r"\[(?P<marker>[\w\-]+) v(?P<version>\d+) key=(?P<key>[^\]]+)\]\s*(?P<json>\{.*\})", re.DOTALL)
_ENTITY_SETS = {"account": "accounts", "contact": "contacts", "opportunity": "opportunities", "lead": "leads"}
_PRIORITY = {"low": 0, "normal": 1, "high": 2}
_PHONECALL_MADE, _PHONECALL_RECEIVED = 2, 4
_COMPLETED = 1
_FOOTER_SEPARATOR = "\n\n---\n"
_FOOTER_FIELDS = (
    ("tpl", "template_id"), ("eng", "engine"), ("model", "model"), ("conf", "confidence"), ("products", "product_ids"),
    ("topics", "topic_ids"), ("rev", "review_status"), ("lang", "language"), ("src", "source_system"),
    ("iid", "source_interaction_id"), ("tp", "transcript_pointer"),
)
_ANNOTATIONS = 'odata.include-annotations="OData.Community.Display.V1.FormattedValue,Microsoft.Dynamics.CRM.lookuplogicalname"'
_FORMATTED = "@OData.Community.Display.V1.FormattedValue"
_LOOKUPLOGICAL = "@Microsoft.Dynamics.CRM.lookuplogicalname"
_ACCOUNT_EXPAND = "parentcustomerid_account($select=accountid,name,websiteurl)"
_PHONE_FIELDS = ("telephone1", "mobilephone", "telephone2")      # business, mobile, home


class DataverseError(RuntimeError):
    """A Dataverse request failed. `status` is 0 for transport errors."""

    def __init__(self, status: int, message: str, *, code: str | None = None) -> None:
        super().__init__(f"Dataverse {status}: {message}" if status else f"Dataverse request failed: {message}")
        self.status = status
        self.code = code
        self.detail = message


class _Credential(Protocol):
    async def get_token(self, *scopes: str, **kwargs: Any) -> Any: ...


class CachedToken:
    """Bearer token from an async credential, cached until shortly before it expires."""

    def __init__(self, credential: _Credential | None, scope: str, clock: Callable[[], float] = time.time) -> None:
        self._credential = credential
        self._scope = scope
        self._clock = clock
        self._token: str | None = None
        self._expires_on = 0.0
        self._owned = credential is None

    async def __call__(self) -> str:
        if self._token and self._expires_on - 300 > self._clock():
            return self._token
        if self._credential is None:
            from azure.identity.aio import DefaultAzureCredential  # imported late: aiohttp is only needed live

            self._credential = DefaultAzureCredential()
        access = await self._credential.get_token(self._scope)
        self._token, self._expires_on = str(access.token), float(access.expires_on)
        return self._token

    def invalidate(self) -> None:
        self._token = None

    async def aclose(self) -> None:
        close = getattr(self._credential, "close", None) if self._owned else None
        if close is not None:
            await close()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _q(value: str) -> str:
    """OData string literal content: single quotes are doubled."""
    return value.replace("'", "''")


def _check_guid(value: str, what: str = "record id") -> str:
    if not _GUID_RE.match(value or ""):
        raise DataverseError(400, f"{what} is not a Dataverse GUID")
    return value


def _utc(value: datetime) -> str:
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _due(value: date) -> str:
    return datetime.combine(value, dt_time(12, 0), tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error_from(resp: httpx.Response) -> DataverseError:
    message, code = resp.reason_phrase or "", None
    try:
        body = resp.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        message, code = str(error.get("message") or message), error.get("code")
    return DataverseError(resp.status_code, _clip(message, 300), code=str(code) if code else None)


def _section_text(section: NoteSection) -> str:
    lines = [section.text.strip()] if section.text and section.text.strip() else []
    lines.extend(f"- {item}" for item in section.items)
    return "\n".join(lines)


# ---- HTTP -------------------------------------------------------------------------------------------------
class DataverseHttp:
    def __init__(
        self, api_url: str, token: TokenProvider, client: httpx.AsyncClient, *, max_retries: int, max_retry_wait: float,
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._api = api_url
        self._token = token
        self.client = client
        self._max_retries = max_retries
        self._max_retry_wait = max_retry_wait
        self._sleep = sleep

    async def request(
        self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None,
        headers: Mapping[str, str] | None = None, expect: Sequence[int] = (200, 201, 204),
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{self._api}{path}"
        attempt, refreshed = 0, False
        while True:
            request_headers = {**_BASE_HEADERS, "Authorization": f"Bearer {await self._token()}", **(headers or {})}
            if body is not None:
                request_headers["Content-Type"] = "application/json; charset=utf-8"
            try:
                resp = await self.client.request(method, url, params=params, json=body, headers=request_headers)
            except httpx.HTTPError as exc:
                raise DataverseError(0, type(exc).__name__) from exc
            if resp.status_code in expect:
                return resp
            invalidate = getattr(self._token, "invalidate", None)
            if resp.status_code == 401 and not refreshed and invalidate is not None:
                invalidate()
                refreshed = True
                continue
            if resp.status_code in (429, 503) and attempt < self._max_retries:
                wait = self._retry_wait(resp, attempt)
                logger.warning("Dataverse %s on %s %s; retrying in %.1fs (%d/%d)", resp.status_code, method,
                               urlsplit(url).path.rsplit("/", 1)[-1], wait, attempt + 1, self._max_retries)
                await self._sleep(wait)
                attempt += 1
                continue
            raise _error_from(resp)

    def _retry_wait(self, resp: httpx.Response, attempt: int) -> float:
        try:
            wait = float(resp.headers.get("Retry-After", ""))
        except ValueError:
            wait = float(min(2 ** attempt, 30))
        return max(0.0, min(wait, self._max_retry_wait))

    async def get_json(
        self, path: str, params: Mapping[str, str] | None = None, headers: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        resp = await self.request("GET", path, params=params, headers=headers, expect=(200,))
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def query(
        self, path: str, params: Mapping[str, str], headers: Mapping[str, str] | None = None
    ) -> list[dict[str, Any]]:
        value = (await self.get_json(path, params, headers)).get("value")
        return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []

    async def create(self, entity_set: str, id_field: str, body: Mapping[str, Any]) -> str:
        resp = await self.request(
            "POST", f"/{entity_set}", params={"$select": id_field}, body=body,
            headers={"Prefer": "return=representation"},
        )
        if resp.content:
            try:
                data = resp.json()
            except ValueError:
                data = None
            if isinstance(data, dict) and data.get(id_field):
                return str(data[id_field])
        match = _ENTITY_ID_RE.search(resp.headers.get("OData-EntityId", ""))
        if not match:
            raise DataverseError(resp.status_code, "response did not identify the created record")
        return match.group(1)


# ---- matching lookup ----------------------------------------------------------------------------------------
class _DataverseLookup:
    """CrmLookup over the Dataverse Web API (one instance per match() call; caches the accounts it has seen).

    `phone_hints` are the callers' numbers as written in the conversation: Dataverse phone columns are free text and
    can only be filtered on exact strings, so the lookup tries those spellings first, then the common ones.
    """

    def __init__(self, http: DataverseHttp, phone_hints: Sequence[str] = ()) -> None:
        self._http = http
        self._phone_hints = list(phone_hints)
        self._accounts: dict[str, AccountRow] = {}

    def _remember(self, row: dict[str, Any] | None) -> AccountRow | None:
        if not row or not row.get("accountid"):
            return None
        account = AccountRow(str(row["accountid"]), str(row.get("name") or ""), row.get("websiteurl"))
        self._accounts[account.id] = account
        return account

    def _contact(self, row: dict[str, Any]) -> ContactRow:
        account = self._remember(row.get("parentcustomerid_account"))
        phones = tuple(dict.fromkeys(n for n in (normalize_phone(row.get(f)) for f in _PHONE_FIELDS) if n))
        return ContactRow(
            str(row["contactid"]), str(row.get("fullname") or ""), row.get("emailaddress1"),
            account.id if account else None, account.name if account else None, phones,
        )

    async def contacts_by_email(self, emails: Sequence[str]) -> list[ContactRow]:
        if not emails:
            return []
        params = {
            "$select": "contactid,fullname,emailaddress1,_parentcustomerid_value",
            "$expand": _ACCOUNT_EXPAND,
            "$filter": " or ".join(f"emailaddress1 eq '{_q(e)}'" for e in emails),
            "$top": "50",
        }
        return [self._contact(r) for r in await self._http.query("/contacts", params)]

    async def contacts_by_phone(self, phones: Sequence[str]) -> list[ContactRow]:
        spellings = list(dict.fromkeys(
            spelling
            for number in phones
            for spelling in phone_variants(number, [h for h in self._phone_hints if normalize_phone(h) == number])
        ))
        if not spellings:
            return []
        clauses = [f"{field} eq '{_q(spelling)}'" for field in _PHONE_FIELDS for spelling in spellings]
        params = {
            "$select": f"contactid,fullname,emailaddress1,{','.join(_PHONE_FIELDS)},_parentcustomerid_value",
            "$expand": _ACCOUNT_EXPAND,
            "$filter": f"({' or '.join(clauses)}) and statecode eq 0",
            "$top": "50",
        }
        return [self._contact(r) for r in await self._http.query("/contacts", params)]

    async def candidate_accounts(self, names: Sequence[str], domains: Sequence[str]) -> list[AccountRow]:
        clauses = [f"name eq '{_q(n)}'" for n in names]
        clauses += [f"contains(websiteurl,'{_q(d)}')" for d in domains]
        for name in names:
            tokens = [t for t in normalize_org(name).split() if len(t) >= 4]
            if tokens:
                clauses.append(f"contains(name,'{_q(max(tokens, key=len))}')")
        if not clauses:
            return []
        params = {"$select": "accountid,name,websiteurl", "$filter": " or ".join(dict.fromkeys(clauses)), "$top": "25"}
        rows = await self._http.query("/accounts", params)
        return [a for a in (self._remember(r) for r in rows) if a]

    async def candidate_contacts(self, names: Sequence[str]) -> list[ContactRow]:
        clauses = []
        for name in names:
            tokens = [t for t in name.split() if len(t) >= 3]
            if tokens:
                clauses.append(f"contains(fullname,'{_q(max(tokens, key=len))}')")
        if not clauses:
            return []
        params = {
            "$select": "contactid,fullname,emailaddress1,_parentcustomerid_value", "$expand": _ACCOUNT_EXPAND,
            "$filter": " or ".join(dict.fromkeys(clauses)), "$top": "25",
        }
        return [self._contact(r) for r in await self._http.query("/contacts", params)]

    async def contacts_of_account(self, account_id: str) -> list[ContactRow]:
        params = {
            "$select": "contactid,fullname,emailaddress1",
            "$filter": f"_parentcustomerid_value eq {_check_guid(account_id)}", "$top": "100",
        }
        account = self._accounts.get(account_id)
        return [
            ContactRow(str(r["contactid"]), str(r.get("fullname") or ""), r.get("emailaddress1"), account_id,
                       account.name if account else None)
            for r in await self._http.query("/contacts", params)
        ]

    async def account_by_id(self, account_id: str) -> AccountRow | None:
        if account_id in self._accounts:
            return self._accounts[account_id]
        try:
            row = await self._http.get_json(
                f"/accounts({_check_guid(account_id)})", {"$select": "accountid,name,websiteurl"}
            )
        except DataverseError as exc:
            if exc.status == 404:
                return None
            raise
        return self._remember(row)

    async def open_opportunities(self, account_id: str) -> list[OpportunityRow]:
        guid = _check_guid(account_id)
        params = {
            "$select": "opportunityid,name,_parentaccountid_value,_parentcontactid_value,statecode",
            "$filter": f"(_parentaccountid_value eq {guid} or _customerid_value eq {guid}) and statecode eq 0",
            "$orderby": "modifiedon desc", "$top": "25",
        }
        try:
            rows = await self._http.query("/opportunities", params)
        except DataverseError as exc:
            if exc.status in (400, 404):      # environment without Dynamics 365 Sales: no opportunity table
                logger.info("Dataverse has no usable opportunity table (HTTP %s); skipping opportunities", exc.status)
                return []
            raise
        return [
            OpportunityRow(str(r["opportunityid"]), str(r.get("name") or ""), r.get("_parentaccountid_value"),
                           r.get("_parentcontactid_value"), "Open")
            for r in rows
        ]


# ---- adapter ---------------------------------------------------------------------------------------------------
class DataverseCrm:
    """CrmPort over the Dataverse Web API. Constructor arguments make every collaborator injectable for tests."""

    name = "dataverse"

    def __init__(
        self,
        base_url: str,
        *,
        token_provider: TokenProvider | None = None,
        credential: _Credential | None = None,
        client: httpx.AsyncClient | None = None,
        field_profile: Literal["standard", "custom"] = "standard",
        profile: FieldProfile | None = None,
        profile_path: Any = None,
        templates: Mapping[str, SummaryTemplate] | None = None,
        topic_ids: Sequence[str] | None = None,
        topic_option_values: Mapping[str, int] | None = None,
        api_version: str = "v9.2",
        max_retries: int = 4,
        max_retry_wait: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        complete_phonecalls: bool = True,
        timeout: float = 30.0,
    ) -> None:
        parts = urlsplit(base_url.strip())
        if not parts.scheme or not parts.netloc:
            raise ValueError("base_url must look like https://<org>.crm.dynamics.com")
        assert_not_msx(base_url)
        self._org = f"{parts.scheme}://{parts.netloc}"
        self._api_version = api_version
        self._profile_name = field_profile
        self._complete_phonecalls = complete_phonecalls

        self._profile = profile or self._load_profile(field_profile, profile_path)
        self._standard: StandardSpec = self._profile.standard if self._profile else StandardSpec()
        self._topic_options: dict[str, int] = {}
        self._section_fields: dict[str, dict[str, str]] = {}
        if field_profile == "custom":
            if self._profile is None:
                raise ValueError("field_profile='custom' needs data/crm/field_profile.yaml (or an explicit profile)")
            self._topic_options = dict(topic_option_values) if topic_option_values is not None else build_topic_option_map(
                self._taxonomy_topic_ids(topic_ids), base=self._profile.custom.topic_options.base,
                overrides=self._profile.custom.topic_options.overrides,
            )
            self._section_fields = self._section_field_map(templates)

        self._token: TokenProvider = token_provider or CachedToken(credential, f"{self._org}/.default")
        self._owns_client = client is None
        self._http = DataverseHttp(
            f"{self._org}/api/data/{api_version}", self._token, client or httpx.AsyncClient(timeout=timeout),
            max_retries=max_retries, max_retry_wait=max_retry_wait, sleep=sleep,
        )
        self._user_ids: dict[str, str | None] = {}
        self._me: str | None = None

    # ---- setup helpers ---------------------------------------------------------------------------------------
    @staticmethod
    def _load_profile(field_profile: str, path: Any) -> FieldProfile | None:
        try:
            return load_field_profile(path)
        except FileNotFoundError:
            if field_profile == "custom":
                raise
            return None

    @staticmethod
    def _taxonomy_topic_ids(topic_ids: Sequence[str] | None) -> Sequence[str]:
        if topic_ids is not None:
            return topic_ids
        try:
            from ...data_loader import load_taxonomy

            return [t.topic_id for t in load_taxonomy().topics]
        except Exception as exc:  # without a taxonomy no topic can be mapped to a choice value
            logger.warning("Could not load the topic taxonomy (%s); topics cannot be written to the choice column",
                           type(exc).__name__)
            return []

    def _section_field_map(self, templates: Mapping[str, SummaryTemplate] | None) -> dict[str, dict[str, str]]:
        if templates is None:
            try:
                from ...data_loader import load_templates

                templates = load_templates()
            except Exception as exc:  # templates are optional; sections still go to the JSON column
                logger.warning(
                    "Could not load summary templates (%s); template sections stay in the JSON column",
                    type(exc).__name__,
                )
                templates = {}
        return {
            tid: {s.key: s.crm_field for s in tpl.sections if s.crm_field} for tid, tpl in templates.items()
        }

    @property
    def _custom(self) -> bool:
        return self._profile_name == "custom" and self._profile is not None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.client.aclose()
        closer = getattr(self._token, "aclose", None)
        if closer is not None:
            await closer()

    # ---- CrmPort ---------------------------------------------------------------------------------------------
    async def match(self, meta: ConversationMeta) -> CrmMatch:
        phone_hints = [p.phone for p in meta.participants if p.phone]
        return await resolve_match(_DataverseLookup(self._http, phone_hints), meta)

    async def write(self, package: CrmPackage) -> CrmWriteResult:
        try:
            assert_no_transcript(package)
        except TranscriptLeakError as exc:
            return CrmWriteResult(ok=False, error=f"rejected: {exc}")
        key = package.idempotency_key.strip()
        if not key:
            return CrmWriteResult(ok=False, error="rejected: idempotency_key is required")

        created: list[tuple[str, CrmRecordRef]] = []        # (entity set, ref) for rollback
        try:
            exists, custom_available = await self._find_existing(key)
            if exists:
                logger.info("Dataverse write skipped (duplicate idempotency key) for conversation %s", package.conversation_id)
                return CrmWriteResult(ok=True, skipped_duplicate=True)
            degraded: list[str] = []
            use_custom = self._custom and custom_available
            if self._custom and not custom_available:
                degraded.append("custom_table_unavailable")
            await self._write_records(package, key, use_custom, created, degraded)
        except (DataverseError, ValueError) as exc:
            remaining = await self._rollback(created)
            logger.warning("Dataverse write failed for conversation %s: %s", package.conversation_id, exc)
            suffix = f" (rollback incomplete: {len(remaining)} record(s) remain)" if remaining else ""
            return CrmWriteResult(ok=False, created=remaining, error=f"{exc}{suffix}")
        except Exception as exc:  # never leave half a package behind, whatever went wrong
            remaining = await self._rollback(created)
            logger.exception("Unexpected error writing conversation %s to Dataverse", package.conversation_id)
            suffix = f" (rollback incomplete: {len(remaining)} record(s) remain)" if remaining else ""
            return CrmWriteResult(ok=False, created=remaining, error=f"unexpected {type(exc).__name__}{suffix}")
        logger.info("Dataverse write created %d records for conversation %s", len(created), package.conversation_id)
        return CrmWriteResult(ok=True, created=[ref for _, ref in created], degraded_fields=degraded)

    async def list_records(self, entity: str, limit: int = 100) -> list[dict[str, Any]]:
        top = str(min(max(1, int(limit)), 500))
        handlers = {
            "phonecall": self._list_phonecalls, "task": self._list_tasks, "insight": self._list_insights,
            "account": self._list_accounts, "contact": self._list_contacts, "opportunity": self._list_opportunities,
            "systemuser": self._list_users,
        }
        if entity not in handlers:
            raise ValueError(f"unknown entity {entity!r}; expected one of {', '.join(handlers)}")
        return await handlers[entity](top)

    async def health(self) -> dict[str, Any]:
        base = {"adapter": self.name, "url": self._org, "api_version": self._api_version, "field_profile": self._profile_name}
        try:
            who = await self._http.get_json("/WhoAmI")
        except DataverseError as exc:
            return {**base, "ok": False, "error": str(exc)}
        return {
            **base, "ok": True, "user_id": who.get("UserId"), "organization_id": who.get("OrganizationId"),
            "business_unit_id": who.get("BusinessUnitId"),
        }

    # ---- idempotency -----------------------------------------------------------------------------------------
    async def _find_existing(self, key: str) -> tuple[bool, bool]:
        """(already written, custom table usable). A missing custom table degrades to the standard footer."""
        if self._custom:
            table = self._profile.custom.table                                    # type: ignore[union-attr]
            column = self._profile.custom.column("idempotency_key")               # type: ignore[union-attr]
            try:
                rows = await self._http.query(
                    f"/{table.entity_set}",
                    {"$select": f"{table.logical_name}id", "$filter": f"{column.logical_name} eq '{_q(key)}'", "$top": "1"},
                )
                return bool(rows), True
            except DataverseError as exc:
                if exc.status not in (400, 404):
                    raise
                logger.warning("Dataverse custom table %s is not available (HTTP %s); using the standard profile",
                               table.logical_name, exc.status)
        marker = f"{self._standard.footer_marker} v{self._standard.footer_version} key={key}]"
        rows = await self._http.query(
            "/phonecalls", {"$select": "activityid", "$filter": f"contains(description,'{_q(marker)}')", "$top": "1"}
        )
        return bool(rows), False

    # ---- write -----------------------------------------------------------------------------------------------
    async def _write_records(
        self, package: CrmPackage, key: str, use_custom: bool, created: list[tuple[str, CrmRecordRef]],
        degraded: list[str],
    ) -> None:
        call = package.phone_call
        targets = self._targets(package)
        owner_id = await self._systemuser_id(call.owner_email)

        description, footer_fields = self._description(package, key, use_custom, degraded)
        body: dict[str, Any] = {
            "subject": _clip(call.subject, self._standard.subject_max_chars),
            "description": description,
            "directioncode": call.direction == "outbound",
            "actualstart": _utc(call.start),
        }
        if call.end is not None:
            body["actualend"] = _utc(call.end)
        duration = call.duration_minutes
        if duration is None and call.end is not None:
            duration = max(0, round((call.end - call.start).total_seconds() / 60))
        if duration is not None:
            body["actualdurationminutes"] = duration
        regarding = self._regarding_bind(targets["regarding"], "phonecall")
        if regarding:
            body.update(regarding)
        elif targets["regarding"] is not None:
            degraded.append("regarding")
        if owner_id:
            body["ownerid@odata.bind"] = f"/systemusers({owner_id})"
        parties = self._parties(call.direction, owner_id, targets["contact"])
        if parties:
            body["phonecall_activity_parties"] = parties

        call_id = await self._http.create("phonecalls", "activityid", body)
        created.append(("phonecalls", CrmRecordRef(entity="phonecall", id=call_id, name=call.subject)))
        if not use_custom:
            degraded.extend(footer_fields)

        if self._complete_phonecalls:
            status_reason = _PHONECALL_MADE if call.direction == "outbound" else _PHONECALL_RECEIVED
            try:
                await self._http.request(
                    "PATCH", f"/phonecalls({call_id})",
                    body={"statecode": _COMPLETED, "statuscode": status_reason},
                    headers={"If-Match": "*"}, expect=(200, 204),
                )
            except DataverseError as exc:
                logger.warning("Could not mark phone call completed (HTTP %s); left open", exc.status)
                degraded.append("phonecall_status")

        for task in package.tasks:
            task_body = await self._task_body(task, call.owner_email, targets["regarding"])
            task_id = await self._http.create("tasks", "activityid", task_body)
            created.append(("tasks", CrmRecordRef(entity="task", id=task_id, name=task.subject)))

        if use_custom:
            insight_id = await self._create_insight(package, key, call_id, targets, degraded)
            created.append((self._profile.custom.table.entity_set,                  # type: ignore[union-attr]
                            CrmRecordRef(entity="insight", id=insight_id, name=call.subject)))

    def _targets(self, package: CrmPackage) -> dict[str, CrmRecordRef | None]:
        match, call = package.regarding, package.phone_call
        regarding = call.regarding or match.opportunity or match.account or match.contact
        return {"regarding": regarding, "contact": call.to_contact or match.contact}

    def _regarding_bind(self, ref: CrmRecordRef | None, activity: str) -> dict[str, str]:
        if ref is None:
            return {}
        entity_set = _ENTITY_SETS.get(ref.entity)
        if entity_set is None:
            logger.warning("Cannot bind a %s as the regarding record of a %s", ref.entity, activity)
            return {}
        return {f"regardingobjectid_{ref.entity}_{activity}@odata.bind": f"/{entity_set}({_check_guid(ref.id)})"}

    def _parties(self, direction: str, seller_id: str | None, contact: CrmRecordRef | None) -> list[dict[str, Any]]:
        seller = {"partyid_systemuser@odata.bind": f"/systemusers({seller_id})"} if seller_id else None
        customer = {"partyid_contact@odata.bind": f"/contacts({_check_guid(contact.id)})"} if contact else None
        sender, recipient = (seller, customer) if direction == "outbound" else (customer, seller)
        parties = []
        if sender:
            parties.append({**sender, "participationtypemask": 1})
        if recipient:
            parties.append({**recipient, "participationtypemask": 2})
        return parties

    async def _task_body(self, task: TaskPayload, call_owner: str | None, fallback: CrmRecordRef | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "subject": _clip(task.subject, self._standard.subject_max_chars),
            "prioritycode": _PRIORITY[task.priority],
        }
        if task.description:
            body["description"] = _clip(task.description, self._standard.task_description_max_chars)
        if task.due:
            body["scheduledend"] = _due(task.due)
        body.update(self._regarding_bind(task.regarding or fallback, "task"))
        owner_id = await self._systemuser_id(task.owner_email or call_owner)
        if owner_id:
            body["ownerid@odata.bind"] = f"/systemusers({owner_id})"
        return body

    async def _systemuser_id(self, email: str | None) -> str | None:
        if not email:
            return None
        key = email.strip().lower()
        if key not in self._user_ids:
            rows = await self._http.query(
                "/systemusers",
                {"$select": "systemuserid", "$filter": f"internalemailaddress eq '{_q(email.strip())}'", "$top": "1"},
            )
            self._user_ids[key] = str(rows[0]["systemuserid"]) if rows else None
            if not rows:
                logger.warning("No Dataverse user matches an owner email; the record stays with the calling identity")
        return self._user_ids[key]

    # ---- description / footer --------------------------------------------------------------------------------
    def _description(self, package: CrmPackage, key: str, use_custom: bool, degraded: list[str]) -> tuple[str, list[str]]:
        note = package.phone_call.description.strip()
        limit = self._standard.description_max_chars
        if use_custom:
            if len(note) > limit:
                degraded.append("description_truncated")
            return _clip(note, limit), []
        footer, fields = self._footer(package, key)
        room = limit - len(_FOOTER_SEPARATOR) - len(footer)
        if room < 0:
            raise ValueError("insight footer exceeds the phone call description limit; use the custom field profile")
        if len(note) > room:
            degraded.append("description_truncated")
            note = note[: max(room - 1, 0)].rstrip() + "…" if room > 0 else ""
        return f"{note}{_FOOTER_SEPARATOR}{footer}", ["idempotency_key", *fields]

    def _footer(self, package: CrmPackage, key: str) -> tuple[str, list[str]]:
        insight = package.insight
        values: dict[str, Any] = {
            "tpl": insight.template_id, "eng": insight.engine, "model": insight.model,
            "conf": round(insight.confidence, 4), "products": insight.product_ids, "topics": insight.topic_ids,
            "rev": insight.review_status, "lang": insight.language, "src": insight.source.value,
            "iid": insight.source_interaction_id, "tp": insight.transcript_pointer,
        }
        present = {k: v for k, v in values.items() if v not in (None, "", [])}
        fields = [name for short, name in _FOOTER_FIELDS if short in present]
        header = f"[{self._standard.footer_marker} v{self._standard.footer_version} key={key}]"
        return f"{header}\n{json.dumps(present, ensure_ascii=False, separators=(',', ':'))}", fields

    # ---- custom insight --------------------------------------------------------------------------------------
    async def _create_insight(
        self, package: CrmPackage, key: str, call_id: str, targets: dict[str, CrmRecordRef | None], degraded: list[str]
    ) -> str:
        spec = self._profile.custom                                              # type: ignore[union-attr]
        insight, call = package.insight, package.phone_call
        sections_json = json.dumps([s.model_dump(mode="json") for s in insight.sections], ensure_ascii=False)
        unknown_topics = [t for t in insight.topic_ids if t not in self._topic_options]
        topic_values = sorted({self._topic_options[t] for t in insight.topic_ids if t in self._topic_options})
        if unknown_topics:
            logger.warning(
                "%d topic id(s) have no choice value in the field profile; they are not written", len(unknown_topics)
            )
            degraded.append("topic_ids")
        facts_json = json.dumps(insight.relationship_facts, ensure_ascii=False) if insight.relationship_facts else None
        values: dict[str, Any] = {
            "name": call.subject, "summary": insight.summary_markdown, "sections": sections_json,
            "relationship_facts": facts_json,
            "product_ids": ",".join(insight.product_ids), "topic_ids": ", ".join(str(v) for v in topic_values) or None,
            "template_id": insight.template_id, "engine": insight.engine, "model": insight.model,
            "confidence": round(insight.confidence, 4), "review_status": insight.review_status,
            "language": insight.language, "source_system": insight.source.value,
            "source_interaction_id": insight.source_interaction_id, "idempotency_key": key,
            "conversation_id": package.conversation_id, "transcript_pointer": insight.transcript_pointer,
        }
        body: dict[str, Any] = {}
        for column in spec.columns:
            value = values.get(column.key)
            if value in (None, ""):
                continue
            if column.type == "choice":
                value = column.options.get(str(value))
                if value is None:
                    degraded.append(column.key)
                    continue
            elif column.type in ("string", "memo") and column.max_length and len(str(value)) > column.max_length:
                if column.key == "sections":
                    degraded.append("sections")
                    continue
                degraded.append(f"{column.key}_truncated")
                value = _clip(str(value), column.max_length)
            body[column.logical_name] = value
        self._add_section_columns(body, insight.template_id, insight.sections, degraded)

        refs = {
            "account": package.regarding.account, "contact": targets["contact"],
            "opportunity": package.regarding.opportunity, "phonecall": CrmRecordRef(entity="phonecall", id=call_id),
        }
        explicit = call.regarding
        if explicit is not None and explicit.entity in ("account", "contact", "opportunity"):
            refs[explicit.entity] = explicit
        for lookup in spec.lookups:
            ref = refs.get(lookup.key)
            if ref is not None:
                body[f"{lookup.schema_name}@odata.bind"] = f"/{lookup.entity_set}({_check_guid(ref.id)})"
        return await self._http.create(spec.table.entity_set, f"{spec.table.logical_name}id", body)

    def _add_section_columns(
        self, body: dict[str, Any], template_id: str, sections: Sequence[NoteSection], degraded: list[str]
    ) -> None:
        spec = self._profile.custom                                              # type: ignore[union-attr]
        mapping = self._section_fields.get(template_id, {})
        collected: dict[str, list[str]] = {}
        for section in sections:
            field = mapping.get(section.key)
            text = _section_text(section)
            if field and text and spec.section_column(field) is not None:
                collected.setdefault(field, []).append(text)
        for field, parts in collected.items():
            column = spec.section_column(field)
            text = "\n\n".join(parts)
            if column is not None and column.max_length and len(text) > column.max_length:
                degraded.append(f"{field}_truncated")
                text = _clip(text, column.max_length)
            body[field] = text

    # ---- rollback --------------------------------------------------------------------------------------------
    async def _rollback(self, created: list[tuple[str, CrmRecordRef]]) -> list[CrmRecordRef]:
        remaining: list[CrmRecordRef] = []
        for entity_set, ref in reversed(created):
            try:
                await self._http.request("DELETE", f"/{entity_set}({ref.id})", expect=(200, 204, 404))
            except DataverseError as exc:
                logger.error("Rollback could not delete %s %s (HTTP %s)", ref.entity, ref.id, exc.status)
                remaining.append(ref)
        return remaining

    # ---- list_records ----------------------------------------------------------------------------------------
    async def _whoami(self) -> str:
        if self._me is None:
            self._me = str((await self._http.get_json("/WhoAmI")).get("UserId") or "")
        return self._me

    @staticmethod
    def _formatted(row: Mapping[str, Any], field: str) -> Any:
        return row.get(f"{field}{_FORMATTED}")

    def _activity_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        regarding = "_regardingobjectid_value"
        owner = "_ownerid_value"
        return {
            "id": row.get("activityid"), "subject": row.get("subject"), "description": row.get("description"),
            "regarding_entity": row.get(f"{regarding}{_LOOKUPLOGICAL}"), "regarding_id": row.get(regarding),
            "regarding_name": self._formatted(row, regarding), "owner_id": row.get(owner),
            "owner_name": self._formatted(row, owner), "created_at": row.get("createdon"),
        }

    async def _created_by_me(self, entity_set: str, select: str, top: str) -> list[dict[str, Any]]:
        params = {
            "$select": select, "$filter": f"_createdby_value eq {_check_guid(await self._whoami())}",
            "$orderby": "createdon desc", "$top": top,
        }
        return await self._http.query(f"/{entity_set}", params, {"Prefer": _ANNOTATIONS})

    async def _list_phonecalls(self, top: str) -> list[dict[str, Any]]:
        select = ("activityid,subject,description,directioncode,actualstart,actualend,actualdurationminutes,createdon,"
                  "_regardingobjectid_value,_ownerid_value")
        out = []
        for row in await self._created_by_me("phonecalls", select, top):
            item = self._activity_row(row)
            item.update({
                "direction": "outbound" if row.get("directioncode") else "inbound", "start": row.get("actualstart"),
                "end": row.get("actualend"), "duration_minutes": row.get("actualdurationminutes"),
            })
            out.append(item)
        return out

    async def _list_tasks(self, top: str) -> list[dict[str, Any]]:
        select = "activityid,subject,description,scheduledend,prioritycode,createdon,_regardingobjectid_value,_ownerid_value"
        out = []
        for row in await self._created_by_me("tasks", select, top):
            item = self._activity_row(row)
            due = row.get("scheduledend")
            item.update({"due": due[:10] if isinstance(due, str) else None,
                         "priority": {v: k for k, v in _PRIORITY.items()}.get(row.get("prioritycode"), "normal")})
            out.append(item)
        return out

    async def _list_insights(self, top: str) -> list[dict[str, Any]]:
        if self._custom:
            spec = self._profile.custom                                          # type: ignore[union-attr]
            columns = [c.logical_name for c in spec.columns]
            params = {"$select": ",".join([f"{spec.table.logical_name}id", "createdon", *columns]),
                      "$orderby": "createdon desc", "$top": top}
            try:
                rows = await self._http.query(f"/{spec.table.entity_set}", params)
            except DataverseError as exc:
                if exc.status not in (400, 404):
                    raise
            else:
                by_value = {v: k for k, v in self._topic_options.items()}
                topic_column = spec.column("topic_ids")
                out = []
                for row in rows:
                    item: dict[str, Any] = {"id": row.get(f"{spec.table.logical_name}id"), "created_at": row.get("createdon")}
                    for column in spec.columns:
                        item[column.key] = row.get(column.logical_name)
                    raw_topics = str(row.get(topic_column.logical_name) or "") if topic_column else ""
                    item["topic_ids"] = [
                        by_value.get(int(v), v.strip()) if v.strip().isdigit() else v.strip()
                        for v in raw_topics.split(",") if v.strip()
                    ]
                    item["product_ids"] = [p for p in str(item.get("product_ids") or "").split(",") if p]
                    out.append(item)
                return out
        marker = f"[{self._standard.footer_marker} v"
        rows = await self._http.query("/phonecalls", {
            "$select": "activityid,subject,description,createdon", "$filter": f"contains(description,'{_q(marker)}')",
            "$orderby": "createdon desc", "$top": top,
        })
        out = []
        for row in rows:
            footer = _FOOTER_RE.search(row.get("description") or "")
            if not footer:
                continue
            try:
                data = json.loads(footer.group("json"))
            except ValueError:
                continue
            out.append({
                "id": row.get("activityid"), "phonecall_id": row.get("activityid"), "subject": row.get("subject"),
                "idempotency_key": footer.group("key"), "created_at": row.get("createdon"),
                "template_id": data.get("tpl"), "engine": data.get("eng"), "model": data.get("model"),
                "confidence": data.get("conf"), "product_ids": data.get("products", []),
                "topic_ids": data.get("topics", []), "review_status": data.get("rev"), "language": data.get("lang"),
                "source": data.get("src"), "source_interaction_id": data.get("iid"), "transcript_pointer": data.get("tp"),
            })
        return out

    async def _list_accounts(self, top: str) -> list[dict[str, Any]]:
        rows = await self._http.query(
            "/accounts", {"$select": "accountid,name,websiteurl", "$orderby": "modifiedon desc", "$top": top}
        )
        return [{"id": r.get("accountid"), "name": r.get("name"), "domain": r.get("websiteurl")} for r in rows]

    async def _list_contacts(self, top: str) -> list[dict[str, Any]]:
        params = {
            "$select": "contactid,fullname,emailaddress1,telephone1,jobtitle,_parentcustomerid_value",
            "$orderby": "modifiedon desc", "$top": top,
        }
        rows = await self._http.query("/contacts", params, {"Prefer": _ANNOTATIONS})
        return [
            {"id": r.get("contactid"), "name": r.get("fullname"), "email": r.get("emailaddress1"),
             "phone": r.get("telephone1"), "title": r.get("jobtitle"), "account_id": r.get("_parentcustomerid_value"),
             "account_name": self._formatted(r, "_parentcustomerid_value")}
            for r in rows
        ]

    async def _list_opportunities(self, top: str) -> list[dict[str, Any]]:
        params = {
            "$select": "opportunityid,name,statecode,_parentaccountid_value,_parentcontactid_value",
            "$orderby": "modifiedon desc", "$top": top,
        }
        rows = await self._http.query("/opportunities", params, {"Prefer": _ANNOTATIONS})
        states = {0: "Open", 1: "Won", 2: "Lost"}
        return [
            {"id": r.get("opportunityid"), "name": r.get("name"), "stage": states.get(r.get("statecode"), "Open"),
             "account_id": r.get("_parentaccountid_value"), "account_name": self._formatted(r, "_parentaccountid_value"),
             "contact_id": r.get("_parentcontactid_value"), "contact_name": self._formatted(r, "_parentcontactid_value")}
            for r in rows
        ]

    async def _list_users(self, top: str) -> list[dict[str, Any]]:
        rows = await self._http.query("/systemusers", {"$select": "systemuserid,fullname,internalemailaddress", "$top": top})
        return [{"id": r.get("systemuserid"), "name": r.get("fullname"), "email": r.get("internalemailaddress")} for r in rows]


__all__ = ["DataverseCrm", "DataverseError"]
