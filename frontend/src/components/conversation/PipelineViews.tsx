import { CheckCircleIcon, ClockIcon, ExclamationCircleIcon, EyeIcon, MinusCircleIcon } from '@heroicons/react/24/solid';
import clsx from 'clsx';
import type { ConversationDetail, Stage, StageRecordView, UsageRecord } from '@/api/types';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { Spinner } from '@/components/ui/Spinner';
import { formatDurationMs, formatInt, formatUsd, humanize } from '@/lib/format';
import { STAGE_HINTS, STAGE_LABELS } from '@/lib/labels';
import { ArrowsRightLeftIcon } from '@heroicons/react/24/outline';

/** The seven automatic stages shown on the compact Inbox stepper (review/write/retention follow approval). */
export const AUTOMATIC_STAGES: readonly Stage[] = ['ingest', 'transcribe', 'normalize', 'policy', 'extract', 'resolve', 'validate'];

type StepState = 'done' | 'active' | 'pending' | 'failed';

function stepState(stage: Stage, stages: StageRecordView[]): StepState {
  const record = stages.find((item) => item.stage === stage);
  if (!record) return 'pending';
  if (record.status === 'failed') return 'failed';
  if (record.status === 'started') return 'active';
  return 'done';
}

/** Compact live pipeline progress for processing rows in the Inbox. */
export function PipelineStepper({ stages, className }: { stages: StageRecordView[]; className?: string }) {
  const states = AUTOMATIC_STAGES.map((stage) => ({ stage, state: stepState(stage, stages) }));
  const active = states.find((item) => item.state === 'active') ?? [...states].reverse().find((item) => item.state === 'done');
  const doneCount = states.filter((item) => item.state === 'done').length;
  return (
    <div className={className} role="group" aria-label="Pipeline progress">
      <div className="flex gap-1" aria-hidden="true">
        {states.map(({ stage, state }) => (
          <span
            key={stage}
            title={`${STAGE_LABELS[stage]}: ${state === 'done' ? 'done' : state === 'active' ? 'running' : state === 'failed' ? 'failed' : 'waiting'}`}
            className={clsx(
              'h-1.5 flex-1 rounded-full transition-colors',
              state === 'done' && 'bg-emerald-500',
              state === 'active' && 'animate-pulse-soft bg-brand-500',
              state === 'failed' && 'bg-red-500',
              state === 'pending' && 'bg-neutral-200',
            )}
          />
        ))}
      </div>
      <p className="mt-1.5 flex items-center gap-1.5 text-xs text-neutral-600">
        <Spinner className="h-3 w-3 text-brand-500" />
        <span className="font-medium text-neutral-800">{active ? `${STAGE_LABELS[active.stage]}${active.state === 'active' ? '\u2026' : ''}` : 'Queued'}</span>
        <span className="text-neutral-400">
          {doneCount} of {AUTOMATIC_STAGES.length} stages
        </span>
      </p>
    </div>
  );
}

// ---- full timeline ---------------------------------------------------------------------------------
const HIDDEN_DETAIL_KEYS = new Set(['text', 'quote', 'transcript', 'content', 'body', 'message', 'excerpt', 'explanation']);

const DETAIL_LABELS: Record<string, string> = {
  engine: 'engine',
  model: 'model',
  provider: 'provider',
  origin: 'transcript origin',
  reason: 'reason',
  payload: 'payload',
  segments: 'segments',
  speakers: 'speakers',
  mentions: 'product mentions',
  topics: 'topics',
  tasks: 'tasks',
  redacted_spans: 'PII spans redacted',
  omitted_segments: 'segments omitted',
  small_talk_segments: 'small-talk segments',
  records_created: 'CRM records',
  decision: 'decision',
  clicks: 'clicks',
  language: 'language',
  market: 'market',
  error: 'error',
};

function detailChips(detail: Record<string, unknown>): string[] {
  const chips: string[] = [];
  for (const [key, value] of Object.entries(detail)) {
    if (HIDDEN_DETAIL_KEYS.has(key)) continue;
    if (value === null || value === undefined || value === '') continue;
    if (typeof value === 'object') continue;
    if (key === 'audio_seconds' && typeof value === 'number') {
      chips.push(`${(value / 60).toFixed(1)} min audio`);
      continue;
    }
    if (typeof value === 'boolean') {
      chips.push(`${DETAIL_LABELS[key] ?? humanize(key).toLowerCase()}: ${value ? 'yes' : 'no'}`);
      continue;
    }
    if (typeof value === 'number' && !Number.isInteger(value)) {
      chips.push(`${DETAIL_LABELS[key] ?? humanize(key).toLowerCase()}: ${Math.abs(value) < 1 ? value.toFixed(2) : value.toFixed(1)}`);
      continue;
    }
    const text = String(value);
    chips.push(`${DETAIL_LABELS[key] ?? humanize(key).toLowerCase()}: ${text.length > 70 ? `${text.slice(0, 69)}\u2026` : text}`);
  }
  return chips.slice(0, 5);
}

function usageLine(usage: UsageRecord): string {
  switch (usage.service) {
    case 'speech':
      return `${usage.provider === 'azure_speech' ? 'Azure Speech' : 'Speech'} \u00b7 ${((usage.audio_seconds ?? 0) / 60).toFixed(1)} min audio`;
    case 'llm':
      return `${usage.model ?? 'LLM'} \u00b7 ${formatInt(usage.tokens_in)} in / ${formatInt(usage.tokens_out)} out tokens`;
    case 'language_pii':
      return `PII detection (${usage.provider}) \u00b7 ${formatInt(usage.units)} records`;
    case 'search':
      return `Catalogue search (${usage.provider}) \u00b7 ${formatInt(usage.units)} queries`;
    default:
      return `${humanize(usage.service)} \u00b7 ${formatInt(usage.units)} units`;
  }
}

