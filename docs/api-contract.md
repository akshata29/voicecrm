# VoiceCRM API contract (`/api/v1`)

Shapes live in code: `backend/voicecrm/api/schemas.py` (HTTP-specific) and `backend/voicecrm/domain/*.py` (shared
models). The frontend mirrors them exactly; field names are snake_case on the wire; datetimes are ISO-8601 UTC.
All endpoints return JSON unless stated. Errors: `{"detail": "..."}` with the usual HTTP status.

## Meta
| Method | Path | Returns |
|---|---|---|
| GET | `/health` | `{"status":"ok"}` |
| GET | `/config` | `ConfigResponse` (which services are live vs mock, markets, templates, effective engine, `llm_deployment` when the model is live) |

## Ingestion (all channels converge on the same pipeline)
| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/ingest/upload` | multipart: `file` (audio `.wav/.mp3/.m4a/.ogg/.webm`, or transcript `.vtt` / `.json`), plus form fields `channel` (teams\|zoom\|genesys\|phone\|mobile\|upload), `market`, `business_line`, `call_type?`, `seller_name`, `seller_email`, `customer_name?`, `customer_org?`, `customer_email?`, `language_hint?`, `title?`, `started_at?`, `source_interaction_id?`, `fixture_id?` | `IngestResponse` |
| GET | `/fixtures` | - | `FixtureInfo[]` (scripted demo calls; `recording_seconds` is set when a real speech recording exists in `data/audio/live`) |
| GET | `/fixtures/{fixture_id}/audio` | - | the real recording (`audio/wav`) so the UI can play it; `404` when there is none (`scripts\make_audio.bat` creates them) |
| POST | `/ingest/simulate` | `SimulateRequest` | `IngestResponse` - renders the scripted call as the channel's native payload (Teams VTT, Zoom VTT/JSON, Genesys transcript JSON, or the real recording / an audio sidecar) and ingests it through the real adapter path |
| POST | `/webhooks/zoom` | Zoom webhook JSON (handles `endpoint.url_validation`; HMAC signature checked when `VOICECRM_ZOOM_WEBHOOK_SECRET` is set) | `202 {"status":"accepted",...}` / validation response; `400` not a JSON object, `422` unrecognised payload, `401` bad signature |
| POST | `/webhooks/genesys` | Genesys notification JSON | `202 {...}`; `400` / `422` as above |
| POST | `/webhooks/teams` | Graph change notification (handles `validationToken`; notifications whose `clientState` differs from `VOICECRM_TEAMS_CLIENT_STATE` are dropped) | `202 {"status","transcripts","ingesting","note"}`; with live Teams ingestion on, the transcripts are fetched and ingested in the background; `400` / `422` as above |
| POST | `/webhooks/teams/lifecycle` | Graph lifecycle notification (handles `validationToken`; `reauthorizationRequired` renews the subscription) | `202` |
| GET | `/sources/teams` | - | `TeamsStatus` (mode, health, organisers, last poll / ingest, counters, recent transcripts; `mode: "off"` when live ingestion is not enabled) |
| POST | `/sources/teams/poll` | - | `{"ingested": n, "status": TeamsStatus}` - look for new transcripts now; `409` when off or misconfigured, `502` with the fix when Graph refuses |
| GET | `/sources/teams/history?days=30` | - | `TeamsHistory` (`days` 1-90, default 30): the transcripts the configured organisers still have from the last `days` days, newest first, at most 60 (`truncated` says there were more), each with `organizer`, `meeting_id`, `transcript_id`, `title`, `started_at`, `duration_seconds`, `attendees` and a `state` of `new`, `in_inbox` (with `conversation_id` and `conversation_status`) or `unfinished` (still being written); `errors` lists organisers that could not be searched. Nothing is ingested. `409` when off or misconfigured, `502` with the fix when Graph refuses |
| POST | `/sources/teams/import` | `{"items": [{"organizer", "meeting_id", "transcript_id"}]}` (1-25, as listed by `history`) | `TeamsImportResult` `{"ingested", "duplicates", "failed", "outcomes": [{"transcript_id", "outcome", "conversation_id", "title", "detail"}]}` - downloads each transcript and runs it through the pipeline like a meeting that just ended, whether or not the poller has seen it; idempotent on the transcript id (`duplicate`), one failure is an outcome and does not stop the rest, and the live counters in `TeamsStatus` do not change. `422` for an organiser that is not in `VOICECRM_TEAMS_ORGANIZERS` or a bad item list, `409` / `502` as above |

## Phone line (Azure Communication Services; see [phone-line.md](phone-line.md))
Operator routes (the API port, never tunnelled):

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/sources/phone` | - | `PhoneStatus`: `mode` (`off\|acs`), `configured`, `problem`, `number` (the display number), `line_identity` (the ACS identity the dial pad calls), `public_url`, `ingress_listening`, `seller` (`mode` `teams\|console\|none`, `name`, `online`), `notice_enabled`, `active[]` and `recent[]` (at most 10, newest first) of `PhoneCall`, the counters `calls`, `ingested`, `failed`, `missed`, `last_event_at`, `last_error`. With the line off: `mode: "off"` and a `problem` that says how to enable it |
| POST | `/phone/token` | `{"role": "caller\|seller", "display_name"?, "identity"?, "profile"?: {"name", "org", "email", "phone"}}` | `PhoneToken` `{identity, token, expires_on, display_name, line_identity, number}` - a VoIP access token for the browser; send the previous `identity` back to reuse it. A caller's `profile` is remembered for that identity and becomes the conversation's customer. `409` when off or misconfigured, `422` for the line's own identity or a non-ACS identity, `502` when ACS refuses |
| POST | `/phone/seller` | `{"identity", "name"?, "email"?}` | `{"registered": true, "ttl_seconds": 45}` - registers (and heartbeats; call again within the TTL) this browser as the seller console; new calls ring it while it is online. `409` when off |
| DELETE | `/phone/seller?identity=` | - | `{"registered": false, "ttl_seconds": 0}` |

