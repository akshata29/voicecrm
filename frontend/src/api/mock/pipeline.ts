/** Conversation lifecycle for the mock API: create, advance through the pipeline, approve, reject, reprocess. */
import type {
  ApproveRequest,
  CanonicalConversation,
  Channel,
  ConversationDetail,
  ConversationListItem,
  ConversationMeta,
  ConversationRecord,
  ConversationStatus,
  CostBreakdown,
  CostLine,
  CrmPackage,
  CrmRecordRef,
  CrmWriteResult,
  Engine,
  Proposal,
  ReprocessRequest,
  Segment,
  StageEvent,
  StageRecordView,
  TopicTag,
  TranscriptOrigin,
  UsageRecord,
  VaultItem,
} from '../types';
import { normalizeText, productById } from './catalogue';
import {
  buildProposal,
  marketOf,
  recomputeReview,
  rerender,
  retentionDecision,
  templateById,
  toMarkdown,
} from './proposal';
import type { MockScript } from './scripts';
import { PRICING, TAXONOMY, TEMPLATES } from './seed';
import { db, HttpError, iso, uuidHex, type MockConv, type SimVia, type StagePlanItem } from './state';

const ENGINES: readonly Engine[] = ['rules', 'llm', 'hybrid'];

const ORIGIN_BY_CHANNEL: Record<Channel, TranscriptOrigin> = {
  teams: 'teams_platform',
  zoom: 'zoom_platform',
  genesys: 'genesys_platform',
  mobile: 'fixture',
  upload: 'fixture',
};

const round = (value: number, digits = 1): number => Math.round(value * 10 ** digits) / 10 ** digits;
const sum = (values: Array<number | null>): number => values.reduce<number>((total, value) => total + (value ?? 0), 0);

export function emptyCost(): CostBreakdown {
  return { lines: [], total: 0, currency: 'USD', illustrative: true };
}

function requireConv(id: string): MockConv {
  const conv = db().convs.get(id);
  if (!conv) throw new HttpError(404, `Conversation ${id} not found`);
  return conv;
}

// ---- pipeline plan ---------------------------------------------------------------------------------
function planFor(script: MockScript, via: SimVia, engine: Engine, pace: number): StagePlanItem[] {
  const ms = (value: number) => Math.round(value * pace);
  const skipTranscribe = via === 'transcript';
  const actions = script.policyActions;
  const countSegments = (kind: string) => actions.filter((action) => action.kind === kind).reduce((total, action) => total + action.segment_ids.length, 0);
  return [
    { stage: 'ingest', ms: ms(700), skipped: false, detail: { channel: script.info.channel, payload: skipTranscribe ? 'platform transcript' : 'audio' } },
    {
      stage: 'transcribe',
      ms: skipTranscribe ? 0 : ms(4300),
      skipped: skipTranscribe,
      detail: skipTranscribe
        ? { reason: 'platform transcript used', origin: ORIGIN_BY_CHANNEL[script.info.channel] }
        : { provider: 'azure_speech', audio_seconds: script.info.duration_minutes * 60, language: script.info.language },
    },
    { stage: 'normalize', ms: ms(600), skipped: false, detail: { segments: script.turns.length, speakers: 2, language: script.info.language } },
    {
      stage: 'policy',
      ms: ms(1300),
      skipped: false,
      detail: {
        redacted_spans: actions.filter((action) => action.kind === 'pii_redacted').length,
        omitted_segments: countSegments('omitted_sensitive'),
        small_talk_segments: countSegments('dropped_small_talk'),
      },
    },
    {
      stage: 'extract',
      ms: engine === 'rules' ? ms(1500) : ms(4100),
      skipped: false,
      detail: { engine, model: engine === 'rules' ? null : 'gpt-4.1-mini' },
    },
    { stage: 'resolve', ms: ms(1600), skipped: false, detail: { mentions: script.mentions.length, topics: script.topics.length, tasks: script.tasks.length } },
    { stage: 'validate', ms: ms(500), skipped: false, detail: { passed: true, leaks: 0 } },
  ];
}

const planTotal = (plan: StagePlanItem[]): number => plan.reduce((total, item) => total + item.ms, 0);

function stageViewsAt(startedAt: number, plan: StagePlanItem[], elapsed: number): StageRecordView[] {
  const views: StageRecordView[] = [];
  let cursor = 0;
  for (const item of plan) {
    const start = cursor;
    const end = cursor + item.ms;
    cursor = end;
    if (elapsed < start) break;
    const base = { stage: item.stage, started_at: iso(startedAt + start), detail: item.detail };
    if (elapsed >= end) views.push({ ...base, status: item.skipped ? 'skipped' : 'completed', duration_ms: item.ms });
    else views.push({ ...base, status: 'started', duration_ms: null });
  }
  return views;
}

