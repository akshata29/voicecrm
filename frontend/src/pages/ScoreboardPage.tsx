import {
  ArrowTrendingUpIcon,
  BeakerIcon,
  ChartBarIcon,
  CheckCircleIcon,
  ClockIcon,
  CurrencyDollarIcon,
  CursorArrowRaysIcon,
  ExclamationTriangleIcon,
  PlayIcon,
  ShieldCheckIcon,
} from '@heroicons/react/24/outline';
import clsx from 'clsx';
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { getErrorMessage } from '@/api/client';
import { useConfig, useEngineOptions, useEvalRun, useEvalRuns, useMetricsSummary, useRunEval } from '@/api/hooks';
import { type EvalCaseResult, type EvalMetrics, type EvalRun } from '@/api/types';
import { EstCost } from '@/components/conversation/Badges';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { Field, PageHeader } from '@/components/ui/Field';
import { EmptyState, ErrorState, ListSkeleton, LoadingBlock } from '@/components/ui/StateViews';
import { Toggle } from '@/components/ui/Toggle';
import { useToast } from '@/components/ui/Toast';
import { formatDateTime, formatDurationMs, formatDurationSeconds, formatInt, formatPercent, formatRelative, formatUsd, humanize, percentDelta } from '@/lib/format';
import { useDocumentTitle } from '@/lib/hooks';
import { tagLabel } from '@/lib/labels';

// ---- targets (straw-man, NOT customer-agreed) ---------------------
const TARGETS = {
  top1: 0.9,
  top3: 0.95,
  topicF1: 0.95,
  p95Ms: 5 * 60 * 1000,
};

function runName(run: EvalRun): string {
  return run.label ?? `${run.engine}${run.with_asr_noise ? ' + ASR noise' : ''}${run.learned_aliases > 0 ? ` + ${run.learned_aliases} aliases` : ''}`;
}

function runOptionLabel(run: EvalRun): string {
  return `${runName(run)} · ${formatRelative(run.created_at)}`;
}

// ---- metric cards ----------------------------------------------------------------------------------
type Direction = 'higher' | 'lower';

interface MetricCardProps {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: 'default' | 'good' | 'bad';
  target?: string;
  delta?: { text: string; good: boolean; up: boolean } | null;
  icon?: typeof ChartBarIcon;
  illustrative?: boolean;
}

function MetricCard({ label, value, hint, tone = 'default', target, delta, icon: Icon, illustrative }: MetricCardProps) {
  return (
    <div className={clsx('card p-4', tone === 'good' && 'border-emerald-300', tone === 'bad' && 'border-red-300 bg-red-50/40')}>
      <div className="flex items-start justify-between gap-2">
        <p className="text-xs font-semibold uppercase tracking-wide text-neutral-500">{label}</p>
        {Icon ? <Icon aria-hidden="true" className="h-5 w-5 text-neutral-300" /> : null}
      </div>
      <p className={clsx('tabular mt-1 text-2xl font-semibold leading-8', tone === 'bad' ? 'text-red-700' : tone === 'good' ? 'text-emerald-700' : 'text-neutral-900')}>{value}</p>
      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-neutral-500">
        {hint ? <span>{hint}</span> : null}
        {delta ? (
          <span className={clsx('font-semibold', delta.good ? 'text-emerald-700' : 'text-red-700')} title="Change versus the baseline run">
            {delta.up ? '▲' : '▼'} {delta.text}
          </span>
        ) : null}
        {illustrative ? <span className="rounded bg-amber-50 px-1 py-px font-medium text-amber-800 ring-1 ring-inset ring-amber-200">illustrative</span> : null}
      </div>
      {target ? <p className="mt-1 text-[11px] text-neutral-400">Straw-man target {target}</p> : null}
    </div>
  );
}

function ratioDelta(after: number, before: number | undefined, direction: Direction = 'higher'): MetricCardProps['delta'] {
  if (before === undefined || Math.abs(after - before) < 0.0005) return null;
  const up = after > before;
  return { text: percentDelta(before, after), good: direction === 'higher' ? up : !up, up };
}

