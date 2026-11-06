/** Mock evaluation: per-case results for the scripted calls, aggregated into scoreboard metrics. */
import type { EvalCaseResult, EvalMetrics, EvalRun, EvalRunRequest } from '../types';
import { SCRIPTS } from './scripts';
import { db, HttpError, iso, uuidHex } from './state';

interface Profile {
  t1: number;
  t3: number;
  conf: number;
  ft: number;
  tFn: number;
  tFp: number;
  kFn: number;
  kFp: number;
  wer: number | null;
  latency: number;
  cost: number;
  review: boolean;
}

const p = (t1: number, t3: number, rest: Partial<Profile> & Pick<Profile, 'latency' | 'cost' | 'review'>): Profile => ({
  t1,
  t3,
  conf: 0,
  ft: 0,
  tFn: 0,
  tFp: 0,
  kFn: 0,
  kFp: 0,
  wer: null,
  ...rest,
});

// Rules engine, no learning, scripted transcripts: the "before" picture.
const BASELINE: Record<string, Profile> = {
  'pb-teams-equity-class-c': p(2, 2, { latency: 11_800, cost: 0.026, review: false }),
  'pb-ambiguous-equity-fund': p(1, 2, { tFn: 1, latency: 13_200, cost: 0.021, review: true }),
  'pb-zoom-fixed-income-followup': p(2, 2, { wer: 0.038, latency: 18_100, cost: 0.178, review: false }),
  'pb-genesys-inbound-service': p(1, 1, { conf: 1, kFp: 1, latency: 9_400, cost: 0.017, review: true }),
  'pb-zoom-es-mandato-renta-variable': p(1, 1, { tFn: 1, tFp: 1, kFn: 1, wer: 0.093, latency: 20_400, cost: 0.196, review: true }),
  'rt-genesys-privacy-seahawks': p(2, 2, { tFn: 1, latency: 12_600, cost: 0.022, review: false }),
  'pb-mobile-inperson-visit': p(2, 2, { wer: 0.071, latency: 16_700, cost: 0.126, review: false }),
  'pb-genesys-near-identical-names': p(0, 1, { ft: 1, latency: 8_900, cost: 0.012, review: true }),
  'int-teams-uk-multi-asset': p(2, 2, { latency: 11_300, cost: 0.023, review: false }),
  'rt-mobile-es-advisor-visit': p(0, 1, { tFn: 1, wer: 0.101, latency: 15_100, cost: 0.111, review: true }),
  'int-zoom-eu-income-allocation': p(2, 2, { tFn: 1, tFp: 1, wer: 0.04, latency: 17_900, cost: 0.152, review: false }),
  'pb-teams-commitments-no-dates': p(1, 1, { kFn: 1, latency: 8_300, cost: 0.014, review: false }),
  'pb-teams-legacy-blue-chip': p(0, 0, { latency: 9_100, cost: 0.016, review: true }),
};

// Learned aliases (from reviewer corrections) fix product resolution.
const LEARNING: Record<string, Partial<Profile>> = {
  'pb-genesys-inbound-service': { t1: 2, t3: 2, conf: 0, review: false },
  'pb-zoom-es-mandato-renta-variable': { t1: 2, t3: 2 },
  'pb-genesys-near-identical-names': { t1: 1, ft: 0, review: false },
  'rt-mobile-es-advisor-visit': { t1: 1, review: false },
  'pb-teams-legacy-blue-chip': { t1: 1, t3: 1, review: false },
};

// An LLM-assisted engine improves topics and tasks.
const LLM: Record<string, Partial<Profile>> = {
  'pb-ambiguous-equity-fund': { tFn: 0 },
  'pb-genesys-inbound-service': { kFp: 0 },
  'pb-zoom-es-mandato-renta-variable': { tFn: 0, tFp: 0, kFn: 0 },
  'rt-mobile-es-advisor-visit': { tFn: 0 },
  'int-zoom-eu-income-allocation': { tFp: 0 },
};

