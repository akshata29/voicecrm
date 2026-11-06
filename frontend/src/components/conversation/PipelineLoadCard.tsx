import { BoltIcon } from '@heroicons/react/24/outline';
import clsx from 'clsx';
import { usePipelineStats } from '@/api/hooks';
import type { PipelineStats, SeriesSummary } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { EM_DASH, formatClock, formatDurationMs, formatDurationSeconds, formatInt, formatUsd } from '@/lib/format';

/** A single click is not "load": the card appears once a batch of this many calls has been submitted. */
const MIN_BATCH = 5;

const STAGES: Array<{ key: string; label: string; hint: string }> = [
  { key: 'transcribe', label: 'Speech to text', hint: 'audio only; platform transcripts skip it' },
  { key: 'policy', label: 'Privacy filter', hint: 'PII detection and note policy' },
  { key: 'extract', label: 'Summarise and extract', hint: 'rules plus the model' },
];

const share = (part: number, whole: number) => (whole > 0 ? Math.min(100, (part / whole) * 100) : 0);

function Figure({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: 'danger' }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs font-medium uppercase tracking-wide text-neutral-500">{label}</dt>
      <dd className={clsx('tabular mt-0.5 truncate text-lg font-semibold leading-6', tone === 'danger' ? 'text-red-600' : 'text-neutral-900')}>{value}</dd>
      {hint ? <p className="truncate text-xs text-neutral-500">{hint}</p> : null}
    </div>
  );
}

function LatencyBar({ label, hint, series, scaleMs, unit }: { label: string; hint?: string; series: SeriesSummary | undefined; scaleMs: number; unit: 'ms' | 's' }) {
  const toMs = (value: number | null) => (value === null ? null : unit === 's' ? value * 1000 : value);
  const p50 = toMs(series?.p50 ?? null);
  const p95 = toMs(series?.p95 ?? null);
  const show = (value: number | null) => (value === null ? EM_DASH : unit === 's' ? formatDurationSeconds(value / 1000) : formatDurationMs(value));
  return (
    <li className="grid grid-cols-[10rem_1fr_9.5rem] items-center gap-3 text-sm">
      <span className="truncate text-neutral-700" title={hint}>
        {label}
      </span>
      <span className="relative h-2.5 overflow-hidden rounded-full bg-neutral-100" aria-hidden="true">
        {p95 !== null ? <span className="absolute inset-y-0 left-0 rounded-full bg-brand-200" style={{ width: `${share(p95, scaleMs)}%` }} /> : null}
        {p50 !== null ? <span className="absolute inset-y-0 left-0 rounded-full bg-brand-500" style={{ width: `${share(p50, scaleMs)}%` }} /> : null}
      </span>
      <span className="tabular text-right text-xs text-neutral-600">
        p50 {show(p50)} <span className="text-neutral-400">/</span> p95 {show(p95)}
      </span>
    </li>
  );
}