function countDelta(after: number, before: number | undefined): MetricCardProps['delta'] {
  if (before === undefined || after === before) return null;
  return { text: `${after > before ? '+' : '−'}${Math.abs(after - before)}`, good: after < before, up: after > before };
}

function sumCases(cases: EvalCaseResult[], pick: (item: EvalCaseResult) => number): number {
  return cases.reduce((total, item) => total + pick(item), 0);
}

function MetricGrid({ run, baseline }: { run: EvalRun; baseline: EvalRun | undefined }) {
  const m = run.metrics;
  const b = baseline?.metrics;
  const confusions = sumCases(run.cases, (item) => item.share_class_confusions);
  const baseConfusions = baseline && baseline.cases.length > 0 ? sumCases(baseline.cases, (item) => item.share_class_confusions) : undefined;

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
      <MetricCard label="Product top-1" value={formatPercent(m.product_top1, 1)} hint="exact product incl. share class" tone={m.product_top1 >= TARGETS.top1 ? 'good' : 'default'} target={formatPercent(TARGETS.top1)} delta={ratioDelta(m.product_top1, b?.product_top1)} icon={ChartBarIcon} />
      <MetricCard label="Product top-3" value={formatPercent(m.product_top3, 1)} hint="right product in the pick-list" tone={m.product_top3 >= TARGETS.top3 ? 'good' : 'default'} target={formatPercent(TARGETS.top3)} delta={ratioDelta(m.product_top3, b?.product_top3)} icon={ChartBarIcon} />
      <MetricCard
        label="Share-class confusions"
        value={confusions > 0 || run.cases.length > 0 ? formatInt(confusions) : formatPercent(m.share_class_confusion_rate, 1)}
        hint={`${formatPercent(m.share_class_confusion_rate, 1)} of gold products · Class C ↔ A`}
        tone={m.share_class_confusion_rate === 0 ? 'good' : 'bad'}
        delta={baseConfusions !== undefined ? countDelta(confusions, baseConfusions) : ratioDelta(m.share_class_confusion_rate, b?.share_class_confusion_rate, 'lower')}
        icon={ExclamationTriangleIcon}
      />
      <MetricCard label="False product tags" value={formatPercent(m.product_false_tag_rate, 1)} hint="chosen products not in the gold set" delta={ratioDelta(m.product_false_tag_rate, b?.product_false_tag_rate, 'lower')} />
      <MetricCard label="Topic F1" value={formatPercent(m.topic_f1, 1)} hint={`precision ${formatPercent(m.topic_precision)} · recall ${formatPercent(m.topic_recall)}`} tone={m.topic_f1 >= TARGETS.topicF1 ? 'good' : 'default'} target={formatPercent(TARGETS.topicF1)} delta={ratioDelta(m.topic_f1, b?.topic_f1)} icon={ChartBarIcon} />
      <MetricCard label="Task precision" value={formatPercent(m.task_precision, 1)} hint="tasks created that were expected" delta={ratioDelta(m.task_precision, b?.task_precision)} />
      <MetricCard label="Task recall" value={formatPercent(m.task_recall, 1)} hint="expected tasks that were created" delta={ratioDelta(m.task_recall, b?.task_recall)} />
      <MetricCard label="Privacy leaks" value={formatInt(m.privacy_leaks)} hint="sensitive strings found in CRM fields" tone={m.privacy_leaks === 0 ? 'good' : 'bad'} target="0" delta={countDelta(m.privacy_leaks, b?.privacy_leaks)} icon={ShieldCheckIcon} />
      <MetricCard label="Relationship kept" value={formatPercent(m.relationship_kept_rate, 0)} hint="rapport facts retained (retail template)" delta={ratioDelta(m.relationship_kept_rate, b?.relationship_kept_rate)} />
      <MetricCard
        label="Latency p50 / p95"
        value={`${formatDurationMs(m.latency_p50_ms)} / ${formatDurationMs(m.latency_p95_ms)}`}
        hint="ingest to review-ready"
        tone={m.latency_p95_ms !== null && m.latency_p95_ms <= TARGETS.p95Ms ? 'good' : 'default'}
        target="p95 ≤ ~5 min"
        icon={ClockIcon}
      />
      <MetricCard label="Mean cost per call" value={formatUsd(m.mean_cost_usd)} hint="speech + model + PII" illustrative icon={CurrencyDollarIcon} />
      <MetricCard label="Auto-approvable" value={formatPercent(m.auto_approvable_rate, 0)} hint="calls that need no human at today's thresholds" delta={ratioDelta(m.auto_approvable_rate, b?.auto_approvable_rate)} icon={CheckCircleIcon} />
      {m.mean_wer !== null ? <MetricCard label="Transcription WER" value={formatPercent(m.mean_wer, 1)} hint="word error rate vs the script (audio path)" delta={b?.mean_wer != null ? ratioDelta(m.mean_wer, b.mean_wer, 'lower') : null} /> : null}
    </div>
  );
}

