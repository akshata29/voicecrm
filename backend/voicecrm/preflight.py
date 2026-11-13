"""Live preflight: validates the configuration, then probes every live service through the app's own adapters.

Only synthetic data is sent (a generated sentence, a fake card number and e-mail address). Nothing is written to a CRM or to
the state database; a vault probe writes and deletes one tiny blob. Secrets are never printed: keys and bearer tokens are
masked in every message, and each failure carries the concrete fix when the cause is recognised.

    python -m voicecrm.cli preflight [--burst 16] [--only speech,language,model] [--json]
"""
from __future__ import annotations

import asyncio
import re
import statistics
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from .config import Settings
from .container import Container, build_container

Status = Literal["pass", "warn", "fail", "skip"]
PROBE_TIMEOUT = 120.0
_PLACEHOLDER = re.compile(r"(<[^>]*>|your[-_ ]|xxxx|changeme|\btodo\b|\.\.\.|replace[-_ ]?me)", re.IGNORECASE)
_TOKEN = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")
CLIP_TEXT = ("Hi Dana, this is Sam Rivera from Contoso Woodgrove. I wanted to follow up on the Contoso Equity Fund, "
             "class C. I will send you the fee comparison by Friday.")
PII_TEXT = "My card number is 4111 1111 1111 1111 and you can email me at dana.whitfield@example.com."


@dataclass
class Check:
    name: str
    status: Status
    detail: str = ""
    hint: str = ""
    seconds: float | None = None


# ---------------------------------------------------------------------------------------------------- helpers
def secrets_of(settings: Settings) -> list[str]:
    values = [settings.speech_key, settings.foundry_key, settings.language_key, settings.search_key, settings.zoom_webhook_secret]
    values += [getattr(settings, name, "") for name in ("teams_client_secret", "teams_client_state", "phone_webhook_secret",
                                                        "phone_acs_connection_string")]
    return [v for v in values if v and len(v) >= 8]


def redact(text: str, settings: Settings) -> str:
    for secret in secrets_of(settings):
        text = text.replace(secret, "<secret>")
    return _TOKEN.sub("<token>", text)


def hint_for(message: str, settings: Settings) -> str:
    low = message.lower()
    if "tenant provided in token does not match" in low or "does not match resource tenant" in low:
        return ("the Entra token is for a different tenant than the Azure resource. Either sign in to the resource's tenant "
                "(`az login --tenant <tenant id>`) and set VOICECRM_AZURE_TENANT_ID, or use the keys: VOICECRM_AZURE_AUTH=key")
    if "failed to invoke the azure cli" in low or "credentialunavailable" in low or "defaultazurecredential failed" in low:
        return "no Entra sign-in available: run `az login` (and `az login --tenant <id>` for the resource's tenant), or use VOICECRM_AZURE_AUTH=key"
    if "http 401" in low or "access denied due to invalid subscription key" in low:
        return ("authentication failed: with keys, check that each VOICECRM_*_KEY belongs to that resource; with Entra assign the "
                "data-plane role (Cognitive Services Speech User / OpenAI User / Language Reader)")
    if "http 403" in low:
        return "access denied: check the role assignment, the key, and the resource's network rules (firewall / private endpoint)"
    if "http 404" in low or "deployment" in low and "not found" in low:
        return "not found: check the endpoint and, for the model, VOICECRM_FOUNDRY_DEPLOYMENT (the deployment name, not the model name)"
    if "http 429" in low or "throttl" in low or "rate limit" in low:
        return "throttled: raise the deployment's quota (TPM) or lower VOICECRM_MAX_CONCURRENCY / VOICECRM_PII_CONCURRENCY"
    if "connecterror" in low or "connecttimeout" in low or "name or service not known" in low or "getaddrinfo" in low:
        return "cannot reach the endpoint: check the URL, DNS, VPN / proxy and the resource's firewall"
    if "timeout" in low or "timed out" in low:
        return "the call timed out: check connectivity, or retry (first calls can be slow)"
    return ""


def _host(url: str) -> str:
    return (urlsplit(url.strip()).hostname or "").lower()