export function PipelineLoadView({ stats }: { stats: PipelineStats }) {
  const finished = stats.completed + stats.failed;
  const rate = stats.recent_calls_per_minute ?? stats.calls_per_minute;
  const stageRows = STAGES.filter((stage) => (stats.stages_ms[stage.key]?.count ?? 0) > 0);
  const scaleMs = Math.max(
    1,
    ...stageRows.map((stage) => stats.stages_ms[stage.key]?.p95 ?? 0),
    (stats.queue_wait_seconds.p95 ?? 0) * 1000,
    (stats.end_to_end_seconds.p95 ?? 0) * 1000,
  );
  const engines = Object.entries(stats.engines);
  return (
    <Card aria-labelledby="load-title" className="mb-5">
      <CardHeader
        id="load-title"
        icon={BoltIcon}
        title="Pipeline throughput"
        subtitle={`${formatInt(finished)} of ${formatInt(stats.submitted)} calls finished, ${stats.max_concurrency} processed in parallel`}
        actions={<Badge tone={stats.active ? 'info' : stats.failed > 0 ? 'warning' : 'success'}>{stats.active ? 'Processing' : 'Batch complete'}</Badge>}
      />
      <CardBody className="space-y-4">
        <div
          role="progressbar"
          aria-label="Calls finished"
          aria-valuemin={0}
          aria-valuemax={stats.submitted}
          aria-valuenow={finished}
          className="flex h-3 overflow-hidden rounded-full bg-neutral-100"
        >
          <span className="bg-emerald-500 transition-all duration-500" style={{ width: `${share(stats.completed, stats.submitted)}%` }} />
          <span className="bg-red-500 transition-all duration-500" style={{ width: `${share(stats.failed, stats.submitted)}%` }} />
          <span className="animate-pulse-soft bg-brand-300 transition-all duration-500" style={{ width: `${share(stats.in_flight, stats.submitted)}%` }} />
        </div>

        <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-4 xl:grid-cols-8">
          <Figure label="Calls / min" value={rate ? formatInt(Math.round(rate)) : EM_DASH} hint={stats.calls_per_minute ? `${(stats.calls_per_minute / 60).toFixed(1)} per second overall` : undefined} />
          <Figure label={stats.active ? 'Time left' : 'Elapsed'} value={stats.active ? (stats.eta_seconds != null ? formatClock(stats.eta_seconds) : EM_DASH) : formatClock(stats.elapsed_seconds)} hint={stats.active ? `${formatClock(stats.elapsed_seconds)} so far` : undefined} />
          <Figure label="Queued" value={formatInt(stats.queued)} hint="waiting for a slot" />
          <Figure label="In flight" value={`${stats.in_flight} / ${stats.max_concurrency}`} hint="being processed" />
          <Figure label="Failed" value={formatInt(stats.failed)} tone={stats.failed > 0 ? 'danger' : undefined} hint={stats.fallbacks > 0 ? `${stats.fallbacks} fell back to rules` : undefined} />
          <Figure label="Model tokens" value={formatInt(stats.tokens_in + stats.tokens_out)} hint={`${formatInt(stats.tokens_in)} in, ${formatInt(stats.tokens_out)} out`} />
          <Figure label="Est. cost" value={formatUsd(stats.cost_usd, { est: false })} hint={stats.cost_illustrative ? 'illustrative prices' : undefined} />
          <Figure label="Per call" value={finished > 0 ? formatUsd(stats.cost_usd / finished, { est: false }) : EM_DASH} hint={engines.length > 0 ? engines.map(([name, count]) => `${count} ${name}`).join(', ') : undefined} />
        </dl>

        {stageRows.length > 0 || (stats.end_to_end_seconds.count ?? 0) > 0 ? (
          <div>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-neutral-500">Time per stage</h3>
            <ul className="space-y-2">
              {stageRows.map((stage) => (
                <LatencyBar key={stage.key} label={stage.label} hint={stage.hint} series={stats.stages_ms[stage.key]} scaleMs={scaleMs} unit="ms" />
              ))}
              <LatencyBar label="Waiting in the queue" hint="from submit until a slot is free" series={stats.queue_wait_seconds} scaleMs={scaleMs} unit="s" />
              <LatencyBar label="End to end" hint="from submit until the proposal is ready" series={stats.end_to_end_seconds} scaleMs={scaleMs} unit="s" />
            </ul>
          </div>
        ) : null}

        {stats.errors.length > 0 ? (
          <ul className="space-y-1 rounded-md bg-red-50 p-3 text-xs text-red-800" aria-label="Failures">
            {stats.errors.map((item) => (
              <li key={item.error}>
                <span className="tabular font-semibold">{item.count}x</span> {item.error}
              </li>
            ))}
          </ul>
        ) : null}
      </CardBody>
    </Card>
  );
}

/** Live throughput of the current or most recent batch; renders nothing until a real batch has been submitted. */
export function PipelineLoadCard() {
  const stats = usePipelineStats();
  if (!stats.data || stats.data.submitted < MIN_BATCH) return null;
  return <PipelineLoadView stats={stats.data} />;
}
