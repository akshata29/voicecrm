import { CubeIcon, PlayCircleIcon, TagIcon, ClipboardDocumentCheckIcon } from '@heroicons/react/24/outline';
import { CheckCircleIcon, CheckIcon, ExclamationCircleIcon, MinusCircleIcon } from '@heroicons/react/24/solid';
import clsx from 'clsx';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { getErrorMessage } from '@/api/client';
import { useApprove, useConfig, useConversation } from '@/api/hooks';
import type { ConversationDetail, ProductMention, ResolvedMode, Stage, StageRecordView } from '@/api/types';
import { StatusBadge } from '@/components/conversation/Badges';
import { TranscriptPanel } from '@/components/conversation/TranscriptPanel';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { ErrorState, LoadingBlock } from '@/components/ui/StateViews';
import { Spinner } from '@/components/ui/Spinner';
import { useToast } from '@/components/ui/Toast';
import { EM_DASH, formatBytes, formatDurationMs, formatDurationSeconds, formatInt, formatUsd, secondsBetween } from '@/lib/format';
import { useNow, useScrollIntoViewOnMount } from '@/lib/hooks';
import { CHANNEL_LABELS, reasonLabel } from '@/lib/labels';

type StepState = 'waiting' | 'running' | 'done' | 'skipped' | 'failed';

interface StepView {
  key: string;
  title: string;
  state: StepState;
  service: { label: string; live: boolean } | null;
  summary: string | null;
  ms: number | null;
}

const num = (value: unknown): number | null => (typeof value === 'number' && Number.isFinite(value) ? value : null);
const str = (value: unknown): string | null => (typeof value === 'string' && value !== '' ? value : null);

function sumCounts(value: unknown): number {
  if (!value || typeof value !== 'object') return 0;
  return Object.values(value as Record<string, unknown>).reduce<number>((total, item) => total + (num(item) ?? 0), 0);
}

function plural(count: number, noun: string): string {
  return `${formatInt(count)} ${noun}${count === 1 ? '' : 's'}`;
}

function stateOf(stages: Stage[], by: Map<Stage, StageRecordView>): StepState {
  const records = stages.map((stage) => by.get(stage));
  if (records.some((record) => record?.status === 'failed')) return 'failed';
  if (records.every((record) => record?.status === 'skipped')) return 'skipped';
  if (records.every((record) => record && (record.status === 'completed' || record.status === 'skipped'))) return 'done';
  return records.some(Boolean) ? 'running' : 'waiting';
}