def _ms(seconds: float) -> str:
    return f"{seconds * 1000:.0f} ms" if seconds < 1 else f"{seconds:.1f} s"


async def _timed(name: str, settings: Settings, coro: Awaitable[str], *, warn_over: float | None = None) -> Check:
    started = time.perf_counter()
    try:
        detail = await asyncio.wait_for(coro, PROBE_TIMEOUT)
    except Exception as exc:                                  # a probe must report, never crash the run
        message = redact(f"{type(exc).__name__}: {exc}".replace("\n", " "), settings)[:420]
        return Check(name, "fail", message, hint_for(message, settings), time.perf_counter() - started)
    elapsed = time.perf_counter() - started
    status: Status = "warn" if warn_over is not None and elapsed > warn_over else "pass"
    return Check(name, status, detail, "" if status == "pass" else f"slower than expected ({_ms(elapsed)})", elapsed)


# ---------------------------------------------------------------------------------------------------- static checks
def check_config(settings: Settings) -> list[Check]:
    """Configuration that can be judged without calling anything."""
    checks: list[Check] = []
    try:
        modes = settings.resolved_modes()
    except ValueError as exc:
        return [Check("config", "fail", str(exc),
                      "set the missing variables, or VOICECRM_*_MODE back to mock/local/rules for that service")]
    live = [name for name, mode in modes.items() if mode.live]
    fallbacks = [f"{n}: {m.reason}" for n, m in modes.items() if not m.live and m.requested != m.effective]
    checks.append(Check("config: modes", "pass" if live else "warn",
                        "live: " + (", ".join(live) or "none") + " | mock/local: " +
                        (", ".join(n for n in modes if n not in live) or "none")))
    for line in fallbacks:
        checks.append(Check(f"config: fallback ({line.split(':')[0]})", "warn", line,
                            "a requested live service silently fell back to its mock (demo mode); complete its settings"))
    if live and settings.demo_mode:
        checks.append(Check("config: demo mode", "warn", "VOICECRM_DEMO_MODE=true",
                            "set VOICECRM_DEMO_MODE=false for a live demo so a missing setting fails loudly instead of "
                            "falling back to a mock"))
    azure_live = [n for n in ("transcription", "llm", "pii", "search") if modes[n].live]
    if settings.teams_mode != "off":
        missing = [n for n, v in (("TENANT_ID", settings.teams_tenant_id), ("CLIENT_ID", settings.teams_client_id),
                                  ("CLIENT_SECRET", settings.teams_client_secret)) if not v]
        checks.append(Check("config: teams app", "fail" if missing else "pass",
                            f"VOICECRM_TEAMS_MODE={settings.teams_mode}"
                            + (f"; missing VOICECRM_TEAMS_{', VOICECRM_TEAMS_'.join(missing)}" if missing else ""),
                            "register the Entra app and set its tenant id, client id and secret (docs/live-demo.md, scenario B)"
                            if missing else ""))
        if not settings.teams_organizers.strip():
            checks.append(Check("config: teams organisers", "warn" if settings.teams_mode == "webhook" else "fail",
                                "VOICECRM_TEAMS_ORGANIZERS is empty",
                                "set it to the UPN or object id of each seller who hosts the demo meetings"))
        if settings.teams_mode == "webhook":
            base = settings.teams_public_base_url.strip()
            problems = [p for p, bad in (("VOICECRM_TEAMS_PUBLIC_BASE_URL must be an https URL Graph can reach",
                                          not base.lower().startswith("https://")),
                                         ("VOICECRM_TEAMS_CLIENT_STATE must be set (any long random string)",
                                          not settings.teams_client_state)) if bad]
            checks.append(Check("config: teams webhook", "fail" if problems else "pass", base or "no public URL",
                                "; ".join(problems)))
    if settings.phone_mode != "off":
        checks.extend(_phone_config(settings))
    if azure_live:
        keys = {"transcription": settings.speech_key, "llm": settings.foundry_key, "pii": settings.language_key,
                "search": settings.search_key}
        if settings.azure_auth == "key":
            missing = [n for n in azure_live if not keys[n]]
            checks.append(Check("config: auth", "fail" if missing else "pass",
                                "VOICECRM_AZURE_AUTH=key" + (f"; missing key for: {', '.join(missing)}" if missing else ""),
                                "set the VOICECRM_*_KEY of each live service" if missing else ""))
        else:
            leftover = [n for n in azure_live if keys[n]]
            checks.append(Check("config: auth", "warn" if leftover else "pass",
                                "VOICECRM_AZURE_AUTH=entra" + (f"; keys are set for {', '.join(leftover)} but are ignored" if leftover else ""),
                                "with entra the identity needs the data-plane roles and `az login` in the resource's tenant "
                                "(VOICECRM_AZURE_TENANT_ID); to use the keys set VOICECRM_AZURE_AUTH=key" if leftover else ""))
    for service, url, suffixes in (
        ("transcription", settings.speech_endpoint, (".cognitiveservices.azure.com", ".api.cognitive.microsoft.com")),
        ("pii", settings.language_endpoint, (".cognitiveservices.azure.com", ".api.cognitive.microsoft.com")),
        ("llm", settings.foundry_endpoint, (".openai.azure.com", ".services.ai.azure.com", ".cognitiveservices.azure.com")),
        ("search", settings.search_endpoint, (".search.windows.net",)),
        ("vault", settings.blob_account_url, (".blob.core.windows.net",)),
        ("crm", settings.dataverse_url, (".dynamics.com",)),
    ):
        if not modes[service].live or not url:
            continue
        host, problems = _host(url), []
        if _PLACEHOLDER.search(url):
            problems.append("looks like a placeholder")
        if not host:
            problems.append("not a URL (include https://)")
        elif not host.endswith(suffixes):
            problems.append(f"host {host} is not the usual {' / '.join(suffixes)}")
        elif host.endswith(".api.cognitive.microsoft.com") and settings.azure_auth == "entra":
            problems.append("regional endpoint: Entra needs the custom-domain https://<name>.cognitiveservices.azure.com")
        if service == "crm" and re.search(r"microsoftsales|msx", host):
            problems.append("this is MSX - never write the demo to MSX")
        status: Status = "fail" if any(("MSX" in p or "regional" in p or "placeholder" in p or "not a URL" in p) for p in problems) \
            else "warn" if problems else "pass"
        checks.append(Check(f"config: {service} endpoint", status, host or url, "; ".join(problems)))
    return checks


