"""CRM matching rules shared by the SQLite simulator and the Dataverse adapter.

One orchestrator (`resolve_match`) applies the rules; each adapter only supplies data access through `CrmLookup`,
so the simulator and a live Dataverse environment behave the same way.

Rules, in order (the policy is "never guess"):
  1. A participant's email equals a contact's email  -> contact, its account (method "email").
  2. A participant's phone number (Genesys ANI, mobile capture) equals a contact's phone, compared on normalised
     digits -> contact, its account (method "phone"). Never overrides rule 1. The participant's name and
     organisation reconcile the hit: they single out one of several contacts that share the number and raise the
     confidence when they agree; a hint that contradicts the number withholds the pick (ambiguous=True).
  3. Participant email domain or organisation name equals an account's domain / name, or is a close fuzzy
     match (method "name_org"). Two accounts with the same evidence -> ambiguous, no account chosen.
  4. Participant name + organisation fuzzy-match a contact (method "name_org").
  5. With an account known: exactly one open opportunity -> chosen; several -> ambiguous=True with the
     opportunities as candidates (none chosen); none -> account/contact only.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from rapidfuzz import fuzz

from ...domain.conversation import ConversationMeta, Participant
from ...domain.crm import CrmMatch, CrmRecordRef
from ...domain.enums import SpeakerRole

logger = logging.getLogger(__name__)

CONF_EMAIL = 0.98
CONF_PHONE_CONFIRMED = 0.96       # one contact owns the number and the participant's name / organisation agree
CONF_PHONE = 0.92                 # one contact owns the number, no name / organisation to check it against
CONF_PHONE_SHARED = 0.88          # several contacts share the number; name / organisation singled one out
CONF_PHONE_ACCOUNT = 0.85         # the number gives the account (organisation agrees) but not the person
CONF_PHONE_CONFLICT = 0.65        # the number and the name / organisation disagree: nothing is chosen
CONF_DOMAIN = 0.88
CONF_DOMAIN_AND_NAME = 0.93
CONF_ORG_EXACT = 0.85
CONF_FUZZY_FLOOR = 0.60
CONF_FUZZY_CEIL = 0.85
CONF_NAME_ORG_CEIL = 0.80
CONF_NAME_ONLY_CEIL = 0.70
CONTACT_BY_NAME_BONUS = 0.03
OPPORTUNITY_BONUS = 0.02
AMBIGUITY_FACTOR = 0.6

ORG_FUZZY_MIN = 90.0              # token_sort_ratio on normalised organisation names
NAME_FUZZY_MIN = 90.0             # contact name inside an already known account
NAME_ORG_NAME_MIN = 88.0          # contact name when the organisation is also given
NAME_ONLY_MIN = 95.0              # contact name when nothing else is known
NAME_ORG_ORG_MIN = 60.0
AMBIGUITY_MARGIN = 4.0            # best fuzzy score must beat the runner-up by this much
HINT_AGREE_MIN = 90.0             # name / organisation hint agrees with the contact the phone number points to
HINT_CONFLICT_MAX = 60.0          # ... and below this it contradicts it (in between: inconclusive)

PHONE_MIN_DIGITS = 7              # shorter strings (extensions, "anonymous", ids) are not phone numbers
PHONE_MAX_DIGITS = 15             # E.164 maximum

PUBLIC_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com", "icloud.com",
    "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "mail.com",
})
_LEGAL_SUFFIXES = frozenset({
    "inc", "incorporated", "llc", "ltd", "limited", "plc", "corp", "corporation", "co", "company", "sa", "ag",
    "gmbh", "nv", "bv", "srl", "spa", "sas", "sarl", "lp", "llp", "pte", "pty", "ab", "oy",
})
_GENERIC_NAMES = frozenset({"unknown", "guest", "caller", "customer", "participant", "attendee", "speaker", "anonymous"})
_CLOSED_STAGE_RE = re.compile(r"^(closed|won|lost)\b", re.IGNORECASE)
_PHONE_URI_RE = re.compile(r"^\s*(?:tel|sips?|callto):", re.IGNORECASE)
_PHONE_EXTENSION_RE = re.compile(r"(?<![A-Za-z])(?:ext\.?|extension|x|#)\s*\d{1,8}\s*$", re.IGNORECASE)
_PHONE_TRUNK_ZERO_RE = re.compile(r"\(\s*0\s*\)")
_PHONE_TEXT_RE = re.compile(r"^[0-9+()\-.\s]{7,50}$")


@dataclass(frozen=True, slots=True)
class AccountRow:
    id: str
    name: str
    domain: str | None = None


@dataclass(frozen=True, slots=True)
class ContactRow:
    id: str
    name: str
    email: str | None = None
    account_id: str | None = None
    account_name: str | None = None
    phones: tuple[str, ...] = ()      # the contact's numbers, normalised; filled in by `contacts_by_phone` only


@dataclass(frozen=True, slots=True)
class OpportunityRow:
    id: str
    name: str
    account_id: str | None = None
    contact_id: str | None = None
    stage: str | None = None


class CrmLookup(Protocol):
    """Data access an adapter provides. Candidate methods may return a superset; scoring happens here."""

    async def contacts_by_email(self, emails: Sequence[str]) -> list[ContactRow]: ...

    async def contacts_by_phone(self, phones: Sequence[str]) -> list[ContactRow]:
        """Contacts holding any of these numbers (already `normalize_phone`d), with `ContactRow.phones` filled in."""
        ...

    async def candidate_accounts(self, names: Sequence[str], domains: Sequence[str]) -> list[AccountRow]: ...

    async def candidate_contacts(self, names: Sequence[str]) -> list[ContactRow]: ...

    async def contacts_of_account(self, account_id: str) -> list[ContactRow]: ...

    async def account_by_id(self, account_id: str) -> AccountRow | None: ...

    async def open_opportunities(self, account_id: str) -> list[OpportunityRow]: ...


# ---- normalisation ---------------------------------------------------------------------------------------
def normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    email = value.strip().lower()
    return email if "@" in email and " " not in email else None


def email_domain(email: str | None) -> str | None:
    normalized = normalize_email(email)
    return normalized.rsplit("@", 1)[1] if normalized else None


def normalize_domain(value: str | None) -> str | None:
    if not value:
        return None
    domain = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", value.strip().lower())
    domain = domain.split("/", 1)[0].split("?", 1)[0].split(":", 1)[0]
    domain = domain.removeprefix("www.")
    return domain or None


def _strip_phone_uri(value: str) -> str:
    """`tel:+12125550171;ext=4` or `sip:+12125550171@host` (how telephony systems report an ANI) -> the number."""
    if not _PHONE_URI_RE.match(value):
        return value.strip()
    return re.split(r"[;@?]", _PHONE_URI_RE.sub("", value, count=1), maxsplit=1)[0].strip()


def normalize_phone(value: str | None) -> str | None:
    """Comparable form of a phone number, or None when it is not usable.

    Digits only, without the international prefix ('+' / '00') or the NANP country code 1, so '+1 212 555 0171',
    '(212) 555-0171' and '212.555.0171' are equal. Extensions, `tel:` / `sip:` wrappers and the '(0)' trunk digit are
    dropped. Strings with fewer than 7 or more than 15 digits are not numbers. Numbers written without their
    country code only compare equal to NANP numbers; other countries must be written internationally.
    """
    if not value:
        return None
    text = _PHONE_EXTENSION_RE.sub("", _strip_phone_uri(str(value)))
    digits = re.sub(r"\D", "", _PHONE_TRUNK_ZERO_RE.sub("", text))
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if PHONE_MIN_DIGITS <= len(digits) <= PHONE_MAX_DIGITS else None


def phone_variants(number: str, written_as: Sequence[str] = ()) -> list[str]:
    """Spellings a CRM may store a normalised number in, for stores that can only filter on exact strings.

    `written_as` are the caller's own spellings and come first: a CRM fed by the same system usually uses the same
    format. Every variant normalises back to `number`. Only NANP numbers have a known digit grouping; others get the
    international spellings without grouping.
    """
    variants = [s for s in (_strip_phone_uri(w) for w in written_as) if _PHONE_TEXT_RE.match(s)]
    if len(number) == 10:
        a, b, c = number[:3], number[3:6], number[6:]
        variants += [
            f"+1 {a} {b} {c}", f"+1{number}", f"+1 ({a}) {b}-{c}", f"+1-{a}-{b}-{c}", f"+1.{a}.{b}.{c}",
            f"({a}) {b}-{c}", f"{a}-{b}-{c}", f"{a}.{b}.{c}", f"{a} {b} {c}", number,
            f"1{number}", f"1-{a}-{b}-{c}", f"1 ({a}) {b}-{c}",
        ]
    else:
        variants += [f"+{number}", f"00{number}", number, *(f"+{number[:k]} {number[k:]}" for k in (1, 2, 3))]
    return [v for v in dict.fromkeys(variants) if normalize_phone(v) == number]


def normalize_name(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text)).strip()


def normalize_org(value: str | None) -> str:
    tokens = normalize_name(value).split()
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def is_open_stage(stage: str | None) -> bool:
    return not (stage and _CLOSED_STAGE_RE.match(stage.strip()))


def domain_matches(account_domain: str | None, mail_domain: str | None) -> bool:
    account = normalize_domain(account_domain)
    mail = normalize_domain(mail_domain)
    if not account or not mail or mail in PUBLIC_EMAIL_DOMAINS:
        return False
    return mail == account or mail.endswith("." + account)


def org_score(left: str | None, right: str | None) -> float:
    a, b = normalize_org(left), normalize_org(right)
    if not a or not b:
        return 0.0
    return 100.0 if a == b else float(fuzz.token_sort_ratio(a, b))


def name_score(left: str | None, right: str | None) -> float:
    a, b = normalize_name(left), normalize_name(right)
    if not a or not b:
        return 0.0
    return 100.0 if a == b else float(fuzz.token_sort_ratio(a, b))


# ---- input extraction ------------------------------------------------------------------------------------
def customer_participants(meta: ConversationMeta) -> list[Participant]:
    seller_email = normalize_email(meta.seller.email)
    out: list[Participant] = []
    for participant in meta.participants:
        if participant.role == SpeakerRole.internal:
            continue
        if seller_email and normalize_email(participant.email) == seller_email:
            continue
        out.append(participant)
    return out


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


def search_emails(participants: Sequence[Participant]) -> list[str]:
    return _unique([normalize_email(p.email) or "" for p in participants])


def phone_owners(
    participants: Sequence[Participant], *, exclude: Sequence[str | None] = ()
) -> dict[str, list[Participant]]:
    """Normalised number -> the participants calling from it. The numbers in `exclude` (the seller's) are no evidence."""
    skip = {n for n in (normalize_phone(e) for e in exclude) if n}
    owners: dict[str, list[Participant]] = {}
    for participant in participants:
        number = normalize_phone(participant.phone)
        if number and number not in skip:
            owners.setdefault(number, []).append(participant)
    return owners


def search_phones(participants: Sequence[Participant]) -> list[str]:
    return list(phone_owners(participants))


def search_domains(participants: Sequence[Participant]) -> list[str]:
    domains = [email_domain(p.email) or "" for p in participants]
    return _unique([d for d in domains if d and d not in PUBLIC_EMAIL_DOMAINS])


def search_orgs(meta: ConversationMeta, participants: Sequence[Participant]) -> list[str]:
    raw = [meta.customer_org or "", *[p.org or "" for p in participants]]
    seen: dict[str, str] = {}
    for value in raw:
        key = normalize_org(value)
        if key and key not in seen:
            seen[key] = value.strip()
    return list(seen.values())


def search_names(participants: Sequence[Participant]) -> list[str]:
    names = []
    for participant in participants:
        norm = normalize_name(participant.display_name)
        if len(norm) >= 3 and norm.split()[0] not in _GENERIC_NAMES and not norm.replace(" ", "").isdigit():
            names.append(participant.display_name.strip())
    return _unique(names)


# ---- refs ------------------------------------------------------------------------------------------------
def account_ref(row: AccountRow) -> CrmRecordRef:
    return CrmRecordRef(entity="account", id=row.id, name=row.name)


def contact_ref(row: ContactRow) -> CrmRecordRef:
    return CrmRecordRef(entity="contact", id=row.id, name=row.name)


def opportunity_ref(row: OpportunityRow) -> CrmRecordRef:
    return CrmRecordRef(entity="opportunity", id=row.id, name=row.name)


# ---- decisions -------------------------------------------------------------------------------------------
@dataclass(slots=True)
class _State:
    account: AccountRow | None = None
    contact: ContactRow | None = None
    base: float = 0.0
    method: str = "none"
    ambiguous: bool = False
    candidates: list[CrmRecordRef] = field(default_factory=list)


def _scaled(score: float, floor: float, ceil: float, lo: float, hi: float) -> float:
    span = max(hi - lo, 1e-9)
    return round(floor + (ceil - floor) * min(max((score - lo) / span, 0.0), 1.0), 3)


async def _match_by_email(lookup: CrmLookup, emails: Sequence[str], state: _State) -> None:
    hits = await lookup.contacts_by_email(emails)
    if not hits:
        return
    per_email: dict[str, list[ContactRow]] = {}
    for hit in hits:
        key = normalize_email(hit.email)
        if key:
            per_email.setdefault(key, []).append(hit)
    ordered = [per_email[e] for e in emails if e in per_email]
    if not ordered:
        return
    state.method, state.base = "email", CONF_EMAIL

    duplicated = [rows for rows in ordered if len({r.id for r in rows}) > 1]
    if duplicated:
        rows = {r.id: r for group in duplicated for r in group}.values()
        state.ambiguous = True
        state.candidates = [contact_ref(r) for r in rows]
        accounts = {r.account_id for r in rows if r.account_id}
        if len(accounts) == 1:
            state.account = await lookup.account_by_id(next(iter(accounts)))
        return

    resolved = list({rows[0].id: rows[0] for rows in ordered}.values())
    account_ids = list(dict.fromkeys(c.account_id for c in resolved if c.account_id))
    if len(account_ids) > 1:
        state.ambiguous = True
        accounts = [await lookup.account_by_id(a) for a in account_ids]
        state.candidates = [account_ref(a) for a in accounts if a]
        return
    state.contact = resolved[0]
    if account_ids:
        state.account = await lookup.account_by_id(account_ids[0])


@dataclass(frozen=True, slots=True)
class _PhoneVerdict:
    """What one number says: a `contact`; only its `account`; a number `shared` by several; or a `conflict`."""

    kind: str
    rows: tuple[ContactRow, ...]
    base: float


def _hint_state(score: float | None) -> str:
    if score is None:
        return "none"
    if score >= HINT_AGREE_MIN:
        return "agree"
    return "conflict" if score < HINT_CONFLICT_MAX else "weak"


def _name_state(row: ContactRow, names: Sequence[str]) -> str:
    return _hint_state(max((name_score(row.name, n) for n in names), default=None))


def _org_state(row: ContactRow, orgs: Sequence[str]) -> str:
    if not orgs or not row.account_name:
        return "none"
    return _hint_state(max(org_score(row.account_name, o) for o in orgs))


def _phone_verdict(rows: Sequence[ContactRow], names: Sequence[str], orgs: Sequence[str]) -> _PhoneVerdict:
    """Reconcile the contacts holding a number with the name / organisation of whoever called from it."""
    by_name = [r for r in rows if _name_state(r, names) == "agree"]
    by_org = [r for r in rows if _org_state(r, orgs) == "agree"]
    if by_name and by_org:
        pool = [r for r in by_name if r in by_org] or list({r.id: r for r in by_name + by_org}.values())
    else:
        pool = by_name or by_org or list(rows)
    if len(pool) > 1:
        return _PhoneVerdict("shared", tuple(pool), CONF_PHONE)

    contact = pool[0]
    name_state, org_state = _name_state(contact, names), _org_state(contact, orgs)
    if name_state == "conflict" and org_state == "agree":      # a colleague's line: right company, other person
        return _PhoneVerdict("account", (contact,), CONF_PHONE_ACCOUNT)
    if "conflict" in (name_state, org_state):
        return _PhoneVerdict("conflict", (contact,), CONF_PHONE_CONFLICT)
    if len(rows) > 1:
        return _PhoneVerdict("contact", (contact,), CONF_PHONE_SHARED)
    confirmed = "agree" in (name_state, org_state)
    return _PhoneVerdict("contact", (contact,), CONF_PHONE_CONFIRMED if confirmed else CONF_PHONE)


async def _match_by_phone(
    lookup: CrmLookup, owners: dict[str, list[Participant]], meta: ConversationMeta, state: _State
) -> None:
    holders: dict[str, dict[str, ContactRow]] = {}
    for row in await lookup.contacts_by_phone(list(owners)):
        for number in owners.keys() & set(row.phones):
            holders.setdefault(number, {})[row.id] = row
    verdicts = [
        _phone_verdict(list(holders[number].values()), search_names(people), search_orgs(meta, people))
        for number, people in owners.items() if number in holders
    ]
    if not verdicts:
        return
    state.method, state.base = "phone", min(v.base for v in verdicts)

    undecided = [v for v in verdicts if v.kind in ("shared", "conflict")]
    if undecided:
        rows = list({r.id: r for v in undecided for r in v.rows}.values())
        state.ambiguous, state.candidates = True, [contact_ref(r) for r in rows]
        shared_accounts = {r.account_id for r in rows if r.account_id}
        if len(shared_accounts) == 1 and all(v.kind == "shared" for v in undecided):
            state.account = await lookup.account_by_id(next(iter(shared_accounts)))
        return

    account_ids = list(dict.fromkeys(v.rows[0].account_id for v in verdicts if v.rows[0].account_id))
    if len(account_ids) > 1:
        state.ambiguous = True
        found = [await lookup.account_by_id(a) for a in account_ids]
        state.candidates = [account_ref(a) for a in found if a]
        return
    decided = [v for v in verdicts if v.kind == "contact"]
    if decided:
        state.contact = decided[0].rows[0]
    if account_ids:
        state.account = await lookup.account_by_id(account_ids[0])


async def _match_account(
    lookup: CrmLookup, orgs: Sequence[str], domains: Sequence[str], state: _State
) -> None:
    if not orgs and not domains:
        return
    rows = await lookup.candidate_accounts(orgs, domains)
    by_domain = [r for r in rows if any(domain_matches(r.domain, d) for d in domains)]
    exact = [r for r in rows if any(org_score(r.name, o) == 100.0 for o in orgs)]

    if by_domain or exact:
        both = [r for r in by_domain if r.id in {e.id for e in exact}]
        if both:
            pool, base = both, CONF_DOMAIN_AND_NAME
        elif by_domain and exact:
            state.method, state.base, state.ambiguous = "name_org", CONF_ORG_EXACT, True
            state.candidates = [account_ref(r) for r in {r.id: r for r in by_domain + exact}.values()]
            return
        elif by_domain:
            pool, base = by_domain, CONF_DOMAIN
        else:
            pool, base = exact, CONF_ORG_EXACT
        unique = list({r.id: r for r in pool}.values())
        state.method, state.base = "name_org", base
        if len(unique) == 1:
            state.account = unique[0]
        else:
            state.ambiguous, state.candidates = True, [account_ref(r) for r in unique]
        return

    scored = sorted(
        ((max((org_score(r.name, o) for o in orgs), default=0.0), r) for r in rows), key=lambda t: t[0], reverse=True
    )
    scored = [(s, r) for s, r in scored if s >= ORG_FUZZY_MIN]
    if not scored:
        return
    state.method = "name_org"
    best_score = scored[0][0]
    close = [r for s, r in scored if best_score - s < AMBIGUITY_MARGIN]
    if len(close) > 1:
        state.base, state.ambiguous = CONF_FUZZY_FLOOR, True
        state.candidates = [account_ref(r) for r in close]
        return
    state.account = scored[0][1]
    state.base = _scaled(best_score, CONF_FUZZY_FLOOR, CONF_FUZZY_CEIL, ORG_FUZZY_MIN, 100.0)


async def _match_name_and_org(
    lookup: CrmLookup, names: Sequence[str], orgs: Sequence[str], state: _State
) -> None:
    if not names:
        return
    rows = await lookup.candidate_contacts(names)
    scored: list[tuple[float, float | None, ContactRow]] = []
    for row in rows:
        n_score = max(name_score(row.name, n) for n in names)
        o_score = max((org_score(row.account_name, o) for o in orgs), default=0.0) if orgs else None
        if orgs:
            if n_score >= NAME_ORG_NAME_MIN and (o_score or 0.0) >= NAME_ORG_ORG_MIN:
                scored.append((0.6 * n_score + 0.4 * (o_score or 0.0), o_score, row))
        elif n_score >= NAME_ONLY_MIN:
            scored.append((n_score, None, row))
    if not scored:
        return
    scored.sort(key=lambda t: t[0], reverse=True)
    best = scored[0]
    close = [row for score, _, row in scored if best[0] - score < AMBIGUITY_MARGIN]
    state.method = "name_org"
    if len({r.id for r in close}) > 1:
        state.base, state.ambiguous = CONF_FUZZY_FLOOR, True
        state.candidates = [contact_ref(r) for r in {r.id: r for r in close}.values()]
        return
    state.contact = best[2]
    state.base = _scaled(best[0], CONF_FUZZY_FLOOR, CONF_NAME_ORG_CEIL if orgs else CONF_NAME_ONLY_CEIL, 85.0, 100.0)
    if best[2].account_id:
        state.account = await lookup.account_by_id(best[2].account_id)


async def _contact_within_account(
    lookup: CrmLookup, account: AccountRow, names: Sequence[str], state: _State
) -> None:
    rows = await lookup.contacts_of_account(account.id)
    scored = sorted(
        ((max((name_score(r.name, n) for n in names), default=0.0), r) for r in rows), key=lambda t: t[0], reverse=True
    )
    scored = [(s, r) for s, r in scored if s >= NAME_FUZZY_MIN]
    if not scored:
        return
    close = [r for s, r in scored if scored[0][0] - s < AMBIGUITY_MARGIN]
    if len(close) == 1:
        state.contact = scored[0][1]
        state.base = min(0.99, state.base + CONTACT_BY_NAME_BONUS)


def pick_opportunity(
    opportunities: Sequence[OpportunityRow],
) -> tuple[OpportunityRow | None, list[OpportunityRow]]:
    """(chosen, candidates): exactly one open opportunity is chosen; several are returned as candidates."""
    open_ones = [o for o in opportunities if is_open_stage(o.stage)]
    if len(open_ones) == 1:
        return open_ones[0], []
    if len(open_ones) > 1:
        return None, open_ones
    return None, []


async def resolve_match(lookup: CrmLookup, meta: ConversationMeta) -> CrmMatch:
    customers = customer_participants(meta)
    emails, domains = search_emails(customers), search_domains(customers)
    phones = phone_owners(customers, exclude=[meta.seller.phone])
    orgs, names = search_orgs(meta, customers), search_names(customers)
    state = _State()

    if emails:
        await _match_by_email(lookup, emails, state)
    if phones and state.method == "none":        # an email hit is the stronger evidence and is never overridden
        await _match_by_phone(lookup, phones, meta, state)
    if state.account is None and not state.ambiguous:
        anchor_method, anchor_base = state.method, state.base
        await _match_account(lookup, orgs, domains, state)
        if anchor_method in ("email", "phone") and state.account is not None:
            state.method, state.base = anchor_method, min(anchor_base, state.base)
    if state.account is None and state.contact is None and not state.ambiguous:
        await _match_name_and_org(lookup, names, orgs, state)
    if state.account is not None and state.contact is None and names and not state.ambiguous:
        await _contact_within_account(lookup, state.account, names, state)

    candidates = list(state.candidates)
    opportunity: OpportunityRow | None = None
    if state.account is not None:
        opportunity, open_candidates = pick_opportunity(await lookup.open_opportunities(state.account.id))
        if open_candidates:
            state.ambiguous = True
            candidates.extend(opportunity_ref(o) for o in open_candidates)

    if state.account is None and state.contact is None and not state.ambiguous:
        return CrmMatch()

    confidence = state.base
    if opportunity is not None:
        confidence = min(0.99, confidence + OPPORTUNITY_BONUS)
    if state.ambiguous:
        confidence *= AMBIGUITY_FACTOR
    return CrmMatch(
        account=account_ref(state.account) if state.account else None,
        contact=contact_ref(state.contact) if state.contact else None,
        opportunity=opportunity_ref(opportunity) if opportunity else None,
        confidence=round(confidence, 3),
        method=state.method,
        ambiguous=state.ambiguous,
        candidates=candidates,
    )