`PhoneCall`: `call_id`, `state` (`ringing\|connecting\|notice\|calling_seller\|in_call\|leaving_message\|ended\|processing\|ingested\|failed\|missed`),
`caller` (E.164 for a phone caller, the display name for a browser caller), `caller_kind` (`phone\|voip`), `caller_name`, `seller`,
`started_at`, `connected_at`, `ended_at`, `duration_seconds`, `notice` (`pending\|played\|skipped\|failed`), `recording`
(`none\|active\|ready\|failed`), `conversation_id` (set once the recording is handed to the pipeline), `error`, `timeline[]`
(`{at, label}`, oldest first). `processing` lasts from the recording being ready until the pipeline has finished with it, and
then the call is `ingested` or, with the pipeline's own reason in `error`, `failed`. `missed` covers a caller who hung up before
the recording started and a recording with no speech (below `VOICECRM_PHONE_MIN_SPEECH_SECONDS`, or none that Azure Speech could
recognise): it has no `error`, and the last `timeline` label says why.

Webhooks (the **ingress** port, 8011, the only thing a tunnel exposes; the same routes also exist on the API app for tests):

| Method | Path | Notes |
|---|---|---|
| GET | `/webhooks/phone/health` | `{"service": "voicecrm-phone", "ok": true, "line": bool}`; no secret, no data; the self-check uses it to prove the tunnel reaches the ingress |
| POST | `/webhooks/phone/events?key=` | Event Grid (`IncomingCall`, `RecordingFileStatusUpdated`). The subscription handshake is answered with `{"validationResponse"}`; a wrong or missing key is `401` (even for the handshake), a body over 1 MB `413`, bad JSON `400`, the line off `503` |
| OPTIONS | `/webhooks/phone/events?key=` | the CloudEvents delivery permission check (`WebHook-Allowed-Origin`) |
| POST | `/webhooks/phone/calls/{call_id}?t=` | Call Automation callbacks for one call; `t` is an HMAC of the call id (`401` otherwise). Accepted at once and processed in order in the background |