_PHONE_SETTING = {"phone_acs_endpoint": "VOICECRM_PHONE_ACS_ENDPOINT (or VOICECRM_PHONE_ACS_CONNECTION_STRING)",
                  "phone_public_base_url": "VOICECRM_PHONE_PUBLIC_BASE_URL", "phone_webhook_secret": "VOICECRM_PHONE_WEBHOOK_SECRET"}


def _phone_config(settings: Settings) -> list[Check]:
    """The phone line's settings, judged without calling anything."""
    missing = settings.phone_missing()
    out = [Check("config: phone line", "fail" if missing else "pass",
                 f"VOICECRM_PHONE_MODE={settings.phone_mode}" + (f"; missing {', '.join(_PHONE_SETTING[m] for m in missing)}" if missing else ""),
                 "set them in .env (docs/phone-line.md, setup)" if missing else "")]
    endpoint = settings.phone_acs_endpoint.strip()
    if endpoint:
        host = _host(endpoint)
        if _PLACEHOLDER.search(endpoint) or not host.endswith(".communication.azure.com"):
            out.append(Check("config: phone endpoint", "fail", host or endpoint,
                             "expected https://<resource>.<geo>.communication.azure.com (Communication Services > Overview)"))
    base = settings.phone_public_base_url.strip()
    if base and not base.lower().startswith("https://"):
        out.append(Check("config: phone public URL", "fail", base,
                         "Event Grid and Call Automation only call https URLs: use the dev tunnel's https address"))
    if settings.phone_webhook_secret and len(settings.phone_webhook_secret) < 16:
        out.append(Check("config: phone webhook secret", "warn", f"{len(settings.phone_webhook_secret)} characters",
                         "the secret is part of the Event Grid URL: use at least 24 random characters"))
    if settings.phone_ingress_port == 0:
        out.append(Check("config: phone ingress", "warn", "VOICECRM_PHONE_INGRESS_PORT=0: the ingress listener is off",
                         "the webhooks are then only on the API port, which must never be put on the internet"))
    if settings.phone_require_notice and not settings.phone_cognitive_endpoint.strip():
        out.append(Check("config: phone notice", "fail", "the recording notice is required but cannot be spoken",
                         "set VOICECRM_PHONE_COGNITIVE_ENDPOINT to the Foundry endpoint (and give the Communication Services "
                         "identity the Cognitive Services User role on it), or set VOICECRM_PHONE_REQUIRE_NOTICE=false"))
    return out