/** One row per stage group, naming the service that did the work and what it produced (never any transcript text). */
function buildSteps(detail: ConversationDetail, via: 'audio' | 'transcript', modes: Record<string, ResolvedMode> | undefined): StepView[] {
  const { record, usage } = detail;
  const by = new Map(detail.stages.map((stage) => [stage.stage, stage] as const));
  const info = (stage: Stage): Record<string, unknown> => by.get(stage)?.detail ?? {};
  const live = (key: string) => modes?.[key]?.live === true;
  const ms = (stages: Stage[]): number | null => {
    const values = stages.map((stage) => by.get(stage)?.duration_ms).filter((value): value is number => typeof value === 'number');
    return values.length > 0 ? values.reduce((total, value) => total + value, 0) : null;
  };

  const ingest = info('ingest');
  const audioIn = (str(ingest.kind) ?? via) === 'audio';
  const transcribe = info('transcribe');
  const speech = usage.find((item) => item.service === 'speech');
  const speechProvider = str(transcribe.provider) ?? speech?.provider ?? null;
  const speechLive = speechProvider === 'azure_speech' || (speechProvider === null && live('transcription') && via === 'audio');
  const transcribeSkipped = by.get('transcribe')?.status === 'skipped';
  const seconds = num(transcribe.audio_seconds);
  const policy = info('policy');
  const language = usage.find((item) => item.service === 'language_pii');
  const languageLive = language?.provider === 'azure_language' || (!language && live('pii'));
  const spans = num(policy.pii_spans);
  const extract = info('extract');
  const model = usage.find((item) => item.service === 'llm' && item.provider !== 'projected');
  const engine = str(extract.engine) ?? record.engine ?? 'rules';
  const modelName = record.proposal?.result.model ?? model?.model ?? null;
  const products = num(extract.products);
  const tokens = num(extract.llm_tokens);
  const resolve = info('resolve');
  const validate = info('validate');
  const crmLive = live('crm');
  const account = str(resolve.account);

  const matched = by.has('resolve') ? (account ? `Matched ${account}` : 'No CRM account matched') : null;
  const checked = by.has('validate')
    ? validate.passed === false
      ? `${plural(num(validate.findings) ?? 0, 'validation finding')}`
      : 'validation passed'
    : null;
  const leaks = num(validate.leaks) ?? 0;

  return [
    {
      key: 'ingest',
      title: audioIn ? 'Recording received' : 'Transcript received',
      state: stateOf(['ingest'], by),
      service: audioIn ? { label: live('vault') ? 'Azure Blob' : 'Local vault', live: live('vault') } : null,
      summary: audioIn
        ? num(ingest.audio_bytes) !== null
          ? `${formatBytes(num(ingest.audio_bytes))} of ${CHANNEL_LABELS[record.meta.source]} audio stored for the transcription`
          : null
        : `${CHANNEL_LABELS[record.meta.source]} transcript parsed into speaker-labelled segments`,
      ms: ms(['ingest']),
    },
    {
      key: 'transcribe',
      title: 'Speech to text',
      state: stateOf(['transcribe'], by),
      service: transcribeSkipped ? null : { label: speechLive ? 'Azure Speech' : 'Mock transcriber', live: speechLive },
      summary: transcribeSkipped
        ? (str(transcribe.reason) ?? 'Skipped: the platform already transcribed the call')
        : seconds !== null
          ? `${formatDurationSeconds(seconds)} of audio into ${plural(num(transcribe.segments) ?? 0, 'segment')}${transcribe.audio_deleted === true ? '; the audio is deleted afterwards (market policy)' : ''}`
          : null,
      ms: ms(['transcribe']),
    },
    {
      key: 'policy',
      title: 'Privacy filter',
      state: stateOf(['policy'], by),
      service: { label: languageLive ? 'Azure AI Language' : 'Local detector', live: languageLive },
      summary:
        spans !== null
          ? `${plural(spans, 'PII span')} found, ${plural(sumCounts(policy.actions), 'policy action')} applied${languageLive && language?.units ? `, ${plural(language.units, 'text record')} billed` : ''}`
          : null,
      ms: ms(['policy']),
    },
    {
      key: 'extract',
      title: 'Summarise and extract',
      state: stateOf(['extract'], by),
      service: model
        ? { label: `Foundry model${modelName ? ` (${modelName})` : ''}`, live: true }
        : { label: engine === 'rules' ? 'Rules engine' : `${engine} engine`, live: false },
      summary:
        products !== null
          ? `${plural(products, 'product')}, ${plural(num(extract.topics) ?? 0, 'topic')}, ${plural(num(extract.tasks) ?? 0, 'task')}${tokens ? `, ${formatInt(tokens)} tokens` : ''}`
          : null,
      ms: ms(['extract']),
    },
    {
      key: 'resolve',
      title: 'Match the CRM account and validate',
      state: stateOf(['resolve', 'validate'], by),
      service: { label: crmLive ? 'Dataverse' : 'Local CRM simulator', live: crmLive },
      summary: [matched, checked, leaks > 0 ? plural(leaks, 'leak') : null].filter(Boolean).join(', ') || null,
      ms: ms(['resolve', 'validate']),
    },
  ];
}

function StepIcon({ state }: { state: StepState }) {
  if (state === 'running') return <Spinner className="h-5 w-5 text-brand-500" />;
  if (state === 'done') return <CheckCircleIcon aria-hidden="true" className="h-5 w-5 text-emerald-500" />;
  if (state === 'failed') return <ExclamationCircleIcon aria-hidden="true" className="h-5 w-5 text-red-500" />;
  if (state === 'skipped') return <MinusCircleIcon aria-hidden="true" className="h-5 w-5 text-neutral-400" />;
  return <span aria-hidden="true" className="block h-5 w-5 rounded-full border-2 border-neutral-200" />;
}

const STATE_TEXT: Record<StepState, string> = { waiting: 'waiting', running: 'running', done: 'done', skipped: 'skipped', failed: 'failed' };