## Conversations and review
| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/conversations?status=&limit=&offset=` | - | `ConversationListItem[]` (newest first; `status` may repeat; `batch_id` is set on calls a load test created) |
| GET | `/conversations/{id}` | - | `ConversationDetail` |
| GET | `/conversations/{id}/transcript` | - | `CanonicalConversation` (redacted) + header `X-Transcript-Source: working\|vault`; `404 {"detail": "<policy explanation>"}` when the market policy discards transcripts |
| POST | `/conversations/{id}/approve` | `ApproveRequest` (empty `{}` = the one-click path; `product_choices`, `drop_mentions`, `topic_ids`, `task_edits`, `section_edits`, `template_id`) | `ConversationDetail` (status becomes `written`; `crm_result` filled). `422` for an unknown template / product choice or when edited text contains sensitive data; `409` when nothing is pending. Approving a mention that still has no product is allowed - the note says "share class to be confirmed" |
| POST | `/conversations/{id}/reject` | `RejectRequest` | `ConversationDetail` |
| POST | `/conversations/{id}/reprocess` | `ReprocessRequest` | `ConversationDetail` (re-extracts from the working copy with another template/engine; only while pending review; `422` unknown template, `410` working copy expired; an engine that is not available falls back to rules and the proposal is flagged `engine_unavailable:<engine>`) |

Statuses: `received -> processing -> needs_review -> (approved ->) written`, or `rejected` / `failed`.
When auto-approve is enabled and the proposal is eligible the pipeline goes `processing -> written` without a human.

## Policy, templates, taxonomy, catalogue
| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/policies` | - | `PoliciesResponse` |
| PUT | `/policies/markets/{market}` | `MarketPolicy` | `PoliciesResponse` (persisted as a runtime override under `.state/`) |
| PUT | `/policies/processing` | `ProcessingPolicy` | `PoliciesResponse` |
| PUT | `/policies/notes` | `NotesPolicy` | `PoliciesResponse` |
| GET | `/policies/retention-preview?market=` | - | `RetentionPreview` |
| GET | `/templates` | - | `SummaryTemplate[]` |
| GET | `/taxonomy` | - | `Taxonomy` |
| GET | `/catalogue/stats` | - | `CatalogueStats` |
| GET | `/catalogue/search?q=&top=10` | - | `ProductSearchHit[]` |
| GET | `/aliases` | - | `LearnedAlias[]` (created by reviewer product corrections) |
| DELETE | `/aliases` | - | `{"deleted": n}` |

## CRM view, vault, retention
| Method | Path | Returns |
|---|---|---|
| GET | `/crm/records?entity=phonecall\|task\|insight\|account\|contact\|opportunity&limit=` | `dict[]` rows (simulator tables, or recent Dataverse rows) |
| GET | `/crm/health` | adapter health |
| GET | `/vault/items?conversation_id=` | `VaultItem[]` |
| POST | `/retention/sweep` | `SweepResponse` (deletes everything past its `expires_at`) |