function eventsFromStages(conversationId: string, stages: StageRecordView[]): StageEvent[] {
  const events: StageEvent[] = [];
  for (const stage of stages) {
    const startedMs = stage.started_at ? Date.parse(stage.started_at) : Date.now();
    events.push({ conversation_id: conversationId, stage: stage.stage, status: 'started', ts: iso(startedMs), duration_ms: null, detail: {} });
    if (stage.status !== 'started') {
      events.push({
        conversation_id: conversationId,
        stage: stage.stage,
        status: stage.status,
        ts: iso(startedMs + (stage.duration_ms ?? 0)),
        duration_ms: stage.duration_ms,
        detail: stage.detail,
      });
    }
  }
  return events;
}

// ---- usage and cost --------------------------------------------------------------------------------
function buildUsage(conv: MockConv, engine: Engine): UsageRecord[] {
  const script = conv.script;
  const startedAt = Date.parse(conv.record.created_at);
  const durationSec = script.info.duration_minutes * 60;
  const realWords = script.info.duration_minutes * 140;
  const usage: UsageRecord[] = [];
  const base = { conversation_id: conv.id, tokens_in: null, tokens_out: null, audio_seconds: null, units: null, model: null };
  if (conv.via === 'audio') {
    usage.push({ ...base, stage: 'transcribe', service: 'speech', provider: 'azure_speech', model: 'fast-transcription', audio_seconds: durationSec, latency_ms: 4300, ts: iso(startedAt + 700) });
  }
  usage.push({ ...base, stage: 'policy', service: 'language_pii', provider: 'local', units: Math.ceil((realWords * 6) / 1000), latency_ms: 1300, ts: iso(startedAt + 5600) });
  if (engine !== 'rules') {
    usage.push({
      ...base,
      stage: 'extract',
      service: 'llm',
      provider: 'foundry',
      model: 'gpt-4.1-mini',
      tokens_in: Math.round(realWords * 1.35 + 1400),
      tokens_out: 620 + 110 * script.mentions.length,
      latency_ms: 4100,
      ts: iso(startedAt + 6900),
    });
  }
  usage.push({ ...base, stage: 'resolve', service: 'search', provider: 'local', units: script.mentions.length, latency_ms: 1600, ts: iso(startedAt + 11000) });
  return usage;
}

export function buildCost(usage: UsageRecord[]): CostBreakdown {
  const lines: CostLine[] = [];
  const audio = sum(usage.map((record) => record.audio_seconds));
  if (audio > 0) lines.push({ service: 'Speech transcription', quantity: round(audio / 3600, 3), unit: 'audio hour', est_cost: (audio / 3600) * PRICING.speechPerAudioHour });
  const tokensIn = sum(usage.map((record) => record.tokens_in));
  const tokensOut = sum(usage.map((record) => record.tokens_out));
  if (tokensIn + tokensOut > 0) {
    lines.push({
      service: 'LLM extraction',
      quantity: round((tokensIn + tokensOut) / 1000, 2),
      unit: 'k tokens',
      est_cost: (tokensIn / 1_000_000) * PRICING.llmInputPer1M + (tokensOut / 1_000_000) * PRICING.llmOutputPer1M,
    });
  }
  const pii = sum(usage.filter((record) => record.service === 'language_pii').map((record) => record.units));
  if (pii > 0) lines.push({ service: 'PII detection', quantity: pii, unit: 'text records', est_cost: (pii / 1000) * PRICING.languagePiiPer1kRecords });
  const search = sum(usage.filter((record) => record.service === 'search').map((record) => record.units));
  if (search > 0) lines.push({ service: 'Catalogue search', quantity: search, unit: 'queries', est_cost: 0 });
  return { lines, total: lines.reduce((total, line) => total + line.est_cost, 0), currency: 'USD', illustrative: true };
}

// ---- creating conversations ------------------------------------------------------------------------
export interface CreateOptions {
  script: MockScript;
  channel?: Channel | null;
  market?: string | null;
  businessLine?: string | null;
  templateId?: string | null;
  callType?: string | null;
  title?: string | null;
  languageHint?: string | null;
  sourceInteractionId?: string | null;
  via: SimVia;
  noise: boolean;
  createdAtMs?: number;
  pace?: number;
  fixtureId?: string | null;
}