# ---------------------------------------------------------------------------------------------------- live probes
async def _synthesize_clip(c: Container, voice: str = "en-US-JennyNeural") -> bytes:
    from .adapters.transcription.azure_tts import AzureSpeechSynthesizer

    s = c.settings
    synth = AzureSpeechSynthesizer(endpoint=s.speech_endpoint, key=s.speech_key or None, auth=s.azure_auth, credential=c.credential)
    try:
        return await synth.synthesize(CLIP_TEXT, voice=voice)
    finally:
        await synth.aclose()


async def _transcribe_clip(c: Container, wav: bytes) -> str:
    from .ports.transcription import AudioInput, TranscriptionRequest

    with tempfile.TemporaryDirectory(prefix="voicecrm-preflight-") as tmp:
        path = Path(tmp) / "clip.wav"
        path.write_bytes(wav)
        request = TranscriptionRequest(
            audio=AudioInput(filename="clip.wav", path=str(path), sha256="preflight"),
            phrase_list=c.deps.resolver.phrase_list(100), diarize=True, max_speakers=2)
        result = await c.deps.transcription.transcribe(request)
    text = " ".join(seg.text for seg in result.segments).strip()
    if not text:
        raise RuntimeError("the service returned no text for the generated speech")
    heard = text.lower()
    if "contoso" not in heard:
        raise RuntimeError(f"the speech was transcribed but did not contain the expected words: {text[:120]!r}")
    return f'{result.audio_seconds:.1f} s of audio, {result.language}: "{text[:110]}"'


async def probe_speech(c: Container) -> list[Check]:
    s = c.settings
    started = time.perf_counter()
    try:
        wav = await asyncio.wait_for(_synthesize_clip(c), PROBE_TIMEOUT)
    except Exception as exc:
        message = redact(f"{type(exc).__name__}: {exc}".replace("\n", " "), s)[:420]
        return [Check("speech: text-to-speech (makes the test clip)", "fail", message, hint_for(message, s),
                      time.perf_counter() - started),
                Check("speech: transcription", "skip", "needs the generated clip")]
    tts = Check("speech: text-to-speech (makes the test clip)", "pass", f"{len(wav) // 1024} KB of 16 kHz WAV", seconds=time.perf_counter() - started)
    stt = await _timed("speech: transcription", s, _transcribe_clip(c, wav), warn_over=20)
    return [tts, stt]


async def probe_language(c: Container) -> Check:
    async def run() -> str:
        spans = (await c.deps.pii.detect_many([PII_TEXT], language="en"))[0]
        kinds = sorted({sp.category for sp in spans})
        if "payment_card" not in kinds:
            raise RuntimeError(f"the card number was not detected (found: {kinds or 'nothing'})")
        extra = "" if "personal_contact" in kinds else " (e-mail not flagged)"
        return f"{len(spans)} entities detected: {', '.join(kinds)}{extra}"

    return await _timed("language: PII detection", c.settings, run(), warn_over=10)


async def probe_model(c: Container) -> Check:
    async def run() -> str:
        if c.llm is None:
            raise RuntimeError("no model configured")
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}, "word": {"type": "string"}},
                  "required": ["ok", "word"], "additionalProperties": False}
        result = await c.llm.generate_json(system="Reply with JSON only.", user='Return {"ok": true, "word": "ping"}.',
                                           json_schema=schema, schema_name="preflight", max_output_tokens=200)
        if result.data.get("ok") is not True:
            raise RuntimeError(f"unexpected model answer: {result.data}")
        return (f"deployment {c.settings.foundry_deployment} ({result.usage.model}), "
                f"{result.usage.tokens_in}/{result.usage.tokens_out} tokens in/out")

    return await _timed("model: structured output", c.settings, run(), warn_over=20)


