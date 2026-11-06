/**
 * Wire types for /api/v1. They mirror, field for field, backend/voicecrm/api/schemas.py and
 * backend/voicecrm/domain/*.py (snake_case, enums as their string values, datetimes as ISO-8601 UTC strings,
 * `date` values as "YYYY-MM-DD"). Do not rename or reshape anything here: change the backend contract first.
 */

export type IsoDateTime = string;
export type IsoDate = string;

// ---- enums (domain/enums.py) -----------------------------------------------------------------------
export type Channel = 'teams' | 'zoom' | 'genesys' | 'mobile' | 'upload';
export const CHANNELS: readonly Channel[] = ['teams', 'zoom', 'genesys', 'mobile', 'upload'];

export type Direction = 'inbound' | 'outbound' | 'meeting' | 'in_person';
export type SpeakerRole = 'seller' | 'customer' | 'internal' | 'unknown';
export type TranscriptOrigin =
  | 'azure_speech'
  | 'teams_platform'
  | 'zoom_platform'
  | 'genesys_platform'
  | 'fixture'
  | 'manual';

export type ConversationStatus =
  | 'received'
  | 'processing'
  | 'needs_review'
  | 'approved'
  | 'written'
  | 'rejected'
  | 'failed';
export const CONVERSATION_STATUSES: readonly ConversationStatus[] = [
  'received',
  'processing',
  'needs_review',
  'approved',
  'written',
  'rejected',
  'failed',
];

export type Stage =
  | 'ingest'
  | 'transcribe'
  | 'normalize'
  | 'policy'
  | 'extract'
  | 'resolve'
  | 'validate'
  | 'review'
  | 'write'
  | 'retention';
export const STAGES: readonly Stage[] = [
  'ingest',
  'transcribe',
  'normalize',
  'policy',
  'extract',
  'resolve',
  'validate',
  'review',
  'write',
  'retention',
];

export type StageStatus = 'started' | 'completed' | 'skipped' | 'failed';
export type MentionType = 'interest' | 'existing_holding' | 'comparison' | 'objection' | 'info_only';
export type OwnerRole = 'seller' | 'customer' | 'internal' | 'unknown';
export type Engine = 'rules' | 'llm' | 'hybrid';
export const ENGINES: readonly Engine[] = ['rules', 'llm', 'hybrid'];

// ---- domain/conversation.py ------------------------------------------------------------------------
export interface Participant {
  id: string;
  display_name: string;
  email: string | null;
  phone: string | null;
  role: SpeakerRole;
  org: string | null;
}

export interface ConsentInfo {
  status: string; // recorded_with_consent | unknown | not_required | withheld
  basis: string | null;
}

export interface Segment {
  id: number;
  speaker_id: string;
  start_ms: number;
  end_ms: number;
  text: string;
  locale: string | null;
  confidence: number | null;
}

export interface ConversationMeta {
  conversation_id: string;
  source: Channel;
  source_interaction_id: string;
  direction: Direction;
  title: string | null;
  started_at: IsoDateTime;
  ended_at: IsoDateTime | null;
  duration_seconds: number | null;
  seller: Participant;
  participants: Participant[];
  customer_org: string | null;
  market: string;
  business_line: string;
  call_type: string | null;
  language_hint: string | null;
  consent: ConsentInfo;
}

export interface ArtifactRefs {
  audio_uri: string | null;
  raw_transcript_uri: string | null;
  source_summary: string | null;
}

export interface CanonicalConversation {
  meta: ConversationMeta;
  segments: Segment[];
  language: string;
  transcript_origin: TranscriptOrigin;
  artifacts: ArtifactRefs;
}

// ---- domain/extraction.py --------------------------------------------------------------------------
export interface Evidence {
  segment_ids: number[];
  quote: string;
}

export interface ProductCandidate {
  product_id: string;
  display_name: string;
  fund_name: string;
  share_class: string;
  score: number;
  matched_on: string[];
}

export interface ProductMention {
  mention_id: string;
  surface: string;
  evidence: Evidence[];
  candidates: ProductCandidate[];
  chosen_product_id: string | null;
  confidence: number;
  mention_type: MentionType;
  share_class_stated: string | null;
  needs_review: boolean;
  reasons: string[];
}

