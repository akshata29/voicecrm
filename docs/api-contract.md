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
| POST | `/ingest/upload` | multipart: `file` (audio `.wav/.mp3/.m4a/.ogg/.webm`, or transcript `.vtt` / `.json`), plus form fields `channel` (teams\|zoom\|genesys\|mobile\|upload), `market`, `business_line`, `call_type?`, `seller_name`, `seller_email`, `customer_name?`, `customer_org?`, `customer_email?`, `language_hint?`, `title?`, `started_at?`, `source_interaction_id?`, `fixture_id?` | `IngestResponse` |
| GET | `/fixtures` | - | `FixtureInfo[]` (scripted demo calls; `recording_seconds` is set when a real speech recording exists in `data/audio/live`) |
| GET | `/fixtures/{fixture_id}/audio` | - | the real recording (`audio/wav`) so the UI can play it; `404` when there is none (`scripts\make_audio.bat` creates them) |
| POST | `/ingest/simulate` | `SimulateRequest` | `IngestResponse` - renders the scripted call as the channel's native payload (Teams VTT, Zoom VTT/JSON, Genesys transcript JSON, or the real recording / an audio sidecar) and ingests it through the real adapter path |
| POST | `/webhooks/zoom` | Zoom webhook JSON (handles `endpoint.url_validation`; HMAC signature checked when `VOICECRM_ZOOM_WEBHOOK_SECRET` is set) | `202 {"status":"accepted",...}` / validation response; `400` not a JSON object, `422` unrecognised payload, `401` bad signature |
| POST | `/webhooks/genesys` | Genesys notification JSON | `202 {...}`; `400` / `422` as above |
| POST | `/webhooks/teams` | Graph change notification (handles `validationToken`; notifications whose `clientState` differs from `VOICECRM_TEAMS_CLIENT_STATE` are dropped) | `202 {"status","transcripts","ingesting","note"}`; with live Teams ingestion on, the transcripts are fetched and ingested in the background; `400` / `422` as above |
| POST | `/webhooks/teams/lifecycle` | Graph lifecycle notification (handles `validationToken`; `reauthorizationRequired` renews the subscription) | `202` |
| GET | `/sources/teams` | - | `TeamsStatus` (mode, health, organisers, last poll / ingest, counters, recent transcripts; `mode: "off"` when live ingestion is not enabled) |
| POST | `/sources/teams/poll` | - | `{"ingested": n, "status": TeamsStatus}` - look for new transcripts now; `409` when off or misconfigured, `502` with the fix when Graph refuses |

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

## Live demo (load test and service check)
The Live demo page's backend (`pipeline/load.py`, `api/routes/live.py`). A run is in-process, one at a time, and every call it
creates carries a batch tag (`ConversationRecord.batch_id = "load-<run id>"`).

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/live/load` | - | `LoadStatus` - `state` (`idle\|preparing\|running\|stopping\|finished\|stopped\|failed`), `run_id`, `request`, `planned`, `sent`, `finished`, `failed`, `approved`, `in_flight`, `queued`, `parallel`, `window`, `estimate`, `report`, `error`, `stored_calls` (load-test calls currently stored), `max_calls`, and the live `pipeline` (`RunSnapshot`). Poll it once a second while active |
| POST | `/live/load/estimate` | `LoadRequest` | `LoadEstimate` - tokens, list-price cost (`lines` per meter), `billed` services, `parallel`, rough duration, `basis`, `warnings`. Spends nothing. `422` for what the run would refuse |
| POST | `/live/load` | `LoadRequest` | `202` `LoadStatus` - starts the run in the background; replaces the previous run's calls. `409` when a run is active, or when billed services are live and `confirm` is not `true`; `422` for more than `VOICECRM_LOAD_MAX_CALLS` calls, unknown calls, an unavailable `engine`, no recordings for a live-Speech audio run, or `auto_approve` with a live CRM |
| POST | `/live/load/stop` | - | `LoadStatus` - stops sending; calls already in the pipeline finish, unsent ones are dropped (`state: stopping`, then `stopped`) |
| DELETE | `/live/load/calls` | - | `{"deleted": n}` - removes every load-test call from the state store, the vault and (local only) the CRM simulator; `409` while a run is active |
| POST | `/live/preflight` | `{"only": ["speech","language","model","search","vault","crm","teams"]?}` | `{"checks": [{name, status: pass\|warn\|fail\|skip, detail, hint, seconds}], "readiness": [{scenario, verdict, why}], "seconds"}` - the preflight CLI's probes against the services this API is running on (synthetic data only; a few cents at most). `409` while another check runs |

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