async def probe_search(c: Container) -> Check:
    async def run() -> str:
        hits = await c.search.search("equity fund class c", top=3)
        if not hits:
            raise RuntimeError("the index returned no results: start the API once so it can create and fill the index")
        return f"{len(hits)} hits, best score {hits[0].score:.2f} (index {c.settings.search_index})"

    return await _timed("search: catalogue query", c.settings, run(), warn_over=10)


async def probe_vault(c: Container) -> Check:
    async def run() -> str:
        cid = f"preflight-{uuid.uuid4().hex[:8]}"
        vault = c.deps.vault
        ensure = getattr(vault, "ensure_containers", None)
        if ensure is not None:
            await ensure()
        await vault.put_audio(cid, b"preflight", filename="p.wav", content_type="audio/wav", expires_at=None)
        got = await vault.get_audio(cid)
        removed = await vault.delete(cid, ("audio",))
        if got is None or got[0] != b"preflight" or removed != 1:
            raise RuntimeError("the test blob could not be written, read back and deleted")
        return "wrote, read back and deleted a test blob"

    return await _timed("vault: blob round trip", c.settings, run(), warn_over=10)


async def probe_crm(c: Container) -> Check:
    async def run() -> str:
        health = await c.deps.crm.health()
        if not health.get("ok", False):
            raise RuntimeError(str(health.get("error") or health))
        if c.modes["crm"].live:
            return f"Dataverse reachable at {health.get('url')} (org {str(health.get('organization_id'))[:8]}...)"
        return "local CRM simulator (SQLite) - no Dataverse configured"

    return await _timed("crm: health", c.settings, run(), warn_over=10)