export interface TopicTag {
  topic_id: string;
  label: string;
  confidence: number;
  evidence: Evidence[];
}

export interface TaskItem {
  task_id: string;
  title: string;
  description: string | null;
  owner_role: OwnerRole;
  owner_ref: string | null;
  due_date: IsoDate | null;
  due_text: string | null;
  due_basis: 'stated' | 'inferred' | 'none';
  confidence: number;
  evidence: Evidence[];
}

export interface NoteSection {
  key: string;
  title: string;
  text: string | null;
  items: string[];
}

export interface RelationshipFact {
  text: string;
  category: string;
  evidence: Evidence[];
}

export type PolicyActionKind = 'pii_redacted' | 'omitted_sensitive' | 'dropped_small_talk' | 'kept_relationship';

/** Audit trail of what the notes policy did: categories and segment ids only, never the omitted text. */
export interface PolicyAction {
  kind: PolicyActionKind;
  category: string | null;
  segment_ids: number[];
  detail: string;
}

export interface ExtractionResult {
  template_id: string;
  language_out: string;
  sections: NoteSection[];
  products: ProductMention[];
  topics: TopicTag[];
  tasks: TaskItem[];
  relationship_facts: RelationshipFact[];
  flags: string[];
  overall_confidence: number;
  engine: Engine;
  model: string | null;
}

export interface ValidationFinding {
  severity: 'error' | 'warning';
  code: string;
  message: string;
  where: string | null;
}

export interface ValidationReport {
  passed: boolean;
  findings: ValidationFinding[];
  leak_count: number;
  unsupported_claims: number;
}

// ---- domain/crm.py ---------------------------------------------------------------------------------
export interface CrmRecordRef {
  entity: string; // account | contact | opportunity | phonecall | task | insight | systemuser
  id: string;
  name: string | null;
}

export interface CrmMatch {
  account: CrmRecordRef | null;
  contact: CrmRecordRef | null;
  opportunity: CrmRecordRef | null;
  confidence: number;
  method: string; // email | name_org | meeting_metadata | manual | none
  ambiguous: boolean;
  candidates: CrmRecordRef[];
}

export interface PhoneCallPayload {
  subject: string;
  description: string;
  direction: 'inbound' | 'outbound';
  start: IsoDateTime;
  end: IsoDateTime | null;
  duration_minutes: number | null;
  regarding: CrmRecordRef | null;
  to_contact: CrmRecordRef | null;
  owner_email: string | null;
}

export interface TaskPayload {
  source_task_id: string;
  subject: string;
  description: string | null;
  due: IsoDate | null;
  regarding: CrmRecordRef | null;
  owner_email: string | null;
  priority: 'low' | 'normal' | 'high';
}

export interface InsightPayload {
  source: Channel;
  source_interaction_id: string;
  template_id: string;
  language: string;
  summary_markdown: string;
  sections: NoteSection[];
  product_ids: string[];
  topic_ids: string[];
  relationship_facts: string[];
  engine: string;
  model: string | null;
  confidence: number;
  review_status: 'approved' | 'auto_approved';
  transcript_pointer: string | null; // pointer into the vault when a transcript is retained; never the text
}

export interface CrmPackage {
  idempotency_key: string;
  conversation_id: string;
  regarding: CrmMatch;
  phone_call: PhoneCallPayload;
  tasks: TaskPayload[];
  insight: InsightPayload;
}

export interface CrmWriteResult {
  ok: boolean;
  created: CrmRecordRef[];
  skipped_duplicate: boolean;
  degraded_fields: string[];
  error: string | null;
}

// ---- domain/pipeline.py ----------------------------------------------------------------------------
export interface StageEvent {
  conversation_id: string;
  stage: Stage;
  status: StageStatus;
  ts: IsoDateTime;
  duration_ms: number | null;
  detail: Record<string, unknown>;
}

export interface RetentionDecision {
  market: string;
  transcript_stored: boolean;
  transcript_redacted: boolean;
  transcript_expires_at: IsoDateTime | null;
  audio_expires_at: IsoDateTime | null; // null = audio deleted immediately after transcription
  crm_contains_transcript: boolean; // invariant: always false
  explanation: string;
}