function buildMeta(options: CreateOptions, id: string, createdAt: number): ConversationMeta {
  const { script } = options;
  const channel = options.channel ?? script.info.channel;
  const durationSec = script.info.duration_minutes * 60;
  return {
    conversation_id: id,
    source: channel,
    source_interaction_id: options.sourceInteractionId ?? `${channel}-${id.slice(0, 10)}`,
    direction: script.direction,
    title: options.title ?? script.info.title,
    started_at: iso(createdAt - durationSec * 1000),
    ended_at: iso(createdAt),
    duration_seconds: durationSec,
    seller: { id: 'seller', display_name: script.seller.name, email: script.seller.email, phone: null, role: 'seller', org: null },
    participants: [{ id: 'cust1', display_name: script.customer.name, email: script.customer.email, phone: null, role: 'customer', org: script.org }],
    customer_org: script.org,
    market: options.market ?? script.info.market,
    business_line: options.businessLine ?? script.info.business_line,
    call_type: options.callType ?? script.info.call_type,
    language_hint: options.languageHint ?? script.info.language,
    consent: {
      status: 'recorded_with_consent',
      basis: channel === 'mobile' ? 'Verbal consent captured by the seller (synthetic)' : 'Participants notified at call start (synthetic)',
    },
  };
}

export function createConversation(options: CreateOptions): MockConv {
  const state = db();
  const { script } = options;
  const createdAt = options.createdAtMs ?? Date.now();
  const id = uuidHex();
  const meta = buildMeta(options, id, createdAt);
  const requestedEngine = state.policies.processing.engine;
  const engine: Engine = ENGINES.includes(requestedEngine) ? requestedEngine : 'hybrid';
  const templateId =
    options.templateId && TEMPLATES.some((template) => template.template_id === options.templateId)
      ? options.templateId
      : (state.policies.processing.default_template_by_business_line[meta.business_line] ?? 'pb_institutional');

  const record: ConversationRecord = {
    meta,
    status: 'received',
    language: null,
    transcript_origin: null,
    engine: null,
    template_id: null,
    audio_seconds: options.via === 'audio' ? script.info.duration_minutes * 60 : null,
    proposal: null,
    retention: null,
    crm_result: null,
    error: null,
    fixture_id: options.fixtureId === undefined ? script.info.fixture_id : options.fixtureId,
    batch_id: null,
    version: 1,
    created_at: iso(createdAt),
    updated_at: iso(createdAt),
  };

  const conv: MockConv = {
    id,
    script,
    via: options.via,
    noise: options.noise,
    record,
    stages: [],
    events: [],
    usage: [],
    cost: emptyCost(),
    sim: { startedAt: createdAt, plan: planFor(script, options.via, engine, options.pace ?? 1), engine, templateId },
    rejectReason: null,
  };
  state.convs.set(id, conv);
  advance(conv);
  return conv;
}

/** A conversation whose pipeline failed (nothing to review). Used for the seeded "failed" row. */
export function createFailedConversation(options: CreateOptions, error: string, failedStage: 'ingest' | 'transcribe'): MockConv {
  const state = db();
  const createdAt = options.createdAtMs ?? Date.now();
  const id = uuidHex();
  const meta = buildMeta(options, id, createdAt);
  const done: StageRecordView = { stage: 'ingest', status: failedStage === 'ingest' ? 'failed' : 'completed', started_at: iso(createdAt), duration_ms: 420, detail: failedStage === 'ingest' ? { error } : { channel: meta.source } };
  const stages: StageRecordView[] = [done];
  if (failedStage === 'transcribe') stages.push({ stage: 'transcribe', status: 'failed', started_at: iso(createdAt + 420), duration_ms: 1900, detail: { error } });
  const conv: MockConv = {
    id,
    script: options.script,
    via: options.via,
    noise: false,
    record: {
      meta,
      status: 'failed',
      language: null,
      transcript_origin: null,
      engine: null,
      template_id: null,
      audio_seconds: null,
      proposal: null,
      retention: null,
      crm_result: null,
      error,
      fixture_id: options.script.info.fixture_id,
      batch_id: null,
      version: 1,
      created_at: iso(createdAt),
      updated_at: iso(createdAt + 2400),
    },
    stages,
    events: eventsFromStages(id, stages),
    usage: [],
    cost: emptyCost(),
    sim: null,
    rejectReason: null,
  };
  state.convs.set(id, conv);
  return conv;
}

// ---- advancing the pipeline ------------------------------------------------------------------------
export function advance(conv: MockConv, now: number = Date.now()): void {
  const sim = conv.sim;
  if (!sim) return;
  const elapsed = now - sim.startedAt;
  if (elapsed >= planTotal(sim.plan)) {
    finalize(conv);
    return;
  }
  conv.record.status = elapsed < (sim.plan[0]?.ms ?? 0) ? 'received' : 'processing';
  conv.stages = stageViewsAt(sim.startedAt, sim.plan, elapsed);
  conv.events = eventsFromStages(conv.id, conv.stages);
  conv.record.updated_at = iso(now);
}

export function tick(): void {
  const now = Date.now();
  for (const conv of db().convs.values()) advance(conv, now);
}

