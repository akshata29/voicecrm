/** Reference-data and admin endpoints for the mock API: config, policies, CRM view, vault, metrics. */
import type {
  ConfigResponse,
  CrmHealth,
  CrmRow,
  DeletedCount,
  LearnedAlias,
  MarketPolicy,
  MetricsSummary,
  NotesPolicy,
  PipelineStats,
  PoliciesResponse,
  ProcessingPolicy,
  RetentionPreview,
  SweepResponse,
  TeamsStatus,
  VaultItem,
} from '../types';
import { CHANNELS } from '../types';
import { PRODUCTS, catalogueStats } from './catalogue';
import { marketOf, retentionDecision } from './proposal';
import { MODES, SEED_ACCOUNTS, SEED_CONTACTS, SEED_OPPORTUNITIES, SEED_USERS, TEMPLATE_INFOS } from './seed';
import { db, HttpError, iso } from './state';
import { tick } from './pipeline';

const BUSINESS_LINES = ['professional_buyer', 'retail', 'international'];

export function configResponse(): ConfigResponse {
  const state = db();
  return {
    app_version: '0.1.0-mock',
    demo_mode: true,
    modes: structuredClone(MODES),
    markets: structuredClone(state.policies.markets),
    templates: structuredClone(TEMPLATE_INFOS),
    business_lines: BUSINESS_LINES,
    channels: [...CHANNELS],
    engine: state.policies.processing.engine,
    catalogue_products: PRODUCTS.length,
    catalogue_synthetic: true,
    pricing_illustrative: true,
    llm_deployment: null,
  };
}

// ---- policies --------------------------------------------------------------------------------------
export function policies(): PoliciesResponse {
  return db().policies;
}

export function updateMarket(market: string, body: MarketPolicy): PoliciesResponse {
  const state = db();
  if (body.market !== market) throw new HttpError(422, `Body market "${body.market}" does not match the path "${market}"`);
  if (body.transcript_retention === 'on' && (!body.transcript_retention_days || body.transcript_retention_days < 1)) {
    throw new HttpError(422, 'transcript_retention_days is required (and must be positive) when transcript retention is on');
  }
  if (body.audio_retention_days < 0) throw new HttpError(422, 'audio_retention_days cannot be negative');
  const next: MarketPolicy = { ...body, transcript_retention_days: body.transcript_retention === 'on' ? body.transcript_retention_days : null };
  const index = state.policies.markets.findIndex((item) => item.market === market);
  if (index === -1) state.policies.markets.push(next);
  else state.policies.markets[index] = next;
  return state.policies;
}

export function updateProcessing(body: ProcessingPolicy): PoliciesResponse {
  if (body.auto_approve_min_confidence < 0 || body.auto_approve_min_confidence > 1) throw new HttpError(422, 'auto_approve_min_confidence must be between 0 and 1');
  if (body.product_review_threshold < 0 || body.product_review_threshold > 1) throw new HttpError(422, 'product_review_threshold must be between 0 and 1');
  if (body.top_k_products < 1 || body.top_k_products > 10) throw new HttpError(422, 'top_k_products must be between 1 and 10');
  db().policies.processing = body;
  return db().policies;
}

export function updateNotes(body: NotesPolicy): PoliciesResponse {
  db().policies.notes = body;
  return db().policies;
}

export function retentionPreview(market: string | undefined): RetentionPreview {
  if (!market) throw new HttpError(422, 'market is required');
  const state = db();
  if (!state.policies.markets.some((item) => item.market === market)) throw new HttpError(404, `Unknown market ${market}`);
  return { market, decision: retentionDecision(marketOf(state.policies, market), Date.now()) };
}

// ---- aliases ---------------------------------------------------------------------------------------
export function aliases(): LearnedAlias[] {
  return [...db().aliases].sort((left, right) => right.count - left.count || Date.parse(right.created_at) - Date.parse(left.created_at));
}

export function resetAliases(): DeletedCount {
  const state = db();
  const deleted = state.aliases.length;
  state.aliases = [];
  return { deleted };
}

export function stats() {
  return catalogueStats(db().aliases.length);
}

// ---- CRM view --------------------------------------------------------------------------------------
export function crmRecords(entity: string | undefined, limit: number): CrmRow[] {
  const state = db();
  const accountName = (id: string) => SEED_ACCOUNTS.find((account) => account.id === id)?.name ?? id;
  const take = (rows: CrmRow[]) => rows.slice(0, limit);
  switch (entity) {
    case 'phonecall':
      return take(state.crm.phonecall);
    case 'task':
      return take(state.crm.task);
    case 'insight':
      return take(state.crm.insight);
    case 'account':
      return take(SEED_ACCOUNTS.map((account) => ({ id: account.id, name: account.name, domain: account.domain, segment: account.segment })));
    case 'contact':
      return take(
        SEED_CONTACTS.map((contact) => ({
          id: contact.id,
          name: contact.name,
          email: contact.email,
          phone: contact.phone,
          title: contact.title,
          account_id: contact.account_id,
          account_name: accountName(contact.account_id),
        })),
      );
    case 'opportunity':
      return take(
        SEED_OPPORTUNITIES.map((opportunity) => ({
          id: opportunity.id,
          name: opportunity.name,
          stage: opportunity.stage,
          account_id: opportunity.account_id,
          account_name: accountName(opportunity.account_id),
          contact_id: opportunity.contact_id,
          contact_name: SEED_CONTACTS.find((contact) => contact.id === opportunity.contact_id)?.name ?? null,
        })),
      );
    default:
      throw new HttpError(422, `Unknown CRM entity "${String(entity)}" (phonecall | task | insight | account | contact | opportunity)`);
  }
}