// ---- before / after chart --------------------------------------------------------------------------
const COMPARE_METRICS: Array<{ key: keyof EvalMetrics; label: string }> = [
  { key: 'product_top1', label: 'Product top-1' },
  { key: 'product_top3', label: 'Product top-3' },
  { key: 'topic_f1', label: 'Topic F1' },
  { key: 'task_precision', label: 'Task precision' },
  { key: 'task_recall', label: 'Task recall' },
  { key: 'relationship_kept_rate', label: 'Relationship kept' },
  { key: 'auto_approvable_rate', label: 'Auto-approvable' },
];

function BeforeAfter({ before, after }: { before: EvalRun; after: EvalRun }) {
  const data = COMPARE_METRICS.map(({ key, label }) => ({ metric: label, before: Number(before.metrics[key] ?? 0), after: Number(after.metrics[key] ?? 0) }));
  const improved = data.filter((row) => row.after - row.before > 0.0005).length;
  const regressed = data.filter((row) => row.before - row.after > 0.0005).length;

  return (
    <Card aria-labelledby="compare-title">
      <CardHeader
        id="compare-title"
        title="Before and after"
        subtitle={`${runName(before)}  →  ${runName(after)}`}
        icon={ArrowTrendingUpIcon}
        actions={
          <>
            <Badge tone="success">{improved} improved</Badge>
            {regressed > 0 ? <Badge tone="danger">{regressed} regressed</Badge> : <Badge tone="neutral">0 regressed</Badge>}
          </>
        }
      />
      <CardBody className="space-y-4">
        <div className="h-80 w-full" role="img" aria-label={`Grouped bar chart comparing ${COMPARE_METRICS.length} accuracy metrics before and after`}>
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data} margin={{ top: 8, right: 8, bottom: 8, left: 0 }} barCategoryGap="22%">
              <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#e5e5e5" />
              <XAxis dataKey="metric" tick={{ fontSize: 11, fill: '#525252' }} interval={0} angle={-18} textAnchor="end" height={56} />
              <YAxis domain={[0, 1]} ticks={[0, 0.25, 0.5, 0.75, 1]} tickFormatter={(value: number) => `${Math.round(value * 100)}%`} tick={{ fontSize: 11, fill: '#525252' }} width={44} />
              <Tooltip formatter={(value) => (typeof value === 'number' ? formatPercent(value, 1) : String(value))} cursor={{ fill: 'rgba(15,108,189,0.06)' }} />
              <Legend verticalAlign="top" height={28} />
              <Bar dataKey="before" name="Before" fill="#a3a3a3" radius={[4, 4, 0, 0]} />
              <Bar dataKey="after" name="After" fill="#0f6cbd" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
        <div className="overflow-x-auto rounded-lg border border-neutral-200">
          <table className="min-w-full divide-y divide-neutral-100 text-sm">
            <caption className="sr-only">Metric values before and after</caption>
            <thead className="bg-neutral-50">
              <tr>
                <th scope="col" className="th">Metric</th>
                <th scope="col" className="th text-right">Before</th>
                <th scope="col" className="th text-right">After</th>
                <th scope="col" className="th text-right">Change</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-neutral-100 bg-white">
              {data.map((row) => {
                const diff = row.after - row.before;
                return (
                  <tr key={row.metric}>
                    <th scope="row" className="td text-left font-medium text-neutral-800">{row.metric}</th>
                    <td className="td tabular text-right text-neutral-600">{formatPercent(row.before, 1)}</td>
                    <td className="td tabular text-right font-semibold text-neutral-900">{formatPercent(row.after, 1)}</td>
                    <td className={clsx('td tabular text-right font-semibold', diff > 0.0005 ? 'text-emerald-700' : diff < -0.0005 ? 'text-red-700' : 'text-neutral-400')}>{Math.abs(diff) < 0.0005 ? '—' : percentDelta(row.before, row.after)}</td>
                  </tr>
                );
              })}
              <tr>
                <th scope="row" className="td text-left font-medium text-neutral-800">Privacy leaks</th>
                <td className="td tabular text-right text-neutral-600">{before.metrics.privacy_leaks}</td>
                <td className="td tabular text-right font-semibold text-neutral-900">{after.metrics.privacy_leaks}</td>
                <td className="td tabular text-right text-neutral-400">{after.metrics.privacy_leaks === before.metrics.privacy_leaks ? '—' : after.metrics.privacy_leaks - before.metrics.privacy_leaks}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </CardBody>
    </Card>
  );
}