export type ReviewState = 'pending' | 'approved' | 'auto_approved' | 'rejected';

export interface ReviewInfo {
  state: ReviewState;
  reasons: string[]; // why a human is needed, e.g. "product_needs_pick"
  auto_approve_eligible: boolean;
  reviewed_at: IsoDateTime | null;
  reviewed_by: string | null;
  clicks: number;
  seconds_to_review: number | null;
}

export interface Proposal {
  template_id: string;
  note_markdown: string;
  result: ExtractionResult;
  validation: ValidationReport;
  crm_match: CrmMatch;
  crm_preview: CrmPackage | null;
  review: ReviewInfo;
  policy_actions: PolicyAction[];
  ready_at: IsoDateTime | null;
}

export interface TaskEdit {
  task_id: string;
  title?: string | null;
  due_date?: IsoDate | null;
  drop?: boolean;
}

/** Reviewer decision. Every field is optional: the happy path is an empty body (one click). */
export interface ApproveRequest {
  product_choices?: Record<string, string>; // mention_id -> product_id
  drop_mentions?: string[];
  topic_ids?: string[] | null;
  task_edits?: TaskEdit[];
  section_edits?: Record<string, string>;
  template_id?: string | null;
  clicks?: number;
}

export interface ConversationRecord {
  meta: ConversationMeta;
  status: ConversationStatus;
  language: string | null;
  transcript_origin: TranscriptOrigin | null;
  engine: string | null;
  template_id: string | null;
  audio_seconds: number | null;
  proposal: Proposal | null;
  retention: RetentionDecision | null;
  crm_result: CrmWriteResult | null;
  error: string | null;
  fixture_id: string | null;
  batch_id: string | null; // set on calls a load test created (Live demo page)
  version: number;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
}

// ---- domain/metering.py ----------------------------------------------------------------------------
export type UsageService = 'speech' | 'llm' | 'language_pii' | 'search' | 'embeddings';

export interface UsageRecord {
  conversation_id: string;
  stage: Stage;
  service: UsageService;
  provider: string;
  model: string | null;
  audio_seconds: number | null;
  tokens_in: number | null;
  tokens_out: number | null;
  units: number | null;
  latency_ms: number | null;
  ts: IsoDateTime;
}

export interface CostLine {
  service: string;
  quantity: number;
  unit: string;
  est_cost: number;
}

export interface CostBreakdown {
  lines: CostLine[];
  total: number;
  currency: string;
  illustrative: boolean;
}

// ---- domain/policy.py ------------------------------------------------------------------------------
export type TranscriptRetention = 'on' | 'off';

export interface MarketPolicy {
  market: string;
  display_name: string;
  transcript_retention: TranscriptRetention;
  transcript_retention_days: number | null;
  store_redacted_only: boolean;
  audio_retention_days: number;
  summary_retention_days: number | null;
  notes: string | null;
}

export interface NotesPolicy {
  omit_categories: string[];
  keep_relationship_context: boolean;
  drop_small_talk: boolean;
}

export interface ProcessingPolicy {
  engine: Engine;
  auto_approve_enabled: boolean;
  auto_approve_min_confidence: number;
  auto_approve_requires_crm_match: boolean;
  product_review_threshold: number;
  top_k_products: number;
  transcription_source_preference: Record<string, string[]>; // channel -> ordered: "platform" | "azure_speech"
  default_template_by_business_line: Record<string, string>;
}

// ---- domain/templates.py, taxonomy.py --------------------------------------------------------------
export interface TemplateSection {
  key: string;
  title: string;
  kind: 'text' | 'list';
  instruction: string;
  max_items: number | null;
  required: boolean;
  crm_field: string | null;
}

export interface SummaryTemplate {
  template_id: string;
  name: string;
  description: string;
  business_lines: string[];
  call_types: string[];
  language_out: string;
  include_relationship_context: boolean;
  tone: string;
  max_words: number;
  sections: TemplateSection[];
}

export interface Topic {
  topic_id: string;
  label: string;
  description: string | null;
  labels_i18n: Record<string, string>;
  synonyms: string[];
  synonyms_i18n: Record<string, string[]>;
  parent_id: string | null;
  business_lines: string[];
}