const NOISE_HITS = ['pb-teams-equity-class-c', 'pb-zoom-fixed-income-followup', 'pb-mobile-inperson-visit', 'int-teams-uk-multi-asset', 'int-zoom-eu-income-allocation'];
const NOISE_HITS_WITH_LEARNING = ['pb-teams-equity-class-c', 'int-teams-uk-multi-asset'];

const ENGINES = ['rules', 'llm', 'hybrid'];

export function buildCases(engine: string, noise: boolean, learning: boolean): EvalCaseResult[] {
  const llm = engine === 'llm' || engine === 'hybrid';
  const noisy = new Set(learning ? NOISE_HITS_WITH_LEARNING : NOISE_HITS);

  return SCRIPTS.map<EvalCaseResult>((script) => {
    const id = script.info.fixture_id;
    let profile: Profile = { ...(BASELINE[id] as Profile) };
    if (learning) profile = { ...profile, ...LEARNING[id] };
    if (llm) profile = { ...profile, ...LLM[id] };
    if (noise && noisy.has(id)) profile = { ...profile, t1: Math.max(0, profile.t1 - 1), review: true };

    const topicGold = script.topics.length;
    const taskGold = script.tasks.length;
    const relationshipGold = script.info.business_line === 'retail' ? script.facts.relationship.length : 0;
    const wer = noise ? (profile.wer ?? 0.06) + 0.05 : profile.wer;

    return {
      fixture_id: id,
      title: script.info.title,
      tags: [...script.info.tags],
      product_top1_correct: profile.t1,
      product_top3_hits: profile.t3,
      product_gold: script.mentions.length,
      product_false_tags: profile.ft,
      share_class_confusions: profile.conf,
      topic_tp: topicGold - profile.tFn,
      topic_fp: profile.tFp,
      topic_fn: profile.tFn,
      task_tp: taskGold - profile.kFn,
      task_fp: profile.kFp,
      task_fn: profile.kFn,
      leaks: 0,
      kept_relationship: relationshipGold,
      kept_relationship_gold: relationshipGold,
      wer: wer === null ? null : Math.round(wer * 1000) / 1000,
      latency_ms: Math.round(profile.latency * (llm ? 1.45 : 1)),
      cost_usd: Math.round(profile.cost * (llm ? 1.35 : 1) * 10_000) / 10_000,
      needs_review: profile.review,
      details: { channel: script.info.channel, language: script.info.language, engine, asr_noise: noise },
    };
  });
}

const ratio = (numerator: number, denominator: number, empty = 0): number => (denominator === 0 ? empty : numerator / denominator);
const total = (cases: EvalCaseResult[], pick: (item: EvalCaseResult) => number): number => cases.reduce((sum, item) => sum + pick(item), 0);

function percentile(values: number[], fraction: number): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((left, right) => left - right);
  return sorted[Math.max(1, Math.ceil(fraction * sorted.length)) - 1] ?? null;
}