## Metrics and evaluation
| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/metrics/summary` | - | `MetricsSummary` |
| GET | `/metrics/pipeline` | - | `RunSnapshot` - live figures for the current (or most recent) batch: `active`, `submitted`, `queued`, `in_flight`, `completed`, `failed`, `calls_per_minute`, `recent_calls_per_minute`, `eta_seconds`, `queue_wait_seconds` / `end_to_end_seconds` and `stages_ms` (`SeriesSummary`: count, mean, p50, p95, max, errors), tokens, `cost_usd`, `engines`, `fallbacks`, top `errors`, lifetime totals. A batch starts after 10 s of idleness. In memory: no database scan, safe to poll every second |
| POST | `/eval/run` | `EvalRunRequest` | `EvalRun` (runs the golden set through the real pipeline, writing to an isolated in-memory CRM) |
| GET | `/eval/runs` | - | `EvalRun[]` (newest first, without per-case details) |
| GET | `/eval/runs/{run_id}` | - | `EvalRun` |

## Live demo (load test, service check, demo reset)
The Live demo page's backend (`pipeline/load.py`, `pipeline/demo_reset.py`, `api/routes/live.py`, `api/routes/demo.py`). A run is
in-process, one at a time, and every call it creates carries a batch tag (`ConversationRecord.batch_id = "load-<run id>"`).

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/live/load` | - | `LoadStatus` - `state` (`idle\|preparing\|running\|stopping\|finished\|stopped\|failed`), `run_id`, `request`, `planned`, `sent`, `finished`, `failed`, `approved`, `in_flight`, `queued`, `parallel`, `window`, `estimate`, `report`, `error`, `stored_calls` (load-test calls currently stored), `max_calls`, and the live `pipeline` (`RunSnapshot`). Poll it once a second while active |
| POST | `/live/load/estimate` | `LoadRequest` | `LoadEstimate` - tokens, list-price cost (`lines` per meter), `billed` services, `parallel`, rough duration, `basis`, `warnings`. Spends nothing. `422` for what the run would refuse |
| POST | `/live/load` | `LoadRequest` | `202` `LoadStatus` - starts the run in the background; replaces the previous run's calls. `409` when a run is active, or when billed services are live and `confirm` is not `true`; `422` for more than `VOICECRM_LOAD_MAX_CALLS` calls, unknown calls, an unavailable `engine`, no recordings for a live-Speech audio run, or `auto_approve` with a live CRM |
| POST | `/live/load/stop` | - | `LoadStatus` - stops sending; calls already in the pipeline finish, unsent ones are dropped (`state: stopping`, then `stopped`) |
| DELETE | `/live/load/calls` | - | `{"deleted": n}` - removes every load-test call from the state store, the vault and (local only) the CRM simulator; `409` while a run is active |
| GET | `/demo/data` | - | `DemoData` - what a reset would remove right now: `conversations`, `crm_phone_calls`, `crm_tasks`, `crm_insights`, `vault_items`, `learned_aliases`, `evaluation_runs`, `policy_changes`; plus `can_reset`, `reason` (why not), `in_flight` and `crm` (the adapter). Removes nothing |
| POST | `/demo/reset` | `{"confirm": true}` | `DemoCounts` - what was removed. Clears every conversation (with events, usage and feedback), every vault copy, what the pipeline wrote to the CRM simulator, learned aliases, evaluation runs, saved policy changes and the in-memory run figures (including the Phone line card's finished calls). Keeps the catalogue, taxonomy, templates, the CRM accounts, contacts and opportunities, the recordings and the lists of Teams meetings and phone events already handled. `409` without `confirm: true`, while calls are being processed or a load test runs, and against a real Dataverse CRM (local simulator only) |
| POST | `/live/preflight` | `{"only": ["speech","language","model","search","vault","crm","teams","phone"]?}` | `{"checks": [{name, status: pass\|warn\|fail\|skip, detail, hint, seconds}], "readiness": [{scenario, verdict, why}], "seconds"}` - the preflight CLI's probes against the services this API is running on (synthetic data only; a few cents at most); `readiness` holds scenarios A to D. `409` while another check runs |

`LoadRequest`: `count` (1..`max_calls`), `via` (`transcript\|audio`), `rate` (calls per second, 0 = burst), `auto_approve`
(approves the calls with no review reason, as the policy would, with zero clicks; local CRM only), `engine?`, `fixtures[]`,
`channels[]`, `asr_noise`, `seed`, `parallel?` (1..256; default 32 with live services), `confirm`. `LoadReport` adds wall time,
`calls_per_minute`, `latency_p50_seconds` / `latency_p95_seconds`, `by_status`, `review_reasons`, `straight_through`, `approved`,
`cost_usd`, `cost_per_call_usd`, `projections` with their `projection_basis`, and the final `pipeline` snapshot.

## Frontend notes
- Poll `GET /conversations` (2 s) and `GET /conversations/{id}` (1 s while `status` is `received|processing`); there is no websocket/SSE.
  The Inbox also polls `GET /metrics/pipeline` (1 s while a batch is active, 10 s idle) and the Sources page `GET /sources/teams` (4 s).
  The Live demo page polls `GET /live/load` (1 s while a run is active, 10 s idle; the Inbox banner reads `stored_calls` from it).
- The review screen's primary action is `POST /conversations/{id}/approve` with `{}`; every edit is optional (`ApproveRequest`).
- Never display or request transcript text unless `ConversationDetail.transcript_available` is true.
