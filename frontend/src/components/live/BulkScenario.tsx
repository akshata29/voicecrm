import { BoltIcon, StopIcon, TrashIcon } from '@heroicons/react/24/outline';
import { useQueryClient } from '@tanstack/react-query';
import clsx from 'clsx';
import { useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { getErrorMessage } from '@/api/client';
import { LOAD_ACTIVE_STATES, qk, useClearLoad, useConfig, useEngineOptions, useFixtures, useLoadEstimate, useLoadStatus, useStartLoad, useStopLoad } from '@/api/hooks';
import type { ConversationStatus, Engine, LoadRequest, LoadReport, LoadStatus, LoadVia, PreflightReadiness } from '@/api/types';
import { PipelineLoadView } from '@/components/conversation/PipelineLoadCard';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { ConfirmDialog } from '@/components/ui/ConfirmDialog';
import { Field } from '@/components/ui/Field';
import { ErrorState, Skeleton } from '@/components/ui/StateViews';
import { Spinner } from '@/components/ui/Spinner';
import { Toggle } from '@/components/ui/Toggle';
import { useToast } from '@/components/ui/Toast';
import { EM_DASH, formatDurationSeconds, formatInt, formatUsd, humanize } from '@/lib/format';
import { useDebouncedValue, useLocalStorageState, useOnChange } from '@/lib/hooks';
import { reasonLabel, STATUS_LABELS } from '@/lib/labels';
import { ScenarioHeading } from './ScenarioHeading';

interface Settings {
  count: number;
  via: LoadVia;
  rate: number;
  parallel: number | null;
  autoApprove: boolean;
  engine: Engine | null;
}

const DEFAULTS: Settings = { count: 200, via: 'transcript', rate: 0, parallel: null, autoApprove: true, engine: null };
const PRESETS = [50, 200, 800];
const RATES: Array<{ value: number; label: string }> = [
  { value: 0, label: 'All at once (a burst)' },
  { value: 5, label: 'Steady: 5 calls a second' },
  { value: 20, label: 'Steady: 20 calls a second' },
];
const ENGINE_LABELS: Record<Engine, string> = { rules: 'Rules only (no model cost)', hybrid: 'Model plus rules (hybrid)', llm: 'Model only' };

function Figure({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: ReactNode; tone?: 'danger' | 'success' }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs font-medium uppercase tracking-wide text-neutral-500">{label}</dt>
      <dd className={clsx('tabular mt-0.5 truncate text-lg font-semibold leading-6', tone === 'danger' ? 'text-red-600' : tone === 'success' ? 'text-emerald-700' : 'text-neutral-900')}>{value}</dd>
      {hint ? <p className="truncate text-xs text-neutral-500">{hint}</p> : null}
    </div>
  );
}

const share = (part: number, whole: number) => (whole > 0 ? Math.min(100, (part / whole) * 100) : 0);