function finalize(conv: MockConv): void {
  const sim = conv.sim;
  if (!sim) return;
  const state = db();
  const readyAt = sim.startedAt + planTotal(sim.plan);
  const meta = conv.record.meta;
  const proposal = buildProposal({
    script: conv.script,
    templateId: sim.templateId,
    engine: sim.engine,
    readyAtMs: readyAt,
    callStartMs: Date.parse(meta.started_at),
    policies: state.policies,
    aliases: state.aliases,
    noise: conv.noise,
  });

  conv.record.proposal = proposal;
  conv.record.status = 'needs_review';
  conv.record.language = conv.script.info.language.startsWith('es') ? 'es-US' : conv.script.info.language;
  conv.record.transcript_origin = conv.via === 'audio' ? 'azure_speech' : ORIGIN_BY_CHANNEL[meta.source];
  conv.record.engine = sim.engine;
  conv.record.template_id = proposal.template_id;
  conv.record.retention = retentionDecision(marketOf(state.policies, meta.market), readyAt);
  conv.record.updated_at = iso(readyAt);

  const stages = stageViewsAt(sim.startedAt, sim.plan, planTotal(sim.plan));
  stages.push({ stage: 'review', status: 'started', started_at: iso(readyAt), duration_ms: null, detail: { state: 'pending', reasons: proposal.review.reasons } });
  conv.stages = stages;
  conv.events = eventsFromStages(conv.id, stages);
  conv.usage = buildUsage(conv, sim.engine);
  conv.cost = buildCost(conv.usage);
  conv.sim = null;
  proposal.crm_preview = buildPreview(conv);
  addWorkingCopy(conv, readyAt);

  if (state.policies.processing.auto_approve_enabled && proposal.review.auto_approve_eligible) {
    approveInternal(conv, {}, { auto: true, atMs: readyAt + 600 });
  }
}

// ---- CRM package and write -------------------------------------------------------------------------
function buildPreview(conv: MockConv): CrmPackage {
  const { meta, language, retention } = conv.record;
  const proposal = conv.record.proposal as Proposal;
  const result = proposal.result;
  const match = proposal.crm_match;
  return {
    idempotency_key: `${meta.source}:${meta.source_interaction_id}:v${conv.record.version}`,
    conversation_id: conv.id,
    regarding: match,
    phone_call: {
      subject: meta.title ?? `Call with ${meta.customer_org ?? 'customer'}`,
      description: proposal.note_markdown,
      direction: meta.direction === 'inbound' ? 'inbound' : 'outbound',
      start: meta.started_at,
      end: meta.ended_at,
      duration_minutes: Math.round((meta.duration_seconds ?? 0) / 60),
      regarding: match.opportunity ?? match.account,
      to_contact: match.contact,
      owner_email: meta.seller.email,
    },
    tasks: result.tasks.map((task) => ({
      source_task_id: task.task_id,
      subject: task.title,
      description: task.description,
      due: task.due_date,
      regarding: match.opportunity ?? match.account,
      owner_email: meta.seller.email,
      priority: 'normal',
    })),
    insight: {
      source: meta.source,
      source_interaction_id: meta.source_interaction_id,
      template_id: proposal.template_id,
      language: language ?? 'en-US',
      summary_markdown: proposal.note_markdown,
      sections: result.sections,
      product_ids: result.products.flatMap((mention) => (mention.chosen_product_id ? [mention.chosen_product_id] : [])),
      topic_ids: result.topics.map((topic) => topic.topic_id),
      relationship_facts: result.relationship_facts.map((fact) => fact.text),
      engine: result.engine,
      model: result.model,
      confidence: result.overall_confidence,
      review_status: proposal.review.state === 'auto_approved' ? 'auto_approved' : 'approved',
      transcript_pointer: retention?.transcript_stored ? `vault://transcript/${conv.id}` : null,
    },
  };
}