export interface Taxonomy {
  version: string;
  synthetic: boolean;
  topics: Topic[];
}

// ---- domain/catalogue.py ---------------------------------------------------------------------------
export interface LearnedAlias {
  surface: string;
  product_id: string;
  count: number;
  source: string;
  created_at: IsoDateTime;
}

// ---- domain/evaluation.py --------------------------------------------------------------------------
export interface EvalCaseResult {
  fixture_id: string;
  title: string;
  tags: string[];
  product_top1_correct: number;
  product_top3_hits: number;
  product_gold: number;
  product_false_tags: number;
  share_class_confusions: number;
  topic_tp: number;
  topic_fp: number;
  topic_fn: number;
  task_tp: number;
  task_fp: number;
  task_fn: number;
  leaks: number;
  kept_relationship: number;
  kept_relationship_gold: number;
  wer: number | null;
  latency_ms: number | null;
  cost_usd: number | null;
  needs_review: boolean;
  details: Record<string, unknown>;
}

export interface EvalMetrics {
  cases: number;
  product_top1: number;
  product_top3: number;
  product_false_tag_rate: number;
  share_class_confusion_rate: number;
  topic_precision: number;
  topic_recall: number;
  topic_f1: number;
  task_precision: number;
  task_recall: number;
  privacy_leaks: number;
  relationship_kept_rate: number;
  mean_wer: number | null;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  mean_cost_usd: number | null;
  auto_approvable_rate: number;
}

export interface EvalRun {
  run_id: string;
  created_at: IsoDateTime;
  engine: string;
  with_asr_noise: boolean;
  learned_aliases: number;
  label: string | null;
  metrics: EvalMetrics;
  cases: EvalCaseResult[];
}

// ---- domain/fixtures.py (only the part the API exposes) -------------------------------------------
export interface FixtureInfo {
  fixture_id: string;
  title: string;
  description: string;
  channel: Channel;
  language: string;
  market: string;
  business_line: string;
  call_type: string | null;
  tags: string[];
  duration_minutes: number;
  turns: number;
  /** Length of the real speech recording (tools/gen_call_audio.py); null or absent when there is none. */
  recording_seconds?: number | null;
}

// ---- ports/vault.py --------------------------------------------------------------------------------
export type VaultKind = 'audio' | 'transcript' | 'working';

export interface VaultItem {
  conversation_id: string;
  kind: VaultKind;
  uri: string;
  created_at: IsoDateTime;
  expires_at: IsoDateTime | null;
  size_bytes: number;
  redacted: boolean;
  filename: string | null;
}

// ---- api/schemas.py --------------------------------------------------------------------------------
export type ServiceKey = 'transcription' | 'llm' | 'search' | 'pii' | 'crm' | 'vault';
export const SERVICE_KEYS: readonly ServiceKey[] = ['transcription', 'llm', 'search', 'pii', 'crm', 'vault'];

export interface ResolvedMode {
  service: string;
  requested: string;
  effective: string;
  live: boolean;
  reason: string | null;
}

export interface TemplateInfo {
  template_id: string;
  name: string;
  description: string;
  business_lines: string[];
  call_types: string[];
  sections: string[]; // section titles
}

export interface ConfigResponse {
  app_version: string;
  demo_mode: boolean;
  modes: Record<string, ResolvedMode>; // transcription | llm | search | pii | crm | vault
  markets: MarketPolicy[];
  templates: TemplateInfo[];
  business_lines: string[];
  channels: string[];
  engine: string; // rules | llm | hybrid (effective)
  catalogue_products: number;
  catalogue_synthetic: boolean;
  pricing_illustrative: boolean;
  llm_deployment: string | null; // the model deployment in use; only when the model is live
}

export interface ConversationListItem {
  conversation_id: string;
  status: ConversationStatus;
  source: Channel;
  market: string;
  business_line: string;
  language: string | null;
  template_id: string | null;
  title: string | null;
  seller_name: string;
  customer_org: string | null;
  started_at: IsoDateTime;
  created_at: IsoDateTime;
  product_count: number;
  topic_count: number;
  task_count: number;
  needs_review: boolean;
  review_reasons: string[];
  auto_approve_eligible: boolean;
  latency_seconds: number | null; // ingest -> review-ready
  cost_usd: number | null;
  fixture_id: string | null;
  batch_id: string | null; // set on calls created by a load test
}