// ---- running ---------------------------------------------------------------------------------------
function LoadBoard({ status, onStop, stopping }: { status: LoadStatus; onStop: () => void; stopping: boolean }) {
  const request = status.request;
  const ok = Math.max(status.finished - status.failed, 0);
  const winding = status.state === 'stopping';
  const waiting = status.state === 'preparing';
  return (
    <>
      <Card aria-labelledby="load-board-title" aria-live="polite" className="scroll-mt-4">
        <CardHeader
          id="load-board-title"
          icon={BoltIcon}
          title={`${waiting ? 'Starting' : winding ? 'Stopping' : 'Running'} ${formatInt(status.planned)} calls`}
          subtitle={
            request
              ? `${request.via === 'audio' ? 'Real audio through Azure Speech' : 'Platform transcripts'} \u00b7 ${status.parallel || '\u2026'} in parallel \u00b7 ${request.rate ? `${request.rate} calls a second` : 'all at once'}${request.auto_approve ? ' \u00b7 auto-approving the clean ones' : ''}`
              : undefined
          }
          actions={
            <>
              <Badge tone={winding ? 'warning' : 'info'} dot pulse>
                {waiting ? 'Preparing' : winding ? 'Stopping' : 'Running'}
              </Badge>
              <Button size="sm" variant="danger-outline" icon={StopIcon} loading={stopping} disabled={winding} onClick={onStop}>
                Stop
              </Button>
            </>
          }
        />
        <CardBody className="space-y-4">
          <div
            role="progressbar"
            aria-label="Calls finished"
            aria-valuemin={0}
            aria-valuemax={status.planned}
            aria-valuenow={status.finished}
            className="flex h-3.5 overflow-hidden rounded-full bg-neutral-100"
          >
            <span className="bg-emerald-500 transition-all duration-500" style={{ width: `${share(ok, status.planned)}%` }} />
            <span className="bg-red-500 transition-all duration-500" style={{ width: `${share(status.failed, status.planned)}%` }} />
            <span className="animate-pulse-soft bg-brand-300 transition-all duration-500" style={{ width: `${share(status.in_flight, status.planned)}%` }} />
          </div>
          {waiting ? (
            <p className="flex items-center gap-2 text-sm text-neutral-600">
              <Spinner className="h-4 w-4 text-brand-500" />
              Removing the previous run&rsquo;s calls from the Inbox, the vault and the CRM simulator&hellip;
            </p>
          ) : null}
          {winding ? <p className="text-sm text-amber-800">Stopping: the {formatInt(status.in_flight)} calls already in the pipeline finish, the ones not yet sent are dropped.</p> : null}
          <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3 lg:grid-cols-6">
            <Figure label="Finished" value={`${formatInt(status.finished)} / ${formatInt(status.planned)}`} />
            <Figure label="In flight" value={formatInt(status.in_flight)} hint="being processed now" />
            <Figure label="Waiting to send" value={formatInt(status.queued)} hint="held back by the arrival rate" />
            <Figure label="Failed" value={formatInt(status.failed)} tone={status.failed > 0 ? 'danger' : undefined} />
            <Figure label="Written to CRM" value={formatInt(status.approved)} tone={status.approved > 0 ? 'success' : undefined} hint="auto-approved" />
            <Figure label="In the Inbox" value={formatInt(status.stored_calls)} hint="tagged load test" />
          </dl>
        </CardBody>
      </Card>
      {status.pipeline.submitted > 0 ? <PipelineLoadView stats={status.pipeline} /> : null}
    </>
  );
}