function writeCrm(conv: MockConv, atMs: number, degraded: string[]): CrmWriteResult {
  const state = db();
  const meta = conv.record.meta;
  const preview = (conv.record.proposal as Proposal).crm_preview as CrmPackage;
  const created: CrmRecordRef[] = [];
  const stamp = iso(atMs);
  const call = preview.phone_call;

  const phoneId = `pc-${uuidHex().slice(0, 8)}`;
  state.crm.phonecall.unshift({
    id: phoneId,
    subject: call.subject,
    description: call.description,
    direction: call.direction,
    start: call.start,
    end: call.end,
    duration_minutes: call.duration_minutes,
    regarding_entity: call.regarding?.entity ?? null,
    regarding_id: call.regarding?.id ?? null,
    regarding_name: call.regarding?.name ?? null,
    contact_id: call.to_contact?.id ?? null,
    contact_name: call.to_contact?.name ?? null,
    owner_email: call.owner_email,
    owner_name: meta.seller.display_name,
    idempotency_key: preview.idempotency_key,
    conversation_id: conv.id,
    created_at: stamp,
  });
  created.push({ entity: 'phonecall', id: phoneId, name: call.subject });

  for (const task of preview.tasks) {
    const taskId = `task-${uuidHex().slice(0, 8)}`;
    state.crm.task.unshift({
      id: taskId,
      subject: task.subject,
      description: task.description,
      due: task.due,
      priority: task.priority,
      regarding_entity: task.regarding?.entity ?? null,
      regarding_id: task.regarding?.id ?? null,
      regarding_name: task.regarding?.name ?? null,
      owner_email: task.owner_email,
      owner_name: meta.seller.display_name,
      source_task_id: task.source_task_id,
      phonecall_id: phoneId,
      conversation_id: conv.id,
      created_at: stamp,
    });
    created.push({ entity: 'task', id: taskId, name: task.subject });
  }

  const insightId = `ins-${uuidHex().slice(0, 8)}`;
  const insight = preview.insight;
  state.crm.insight.unshift({
    id: insightId,
    idempotency_key: preview.idempotency_key,
    conversation_id: conv.id,
    source: insight.source,
    source_interaction_id: insight.source_interaction_id,
    template_id: insight.template_id,
    language: insight.language,
    summary_markdown: insight.summary_markdown,
    engine: insight.engine,
    model: insight.model,
    confidence: insight.confidence,
    review_status: insight.review_status,
    transcript_pointer: insight.transcript_pointer,
    phonecall_id: phoneId,
    regarding_entity: call.regarding?.entity ?? null,
    regarding_id: call.regarding?.id ?? null,
    created_at: stamp,
    regarding_name: call.regarding?.name ?? null,
    sections: insight.sections,
    relationship_facts: insight.relationship_facts,
    product_ids: insight.product_ids,
    topic_ids: insight.topic_ids,
  });
  created.push({ entity: 'insight', id: insightId, name: call.subject });

  return { ok: true, created, skipped_duplicate: false, degraded_fields: degraded, error: null };
}

// ---- vault -----------------------------------------------------------------------------------------
function addWorkingCopy(conv: MockConv, atMs: number): void {
  db().vault.push({
    conversation_id: conv.id,
    kind: 'working',
    uri: `.state/vault/${conv.id}/working.json`,
    created_at: iso(atMs),
    expires_at: iso(atMs + 6 * 3_600_000),
    size_bytes: 4200 + conv.script.turns.length * 310,
    redacted: true,
    filename: 'working.json',
  });
}

function settleVault(conv: MockConv, atMs: number): void {
  const state = db();
  const retention = conv.record.retention;
  state.vault = state.vault.filter((item) => item.conversation_id !== conv.id);
  if (retention?.transcript_stored) {
    state.vault.push({
      conversation_id: conv.id,
      kind: 'transcript',
      uri: `.state/vault/${conv.id}/transcript.json`,
      created_at: iso(atMs),
      expires_at: retention.transcript_expires_at,
      size_bytes: 3800 + conv.script.turns.length * 290,
      redacted: retention.transcript_redacted,
      filename: 'transcript.json',
    });
  }
}

export function vaultItemsFor(conversationId: string): VaultItem[] {
  return db().vault.filter((item) => item.conversation_id === conversationId);
}

function transcriptItem(conv: MockConv): VaultItem | undefined {
  const now = Date.now();
  return vaultItemsFor(conv.id).find((item) => (item.kind === 'transcript' || item.kind === 'working') && (!item.expires_at || Date.parse(item.expires_at) > now));
}

function transcriptAvailable(conv: MockConv): boolean {
  return Boolean(conv.record.retention?.transcript_stored && transcriptItem(conv));
}

// ---- approve / reject / reprocess ------------------------------------------------------------------
interface ApproveOptions {
  auto: boolean;
  atMs: number;
  degraded?: string[];
}

function learnAlias(surface: string, productId: string, atMs: number): void {
  const aliases = db().aliases;
  const key = normalizeText(surface);
  const existing = aliases.find((alias) => alias.surface === key && alias.product_id === productId);
  if (existing) {
    existing.count += 1;
    return;
  }
  aliases.unshift({ surface: key, product_id: productId, count: 1, source: 'reviewer_correction', created_at: iso(atMs) });
}