function StageIcon({ status, waiting }: { status: StageRecordView['status']; waiting: boolean }) {
  if (status === 'started' && waiting) return <EyeIcon aria-hidden="true" className="h-5 w-5 text-amber-500" />;
  if (status === 'started') return <Spinner className="h-5 w-5 text-brand-500" />;
  if (status === 'failed') return <ExclamationCircleIcon aria-hidden="true" className="h-5 w-5 text-red-500" />;
  if (status === 'skipped') return <MinusCircleIcon aria-hidden="true" className="h-5 w-5 text-neutral-400" />;
  return <CheckCircleIcon aria-hidden="true" className="h-5 w-5 text-emerald-500" />;
}

export function PipelineTimeline({ detail }: { detail: ConversationDetail }) {
  const { stages, usage, cost, record } = detail;
  const result = record.proposal?.result;
  const totalMs = stages.reduce((total, stage) => total + (stage.duration_ms ?? 0), 0);
  const automatic = stages.filter((stage) => (AUTOMATIC_STAGES as readonly string[]).includes(stage.stage));
  const readyMs = automatic.reduce((total, stage) => total + (stage.duration_ms ?? 0), 0);

  return (
    <Card>
      <CardHeader
        title="Pipeline"
        subtitle={result ? `Engine: ${result.engine}${result.model ? ` \u00b7 model: ${result.model}` : ''}` : 'Stage, status, duration, usage'}
        icon={ArrowsRightLeftIcon}
        actions={
          <span className="tabular text-xs text-neutral-500" title="Ingest to review-ready">
            {formatDurationMs(readyMs || totalMs)}
          </span>
        }
      />
      <CardBody className="!p-0">
        {stages.length === 0 ? (
          <p className="flex items-center gap-2 px-5 py-6 text-sm text-neutral-500">
            <ClockIcon aria-hidden="true" className="h-4 w-4" /> Waiting for the first stage…
          </p>
        ) : (
          <ol className="divide-y divide-neutral-100">
            {stages.map((stage) => {
              const stageUsage = usage.filter((item) => item.stage === stage.stage);
              const chips = detailChips(stage.detail);
              // The review stage stays "started" until a human decides: that is waiting, not work in progress.
              const waiting = stage.stage === 'review' && stage.status === 'started' && record.status === 'needs_review';
              return (
                <li key={`${stage.stage}-${stage.started_at ?? ''}`} className="flex gap-3 px-4 py-3 sm:px-5">
                  <span className="mt-0.5 shrink-0">
                    <StageIcon status={stage.status} waiting={waiting} />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline justify-between gap-2">
                      <p className="text-sm font-medium text-neutral-900" title={STAGE_HINTS[stage.stage]}>
                        {STAGE_LABELS[stage.stage]}
                        {stage.status === 'skipped' ? <span className="ml-1.5 text-xs font-normal text-neutral-400">skipped</span> : null}
                        {stage.status === 'started' && waiting ? <span className="ml-1.5 text-xs font-normal text-amber-700">waiting for you</span> : null}
                        {stage.status === 'started' && !waiting ? <span className="ml-1.5 text-xs font-normal text-brand-600">running</span> : null}
                        {stage.status === 'failed' ? <span className="ml-1.5 text-xs font-normal text-red-600">failed</span> : null}
                      </p>
                      <span className="tabular shrink-0 text-xs text-neutral-500">{stage.duration_ms === null ? '' : formatDurationMs(stage.duration_ms)}</span>
                    </div>
                    {chips.length > 0 ? (
                      <p className="mt-0.5 flex flex-wrap gap-x-2 gap-y-0.5 text-xs text-neutral-500">
                        {chips.map((chip) => (
                          <span key={chip}>{chip}</span>
                        ))}
                      </p>
                    ) : null}
                    {stageUsage.map((item) => (
                      <p key={`${item.service}-${item.ts}`} className="mt-1 inline-block rounded bg-neutral-100 px-1.5 py-0.5 text-xs text-neutral-700">
                        {usageLine(item)}
                      </p>
                    ))}
                  </div>
                </li>
              );
            })}
          </ol>
        )}
        {cost.lines.length > 0 ? (
          <div className="border-t border-neutral-200 bg-neutral-50 px-4 py-3 sm:px-5">
            <div className="flex items-baseline justify-between">
              <p className="text-xs font-semibold uppercase tracking-wide text-neutral-500">Usage and cost</p>
              <p className="tabular text-sm font-semibold text-neutral-900" title="Illustrative price table - not a quote">
                {formatUsd(cost.total)}
              </p>
            </div>
            <dl className="mt-1.5 space-y-0.5 text-xs text-neutral-600">
              {cost.lines.map((line) => (
                <div key={line.service} className="flex justify-between gap-3">
                  <dt>
                    {line.service} <span className="text-neutral-400">({formatInt(line.quantity)} {line.unit})</span>
                  </dt>
                  <dd className="tabular">{formatUsd(line.est_cost)}</dd>
                </div>
              ))}
            </dl>
            <p className="mt-1.5 text-[11px] text-amber-800">{cost.illustrative ? 'Illustrative prices: validate against current Azure pricing before quoting.' : ''}</p>
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}