export interface StageRecordView {
  stage: Stage;
  status: StageStatus;
  started_at: IsoDateTime | null;
  duration_ms: number | null;
  detail: Record<string, unknown>;
}

export interface ConversationDetail {
  record: ConversationRecord;
  stages: StageRecordView[];
  events: StageEvent[];
  usage: UsageRecord[];
  cost: CostBreakdown;
  transcript_available: boolean;
  transcript_note: string; // why it is / is not available (market policy explanation)
  vault_items: VaultItem[];
}

export interface IngestResponse {
  conversation_id: string;
  duplicate_of: string | null; // set when the same (source, source_interaction_id) was already ingested
}

export type SimulateVia = 'transcript' | 'audio';

export interface SimulateRequest {
  fixture_id: string;
  channel?: Channel | null;
  market?: string | null;
  business_line?: string | null;
  template_id?: string | null;
  via?: SimulateVia;
  asr_noise?: boolean;
}

export interface ReprocessRequest {
  template_id?: string | null;
  engine?: Engine | null;
}

export interface RejectRequest {
  reason?: string | null;
}

export interface PoliciesResponse {
  markets: MarketPolicy[];
  notes: NotesPolicy;
  processing: ProcessingPolicy;
}

export interface CatalogueStats {
  products: number;
  families: number;
  share_classes: Record<string, number>;
  asset_classes: Record<string, number>;
  synthetic: boolean;
  note: string;
  learned_aliases: number;
}

export interface ProductSearchHit {
  product_id: string;
  display_name: string;
  fund_name: string;
  share_class: string;
  asset_class: string;
  score: number;
}

export interface MetricsSummary {
  conversations: number;
  by_status: Record<string, number>;
  by_channel: Record<string, number>;
  written: number;
  needs_review: number;
  auto_approved: number;
  latency_p50_seconds: number | null;
  latency_p95_seconds: number | null;
  mean_cost_usd: number | null;
  mean_clicks_to_approve: number | null;
  mean_seconds_to_review: number | null;
  cost_illustrative: boolean;
}

/** GET /metrics/pipeline - live figures for the current (or most recent) batch of calls (pipeline/stats.py). */
export interface SeriesSummary {
  count: number;
  mean: number | null;
  p50: number | null;
  p95: number | null;
  max: number | null;
  errors: number;
}

export interface PipelineErrorCount {
  error: string;
  count: number;
}

export interface PipelineStats {
  active: boolean;
  started_at: IsoDateTime | null;
  max_concurrency: number;
  submitted: number;
  queued: number;
  in_flight: number;
  completed: number;
  failed: number;
  elapsed_seconds: number;
  calls_per_minute: number | null;
  recent_calls_per_minute: number | null;
  eta_seconds: number | null;
  queue_wait_seconds: SeriesSummary;
  end_to_end_seconds: SeriesSummary;
  stages_ms: Record<string, SeriesSummary>;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  cost_illustrative: boolean;
  engines: Record<string, number>;
  fallbacks: number;
  errors: PipelineErrorCount[];
  lifetime_completed: number;
  lifetime_failed: number;
}

/** GET /sources/teams - live Microsoft Teams ingestion through Graph (pipeline/teams_live.py). */
export type TeamsMode = 'off' | 'poll' | 'webhook';

export interface TeamsRecent {
  transcript_id: string;
  title: string | null;
  conversation_id: string | null;
  at: IsoDateTime;
  outcome: 'ingested' | 'duplicate' | 'failed';
  detail: string | null;
}

export interface TeamsStatus {
  mode: TeamsMode;
  configured: boolean;
  problem: string | null;
  organizers: string[];
  poll_seconds: number;
  notification_url: string | null;
  last_poll_at: IsoDateTime | null;
  last_poll_ok: boolean | null;
  last_error: string | null;
  last_ingest_at: IsoDateTime | null;
  ingested: number;
  duplicates: number;
  failed: number;
  recent: TeamsRecent[];
}

export interface TeamsPollResult {
  ingested: number;
  status: TeamsStatus;
}