# ---------------------------------------------------------------------------------------------------- teams
async def probe_teams(settings: Settings) -> list[Check]:
    """Signs in to Graph as the app and tests each step live ingestion depends on, with the fix for each failure."""
    from datetime import timedelta

    from .adapters.sources.teams import parse_teams_vtt
    from .adapters.sources.teams_graph import ClientSecretAuth, GraphClient, GraphError
    from .domain.conversation import utcnow
    from .pipeline.teams_live import TeamsIngestor, _iso

    try:
        auth = ClientSecretAuth(tenant_id=settings.teams_tenant_id, client_id=settings.teams_client_id,
                                client_secret=settings.teams_client_secret)
    except ValueError as exc:
        return [Check("teams: Graph access", "fail", str(exc), "see docs/live-demo.md, scenario B (app registration)")]
    graph = GraphClient(auth)
    ingestor = TeamsIngestor(None, graph, settings=settings)
    checks: list[Check] = []
    downloaded = False

    async def step(name: str, coro: Awaitable[str], *, level: Status = "fail") -> bool:
        started = time.perf_counter()
        try:
            detail = await asyncio.wait_for(coro, 60)
        except GraphError as exc:
            checks.append(Check(name, level, redact(exc.message, settings)[:300], exc.hint or "", time.perf_counter() - started))
            return False
        except Exception as exc:
            message = redact(f"{type(exc).__name__}: {exc}".replace("\n", " "), settings)[:300]
            checks.append(Check(name, level, message, hint_for(message, settings), time.perf_counter() - started))
            return False
        checks.append(Check(name, "pass", detail, "", time.perf_counter() - started))
        return True

    async def sign_in() -> str:
        await auth.token(force=True)
        return f"signed in as the app (client {settings.teams_client_id[:8]}...) to tenant {settings.teams_tenant_id[:8]}..."

    try:
        if await step("teams: app sign-in", sign_in()):
            organizers = [o.strip() for o in settings.teams_organizers.split(",") if o.strip()]
            if not organizers:
                checks.append(Check("teams: organisers", "warn", "none configured",
                                    "set VOICECRM_TEAMS_ORGANIZERS to the UPN or object id of each seller who hosts meetings"))
            newest: tuple[Any, str, str] | None = None
            for ident in organizers:
                found: list[dict] = []

                async def list_transcripts(ident: str = ident, found: list[dict] = found) -> str:
                    user = await ingestor._user(ident)
                    since = _iso(utcnow() - timedelta(hours=24))
                    path = (f"/users/{user.id}/onlineMeetings/getAllTranscripts"
                            f"(meetingOrganizerUserId='{user.id}',startDateTime={since})")
                    found.extend([i async for i in graph.paged(path, params={"$top": 25}, limit=25)])
                    who = user.name or ident
                    if not found:
                        return (f"{who}: access works; no transcripts in the last 24 h (hold a scheduled Teams meeting with "
                                "transcription on, then run this again to test the download)")
                    return f"{who}: {len(found)} transcript(s) in the last 24 h"

                if await step(f"teams: list transcripts ({ident})", list_transcripts()) and found:
                    latest = max(found, key=lambda i: str(i.get("createdDateTime") or ""))
                    newest = (await ingestor._user(ident), str(latest.get("meetingId")), str(latest.get("id")))
            if newest is not None:
                user, meeting_id, transcript_id = newest
                base = f"/users/{user.id}/onlineMeetings/{meeting_id}"

                async def download() -> str:
                    vtt = await graph.get_text(f"{base}/transcripts/{transcript_id}/content", accept="text/vtt")
                    cues = parse_teams_vtt(vtt)
                    speakers = sorted({c.speaker_id for c in cues if c.speaker_id and c.speaker_id != "Unknown"})
                    if cues and not speakers:
                        raise GraphError("the transcript has no speaker names", code="SpeakerAttributionNotAllowed",
                                         hint="turn on 'Include speaker attribution' in the Teams admin centre transcript settings")
                    return f"downloaded the newest transcript: {len(cues)} cues, speakers: {', '.join(speakers[:4])}"

                downloaded = await step("teams: download transcript", download())

                async def meeting() -> str:
                    details = await graph.get_json(base)
                    attendees = (details.get("participants") or {}).get("attendees") or []
                    return f"meeting '{details.get('subject') or '(no subject)'}' with {len(attendees)} attendee(s)"

                await step("teams: meeting details", meeting(), level="warn")
            if settings.teams_mode == "webhook":
                async def subscriptions() -> str:
                    mine = [s for s in await graph.list_subscriptions() if "getAllTranscripts" in str(s.get("resource"))]
                    if not mine:
                        raise GraphError("no transcript subscription exists", hint="run `python -m voicecrm.cli teams subscribe`")
                    return f"{len(mine)} transcript subscription(s); next expiry {min(str(s.get('expirationDateTime')) for s in mine)}"

                await step("teams: webhook subscription", subscriptions(), level="warn")
    finally:
        await graph.aclose()
    worst: Status = "fail" if any(c.status == "fail" for c in checks) else "warn" if any(c.status == "warn" for c in checks) else "pass"
    if worst != "pass":
        summary = "see the failed steps above"
    elif downloaded:
        summary = "app sign-in, transcript listing and download"
    else:
        summary = "app sign-in and transcript listing; the download is untested until a transcript exists"
    checks.append(Check("teams: Graph access", worst, summary))
    return checks


# ---------------------------------------------------------------------------------------------------- phone
async def probe_phone(c: Container) -> list[Check]:
    """The phone line's own self-check: Communication Services access, the public URL, the seller and the recording notice."""
    line = c.phone
    if line is None:
        return [Check("phone: line", "fail", "the phone line is not running",
                      "set VOICECRM_PHONE_MODE=acs and the VOICECRM_PHONE_* settings in .env, then restart the app")]
    started = time.perf_counter()
    try:
        results = await asyncio.wait_for(line.checks(), 90)
    except Exception as exc:
        message = redact(f"{type(exc).__name__}: {exc}".replace("\n", " "), c.settings)[:300]
        return [Check("phone: line", "fail", message, hint_for(message, c.settings), time.perf_counter() - started)]
    checks = [Check(name, status, redact(detail, c.settings)[:300], hint) for name, status, detail, hint in results]
    worst: Status = "fail" if any(ch.status == "fail" for ch in checks) else "warn" if any(ch.status == "warn" for ch in checks) else "pass"
    summary = ("Communication Services access, public URL, seller and recording notice" if worst == "pass"
               else "see the steps above")
    seller = next((ch for ch in checks if ch.name == "phone: seller"), None)
    if worst == "pass" and seller is not None:
        summary += f"; {seller.detail}"
    checks.append(Check("phone: line", worst, summary, "", time.perf_counter() - started))
    return checks