// ---- finished --------------------------------------------------------------------------------------
function OutcomeChips({ report }: { report: LoadReport }) {
  const statuses = Object.entries(report.by_status).sort((left, right) => right[1] - left[1]);
  const reasons = Object.entries(report.review_reasons).sort((left, right) => right[1] - left[1]).slice(0, 6);
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <div>
        <h3 className="section-title mb-1.5">Where the calls ended up</h3>
        <ul className="flex flex-wrap gap-1.5" aria-label="Outcome by status">
          {statuses.map(([name, count]) => (
            <li key={name}>
              <Badge tone={name === 'failed' ? 'danger' : name === 'written' ? 'success' : name === 'needs_review' ? 'warning' : 'neutral'}>
                {formatInt(count)} {STATUS_LABELS[name as ConversationStatus] ?? humanize(name)}
              </Badge>
            </li>
          ))}
        </ul>
      </div>
      <div>
        <h3 className="section-title mb-1.5">Why some need a person</h3>
        {reasons.length === 0 ? (
          <p className="text-sm text-neutral-500">Nothing: every call was clean.</p>
        ) : (
          <ul className="space-y-0.5 text-sm text-neutral-700" aria-label="Review reasons">
            {reasons.map(([reason, count]) => (
              <li key={reason}>
                <span className="tabular font-semibold">{formatInt(count)}</span> {reasonLabel(reason)}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function LoadResult({ status, runnable, onRunAgain, onRemove, removing }: { status: LoadStatus; runnable: boolean; onRunAgain: () => void; onRemove: () => void; removing: boolean }) {
  const report = status.report;
  const failedRun = status.state === 'failed';
  const stopped = status.state === 'stopped';
  const modes = report ? Object.entries(report.modes).filter(([name]) => ['transcription', 'pii', 'llm', 'crm'].includes(name)) : [];
  return (
    <>
      <Card aria-labelledby="load-report-title">
        <CardHeader
          id="load-report-title"
          icon={BoltIcon}
          title={failedRun ? 'The run failed' : stopped ? 'Stopped early' : 'Run complete'}
          subtitle={report ? `${formatInt(report.finished)} of ${formatInt(report.planned)} calls finished in ${formatDurationSeconds(report.wall_seconds)}, ${report.parallel} in parallel` : status.run_id ? `Run ${status.run_id}` : undefined}
          actions={
            <>
              <Badge tone={failedRun ? 'danger' : stopped ? 'warning' : 'success'}>{failedRun ? 'Failed' : stopped ? 'Stopped' : 'Complete'}</Badge>
              <Button size="sm" variant="primary" icon={BoltIcon} disabled={!runnable} onClick={onRunAgain} title="Runs again with the settings above">
                Run again
              </Button>
            </>
          }
        />
        <CardBody className="space-y-5">
          {failedRun && status.error ? (
            <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-800">
              {status.error}
            </p>
          ) : null}
          {report ? (
            <>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-4 sm:grid-cols-4">
                <Figure label="Throughput" value={report.calls_per_minute ? `${formatInt(Math.round(report.calls_per_minute))} / min` : EM_DASH} hint={report.calls_per_minute ? `${(report.calls_per_minute / 60).toFixed(1)} calls a second over the whole run` : undefined} />
                <Figure label="Latency p50 / p95" value={`${formatDurationSeconds(report.latency_p50_seconds)} / ${formatDurationSeconds(report.latency_p95_seconds)}`} hint="submit to review-ready" />
                <Figure label="Straight-through" value={`${formatInt(report.straight_through)} of ${formatInt(report.finished)}`} hint={report.finished > 0 ? `${Math.round((report.straight_through / report.finished) * 100)}% need no human` : undefined} tone="success" />
                <Figure label="Written to the CRM" value={formatInt(report.approved)} hint={report.approved > 0 ? 'auto-approved, no clicks' : 'auto-approve was off'} />
                <Figure label="Failed" value={formatInt(report.failed)} tone={report.failed > 0 ? 'danger' : undefined} hint={report.pipeline.fallbacks > 0 ? `${report.pipeline.fallbacks} fell back to rules` : undefined} />
                <Figure label="Model tokens" value={formatInt(report.pipeline.tokens_in + report.pipeline.tokens_out)} hint={`${formatInt(report.pipeline.tokens_in)} in, ${formatInt(report.pipeline.tokens_out)} out`} />
                <Figure label="Est. cost" value={formatUsd(report.cost_usd, { est: false })} hint={report.pipeline.cost_illustrative ? 'list prices, illustrative' : undefined} />
                <Figure label="Per call" value={report.cost_per_call_usd === null ? EM_DASH : formatUsd(report.cost_per_call_usd, { est: false })} />
              </dl>
              <OutcomeChips report={report} />
              {report.projections.length > 0 ? (
                <p className="rounded-md bg-neutral-50 px-3 py-2 text-sm text-neutral-700">
                  <span className="font-semibold text-neutral-900">Projected: </span>
                  {report.projections.map((item) => `${formatInt(item.calls)} calls in ${formatDurationSeconds(item.seconds)}`).join(', ')}
                  <span className="text-neutral-500"> ({report.projection_basis}).</span>
                </p>
              ) : null}
              {report.errors.length > 0 ? (
                <ul className="space-y-1 rounded-md bg-red-50 p-3 text-xs text-red-800" aria-label="Failures">
                  {report.errors.map((item) => (
                    <li key={item.error}>
                      <span className="tabular font-semibold">{item.count}x</span> {item.error}
                    </li>
                  ))}
                </ul>
              ) : null}
              <p className="text-xs text-neutral-500">
                Services: {modes.map(([name, mode]) => `${name === 'transcription' ? 'speech' : name} ${mode}`).join(' \u00b7 ')}
              </p>
            </>
          ) : null}
          <div className="flex flex-wrap items-center gap-2 border-t border-neutral-100 pt-4">
            <Link to="/" className="btn btn-secondary">
              Open the Inbox ({formatInt(status.stored_calls)} load-test calls)
            </Link>
            <Button variant="danger-outline" icon={TrashIcon} loading={removing} disabled={status.stored_calls === 0} onClick={onRemove}>
              Remove load-test calls
            </Button>
          </div>
        </CardBody>
      </Card>
      {report && report.pipeline.submitted > 0 ? <PipelineLoadView stats={report.pipeline} /> : null}
    </>
  );
}

// ---- the scenario ----------------------------------------------------------------------------------
/** Scenario C: send hundreds of calls through the real pipeline from the UI, watch the throughput, and read what it took. */
export function BulkScenario({ verdict }: { verdict: PreflightReadiness | undefined }) {
  const client = useQueryClient();
  const toast = useToast();
  const config = useConfig();
  const fixtures = useFixtures();
  const status = useLoadStatus();
  const { engines, modelLive } = useEngineOptions();
  const start = useStartLoad();
  const stop = useStopLoad();
  const clear = useClearLoad();
  const [stored, setStored] = useLocalStorageState<Settings>('voicecrm.live.bulk.v1', DEFAULTS);
  const [confirmStart, setConfirmStart] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);

  const settings: Settings = { ...DEFAULTS, ...stored };
  const update = (patch: Partial<Settings>) => setStored({ ...settings, ...patch });
  const active = status.data ? LOAD_ACTIVE_STATES.has(status.data.state) : false;
  const maxCalls = status.data?.max_calls ?? 2000;
  const crmLive = config.data?.modes.crm?.live === true;
  const speechLive = config.data?.modes.transcription?.live === true;
  const recorded = (fixtures.data ?? []).filter((fixture) => (fixture.recording_seconds ?? 0) > 0).length;
  const audioBlocked = settings.via === 'audio' && speechLive && recorded === 0 && fixtures.isSuccess;
  const valid = Number.isInteger(settings.count) && settings.count >= 1 && settings.count <= maxCalls && !audioBlocked;

  const request = useMemo<LoadRequest>(
    () => ({
      count: settings.count,
      via: settings.via,
      rate: settings.rate,
      auto_approve: settings.autoApprove && !crmLive,
      engine: modelLive ? settings.engine : null,
      parallel: settings.parallel,
    }),
    [settings.count, settings.via, settings.rate, settings.autoApprove, settings.engine, settings.parallel, crmLive, modelLive],
  );
  const settled = useDebouncedValue(request, 400);
  const estimate = useLoadEstimate(valid ? settled : null);
  const sized = valid && settled === request && estimate.data !== undefined && !estimate.isPlaceholderData ? estimate.data : null;

  // When a run ends, everything that shows calls, CRM rows and metrics has changed.
  useOnChange(status.data?.state, (state, previous) => {
    if (previous && LOAD_ACTIVE_STATES.has(previous) && state && !LOAD_ACTIVE_STATES.has(state)) {
      for (const key of [qk.conversations, qk.metrics, qk.crmAll, qk.pipelineStats, qk.vault]) void client.invalidateQueries({ queryKey: key });
    }
  });
  // A run that starts below the fold should not need a scroll to be seen.
  useOnChange(active, (now, before) => {
    if (now && !before) document.querySelector('section[aria-labelledby="load-board-title"]')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });

  const launch = (confirm: boolean) =>
    start.mutate(
      { ...request, confirm },
      {
        onSettled: () => setConfirmStart(false),
        onError: (error) => toast.error('Could not start the run', { message: getErrorMessage(error) }),
      },
    );
  const begin = () => {
    if (!sized) return;
    if (sized.billed.length > 0) setConfirmStart(true);
    else launch(false);
  };
  const remove = () =>
    clear.mutate(undefined, {
      onSuccess: (result) => toast.success(`Removed ${formatInt(result.deleted)} load-test calls`, { message: 'They are gone from the Inbox, the vault and the local CRM simulator.' }),
      onError: (error) => toast.error('Could not remove them', { message: getErrorMessage(error) }),
      onSettled: () => setConfirmRemove(false),
    });

  const data = status.data;
  const finishedRun = data && !active && (data.state === 'finished' || data.state === 'stopped' || data.state === 'failed');
  const left = data && !active ? data.stored_calls : 0;

  return (
    <section id="scenario-c" aria-labelledby="scenario-c-title" className="scroll-mt-4 space-y-4">
      <ScenarioHeading
        id="scenario-c-title"
        letter="C"
        title="Hundreds of calls at once"
        description="The scripted calls are cycled with unique ids and sent through the real pipeline in parallel: each is transcribed, privacy-filtered, summarised, matched to a CRM account and queued. Watch the throughput, then read what it took and what it cost."
        verdict={verdict}
      />

      {left > 0 && !finishedRun ? (
        <p className="flex flex-wrap items-center gap-3 rounded-lg border border-sky-200 bg-sky-50 px-4 py-2.5 text-sm text-sky-900">
          <span>
            {formatInt(left)} load-test calls from the last run are in the Inbox. The next run replaces them.
          </span>
          <Link to="/" className="font-medium underline">
            Open the Inbox
          </Link>
          <button type="button" className="font-medium underline" onClick={() => setConfirmRemove(true)}>
            Remove them
          </button>
        </p>
      ) : null}

      <Card aria-labelledby="bulk-settings-title">
        <CardHeader id="bulk-settings-title" icon={BoltIcon} title="Set up the run" subtitle="Nothing is spent until you press Start; a billed run asks you to confirm first" />
        <CardBody>
          <fieldset disabled={active} className="grid min-w-0 gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,24rem)]">
            <legend className="sr-only">Load test settings</legend>
            <div className="space-y-5">
              <div>
                <span className="label">How many calls</span>
                <div className="flex flex-wrap items-center gap-2">
                  {PRESETS.map((preset) => (
                    <button
                      key={preset}
                      type="button"
                      aria-pressed={settings.count === preset}
                      onClick={() => update({ count: preset })}
                      className={clsx('btn btn-sm', settings.count === preset ? 'btn-primary' : 'btn-secondary')}
                    >
                      {formatInt(preset)}
                    </button>
                  ))}
                  <label className="ml-1 flex items-center gap-2 text-sm text-neutral-600">
                    <span>or</span>
                    <input
                      type="number"
                      min={1}
                      max={maxCalls}
                      inputMode="numeric"
                      aria-label="Number of calls"
                      className="input w-28"
                      value={Number.isFinite(settings.count) ? settings.count : ''}
                      onChange={(event) => update({ count: Math.floor(Number(event.target.value)) || 0 })}
                    />
                  </label>
                </div>
                <p className="hint">Up to {formatInt(maxCalls)} per run (VOICECRM_LOAD_MAX_CALLS). The 18 scripted calls repeat, each with a fresh id.</p>
              </div>

              <div>
                <span className="label">What goes in</span>
                <div className="grid gap-2 sm:grid-cols-2" role="radiogroup" aria-label="Input">
                  {(
                    [
                      { value: 'transcript', title: 'Platform transcripts', hint: 'Teams, Zoom and Genesys text, as their webhooks deliver it. No speech charge.' },
                      { value: 'audio', title: 'Real audio', hint: `${recorded} recordings go through ${speechLive ? 'Azure Speech' : 'the mock transcriber'}. Slower; Speech is billed per audio hour.` },
                    ] as Array<{ value: LoadVia; title: string; hint: string }>
                  ).map((option) => (
                    <label
                      key={option.value}
                      className={clsx(
                        'cursor-pointer rounded-lg border p-3 text-sm transition-colors focus-within:ring-2 focus-within:ring-brand-500',
                        settings.via === option.value ? 'border-brand-500 bg-brand-50' : 'border-neutral-200 bg-white hover:bg-neutral-50',
                      )}
                    >
                      <input type="radio" name="bulk-via" className="sr-only" checked={settings.via === option.value} onChange={() => update({ via: option.value })} />
                      <span className="block font-medium text-neutral-900">{option.title}</span>
                      <span className="mt-0.5 block text-xs text-neutral-500">{option.hint}</span>
                    </label>
                  ))}
                </div>
                {audioBlocked ? (
                  <p className="mt-1.5 text-xs text-amber-800">
                    There are no recordings yet: run <code>scripts\make_audio.bat</code> once, or choose platform transcripts.
                  </p>
                ) : null}
              </div>

              <div className="grid gap-4 sm:grid-cols-3">
                <Field label="Arrival" htmlFor="bulk-rate" hint="How fast calls are handed over.">
                  <select id="bulk-rate" className="input" value={settings.rate} onChange={(event) => update({ rate: Number(event.target.value) })}>
                    {RATES.map((rate) => (
                      <option key={rate.value} value={rate.value}>
                        {rate.label}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Calls in parallel" htmlFor="bulk-parallel" hint={sized ? `Automatic is ${sized.parallel}.` : 'Automatic when empty.'}>
                  <input
                    id="bulk-parallel"
                    type="number"
                    min={1}
                    max={256}
                    inputMode="numeric"
                    className="input"
                    placeholder={sized ? `auto (${sized.parallel})` : 'auto'}
                    value={settings.parallel ?? ''}
                    onChange={(event) => update({ parallel: event.target.value === '' ? null : Math.min(256, Math.max(1, Math.floor(Number(event.target.value)) || 1)) })}
                  />
                </Field>
                {modelLive && engines.length > 1 ? (
                  <Field label="Summaries" htmlFor="bulk-engine" hint="Rules only skips the model cost.">
                    <select id="bulk-engine" className="input" value={settings.engine ?? ''} onChange={(event) => update({ engine: event.target.value === '' ? null : (event.target.value as Engine) })}>
                      <option value="">Default ({config.data?.engine ?? 'hybrid'})</option>
                      {engines.map((engine) => (
                        <option key={engine} value={engine}>
                          {ENGINE_LABELS[engine]}
                        </option>
                      ))}
                    </select>
                  </Field>
                ) : null}
              </div>

              <Toggle
                label="Auto-approve the calls that need no human"
                description={
                  crmLive
                    ? 'Off: a load test never writes to a live CRM. Use the local CRM simulator for bulk runs.'
                    : 'Clean calls are written to the local CRM simulator with zero clicks, the way the auto-approve policy would. The rest wait in the Inbox for a person.'
                }
                checked={settings.autoApprove && !crmLive}
                disabled={crmLive}
                onChange={(value) => update({ autoApprove: value })}
                className="rounded-lg border border-neutral-200 bg-white px-3 py-2.5"
              />
            </div>

            <div className="space-y-3">
              <div className="rounded-lg border border-neutral-200 bg-neutral-50 p-4" aria-live="polite">
                <h3 className="section-title">This run will</h3>
                {!valid ? (
                  <p className="mt-2 text-sm text-neutral-600">{audioBlocked ? 'Pick platform transcripts or make the recordings first.' : `Choose between 1 and ${formatInt(maxCalls)} calls.`}</p>
                ) : estimate.isError ? (
                  <p role="alert" className="mt-2 text-sm text-red-700">
                    {getErrorMessage(estimate.error)}
                  </p>
                ) : estimate.data ? (
                  <div className={clsx('transition-opacity', sized ? 'opacity-100' : 'opacity-60')}>
                    <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-3">
                      <Figure label="Calls" value={formatInt(estimate.data.calls)} hint={`${estimate.data.pool} scripted calls, repeated`} />
                      <Figure label="Takes about" value={formatDurationSeconds(estimate.data.seconds)} hint={`${formatInt(estimate.data.calls_per_minute)} calls a minute`} />
                      <Figure label="Model tokens" value={estimate.data.tokens_in + estimate.data.tokens_out > 0 ? formatInt(estimate.data.tokens_in + estimate.data.tokens_out) : 'none'} hint={estimate.data.tokens_in > 0 ? `${formatInt(estimate.data.tokens_in)} in, ${formatInt(estimate.data.tokens_out)} out` : 'no model in this run'} />
                      <Figure label="Cost at list prices" value={estimate.data.billed.length > 0 ? formatUsd(estimate.data.cost_usd, { est: false }) : '$0.00'} hint={estimate.data.billed.length > 0 && estimate.data.illustrative ? 'illustrative prices' : 'nothing billed'} />
                    </dl>
                    <p className="mt-3 flex flex-wrap items-center gap-1.5 text-xs text-neutral-600">
                      <span className="font-semibold text-neutral-800">Billed:</span>
                      {estimate.data.billed.length > 0 ? estimate.data.billed.map((name) => <Badge key={name}>{name}</Badge>) : <span>nothing, everything runs locally or as a mock</span>}
                    </p>
                    {estimate.data.warnings.map((warning) => (
                      <p key={warning} className="mt-1.5 text-xs text-amber-800">
                        {warning}
                      </p>
                    ))}
                    <p className="mt-2 text-[11px] leading-4 text-neutral-500">{estimate.data.basis}</p>
                  </div>
                ) : (
                  <div className="mt-2 space-y-2" role="status" aria-label="Sizing the run">
                    <Skeleton className="h-6 w-2/3" />
                    <Skeleton className="h-6 w-1/2" />
                  </div>
                )}
              </div>
              <Button variant="primary" size="lg" icon={BoltIcon} className="w-full" loading={start.isPending && !confirmStart} disabled={!sized} onClick={begin}>
                {valid ? `Start ${formatInt(settings.count)}-call run` : 'Start the run'}
              </Button>
            </div>
          </fieldset>
        </CardBody>
      </Card>

      {status.isError ? <ErrorState error={status.error} title="Could not read the run status" compact onRetry={() => void status.refetch()} /> : null}
      {data && active ? <LoadBoard status={data} stopping={stop.isPending} onStop={() => stop.mutate(undefined, { onError: (error) => toast.error('Could not stop the run', { message: getErrorMessage(error) }) })} /> : null}
      {data && finishedRun ? <LoadResult status={data} runnable={sized !== null} removing={clear.isPending && !confirmRemove} onRunAgain={begin} onRemove={() => setConfirmRemove(true)} /> : null}

      <ConfirmDialog
        open={confirmStart}
        title="Start a run that is billed?"
        description="This sends real requests to Azure. Prices are list prices from data/pricing.yaml: a guide, not a quote."
        confirmLabel={`Start ${sized ? formatInt(sized.calls) : ''} calls`}
        loading={start.isPending}
        onConfirm={() => launch(true)}
        onCancel={() => setConfirmStart(false)}
      >
        {sized ? (
          <dl className="space-y-1.5 text-sm">
            <div className="flex justify-between gap-3">
              <dt className="text-neutral-500">Billed services</dt>
              <dd className="text-right font-medium text-neutral-900">{sized.billed.join(', ')}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-neutral-500">Estimated cost</dt>
              <dd className="tabular font-semibold text-neutral-900">{formatUsd(sized.cost_usd, { est: false })}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-neutral-500">Model tokens</dt>
              <dd className="tabular text-neutral-900">{formatInt(sized.tokens_in + sized.tokens_out)}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-neutral-500">Takes about</dt>
              <dd className="tabular text-neutral-900">{formatDurationSeconds(sized.seconds)}</dd>
            </div>
          </dl>
        ) : null}
      </ConfirmDialog>

      <ConfirmDialog
        open={confirmRemove}
        title="Remove the load-test calls?"
        description={`${formatInt(data?.stored_calls ?? 0)} calls are removed from the Inbox, the transcript vault, the state store and the local CRM simulator (the phone calls, tasks and insights they wrote). Calls made any other way are not touched.`}
        confirmLabel="Remove them"
        tone="danger"
        loading={clear.isPending}
        onConfirm={remove}
        onCancel={() => setConfirmRemove(false)}
      />
    </section>
  );
}