function rebuildProposal(conv: MockConv, templateId: string, engine: Engine, atMs: number): Proposal {
  const state = db();
  const proposal = buildProposal({
    script: conv.script,
    templateId,
    engine,
    readyAtMs: atMs,
    callStartMs: Date.parse(conv.record.meta.started_at),
    policies: state.policies,
    aliases: state.aliases,
    noise: conv.noise,
  });
  conv.record.proposal = proposal;
  conv.record.template_id = templateId;
  conv.record.engine = engine;
  conv.record.updated_at = iso(atMs);
  proposal.crm_preview = buildPreview(conv);
  conv.usage = buildUsage(conv, engine);
  conv.cost = buildCost(conv.usage);
  for (const stage of conv.stages) {
    if (stage.stage === 'extract') stage.detail = { engine, model: engine === 'rules' ? null : 'gpt-4.1-mini', template_id: templateId };
    if (stage.stage === 'review') {
      stage.started_at = iso(atMs);
      stage.detail = { state: 'pending', reasons: proposal.review.reasons };
    }
  }
  conv.events = eventsFromStages(conv.id, conv.stages);
  return proposal;
}

function approveInternal(conv: MockConv, body: ApproveRequest, options: ApproveOptions): void {
  const state = db();
  if (!conv.record.proposal || conv.record.status !== 'needs_review') {
    throw new HttpError(409, `Conversation is ${conv.record.status}; only conversations pending review can be approved`);
  }
  const before = conv.record.proposal;
  const picks = body.product_choices ?? {};
  const drops = new Set(body.drop_mentions ?? []);

  // Validate everything first so a rejected request changes nothing.
  for (const [mentionId, productId] of Object.entries(picks)) {
    if (!before.result.products.some((mention) => mention.mention_id === mentionId)) throw new HttpError(422, `Unknown mention ${mentionId}`);
    if (!productById(productId)) throw new HttpError(422, `Unknown product ${productId}`);
  }
  for (const mentionId of drops) {
    if (!before.result.products.some((mention) => mention.mention_id === mentionId)) throw new HttpError(422, `Unknown mention ${mentionId}`);
  }
  const unresolved = before.result.products.filter((mention) => !drops.has(mention.mention_id) && mention.chosen_product_id === null && !picks[mention.mention_id]);
  if (unresolved.length > 0) {
    throw new HttpError(409, `Pick a product (or drop the mention) before approving: ${unresolved.map((mention) => `${mention.mention_id} "${mention.surface}"`).join(', ')}`);
  }
  const topicIds = new Set(TAXONOMY.topics.map((topic) => topic.topic_id));
  for (const topicId of body.topic_ids ?? []) if (!topicIds.has(topicId)) throw new HttpError(422, `Unknown topic ${topicId}`);
  for (const edit of body.task_edits ?? []) {
    if (!before.result.tasks.some((task) => task.task_id === edit.task_id)) throw new HttpError(422, `Unknown task ${edit.task_id}`);
  }
  if (body.template_id && !TEMPLATES.some((template) => template.template_id === body.template_id)) throw new HttpError(422, `Unknown template ${body.template_id}`);

  if (body.template_id && body.template_id !== before.template_id) {
    rebuildProposal(conv, body.template_id, (conv.record.engine as Engine | null) ?? 'hybrid', options.atMs);
  }
  const proposal = conv.record.proposal as Proposal;
  const result = proposal.result;

  for (const mention of result.products) {
    const picked = picks[mention.mention_id];
    if (!picked) continue;
    // Like the real backend: a phrase that never named a share class is not turned into an alias.
    if (mention.chosen_product_id !== picked && !mention.reasons.includes('share_class_not_stated')) learnAlias(mention.surface, picked, options.atMs);
    mention.chosen_product_id = picked;
    mention.confidence = 1;
    mention.needs_review = false;
    mention.reasons = [...mention.reasons, 'reviewer_selected'];
  }
  result.products = result.products.filter((mention) => !drops.has(mention.mention_id));

  if (body.topic_ids) {
    const existing = new Map(result.topics.map((topic) => [topic.topic_id, topic]));
    result.topics = body.topic_ids.map<TopicTag>((topicId) => {
      const known = existing.get(topicId);
      if (known) return known;
      return { topic_id: topicId, label: TAXONOMY.topics.find((topic) => topic.topic_id === topicId)?.label ?? topicId, confidence: 1, evidence: [] };
    });
  }

  for (const edit of body.task_edits ?? []) {
    const index = result.tasks.findIndex((task) => task.task_id === edit.task_id);
    const task = result.tasks[index];
    if (!task) continue;
    if (edit.drop) {
      result.tasks.splice(index, 1);
      continue;
    }
    if (edit.title !== undefined && edit.title !== null && edit.title.trim()) task.title = edit.title.trim();
    if (edit.due_date !== undefined) {
      task.due_date = edit.due_date;
      task.due_basis = edit.due_date ? 'stated' : 'none';
    }
  }

  rerender(proposal, conv.script);
  const sectionEdits = body.section_edits ?? {};
  for (const [key, text] of Object.entries(sectionEdits)) {
    const section = result.sections.find((item) => item.key === key);
    if (!section) continue;
    if (section.text !== null) section.text = text;
    else section.items = text.split('\n').map((line) => line.replace(/^[-*]\s*/, '').trim()).filter(Boolean);
  }
  if (Object.keys(sectionEdits).length > 0) proposal.note_markdown = toMarkdown(result.sections);
  recomputeReview(proposal, state.policies);

  const readyAt = proposal.ready_at ? Date.parse(proposal.ready_at) : options.atMs;
  const seconds = Math.max(0, (options.atMs - readyAt) / 1000);
  const clicks = options.auto ? 0 : Math.max(1, body.clicks ?? 1);
  proposal.review = {
    ...proposal.review,
    state: options.auto ? 'auto_approved' : 'approved',
    reviewed_at: iso(options.atMs),
    reviewed_by: options.auto ? 'auto-approve policy' : conv.record.meta.seller.email,
    clicks,
    seconds_to_review: round(seconds, 1),
  };

  proposal.crm_preview = buildPreview(conv);
  const crmResult = writeCrm(conv, options.atMs, options.degraded ?? []);
  conv.record.crm_result = crmResult;
  settleVault(conv, options.atMs);

  const review = conv.stages.find((stage) => stage.stage === 'review');
  if (review) {
    review.status = 'completed';
    review.duration_ms = Math.round(seconds * 1000);
    review.detail = { decision: proposal.review.state, clicks, seconds_to_review: round(seconds, 1) };
  }
  conv.stages.push({
    stage: 'write',
    status: 'completed',
    started_at: iso(options.atMs),
    duration_ms: 850,
    detail: { records_created: crmResult.created.length, idempotency_key: proposal.crm_preview.idempotency_key },
  });
  conv.stages.push({
    stage: 'retention',
    status: 'completed',
    started_at: iso(options.atMs + 850),
    duration_ms: 160,
    detail: { market: conv.record.meta.market, transcript_stored: Boolean(conv.record.retention?.transcript_stored) },
  });
  conv.events = eventsFromStages(conv.id, conv.stages);
  conv.record.status = 'written';
  conv.record.updated_at = iso(options.atMs + 1010);
}