// ---- per-case table --------------------------------------------------------------------------------
function CaseTable({ cases }: { cases: EvalCaseResult[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full divide-y divide-neutral-100 text-sm">
        <thead className="bg-neutral-50">
          <tr>
            <th scope="col" className="th">Case</th>
            <th scope="col" className="th">Products <span className="normal-case text-neutral-400">top-1 / top-3 / gold</span></th>
            <th scope="col" className="th">Topics <span className="normal-case text-neutral-400">tp / fp / fn</span></th>
            <th scope="col" className="th">Tasks <span className="normal-case text-neutral-400">tp / fp / fn</span></th>
            <th scope="col" className="th">Leaks</th>
            <th scope="col" className="th">Rapport</th>
            <th scope="col" className="th">WER</th>
            <th scope="col" className="th">Latency</th>
            <th scope="col" className="th">Cost</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-neutral-100 bg-white">
          {cases.map((item) => {
            const problem = item.share_class_confusions > 0 || item.product_false_tags > 0 || item.leaks > 0 || item.product_top1_correct < item.product_gold;
            return (
              <tr key={item.fixture_id} className={clsx(problem && 'bg-amber-50/50')}>
                <td className="td min-w-[16rem]">
                  <p className="font-medium text-neutral-900">{item.title}</p>
                  <p className="font-mono text-[11px] text-neutral-400">{item.fixture_id}</p>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {item.tags.map((tag) => (
                      <Badge key={tag} tone="neutral">
                        {tagLabel(tag)}
                      </Badge>
                    ))}
                    {item.needs_review ? <Badge tone="warning">needs review</Badge> : null}
                  </div>
                </td>
                <td className="td tabular whitespace-nowrap">
                  <span className={clsx('font-semibold', item.product_top1_correct < item.product_gold ? 'text-amber-700' : 'text-emerald-700')}>{item.product_top1_correct}</span> / {item.product_top3_hits} / {item.product_gold}
                  {item.share_class_confusions > 0 ? <Badge tone="danger" className="ml-2">{item.share_class_confusions} class confusion</Badge> : null}
                  {item.product_false_tags > 0 ? <Badge tone="warning" className="ml-2">{item.product_false_tags} false tag</Badge> : null}
                </td>
                <td className="td tabular whitespace-nowrap">
                  {item.topic_tp} / <span className={item.topic_fp > 0 ? 'font-semibold text-amber-700' : undefined}>{item.topic_fp}</span> / <span className={item.topic_fn > 0 ? 'font-semibold text-amber-700' : undefined}>{item.topic_fn}</span>
                </td>
                <td className="td tabular whitespace-nowrap">
                  {item.task_tp} / <span className={item.task_fp > 0 ? 'font-semibold text-amber-700' : undefined}>{item.task_fp}</span> / <span className={item.task_fn > 0 ? 'font-semibold text-amber-700' : undefined}>{item.task_fn}</span>
                </td>
                <td className={clsx('td tabular font-semibold', item.leaks > 0 ? 'text-red-700' : 'text-emerald-700')}>{item.leaks}</td>
                <td className="td tabular whitespace-nowrap">{item.kept_relationship_gold === 0 ? <span className="text-neutral-300">n/a</span> : `${item.kept_relationship} / ${item.kept_relationship_gold}`}</td>
                <td className="td tabular">{item.wer === null ? <span className="text-neutral-300">n/a</span> : formatPercent(item.wer, 1)}</td>
                <td className="td tabular whitespace-nowrap">{formatDurationMs(item.latency_ms)}</td>
                <td className="td tabular whitespace-nowrap">
                  <EstCost value={item.cost_usd} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---- run form --------------------------------------------------------------------------------------
function RunForm({ onRan }: { onRan: (run: EvalRun) => void }) {
  const config = useConfig();
  const { engines, modelLive } = useEngineOptions();
  const run = useRunEval();
  const toast = useToast();
  const [engine, setEngine] = useState('');
  const [noise, setNoise] = useState(false);
  const [learning, setLearning] = useState(false);
  const [label, setLabel] = useState('');

  const submit = () =>
    run.mutate(
      { engine: engine || null, with_asr_noise: noise, apply_learning: learning, label: label.trim() || null },
      {
        onSuccess: (result) => {
          toast.success('Evaluation finished', { message: `${runName(result)}: top-1 ${formatPercent(result.metrics.product_top1, 1)}, topic F1 ${formatPercent(result.metrics.topic_f1, 1)}` });
          onRan(result);
          setLabel('');
        },
        onError: (error) => toast.error('The evaluation failed', { message: getErrorMessage(error) }),
      },
    );

  return (
    <Card aria-labelledby="run-title">
      <CardHeader id="run-title" title="Run an evaluation" subtitle="Replays the golden set through the real pipeline into an isolated in-memory CRM" icon={BeakerIcon} />
      <CardBody>
        <form
          className="grid gap-4 md:grid-cols-2 xl:grid-cols-[12rem_1fr_1fr_1fr_auto]"
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <Field label="Engine" htmlFor="eval-engine" hint={`Default: ${config.data?.engine ?? 'policy engine'}${modelLive ? '' : ' (no model configured)'}`}>
            <select id="eval-engine" className="input" value={engine} onChange={(event) => setEngine(event.target.value)}>
              <option value="">Policy default</option>
              {engines.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </Field>
          <Toggle label="ASR noise" description='Inject mishearings such as "class see"' checked={noise} onChange={setNoise} className="self-center" />
          <Toggle label="Apply learning" description="Use aliases learned from reviewer corrections" checked={learning} onChange={setLearning} className="self-center" />
          <Field label="Label (optional)" htmlFor="eval-label">
            <input id="eval-label" className="input" value={label} onChange={(event) => setLabel(event.target.value)} placeholder="e.g. after Friday's corrections" />
          </Field>
          <div className="flex items-end">
            <Button type="submit" variant="primary" icon={PlayIcon} loading={run.isPending} className="w-full xl:w-auto">
              Run evaluation
            </Button>
          </div>
        </form>
        {run.isPending ? (
          <p role="status" className="mt-3 text-sm text-neutral-600">
            Running the golden set through the pipeline… this takes a few seconds.
          </p>
        ) : null}
      </CardBody>
    </Card>
  );
}

// ---- operations ------------------------------------------------------------------------------------
function Operations() {
  const metrics = useMetricsSummary();
  const m = metrics.data;
  return (
    <Card aria-labelledby="ops-title">
      <CardHeader id="ops-title" title="Operations: effort and throughput" subtitle="Live from the conversations processed in this environment" icon={CursorArrowRaysIcon} />
      <CardBody>
        {metrics.isPending ? <LoadingBlock label="Loading operational metrics…" className="!py-6" /> : null}
        {metrics.isError ? <ErrorState error={metrics.error} title="Could not load the operational metrics" onRetry={() => void metrics.refetch()} compact /> : null}
        {m ? (
          <div className="space-y-5">
            <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
              <MetricCard label="Conversations" value={formatInt(m.conversations)} hint={`${m.written} written · ${m.needs_review} to review`} />
              <MetricCard label="Clicks to approve" value={m.mean_clicks_to_approve === null ? '—' : m.mean_clicks_to_approve.toFixed(2)} hint="mean, human approvals" tone={m.mean_clicks_to_approve !== null && m.mean_clicks_to_approve <= 1.5 ? 'good' : 'default'} icon={CursorArrowRaysIcon} />
              <MetricCard label="Seconds to review" value={m.mean_seconds_to_review === null ? '—' : formatDurationSeconds(m.mean_seconds_to_review)} hint="review-ready to decision" icon={ClockIcon} />
              <MetricCard label="Auto-approved" value={formatInt(m.auto_approved)} hint="zero clicks, by policy" />
              <MetricCard label="Ready in p50 / p95" value={`${m.latency_p50_seconds === null ? '—' : formatDurationSeconds(m.latency_p50_seconds)} / ${m.latency_p95_seconds === null ? '—' : formatDurationSeconds(m.latency_p95_seconds)}`} hint="ingest to review-ready" />
              <MetricCard label="Mean cost per call" value={formatUsd(m.mean_cost_usd)} illustrative={m.cost_illustrative} icon={CurrencyDollarIcon} />
            </div>
            <div className="grid gap-6 md:grid-cols-2">
              <Breakdown title="By status" data={m.by_status} />
              <Breakdown title="By channel" data={m.by_channel} />
            </div>
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}

function Breakdown({ title, data }: { title: string; data: Record<string, number> }) {
  const entries = Object.entries(data).sort((left, right) => right[1] - left[1]);
  const max = Math.max(1, ...entries.map(([, count]) => count));
  return (
    <div>
      <p className="section-title mb-2">{title}</p>
      {entries.length === 0 ? (
        <p className="text-sm text-neutral-400">No data yet.</p>
      ) : (
        <ul className="space-y-1.5">
          {entries.map(([name, count]) => (
            <li key={name} className="flex items-center gap-2 text-sm">
              <span className="w-28 shrink-0 truncate text-neutral-700">{humanize(name)}</span>
              <div className="h-2 flex-1 overflow-hidden rounded-full bg-neutral-100">
                <div className="h-full rounded-full bg-brand-400" style={{ width: `${(count / max) * 100}%` }} />
              </div>
              <span className="tabular w-6 shrink-0 text-right text-xs text-neutral-500">{count}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ---- page ------------------------------------------------------------------------------------------
export default function ScoreboardPage() {
  useDocumentTitle('Scoreboard');
  const runs = useEvalRuns();
  const [afterId, setAfterId] = useState<string | null>(null);
  const [beforeId, setBeforeId] = useState<string | null>(null);

  const list = runs.data ?? [];
  // Defaults: "after" = newest run, "before" = oldest run (the before/after story).
  useEffect(() => {
    if (list.length === 0) return;
    if (afterId === null || !list.some((run) => run.run_id === afterId)) setAfterId(list[0]?.run_id ?? null);
    if (beforeId === null && list.length > 1) setBeforeId(list[list.length - 1]?.run_id ?? null);
  }, [list, afterId, beforeId]);

  const after = useEvalRun(afterId);
  const before = useEvalRun(beforeId && beforeId !== afterId ? beforeId : null);
  const baseline = beforeId && beforeId !== afterId ? before.data : undefined;

  const handleRan = (run: EvalRun) => {
    setBeforeId((current) => (current && current !== run.run_id ? current : (list[list.length - 1]?.run_id ?? null)));
    setAfterId(run.run_id);
  };

  const options = useMemo(() => list.map((run) => ({ id: run.run_id, label: runOptionLabel(run) })), [list]);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Scoreboard"
        icon={ChartBarIcon}
        description="Accuracy, effort and cost, measured on a labelled golden set of English and Spanish calls with deliberate traps. Run it again after reviewers correct products to see the numbers improve."
        actions={<Badge tone="warning">Targets are straw-man, not customer-agreed</Badge>}
      />

      <RunForm onRan={handleRan} />

      {runs.isPending ? <ListSkeleton rows={2} /> : null}
      {runs.isError ? <ErrorState error={runs.error} title="Could not load the evaluation runs" onRetry={() => void runs.refetch()} /> : null}
      {runs.data && runs.data.length === 0 ? (
        <div className="card">
          <EmptyState icon={BeakerIcon} title="No evaluation runs yet" description="Run the first evaluation above to establish a baseline." />
        </div>
      ) : null}

      {list.length > 0 ? (
        <>
          <Card>
            <CardBody className="grid gap-4 md:grid-cols-2">
              <Field label="Run to inspect (after)" htmlFor="run-after" hint={after.data ? `${formatDateTime(after.data.created_at)} · engine ${after.data.engine}${after.data.with_asr_noise ? ' · ASR noise' : ''} · ${after.data.learned_aliases} learned aliases` : undefined}>
                <select id="run-after" className="input" value={afterId ?? ''} onChange={(event) => setAfterId(event.target.value)}>
                  {options.map((option) => (
                    <option key={option.id} value={option.id}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Compare with (before)" htmlFor="run-before" hint="Pick an earlier run to see the improvement over time.">
                <select id="run-before" className="input" value={beforeId ?? ''} onChange={(event) => setBeforeId(event.target.value || null)}>
                  <option value="">No comparison</option>
                  {options
                    .filter((option) => option.id !== afterId)
                    .map((option) => (
                      <option key={option.id} value={option.id}>
                        {option.label}
                      </option>
                    ))}
                </select>
              </Field>
            </CardBody>
          </Card>

          {after.isPending ? <LoadingBlock label="Loading the run…" /> : null}
          {after.isError ? <ErrorState error={after.error} title="Could not load this run" onRetry={() => void after.refetch()} /> : null}
          {after.data ? (
            <>
              <section aria-label="Metrics for the selected run" className="space-y-3">
                <h2 className="text-lg font-semibold text-neutral-900">
                  {runName(after.data)} <span className="text-sm font-normal text-neutral-500">· {after.data.metrics.cases} cases</span>
                </h2>
                <MetricGrid run={after.data} baseline={baseline} />
              </section>

              {baseline ? <BeforeAfter before={baseline} after={after.data} /> : null}
              {beforeId && beforeId !== afterId && before.isPending ? <LoadingBlock label="Loading the comparison run…" /> : null}

              <Card aria-labelledby="cases-title">
                <CardHeader id="cases-title" title="Per-case results" subtitle="Each scripted call, with its deliberate traps (tags)" icon={ChartBarIcon} />
                <CardBody className="!p-0">
                  {after.data.cases.length === 0 ? <EmptyState title="No per-case details" description="This run was stored without case results." /> : <CaseTable cases={after.data.cases} />}
                </CardBody>
              </Card>
            </>
          ) : null}
        </>
      ) : null}

      <Operations />
    </div>
  );
}