export function computeMetrics(cases: EvalCaseResult[]): EvalMetrics {
  const gold = total(cases, (item) => item.product_gold);
  const top1 = total(cases, (item) => item.product_top1_correct);
  const confusions = total(cases, (item) => item.share_class_confusions);
  const falseTags = total(cases, (item) => item.product_false_tags);
  const tp = total(cases, (item) => item.topic_tp);
  const fp = total(cases, (item) => item.topic_fp);
  const fn = total(cases, (item) => item.topic_fn);
  const topicPrecision = ratio(tp, tp + fp);
  const topicRecall = ratio(tp, tp + fn);
  const taskTp = total(cases, (item) => item.task_tp);
  const taskFp = total(cases, (item) => item.task_fp);
  const taskFn = total(cases, (item) => item.task_fn);
  const wers = cases.flatMap((item) => (item.wer === null ? [] : [item.wer]));
  const latencies = cases.flatMap((item) => (item.latency_ms === null ? [] : [item.latency_ms]));
  const costs = cases.flatMap((item) => (item.cost_usd === null ? [] : [item.cost_usd]));

  return {
    cases: cases.length,
    product_top1: ratio(top1, gold),
    product_top3: ratio(total(cases, (item) => item.product_top3_hits), gold),
    product_false_tag_rate: ratio(falseTags, top1 + confusions + falseTags),
    share_class_confusion_rate: ratio(confusions, gold),
    topic_precision: topicPrecision,
    topic_recall: topicRecall,
    topic_f1: ratio(2 * topicPrecision * topicRecall, topicPrecision + topicRecall),
    task_precision: ratio(taskTp, taskTp + taskFp),
    task_recall: ratio(taskTp, taskTp + taskFn),
    privacy_leaks: total(cases, (item) => item.leaks),
    relationship_kept_rate: ratio(total(cases, (item) => item.kept_relationship), total(cases, (item) => item.kept_relationship_gold), 1),
    mean_wer: wers.length === 0 ? null : wers.reduce((sum, value) => sum + value, 0) / wers.length,
    latency_p50_ms: percentile(latencies, 0.5),
    latency_p95_ms: percentile(latencies, 0.95),
    mean_cost_usd: costs.length === 0 ? null : costs.reduce((sum, value) => sum + value, 0) / costs.length,
    auto_approvable_rate: ratio(cases.filter((item) => !item.needs_review).length, cases.length),
  };
}

function labelFor(engine: string, noise: boolean, learning: boolean, aliases: number): string {
  const parts = [engine === 'rules' ? 'Rules engine' : engine === 'llm' ? 'LLM engine' : 'Hybrid engine'];
  if (noise) parts.push('ASR noise');
  parts.push(learning ? `learning on (${aliases} aliases)` : 'no learning');
  return parts.join(' \u00b7 ');
}

export function makeRun(request: EvalRunRequest, createdAtMs: number, runId: string = `run-${uuidHex().slice(0, 8)}`): EvalRun {
  const state = db();
  const engine = request.engine ?? state.policies.processing.engine;
  if (!ENGINES.includes(engine)) throw new HttpError(422, `Unknown engine "${engine}" (rules | llm | hybrid)`);
  const noise = Boolean(request.with_asr_noise);
  const learning = Boolean(request.apply_learning);
  const aliases = learning ? state.aliases.length : 0;
  const cases = buildCases(engine, noise, learning);
  return {
    run_id: runId,
    created_at: iso(createdAtMs),
    engine,
    with_asr_noise: noise,
    learned_aliases: aliases,
    label: request.label?.trim() || labelFor(engine, noise, learning, aliases),
    metrics: computeMetrics(cases),
    cases,
  };
}

/** Newest first, like GET /eval/runs. */
export function seedEvalRuns(nowMs: number, aliasCount: number): EvalRun[] {
  const hour = 3_600_000;
  const baseline = makeRun({ engine: 'rules', label: 'Baseline: rules engine, no learning' }, nowMs - 48 * hour, 'run-baseline');
  const noisy = makeRun({ engine: 'rules', with_asr_noise: true, label: 'Stress test: ASR noise ("class see")' }, nowMs - 36 * hour, 'run-asr-noise');
  const learned = makeRun({ engine: 'hybrid', apply_learning: true, label: `After learning: hybrid engine, ${aliasCount} corrected aliases applied` }, nowMs - 24 * hour, 'run-after-learning');
  learned.learned_aliases = aliasCount;
  return [learned, noisy, baseline];
}

export function listRuns(): EvalRun[] {
  return db().evalRuns.map((run) => ({ ...run, cases: [] }));
}

export function getRun(runId: string): EvalRun {
  const run = db().evalRuns.find((item) => item.run_id === runId);
  if (!run) throw new HttpError(404, `Evaluation run ${runId} not found`);
  return run;
}

export function runEvaluation(request: EvalRunRequest): EvalRun {
  const run = makeRun(request, Date.now());
  db().evalRuns.unshift(run);
  return run;
}