function Steps({ steps }: { steps: StepView[] }) {
  return (
    <ol className="divide-y divide-neutral-100 rounded-lg border border-neutral-200" aria-label="Pipeline steps">
      {steps.map((step) => (
        <li key={step.key} className={clsx('flex gap-3 px-4 py-3', step.state === 'waiting' && 'bg-neutral-50/60')}>
          <span className="mt-0.5 shrink-0">
            <StepIcon state={step.state} />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
              <p className="flex flex-wrap items-center gap-2 text-sm font-medium text-neutral-900">
                <span className={step.state === 'waiting' ? 'text-neutral-500' : undefined}>{step.title}</span>
                <span className="sr-only">{STATE_TEXT[step.state]}</span>
                {step.service ? (
                  <Badge tone={step.service.live ? 'success' : 'neutral'} dot pulse={step.state === 'running' && step.service.live}>
                    {step.service.label}
                  </Badge>
                ) : null}
              </p>
              <span className="tabular text-xs text-neutral-500">{step.ms === null ? (step.state === 'running' ? 'running\u2026' : '') : formatDurationMs(step.ms)}</span>
            </div>
            {step.summary ? <p className="mt-0.5 text-xs text-neutral-600">{step.summary}</p> : null}
          </div>
        </li>
      ))}
    </ol>
  );
}

function productName(mention: ProductMention): string {
  const chosen = mention.candidates.find((candidate) => candidate.product_id === mention.chosen_product_id);
  return chosen ? chosen.display_name : `${mention.surface} (share class to confirm)`;
}