# ---------------------------------------------------------------------------------------------------- burst
async def _burst(name: str, n: int, call: Callable[[], Awaitable[object]], settings: Settings) -> Check:
    """N concurrent calls: how many succeed and how long they take (a quick look at throttling before a bulk run)."""
    started = time.perf_counter()
    times: list[float] = []
    errors: list[str] = []

    async def one() -> None:
        t0 = time.perf_counter()
        try:
            await asyncio.wait_for(call(), PROBE_TIMEOUT)
            times.append(time.perf_counter() - t0)
        except Exception as exc:
            errors.append(redact(f"{type(exc).__name__}: {exc}", settings)[:160])

    await asyncio.gather(*(one() for _ in range(n)))
    wall = time.perf_counter() - started
    if times:
        ordered = sorted(times)
        p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
        detail = (f"{len(times)}/{n} ok in {_ms(wall)}; per call p50 {_ms(statistics.median(times))}, p95 {_ms(p95)}"
                  f" ({len(times) / wall * 60:.0f} calls/min)")
    else:
        detail = f"0/{n} succeeded"
    if errors:
        detail += f"; {len(errors)} failed, e.g. {errors[0]}"
    status: Status = "pass" if not errors else ("warn" if times else "fail")
    return Check(name, status, detail, hint_for(errors[0], settings) if errors else "", wall)


async def burst_checks(c: Container, n: int, wav: bytes | None) -> list[Check]:
    out: list[Check] = []
    if c.modes["llm"].live and c.llm is not None:
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
        llm = c.llm
        out.append(await _burst(f"burst x{n}: model", n, lambda: llm.generate_json(
            system="Reply with JSON only.", user='Return {"ok": true}.', json_schema=schema, schema_name="burst",
            max_output_tokens=200), c.settings))
    if c.modes["pii"].live:
        texts = [PII_TEXT] * 5
        out.append(await _burst(f"burst x{n}: language (5 documents each)", n, lambda: c.deps.pii.detect_many(texts, language="en"),
                                c.settings))
    if c.modes["transcription"].live and wav is not None:
        m = min(n, 16)
        out.append(await _burst(f"burst x{m}: speech", m, lambda: _transcribe_clip(c, wav), c.settings))
    return out


# ---------------------------------------------------------------------------------------------------- driver
ALL_PROBES = ("speech", "language", "model", "search", "vault", "crm", "teams", "phone")