export function crmHealth(): CrmHealth {
  const state = db();
  return {
    adapter: 'local_sim',
    ok: true,
    synthetic: true,
    path: '.state/crm_sim.sqlite',
    counts: {
      accounts: SEED_ACCOUNTS.length,
      contacts: SEED_CONTACTS.length,
      opportunities: SEED_OPPORTUNITIES.length,
      users: SEED_USERS.length,
      phonecalls: state.crm.phonecall.length,
      tasks: state.crm.task.length,
      insights: state.crm.insight.length,
    },
  };
}

// ---- vault -----------------------------------------------------------------------------------------
export function vaultItems(conversationId: string | undefined): VaultItem[] {
  tick();
  return db()
    .vault.filter((item) => !conversationId || item.conversation_id === conversationId)
    .sort((left, right) => Date.parse(right.created_at) - Date.parse(left.created_at));
}

export function sweepVault(): SweepResponse {
  const state = db();
  const now = Date.now();
  const deleted = state.vault.filter((item) => item.expires_at !== null && Date.parse(item.expires_at) <= now);
  state.vault = state.vault.filter((item) => !deleted.includes(item));
  return { deleted };
}

// ---- metrics ---------------------------------------------------------------------------------------
function percentile(values: number[], p: number): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((left, right) => left - right);
  const rank = Math.max(1, Math.ceil(p * sorted.length));
  return sorted[rank - 1] ?? null;
}

const mean = (values: number[]): number | null => (values.length === 0 ? null : values.reduce((total, value) => total + value, 0) / values.length);

export function metricsSummary(): MetricsSummary {
  tick();
  const convs = [...db().convs.values()];
  const byStatus: Record<string, number> = {};
  const byChannel: Record<string, number> = {};
  const latencies: number[] = [];
  const costs: number[] = [];
  const clicks: number[] = [];
  const seconds: number[] = [];

  for (const conv of convs) {
    byStatus[conv.record.status] = (byStatus[conv.record.status] ?? 0) + 1;
    byChannel[conv.record.meta.source] = (byChannel[conv.record.meta.source] ?? 0) + 1;
    const proposal = conv.record.proposal;
    if (proposal?.ready_at) latencies.push((Date.parse(proposal.ready_at) - Date.parse(conv.record.created_at)) / 1000);
    if (conv.cost.total > 0) costs.push(conv.cost.total);
    if (proposal?.review.state === 'approved') {
      clicks.push(proposal.review.clicks);
      if (proposal.review.seconds_to_review !== null) seconds.push(proposal.review.seconds_to_review);
    }
  }

  const round = (value: number | null, digits: number) => (value === null ? null : Math.round(value * 10 ** digits) / 10 ** digits);
  return {
    conversations: convs.length,
    by_status: byStatus,
    by_channel: byChannel,
    written: byStatus.written ?? 0,
    needs_review: byStatus.needs_review ?? 0,
    auto_approved: convs.filter((conv) => conv.record.proposal?.review.state === 'auto_approved').length,
    latency_p50_seconds: round(percentile(latencies, 0.5), 1),
    latency_p95_seconds: round(percentile(latencies, 0.95), 1),
    mean_cost_usd: round(mean(costs), 4),
    mean_clicks_to_approve: round(mean(clicks), 2),
    mean_seconds_to_review: round(mean(seconds), 1),
    cost_illustrative: true,
  };
}

export const nowIso = (): string => iso(Date.now());

/** The offline mock never talks to Microsoft Graph: live Teams ingestion is reported as off. */
export function teamsStatus(): TeamsStatus {
  return {
    mode: 'off', configured: false, organizers: [], poll_seconds: 0, notification_url: null, last_poll_at: null, last_poll_ok: null,
    last_error: null, last_ingest_at: null, ingested: 0, duplicates: 0, failed: 0, recent: [],
    problem: 'Live Teams ingestion is off. Set VOICECRM_TEAMS_MODE=poll and the VOICECRM_TEAMS_* app settings in .env, then restart (see docs/live-demo.md, scenario B).',
  };
}

const EMPTY_SERIES = { count: 0, mean: null, p50: null, p95: null, max: null, errors: 0 };

/** The mock has no queue: report an idle pipeline, so the live-load card stays hidden offline. */
export function pipelineStats(): PipelineStats {
  return {
    active: false, started_at: null, max_concurrency: 8, submitted: 0, queued: 0, in_flight: 0, completed: 0, failed: 0,
    elapsed_seconds: 0, calls_per_minute: null, recent_calls_per_minute: null, eta_seconds: null,
    queue_wait_seconds: EMPTY_SERIES, end_to_end_seconds: EMPTY_SERIES, stages_ms: {}, tokens_in: 0, tokens_out: 0,
    cost_usd: 0, cost_illustrative: true, engines: {}, fallbacks: 0, errors: [], lifetime_completed: 0, lifetime_failed: 0,
  };
}