/** /live/* - the Live demo page: a load test run from the UI, and the in-app service check (pipeline/load.py). */
export type LoadVia = 'transcript' | 'audio';
export type LoadState = 'idle' | 'preparing' | 'running' | 'stopping' | 'finished' | 'stopped' | 'failed';

export interface LoadRequest {
  count: number;
  via?: LoadVia;
  rate?: number; // calls per second; 0 = as fast as the pipeline accepts them
  auto_approve?: boolean;
  engine?: Engine | null;
  fixtures?: string[];
  channels?: Channel[];
  asr_noise?: boolean;
  seed?: number;
  parallel?: number | null; // null = chosen from the live services
  confirm?: boolean; // the caller accepts that billed services will be used
}

export interface LoadEstimate {
  calls: number;
  via: string;
  engine: string;
  parallel: number;
  pool: number; // distinct scripted calls the plan cycles through
  speech_hours: number;
  pii_records: number;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  lines: CostLine[];
  billed: string[]; // live services this run is charged for
  seconds: number;
  calls_per_minute: number;
  basis: string;
  warnings: string[];
  illustrative: boolean;
}

export interface LoadProjection {
  calls: number;
  seconds: number;
}

export interface LoadReport {
  calls: number;
  finished: number;
  failed: number;
  wall_seconds: number;
  calls_per_minute: number | null;
  parallel: number;
  by_status: Record<string, number>;
  review_reasons: Record<string, number>;
  straight_through: number;
  approved: number;
  latency_p50_seconds: number | null;
  latency_p95_seconds: number | null;
  cost_usd: number;
  cost_per_call_usd: number | null;
  projections: LoadProjection[];
  projection_basis: string; // what the projections assume, e.g. "32 in parallel at about 8.0 s per call"
  errors: PipelineErrorCount[];
  modes: Record<string, string>;
  stopped_early: boolean;
  planned: number;
  pipeline: PipelineStats;
}

export interface LoadStatus {
  state: LoadState;
  run_id: string | null;
  request: LoadRequest | null;
  started_at: IsoDateTime | null;
  finished_at: IsoDateTime | null;
  planned: number;
  sent: number;
  finished: number;
  failed: number;
  approved: number;
  in_flight: number;
  queued: number;
  parallel: number;
  window: number;
  estimate: LoadEstimate | null;
  report: LoadReport | null;
  error: string | null;
  stored_calls: number; // load-test calls currently in the inbox
  max_calls: number;
  pipeline: PipelineStats;
}

export type PreflightProbe = 'speech' | 'language' | 'model' | 'search' | 'vault' | 'crm' | 'teams';
export type PreflightStatus = 'pass' | 'warn' | 'fail' | 'skip';

export interface PreflightCheck {
  name: string;
  status: PreflightStatus;
  detail: string;
  hint: string;
  seconds: number | null;
}

export interface PreflightReadiness {
  scenario: string;
  verdict: string; // READY | NOT READY | NOT CONFIGURED
  why: string;
}

export interface PreflightResponse {
  checks: PreflightCheck[];
  readiness: PreflightReadiness[];
  seconds: number;
}

export interface EvalRunRequest {
  engine?: string | null;
  with_asr_noise?: boolean;
  apply_learning?: boolean;
  label?: string | null;
}

export interface SweepResponse {
  deleted: VaultItem[];
}

export interface RetentionPreview {
  market: string;
  decision: RetentionDecision;
}

// ---- endpoints whose bodies the contract describes only loosely ------------------------------------
export interface HealthResponse {
  status: string;
}

export type CrmEntity = 'phonecall' | 'task' | 'insight' | 'account' | 'contact' | 'opportunity';
export const CRM_ENTITIES: readonly CrmEntity[] = ['phonecall', 'task', 'insight', 'account', 'contact', 'opportunity'];

/** `GET /crm/records` returns untyped rows (simulator tables or recent Dataverse rows). */
export type CrmRow = Record<string, unknown>;

export type CrmHealth = Record<string, unknown>;

export interface DeletedCount {
  deleted: number;
}

export type TranscriptSource = 'working' | 'vault';

export interface TranscriptResponse {
  transcript: CanonicalConversation;
  source: TranscriptSource | null; // from the X-Transcript-Source response header
}