async def run_preflight(settings: Settings, *, burst: int = 0, only: set[str] | None = None,
                        container: Container | None = None) -> list[Check]:
    """Probes the live services. With `container` (the API's own) it checks what is actually running and leaves it open."""
    checks = check_config(settings)
    # a broken Teams or phone setup must not stop the Azure probes, so their config failures are handled by their own probes
    if any(ch.status == "fail" for ch in checks
           if ch.name.startswith("config") and not ch.name.startswith(("config: teams", "config: phone"))):
        checks.append(Check("live probes", "skip", "fix the configuration first"))
        return checks
    owned = container is None
    probed = container or build_container(settings)
    try:
        wanted = only or set(ALL_PROBES)
        modes = probed.modes
        wav: bytes | None = None
        if "speech" in wanted:
            if modes["transcription"].live:
                speech = await probe_speech(probed)
                checks.extend(speech)
                if burst > 0 and speech and speech[0].status == "pass":
                    wav = await _synthesize_clip(probed)
            else:
                checks.append(Check("speech", "skip", "transcription is mock (VOICECRM_TRANSCRIPTION_MODE)"))
        if "language" in wanted:
            checks.append(await probe_language(probed) if modes["pii"].live else Check("language", "skip", "PII is local"))
        if "model" in wanted:
            checks.append(await probe_model(probed) if modes["llm"].live else Check("model", "skip", "no model (rules engine)"))
        if "search" in wanted and modes["search"].live:
            checks.append(await probe_search(probed))
        if "vault" in wanted and modes["vault"].live:
            checks.append(await probe_vault(probed))
        if "crm" in wanted:
            checks.append(await probe_crm(probed))
        if "teams" in wanted and settings.teams_mode != "off":
            if any(ch.status == "fail" for ch in checks if ch.name.startswith("config: teams")):
                checks.append(Check("teams: Graph access", "fail", "the Teams settings are incomplete",
                                    "fix the 'config: teams' lines above"))
            else:
                checks.extend(await probe_teams(settings))
        if "phone" in wanted and settings.phone_mode != "off":
            if any(ch.status == "fail" for ch in checks if ch.name.startswith("config: phone")):
                checks.append(Check("phone: line", "fail", "the phone settings are incomplete", "fix the 'config: phone' lines above"))
            else:
                checks.extend(await probe_phone(probed))
        if burst > 0:
            checks.extend(await burst_checks(probed, burst, wav))
    finally:
        if owned:
            await probed.shutdown()
    return checks


def readiness(checks: list[Check], settings: Settings) -> list[tuple[str, str, str]]:
    """(scenario, verdict, why) for the four live demo scenarios, derived from the checks."""
    by = {ch.name: ch for ch in checks}

    def state(*names: str) -> tuple[str, str]:
        found = [by[n] for n in names if n in by]
        if any(ch.status == "fail" for ch in found) or len(found) < len(names):
            bad = next((ch for ch in found if ch.status == "fail"), None)
            return "NOT READY", (bad.name if bad else "not probed")
        if any(ch.status == "skip" for ch in found):
            return "NOT READY", next(ch.name for ch in found if ch.status == "skip") + " is mock/skipped"
        return "READY", ", ".join(f"{ch.name.split(':')[0]} {_ms(ch.seconds or 0)}" for ch in found)

    a = state("speech: transcription", "language: PII detection", "model: structured output")
    c_state = state("language: PII detection", "model: structured output")
    teams = "NOT CONFIGURED", "set VOICECRM_TEAMS_* (see docs/live-demo.md, scenario B)"
    if getattr(settings, "teams_mode", "off") != "off":
        teams = state("teams: Graph access") if "teams: Graph access" in by else ("NOT READY", "teams not probed")
        if teams[0] == "READY":
            teams = "READY", by["teams: Graph access"].detail      # says what was proven, e.g. that the download is untested
    phone = "NOT CONFIGURED", "set VOICECRM_PHONE_* (see docs/phone-line.md)"
    if getattr(settings, "phone_mode", "off") != "off":
        needed = ["phone: line"] + (["speech: transcription"] if "speech: transcription" in by else [])
        phone = state(*needed) if "phone: line" in by else ("NOT READY", "phone not probed")
        if phone[0] == "READY":
            phone = "READY", by["phone: line"].detail
    return [("A  audio -> Speech -> Language -> model", *a), ("B  Teams call -> VoiceCRM", *teams),
            ("C  bulk run (hundreds of calls)", *c_state), ("D  phone call -> VoiceCRM", *phone)]


def format_checks(checks: list[Check]) -> str:
    mark = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}
    lines = []
    for ch in checks:
        took = f"{_ms(ch.seconds):>8}" if ch.seconds is not None else " " * 8
        lines.append(f"[{mark[ch.status]}] {ch.name:<46} {took}  {ch.detail}")
        if ch.hint:
            lines.append(f"       -> {ch.hint}")
    return "\n".join(lines)


def format_report(checks: list[Check], settings: Settings) -> str:
    lines = [format_checks(checks), "", "Scenario readiness"]
    for scenario, verdict, why in readiness(checks, settings):
        lines.append(f"  {scenario:<42} {verdict:<15} {why}")
    return "\n".join(lines)


def to_json(checks: list[Check]) -> list[dict]:
    return [asdict(ch) for ch in checks]