export function approve(id: string, body: ApproveRequest, options?: { atMs?: number; auto?: boolean; degraded?: string[] }): ConversationDetail {
  const conv = requireConv(id);
  advance(conv);
  approveInternal(conv, body, { auto: options?.auto ?? false, atMs: options?.atMs ?? Date.now(), degraded: options?.degraded });
  return detailOf(id);
}

export function reject(id: string, reason: string | null | undefined, atMs: number = Date.now()): ConversationDetail {
  const conv = requireConv(id);
  advance(conv);
  const proposal = conv.record.proposal;
  if (!proposal || conv.record.status !== 'needs_review') {
    throw new HttpError(409, `Conversation is ${conv.record.status}; only conversations pending review can be rejected`);
  }
  const readyAt = proposal.ready_at ? Date.parse(proposal.ready_at) : atMs;
  const seconds = Math.max(0, (atMs - readyAt) / 1000);
  proposal.review = {
    ...proposal.review,
    state: 'rejected',
    reviewed_at: iso(atMs),
    reviewed_by: conv.record.meta.seller.email,
    clicks: 1,
    seconds_to_review: round(seconds, 1),
  };
  conv.rejectReason = reason?.trim() || null;
  const review = conv.stages.find((stage) => stage.stage === 'review');
  if (review) {
    review.status = 'completed';
    review.duration_ms = Math.round(seconds * 1000);
    review.detail = { decision: 'rejected', reason: conv.rejectReason };
  }
  db().vault = db().vault.filter((item) => item.conversation_id !== conv.id);
  conv.events = eventsFromStages(conv.id, conv.stages);
  conv.record.status = 'rejected';
  conv.record.updated_at = iso(atMs);
  return detailOf(id);
}

export function reprocess(id: string, body: ReprocessRequest): ConversationDetail {
  const conv = requireConv(id);
  advance(conv);
  if (!conv.record.proposal || conv.record.status !== 'needs_review') {
    throw new HttpError(409, `Conversation is ${conv.record.status}; only conversations pending review can be reprocessed`);
  }
  const templateId = body.template_id ?? conv.record.proposal.template_id;
  if (!TEMPLATES.some((template) => template.template_id === templateId)) throw new HttpError(404, `Unknown template ${templateId}`);
  const engine = (body.engine ?? conv.record.engine ?? 'hybrid') as Engine;
  if (!ENGINES.includes(engine)) throw new HttpError(422, `Unknown engine ${String(body.engine)}`);
  templateById(templateId);
  conv.record.version += 1;
  rebuildProposal(conv, templateId, engine, Date.now());
  return detailOf(id);
}

