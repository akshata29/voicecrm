# Runbook

How to run VoiceCRM locally, and how to switch each service from its mock to the live Azure / Dynamics service one step at
a time. For the Bicep deployment itself see [infrastructure.md](infrastructure.md).

> Everything in `data/` is synthetic. Do not load real customer conversations or real products until legal, compliance and
> security have approved the processing, and use a non-production Dataverse environment only (never MSX).

## 1. Run locally (no Azure)

```bat
scripts\setup_backend.bat      rem venv, pip install, copies .env.example to .env
scripts\run_all.bat            rem API on :8010, UI on :5173 (two windows)
scripts\seed_demo.bat          rem process a representative set of calls into the inbox
scripts\test_all.bat           rem backend pytest + frontend typecheck/build
.venv\Scripts\python.exe -m ruff check backend tools --config backend\pyproject.toml     rem lint (rule set is pinned there)
```

CLI without the UI: `.venv\Scripts\python.exe -m voicecrm.cli doctor | fixtures | simulate <fixture_id> --approve | eval [--noise] [--learn] | sweep`
(run from `backend\`).

State lives in `.state\` (SQLite, vault, CRM simulator, policy overrides). For a clean slate use the UI's **Live demo -> Start clean
-> Reset demo data** (it works while the backend runs, and refuses against a real Dataverse CRM), or stop the backend and delete
the folder. The UI also has "clear learned aliases" and "retention sweep".

**Check what is live:** the UI top bar, `GET /api/v1/config`, or `python -m voicecrm.cli doctor`. With
`VOICECRM_DEMO_MODE=true` a service whose settings are incomplete silently falls back to its mock - always check `config`.
With `VOICECRM_DEMO_MODE=false` the app refuses to start when a requested live service is incomplete.

## 2. Enabling live services (recommended order)

Auth is Entra ID by default (`VOICECRM_AZURE_AUTH=entra`): sign in with `az login` locally, or use the managed identity in
Azure. Entra needs the token's tenant to match the resource's tenant: if your `az login` default tenant is not the one that owns
the Azure resources, sign in to the right one (`az login --tenant <id>`) and set `VOICECRM_AZURE_TENANT_ID`. Keys
(`VOICECRM_AZURE_AUTH=key` + `*_KEY`) are simplest for a demo and for throw-away experiments; use managed identity in production.
`scripts\preflight.bat` validates the configuration and calls every live service for real, and
[live-demo.md](live-demo.md) walks through the three live demo scenarios (audio, Teams, bulk). The same check and the audio and
bulk scenarios run from the browser on the **Live demo** page (`/live`); the Teams panel there checks the tenant setup.

| Step | Service | Settings | Role needed by the identity | Verify |
|---|---|---|---|---|
| 1 | PII (Azure AI Language) | `VOICECRM_PII_MODE=azure_language`, `VOICECRM_LANGUAGE_ENDPOINT` | Cognitive Services Language Reader | simulate the privacy call; policy actions list `pii_redacted`; `leaks=0` in the scoreboard |
| 2 | Vault (Blob) | `VOICECRM_VAULT_MODE=azure_blob`, `VOICECRM_BLOB_ACCOUNT_URL` (+ container names) | Storage Blob Data Contributor | approve an EU-market call; `GET /api/v1/vault/items?conversation_id=...` lists the redacted transcript, and the blob exists |
| 3 | Search (AI Search) | `VOICECRM_SEARCH_MODE=azure_search`, `VOICECRM_SEARCH_ENDPOINT`, `VOICECRM_SEARCH_INDEX` | Search Service Contributor + Search Index Data Contributor | Catalogue page search; the index is created and filled at start-up |
| 4 | Speech (fast transcription) | `VOICECRM_TRANSCRIPTION_MODE=azure_speech`, `VOICECRM_SPEECH_ENDPOINT` | Cognitive Services Speech User | upload a short wav on Sources; the cost line shows audio hours |
| 5 | Model (Foundry) | `VOICECRM_LLM_MODE=hybrid` (or `foundry`), `VOICECRM_FOUNDRY_ENDPOINT`, `VOICECRM_FOUNDRY_DEPLOYMENT` | Cognitive Services OpenAI User | Policies page -> engine `hybrid`; usage shows tokens; run `eval --engine hybrid` |
| 6 | CRM (Dataverse) | `VOICECRM_CRM_MODE=dataverse`, `VOICECRM_DATAVERSE_URL` | application user + security role in the environment | approve a call; the phone call, tasks and insight appear in the sandbox |

Notes
- **Entra auth needs custom-domain endpoints** for Speech and Language (`https://<name>.cognitiveservices.azure.com`) and
  `https://<name>.openai.azure.com` for the model.
- **Always run `eval` after changing a service** and compare with the previous run (the scoreboard stores every run). For the model,
  also compare `--engine rules` against `hybrid` on the same calls.
- **Roll back** by setting the mode back (`mock`, `rules`, `local`) and restarting - state is compatible across modes.
- The default engine is taken from the policy (`Policies -> processing -> engine`) unless `VOICECRM_LLM_MODE` provides a live model.
- A failing model call never blocks a call: the pipeline falls back to the rules engine and flags the proposal `llm_fallback`.

### Dataverse

1. Create a **sandbox** environment (not MSX, not production) and register the app's identity as an application user with a role that
   can create phone calls, tasks and the insight table.
2. Start with the **standard** field profile (`VOICECRM_DATAVERSE_FIELD_PROFILE=standard`): standard `phonecall` + `task` tables and an
   insight record; no schema changes.
3. For structured columns (products, topics, next steps, ...) switch to `custom`; preview the schema first:
   `python tools\dataverse_bootstrap.py` (dry-run; `--show-bodies` prints every request), then `--apply` against the sandbox only.
   Pin topic option values in `data/crm/field_profile.yaml` before reordering the taxonomy.
4. The adapter is **unit-tested against mocked HTTP only**. Treat the first live write as a test: check lookups, lookups to
   `regarding`, option-set values and the idempotency key (`source:interaction:vN` - re-running must not duplicate).
5. The CRM never receives the transcript; the insight record may carry a *pointer* into the vault when the market keeps transcripts.

## 3. Channels

**Teams** has live ingestion through Microsoft Graph (poll or webhook; `VOICECRM_TEAMS_*`, see [live-demo.md](live-demo.md)
scenario B for the tenant setup and `scripts\teams_check.bat` to test it); it is tested against a mocked Graph, not yet a live tenant.
The **Zoom** and **Genesys** webhook endpoints and parsers exist, but live credentials and subscriptions are **not** wired in this
MVP. Every channel can be driven with a file upload (Sources page / `POST /api/v1/ingest/upload`) or the simulator, through
exactly the same pipeline.

| Channel | To go live | Endpoint |
|---|---|---|
| Teams | **Built.** Entra app with *application* permissions `OnlineMeetingTranscript.Read.All` and `OnlineMeetings.Read.All` (+ `User.Read.All` for UPNs), admin consent, Teams admin centre -> Transcript API access (Graph access and speaker attribution On), an application access policy per organiser, then `VOICECRM_TEAMS_MODE=poll` (no public URL) or `webhook` (`scripts\teams_check.bat subscribe`, subscribe before the meeting starts). Scheduled meetings only; the transcript arrives after the meeting ends | `POST /api/v1/webhooks/teams` (validation handshake, `clientState` check, background ingest), `POST /api/v1/webhooks/teams/lifecycle`, `GET /api/v1/sources/teams`, `POST /api/v1/sources/teams/poll` |
| Zoom | Server-to-Server OAuth app (recording scopes) + event subscription for recording / transcript completed; set `VOICECRM_ZOOM_WEBHOOK_SECRET` | `POST /api/v1/webhooks/zoom` (URL validation + signature check implemented) |
| Genesys Cloud | OAuth client (client credentials) with recording / speech-and-text analytics access and a notification subscription; the transcript URL is short-lived. **Not yet implemented:** extracting the caller's number (ANI) from a live notification - uploaded / simulated payloads carry it and drive the phone match | `POST /api/v1/webhooks/genesys` |
| Mobile | serve the UI over HTTPS (microphone access needs a secure context; `localhost` is fine) and open `/capture` | `POST /api/v1/ingest/upload` |

Confirm the exact scopes and licensing in the vendor documentation before requesting consent. **Open questions for the customer**
that change the design: whether Zoom transcripts are entitled on their plan, whether Genesys transcription is billed per minute for
their volume (list price applies above a fair-use allowance), and whether Genesys already writes transcripts to Dataverse.

## 4. Policies, retention and templates

- `data/policies/policies.yaml` is the default; edits made on the Policies page are stored as an override in
  `.state\policies.override.yaml`. Markets: US (transcript off), UK (on, 365 days), EU (on, 1825 days) - **examples, not any real firm's policy**.
- `processing.transcription_source_preference` decides per channel whether the platform's transcript or central Speech is used.
- Retention is enforced by the pipeline at finalize; `POST /api/v1/retention/sweep` (or `voicecrm.cli sweep`) removes expired vault items.
  In Azure, also set the storage lifecycle backstop (see infrastructure.md).
- Templates are YAML (`data/templates/`): every section maps to a CRM column via `crm_field`. Add a template by dropping a file; map it
  to a business line in `default_template_by_business_line`.

## 5. Cost, token and throughput reporting

Every stage records usage (audio seconds, tokens in/out per model, PII text records, search queries). The scoreboard and each call's
detail show a cost breakdown from `data/pricing.yaml`, which holds Azure **list prices** (East US 2, read 2026-11-03) and per-deployment
model prices (the key is the deployment name). It stays flagged illustrative: replace the values with the customer's negotiated prices
before quoting anything. The rules engine also records *projected* tokens (what the model engine would have used) as a separate line
that is not added to the total.

While a batch runs, `GET /api/v1/metrics/pipeline` (and the Inbox's *Pipeline throughput* card) report queue depth, calls in flight,
calls/min, p50/p95 per stage, tokens and cost for the current batch. Throughput is `VOICECRM_MAX_CONCURRENCY` divided by the time per
call; keep it below the model quota (a 5M-TPM gpt-4.1-mini deployment sustained about 370 calls/min at 64). The Language client packs
transcript segments into larger documents (`VOICECRM_PII_PACK_CHARS`, default 4000; 0 = one document per segment), which cuts billed
text records about 7x. `scripts\run_bulk_backend.bat` and `scripts\bulk_load.bat` run and drive a bulk profile.

The **Live demo** page runs the same kind of batch from the browser (`POST /api/v1/live/load`): it sizes the run first, asks before
spending when a billed service is live, raises the calls in parallel to 32 for the run (or `VOICECRM_MAX_CONCURRENCY` if higher) and
restores the setting afterwards, and tags every call it creates so *Remove load-test calls* can delete them from the state store, the
vault and the CRM simulator. `VOICECRM_LOAD_MAX_CALLS` (default 2000) caps a run. A load test never auto-approves into a live CRM.

## 6. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Chip says "mock" for a service you configured | a required setting is empty - `doctor` / `GET /config` shows the missing variable |
| App refuses to start in non-demo mode | a requested live service is incomplete (by design) |
| 401/403 from Speech, Language, Search, Blob right after deployment | role assignments take minutes to propagate; confirm the identity and `az login` account |
| Speech or Language returns 401 with Entra auth | endpoint is the regional one; use the custom-domain endpoint |
| Every call "needs review" for CRM match | no email / phone / name match in the CRM - check participant emails and phone formats |
| Product always "needs pick" | the share class was never said (by design) - or ASR dropped it; check the transcript and the learned aliases |
| Conversation vanished after a redeploy | state is SQLite on the replica (ephemeral); see the production path in architecture.md |
| `PipelineError: nothing to approve` (409) | the item is already written / rejected, or still processing |
| Browser cannot reach the API on `localhost:8010` | on some machines `localhost` resolves to `::1` first, where a WSL/Docker relay may be listening; use `http://127.0.0.1:8010` (the Vite proxy already does) or bind uvicorn to `::` |
| Port 8010 is already in use | pick another: `python -m voicecrm.cli serve --port 8011` (from `backend\`), then point the dev UI at it with `set VITE_API_PROXY=http://127.0.0.1:8011` before `npm run dev`. The built UI served by the API needs no change |
| Frontend shows canned data | it was started with the mock switch (`run_frontend.bat mock` / `npm run dev:mock`); run `npm run dev` (or open the API-served build) for the real backend |

## 7. Before showing it to anyone

1. `doctor` and the UI chips agree with what you intend to say is live.
2. `eval` is green (leaks = 0) and you can explain that the golden set is a **development set**.
3. The Policies page shows the market you are demonstrating, and says "examples".
4. Nothing real is loaded: names, products, policies and prices are synthetic.