function Outcome({ title, icon: Icon, items, empty }: { title: string; icon: typeof CubeIcon; items: ReactNode[]; empty: string }) {
  return (
    <div className="min-w-0 rounded-lg border border-neutral-200 bg-white p-3">
      <h3 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-neutral-500">
        <Icon aria-hidden="true" className="h-4 w-4" />
        {title}
      </h3>
      {items.length === 0 ? (
        <p className="mt-1.5 text-sm text-neutral-400">{empty}</p>
      ) : (
        <ul className="mt-1.5 space-y-1 text-sm text-neutral-800">
          {items.map((item, index) => (
            <li key={index} className="break-words">
              {item}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Result({ detail, id }: { detail: ConversationDetail; id: string }) {
  const toast = useToast();
  const approve = useApprove(id);
  const { record, cost } = detail;
  const proposal = record.proposal;
  if (!proposal) return null;
  const result = proposal.result;
  const review = proposal.review;
  const reasons = record.status === 'needs_review' ? review.reasons : [];
  const written = record.status === 'written' || record.status === 'approved';

  const send = () =>
    approve.mutate(
      {},
      {
        onSuccess: (updated) => {
          const created = updated.record.crm_result?.created.length ?? 0;
          toast.success('Approved and sent to the CRM', { message: `${created} record${created === 1 ? '' : 's'} created`, action: { label: 'See it in the CRM', to: '/crm' } });
        },
        onError: (error) => toast.error('Could not approve', { message: getErrorMessage(error) }),
      },
    );

  return (
    <div className="mt-4 space-y-4">
      <div className="grid gap-3 sm:grid-cols-3">
        <Outcome title="Products" icon={CubeIcon} empty="None mentioned" items={result.products.map((mention) => productName(mention))} />
        <Outcome title="Topics" icon={TagIcon} empty="None tagged" items={result.topics.map((topic) => topic.label)} />
        <Outcome
          title="Follow-up tasks"
          icon={ClipboardDocumentCheckIcon}
          empty="None found"
          items={result.tasks.map((task) => (
            <>
              {task.title}
              {task.due_date ? <span className="text-neutral-500"> &middot; due {task.due_date}</span> : null}
            </>
          ))}
        />
      </div>

      {cost.lines.length > 0 ? (
        <p className="text-xs text-neutral-600">
          <span className="font-semibold text-neutral-800">What it cost: </span>
          {cost.lines.map((line) => `${line.service} ${formatInt(Math.round(line.quantity * 1000) / 1000)} ${line.unit} ${formatUsd(line.est_cost, { est: false })}`).join(' \u00b7 ')}
          <span className="font-semibold text-neutral-800"> &middot; total {formatUsd(cost.total)}</span>
        </p>
      ) : null}

      {written ? (
        <div className="flex flex-wrap items-center gap-2 rounded-lg bg-emerald-50 p-3 text-sm text-emerald-900">
          <CheckCircleIcon aria-hidden="true" className="h-5 w-5 shrink-0 text-emerald-600" />
          <span className="font-medium">Written to the CRM:</span>
          {(record.crm_result?.created ?? []).map((ref) => (
            <Badge key={`${ref.entity}-${ref.id}`} tone="success">
              {ref.entity}
            </Badge>
          ))}
          <Link to="/crm" className="ml-auto text-xs font-medium text-emerald-800 underline">
            Open the CRM view
          </Link>
        </div>
      ) : record.status === 'needs_review' ? (
        <div className="flex flex-wrap items-center gap-3 rounded-lg bg-amber-50 p-3 text-sm text-amber-900">
          {reasons.length === 0 ? (
            <>
              <span className="font-medium">Ready: no review reason, so one click sends it to the CRM.</span>
              <Button variant="primary" size="sm" icon={CheckIcon} loading={approve.isPending} onClick={send} className="ml-auto">
                Approve and write to the CRM
              </Button>
            </>
          ) : (
            <>
              <span className="font-medium">A person has to look: {reasons.map(reasonLabel).join(', ')}.</span>
              <Link to={`/conversations/${id}`} className="btn btn-primary btn-sm ml-auto">
                Open the review
              </Link>
            </>
          )}
        </div>
      ) : null}

      <details className="rounded-lg border border-neutral-200 bg-white">
        <summary className="cursor-pointer select-none px-4 py-2.5 text-sm font-medium text-neutral-700">What was heard: the redacted transcript</summary>
        <div className="border-t border-neutral-100 p-3">
          <TranscriptPanel conversationId={id} revision={record.updated_at} available={detail.transcript_available} note={detail.transcript_note} policyActions={proposal.policy_actions} />
        </div>
      </details>
    </div>
  );
}

/**
 * One call, live: the pipeline steps with the Azure service behind each, how long each took, what came out, what it cost,
 * and a one-click approval into the CRM. Used for the audio scenario (A) and the simulated Teams call (B).
 */
export function ConversationRun(props: { id: string; via: 'audio' | 'transcript'; title: string }) {
  const ref = useScrollIntoViewOnMount<HTMLDivElement>();
  return (
    <div ref={ref} className="scroll-mt-4">
      <RunBody {...props} />
    </div>
  );
}

function RunBody({ id, via, title }: { id: string; via: 'audio' | 'transcript'; title: string }) {
  const detail = useConversation(id);
  const config = useConfig();
  const now = useNow(500);

  if (detail.isPending) return <LoadingBlock label="Starting…" />;
  if (detail.isError || !detail.data) return <ErrorState error={detail.error} title="Could not read the call" onRetry={() => void detail.refetch()} compact />;

  const data = detail.data;
  const { record } = data;
  const running = record.status === 'received' || record.status === 'processing';
  const readyAt = record.proposal?.ready_at ?? null;
  const elapsed = running ? secondsBetween(record.created_at, now) : readyAt ? Math.max(0, (Date.parse(readyAt) - Date.parse(record.created_at)) / 1000) : null;
  const steps = buildSteps(data, via, config.data?.modes);

  return (
    <Card aria-labelledby={`run-${id}`}>
      <CardHeader
        id={`run-${id}`}
        icon={PlayCircleIcon}
        title={title}
        subtitle={`${via === 'audio' ? 'Audio' : 'Transcript'} path \u00b7 ${CHANNEL_LABELS[record.meta.source]} \u00b7 call ${id.slice(0, 8)}`}
        actions={
          <>
            <StatusBadge status={record.status} />
            <span className="tabular text-sm font-semibold text-neutral-900" title="From the moment the call was handed to the pipeline until the proposal was ready">
              {elapsed === null ? EM_DASH : formatDurationSeconds(elapsed)}
            </span>
            <Link to={`/conversations/${id}`} className="btn btn-secondary btn-sm">
              Open full review
            </Link>
          </>
        }
      />
      <CardBody>
        <Steps steps={steps} />
        {record.status === 'failed' ? (
          <p role="alert" className="mt-3 rounded-md bg-red-50 p-3 text-sm text-red-800">
            {record.error ?? 'Processing failed.'}
          </p>
        ) : null}
        <Result detail={data} id={id} />
      </CardBody>
    </Card>
  );
}