// ---- reading ---------------------------------------------------------------------------------------
export function toListItem(conv: MockConv): ConversationListItem {
  const { record } = conv;
  const { meta, proposal } = record;
  const readyAt = proposal?.ready_at ? Date.parse(proposal.ready_at) : null;
  return {
    conversation_id: conv.id,
    status: record.status,
    source: meta.source,
    market: meta.market,
    business_line: meta.business_line,
    language: record.language ?? meta.language_hint,
    template_id: record.template_id,
    title: meta.title,
    seller_name: meta.seller.display_name,
    customer_org: meta.customer_org,
    started_at: meta.started_at,
    created_at: record.created_at,
    product_count: proposal?.result.products.length ?? 0,
    topic_count: proposal?.result.topics.length ?? 0,
    task_count: proposal?.result.tasks.length ?? 0,
    needs_review: record.status === 'needs_review',
    review_reasons: record.status === 'needs_review' ? [...(proposal?.review.reasons ?? [])] : [],
    auto_approve_eligible: proposal?.review.auto_approve_eligible ?? false,
    latency_seconds: readyAt === null ? null : round((readyAt - Date.parse(record.created_at)) / 1000, 1),
    cost_usd: conv.cost.total > 0 ? conv.cost.total : null,
    fixture_id: record.fixture_id,
    batch_id: record.batch_id,
  };
}

export function listItems(statuses: ConversationStatus[], limit: number, offset: number): ConversationListItem[] {
  tick();
  const wanted = new Set(statuses);
  return [...db().convs.values()]
    .filter((conv) => wanted.size === 0 || wanted.has(conv.record.status))
    .sort((left, right) => Date.parse(right.record.created_at) - Date.parse(left.record.created_at))
    .slice(offset, offset + limit)
    .map(toListItem);
}

export function detailOf(id: string): ConversationDetail {
  const conv = requireConv(id);
  advance(conv);
  const retention = conv.record.retention;
  const available = transcriptAvailable(conv);
  const item = transcriptItem(conv);
  let note = 'The market policy decides whether a transcript is kept once processing completes.';
  if (retention) {
    note = available
      ? `Redacted transcript served from the ${item?.kind === 'transcript' ? 'vault' : 'review working copy'}. ${retention.explanation}`
      : retention.explanation;
  }
  return {
    record: conv.record,
    stages: conv.stages,
    events: conv.events,
    usage: conv.usage,
    cost: conv.cost,
    transcript_available: available,
    transcript_note: note,
    vault_items: vaultItemsFor(conv.id),
  };
}

export function transcriptOf(id: string): { data: CanonicalConversation; source: 'working' | 'vault' } {
  const conv = requireConv(id);
  advance(conv);
  if (!conv.record.retention) throw new HttpError(404, 'No transcript yet: the market policy is applied once processing completes.');
  if (!transcriptAvailable(conv)) throw new HttpError(404, conv.record.retention.explanation);
  const item = transcriptItem(conv);
  return { data: buildTranscript(conv), source: item?.kind === 'transcript' ? 'vault' : 'working' };
}

const TEXT_NOISE: Record<string, string> = { C: 'class see', A: 'class eh', I: 'class eye', R6: 'class are six', Advisor: 'advice or shares' };

function buildTranscript(conv: MockConv): CanonicalConversation {
  const { script, record } = conv;
  const noiseSpec = conv.noise ? script.mentions.find((mention) => mention.stated !== null) : undefined;
  let cursor = 1500;
  const segments: Segment[] = script.turns.map((turn, index) => {
    let text = turn.text;
    if (noiseSpec?.stated && noiseSpec.seg.includes(index)) {
      text = text.replace(/(class|clase)\s+\w+|\w+\s+shares/i, TEXT_NOISE[noiseSpec.stated] ?? 'class unknown');
    }
    const start = cursor;
    cursor += Math.max(1800, text.length * 62) + 700;
    return {
      id: index,
      speaker_id: turn.who === 'seller' ? 'seller' : turn.who === 'internal' ? 'int1' : 'cust1',
      start_ms: start,
      end_ms: cursor - 400,
      text,
      locale: turn.locale ?? script.info.language,
      confidence: text ? round(0.93 + ((index * 7) % 6) / 100, 2) : null,
    };
  });
  return {
    meta: record.meta,
    segments,
    language: record.language ?? script.info.language,
    transcript_origin: record.transcript_origin ?? 'fixture',
    artifacts: { audio_uri: null, raw_transcript_uri: null, source_summary: null },
  };
}

export function rejectReasonOf(id: string): string | null {
  return requireConv(id).rejectReason;
}

export function conversationCount(): number {
  return db().convs.size;
}

export function findByInteraction(source: Channel, sourceInteractionId: string): MockConv | undefined {
  return [...db().convs.values()].find((conv) => conv.record.meta.source === source && conv.record.meta.source_interaction_id === sourceInteractionId);
}
