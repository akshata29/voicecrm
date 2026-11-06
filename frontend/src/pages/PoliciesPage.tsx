import {
  AdjustmentsHorizontalIcon,
  ArchiveBoxIcon,
  BoltIcon,
  CheckCircleIcon,
  ClockIcon,
  DocumentTextIcon,
  GlobeAltIcon,
  LockClosedIcon,
  LockOpenIcon,
  ShieldCheckIcon,
  TrashIcon,
} from '@heroicons/react/24/outline';
import clsx from 'clsx';
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { getErrorMessage } from '@/api/client';
import {
  INBOX_PARAMS,
  useConfig,
  useConversationList,
  useEngineOptions,
  useEvalRuns,
  usePolicies,
  useRetentionPreview,
  useSweep,
  useUpdateMarketPolicy,
  useUpdateNotesPolicy,
  useUpdateProcessingPolicy,
  useVaultItems,
} from '@/api/hooks';
import { type Engine, type MarketPolicy, type NotesPolicy, type ProcessingPolicy, type VaultItem } from '@/api/types';
import { MarketChip } from '@/components/conversation/Badges';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { ConfirmDialog } from '@/components/ui/ConfirmDialog';
import { Field, PageHeader } from '@/components/ui/Field';
import { Slider } from '@/components/ui/Slider';
import { EmptyState, ErrorState, ListSkeleton, LoadingBlock, QueryState } from '@/components/ui/StateViews';
import { Toggle } from '@/components/ui/Toggle';
import { useToast } from '@/components/ui/Toast';
import { formatBytes, formatDate, formatDateTime, formatExpiry, formatPercent, formatRelative, humanize, pluralize } from '@/lib/format';
import { useDocumentTitle } from '@/lib/hooks';
import { businessLineLabel, CHANNEL_SHORT } from '@/lib/labels';
import type { Channel } from '@/api/types';

type SaveStatus = 'idle' | 'saving' | 'saved' | 'error';

/** Tiny "Saving… / Saved" indicator that fades back to idle. */
function useSaveIndicator() {
  const [status, setStatus] = useState<SaveStatus>('idle');
  const [message, setMessage] = useState<string | null>(null);
  const timer = useRef<number | null>(null);
  useEffect(() => () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
  }, []);
  return {
    status,
    message,
    saving: () => {
      setStatus('saving');
      setMessage(null);
    },
    saved: () => {
      setStatus('saved');
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setStatus('idle'), 2500);
    },
    failed: (error: unknown) => {
      setStatus('error');
      setMessage(getErrorMessage(error));
    },
  };
}

function SaveStatusText({ status, message }: { status: SaveStatus; message: string | null }) {
  return (
    <span aria-live="polite" className={clsx('text-xs', status === 'error' ? 'text-red-700' : status === 'saved' ? 'text-emerald-700' : 'text-neutral-400')}>
      {status === 'saving' ? 'Saving…' : status === 'saved' ? 'Saved' : status === 'error' ? (message ?? 'Could not save') : ''}
    </span>
  );
}

// ---- market cards ----------------------------------------------------------------------------------
function NumberField({ id, label, value, min, placeholder, hint, suffix, onCommit, disabled }: { id: string; label: string; value: number | null; min: number; placeholder?: string; hint?: string; suffix?: string; onCommit: (value: number | null) => void; disabled?: boolean }) {
  const [text, setText] = useState(value === null ? '' : String(value));
  useEffect(() => setText(value === null ? '' : String(value)), [value]);
  const commit = () => {
    const trimmed = text.trim();
    if (trimmed === '') {
      if (value !== null) onCommit(null);
      return;
    }
    const parsed = Math.floor(Number(trimmed));
    if (!Number.isFinite(parsed) || parsed < min) {
      setText(value === null ? '' : String(value));
      return;
    }
    if (parsed !== value) onCommit(parsed);
  };
  return (
    <Field label={label} htmlFor={id} hint={hint}>
      <div className="flex items-center gap-2">
        <input
          id={id}
          type="number"
          inputMode="numeric"
          min={min}
          value={text}
          placeholder={placeholder}
          disabled={disabled}
          onChange={(event) => setText(event.target.value)}
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === 'Enter') event.currentTarget.blur();
          }}
          className="input !w-32"
        />
        {suffix ? <span className="text-sm text-neutral-500">{suffix}</span> : null}
      </div>
    </Field>
  );
}

function RetentionPreviewBox({ market }: { market: string }) {
  const preview = useRetentionPreview(market);
  return (
    <div className="rounded-lg border border-neutral-200 bg-neutral-50 p-3" aria-live="polite">
      <p className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-neutral-500">
        <ClockIcon aria-hidden="true" className="h-4 w-4" /> Retention preview
      </p>
      {preview.isPending ? <p className="mt-1 text-sm text-neutral-500">Calculating…</p> : null}
      {preview.isError ? <p className="mt-1 text-sm text-red-700">{getErrorMessage(preview.error)}</p> : null}
      {preview.data ? (
        <div className="mt-1.5 space-y-1.5 text-sm">
          <p className="text-neutral-800">
            A call processed today:{' '}
            <strong>
              {preview.data.decision.transcript_stored
                ? `transcript kept${preview.data.decision.transcript_expires_at ? ` until ${formatDate(preview.data.decision.transcript_expires_at)}` : ''}${preview.data.decision.transcript_redacted ? ' (redacted)' : ''}`
                : 'no transcript kept'}
            </strong>
            {preview.data.decision.audio_expires_at ? `, audio until ${formatDate(preview.data.decision.audio_expires_at)}` : ', audio deleted right after transcription'}.
          </p>
          <p className="text-xs text-neutral-600">{preview.data.decision.explanation}</p>
          <p className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-semibold text-emerald-800 ring-1 ring-inset ring-emerald-200">
            <ShieldCheckIcon aria-hidden="true" className="h-3.5 w-3.5" /> CRM holds no transcript
          </p>
        </div>
      ) : null}
    </div>
  );
}

function MarketCard({ policy }: { policy: MarketPolicy }) {
  const update = useUpdateMarketPolicy();
  const indicator = useSaveIndicator();
  const on = policy.transcript_retention === 'on';
  const idBase = `market-${policy.market}`;

  const save = (next: MarketPolicy) => {
    indicator.saving();
    update.mutate(next, { onSuccess: indicator.saved, onError: indicator.failed });
  };

  return (
    <Card aria-labelledby={`${idBase}-title`}>
      <CardHeader
        id={`${idBase}-title`}
        title={policy.display_name}
        subtitle={policy.notes ?? undefined}
        icon={GlobeAltIcon}
        actions={
          <>
            <SaveStatusText status={indicator.status} message={indicator.message} />
            <MarketChip market={policy.market} />
            <Badge tone={on ? 'violet' : 'neutral'} icon={on ? LockClosedIcon : LockOpenIcon}>
              {on ? 'Transcript on' : 'Summary only'}
            </Badge>
          </>
        }
      />
      <CardBody className="space-y-4">
        <Toggle
          label="Retain a transcript"
          description="Off: only the summary and structured fields are kept. On: a transcript is kept in the vault (never in the CRM)."
          checked={on}
          onChange={(checked) =>
            save({
              ...policy,
              transcript_retention: checked ? 'on' : 'off',
              transcript_retention_days: checked ? (policy.transcript_retention_days ?? 365) : null,
            })
          }
        />
        {on ? (
          <div className="space-y-3 rounded-lg border border-neutral-200 p-3">
            <NumberField id={`${idBase}-days`} label="Keep the transcript for" value={policy.transcript_retention_days} min={1} suffix="days" onCommit={(value) => value !== null && save({ ...policy, transcript_retention_days: value })} />
            <div className="flex flex-wrap gap-1.5" role="group" aria-label="Retention presets">
              {[
                { label: '90 days', days: 90 },
                { label: '1 year', days: 365 },
                { label: '5 years', days: 1825 },
              ].map((preset) => (
                <button
                  key={preset.days}
                  type="button"
                  onClick={() => save({ ...policy, transcript_retention_days: preset.days })}
                  aria-pressed={policy.transcript_retention_days === preset.days}
                  className={clsx(
                    'rounded-full border px-2.5 py-0.5 text-xs font-medium',
                    policy.transcript_retention_days === preset.days ? 'border-brand-500 bg-brand-50 text-brand-800' : 'border-neutral-300 text-neutral-600 hover:bg-neutral-50',
                  )}
                >
                  {preset.label}
                </button>
              ))}
            </div>
            <Toggle label="Store the redacted transcript only" description="PII is replaced with tokens before anything is persisted." checked={policy.store_redacted_only} onChange={(checked) => save({ ...policy, store_redacted_only: checked })} />
          </div>
        ) : null}
        <div className="grid gap-3 sm:grid-cols-2">
          <NumberField id={`${idBase}-audio`} label="Keep audio for" value={policy.audio_retention_days} min={0} suffix="days" hint="0 = delete right after transcription" onCommit={(value) => save({ ...policy, audio_retention_days: value ?? 0 })} />
          <NumberField id={`${idBase}-summary`} label="Keep the summary for" value={policy.summary_retention_days} min={1} suffix="days" placeholder="no limit" hint="Empty = as long as the CRM record exists" onCommit={(value) => save({ ...policy, summary_retention_days: value })} />
        </div>
        <RetentionPreviewBox market={policy.market} />
      </CardBody>
    </Card>
  );
}

// ---- notes policy ----------------------------------------------------------------------------------
const KNOWN_CATEGORIES = ['payment_card', 'bank_account', 'government_id', 'credentials', 'medical', 'family_health', 'personal_contact'];

function NotesPolicyCard({ policy }: { policy: NotesPolicy }) {
  const update = useUpdateNotesPolicy();
  const indicator = useSaveIndicator();
  const [confirm, setConfirm] = useState<string | null>(null);
  const categories = [...new Set([...KNOWN_CATEGORIES, ...policy.omit_categories])];

  const save = (next: NotesPolicy) => {
    indicator.saving();
    update.mutate(next, { onSuccess: indicator.saved, onError: indicator.failed });
  };

  return (
    <Card aria-labelledby="notes-policy-title">
      <CardHeader id="notes-policy-title" title="Notes policy" subtitle="What may enter a CRM note" icon={DocumentTextIcon} actions={<SaveStatusText status={indicator.status} message={indicator.message} />} />
      <CardBody className="space-y-4">
        <Toggle label="Keep relationship context" description="Non-sensitive rapport facts (for example a favourite team) stay in the retail note." checked={policy.keep_relationship_context} onChange={(checked) => save({ ...policy, keep_relationship_context: checked })} />
        <Toggle label="Drop small talk" description="Weather, scores and other chit-chat never reaches the note." checked={policy.drop_small_talk} onChange={(checked) => save({ ...policy, drop_small_talk: checked })} />
        <div>
          <p className="text-sm font-medium text-neutral-800">Always omit these categories</p>
          <p className="mb-2 text-xs text-neutral-500">Detected content in these categories is removed before extraction and can never reach a note.</p>
          <ul className="flex flex-wrap gap-2" aria-label="Omitted categories">
            {categories.map((category) => {
              const omitted = policy.omit_categories.includes(category);
              return (
                <li key={category}>
                  <button
                    type="button"
                    aria-pressed={omitted}
                    onClick={() => (omitted ? setConfirm(category) : save({ ...policy, omit_categories: [...policy.omit_categories, category] }))}
                    className={clsx(
                      'inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-sm font-medium transition-colors',
                      omitted ? 'border-red-200 bg-red-50 text-red-800 hover:bg-red-100' : 'border-neutral-300 bg-white text-neutral-500 hover:bg-neutral-50',
                    )}
                  >
                    {omitted ? <LockClosedIcon aria-hidden="true" className="h-3.5 w-3.5" /> : <LockOpenIcon aria-hidden="true" className="h-3.5 w-3.5" />}
                    {humanize(category)}
                    <span className="sr-only">{omitted ? ' (omitted)' : ' (allowed)'}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      </CardBody>
      <ConfirmDialog
        open={confirm !== null}
        title={`Stop omitting “${confirm ? humanize(confirm) : ''}”?`}
        description="Content in this category could then reach CRM notes. Only do this if your compliance policy allows it."
        confirmLabel="Allow in notes"
        tone="danger"
        onCancel={() => setConfirm(null)}
        onConfirm={() => {
          if (confirm) save({ ...policy, omit_categories: policy.omit_categories.filter((item) => item !== confirm) });
          setConfirm(null);
        }}
      />
    </Card>
  );
}

// ---- processing policy (the zero-touch dial) -------------------------------------------------------
const ENGINE_HELP: Record<Engine, string> = {
  rules: 'Deterministic rules only: no LLM',
  llm: 'A Foundry model does the extraction',
  hybrid: 'Rules propose candidates, the model summarises, rules validate',
};

function ZeroTouchSteps({ enabled, threshold }: { enabled: boolean; threshold: number }) {
  const steps = [
    { title: 'One-click review', text: 'The seller approves every call.', active: !enabled },
    { title: 'Auto-approve when confident', text: `Calls at or above ${formatPercent(threshold)} confidence skip review.`, active: enabled },
    { title: 'Zero-touch', text: 'Everything in the background; humans only audit.', active: false, future: true },
  ];
  return (
    <ol className="grid gap-2 sm:grid-cols-3" aria-label="Path to zero-touch">
      {steps.map((step, index) => (
        <li key={step.title} className={clsx('rounded-lg border p-3', step.active ? 'border-brand-500 bg-brand-50 ring-1 ring-brand-500' : 'border-neutral-200 bg-white', step.future && 'opacity-60')}>
          <p className="flex items-center gap-2 text-sm font-semibold text-neutral-900">
            <span className={clsx('flex h-5 w-5 items-center justify-center rounded-full text-xs', step.active ? 'bg-brand-500 text-white' : 'bg-neutral-200 text-neutral-600')}>{index + 1}</span>
            {step.title}
            {step.active ? <CheckCircleIcon aria-hidden="true" className="ml-auto h-5 w-5 text-brand-600" /> : null}
          </p>
          <p className="mt-1 text-xs text-neutral-600">{step.text}</p>
        </li>
      ))}
    </ol>
  );
}

function ProcessingCard({ policy }: { policy: ProcessingPolicy }) {
  const update = useUpdateProcessingPolicy();
  const { engines, modelLive } = useEngineOptions();
  const indicator = useSaveIndicator();
  const runs = useEvalRuns();
  const latest = runs.data?.[0];

  const save = (patch: Partial<ProcessingPolicy>) => {
    indicator.saving();
    update.mutate({ ...policy, ...patch }, { onSuccess: indicator.saved, onError: indicator.failed });
  };

  return (
    <Card aria-labelledby="processing-title">
      <CardHeader
        id="processing-title"
        title="Processing: the path to zero-touch"
        subtitle="Turn the dial as the scoreboard earns trust"
        icon={BoltIcon}
        actions={<SaveStatusText status={indicator.status} message={indicator.message} />}
      />
      <CardBody className="space-y-5">
        <ZeroTouchSteps enabled={policy.auto_approve_enabled} threshold={policy.auto_approve_min_confidence} />
        <div className="grid gap-6 lg:grid-cols-2">
          <div className="space-y-5">
            <Toggle label="Auto-approve eligible proposals" description="Proposals with no pending picks, high confidence and a CRM match are written without a click." checked={policy.auto_approve_enabled} onChange={(checked) => save({ auto_approve_enabled: checked })} />
            <Slider
              label="Minimum confidence to auto-approve"
              description={policy.auto_approve_enabled ? 'Raise it to review more, lower it to trust more.' : 'Takes effect once auto-approve is on.'}
              value={policy.auto_approve_min_confidence}
              min={0.5}
              max={1}
              step={0.01}
              format={(value) => `${Math.round(value * 100)}%`}
              onCommit={(value) => save({ auto_approve_min_confidence: Math.round(value * 100) / 100 })}
            />
            <Toggle label="Require a CRM match" description="Without an account match a human always confirms." checked={policy.auto_approve_requires_crm_match} onChange={(checked) => save({ auto_approve_requires_crm_match: checked })} />
          </div>
          <div className="space-y-5">
            <Slider
              label="Product review threshold"
              description="Below this confidence a product mention needs a human pick (top-3 list)."
              value={policy.product_review_threshold}
              min={0.5}
              max={1}
              step={0.01}
              format={(value) => `${Math.round(value * 100)}%`}
              onCommit={(value) => save({ product_review_threshold: Math.round(value * 100) / 100 })}
            />
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Extraction engine" htmlFor="policy-engine" hint={modelLive ? ENGINE_HELP[policy.engine] : 'Rules only: no model is configured (set VOICECRM_LLM_MODE to enable llm / hybrid)'}>
                <select id="policy-engine" className="input" value={policy.engine} onChange={(event) => save({ engine: event.target.value as Engine })}>
                  {[...new Set<Engine>([...engines, policy.engine])].map((engine) => (
                    <option key={engine} value={engine} disabled={!engines.includes(engine)}>
                      {engines.includes(engine) ? engine : `${engine} (no model configured)`}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Candidates shown (top-k)" htmlFor="policy-topk" hint="Pick-list length">
                <select id="policy-topk" className="input" value={policy.top_k_products} onChange={(event) => save({ top_k_products: Number(event.target.value) })}>
                  {[1, 2, 3, 4, 5].map((count) => (
                    <option key={count} value={count}>
                      {count}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            {latest ? (
              <p className="rounded-lg bg-neutral-50 px-3 py-2 text-xs text-neutral-600 ring-1 ring-inset ring-neutral-200">
                Evidence from the scoreboard: <strong className="text-neutral-900">{formatPercent(latest.metrics.auto_approvable_rate)}</strong> of the golden-set calls were auto-approvable in the latest run
                {latest.label ? ` (“${latest.label}”)` : ''}. <Link to="/scoreboard" className="font-semibold text-brand-700 hover:underline">Open the scoreboard</Link>
              </p>
            ) : null}
          </div>
        </div>
        <details className="rounded-md border border-neutral-200 bg-neutral-50 text-sm">
          <summary className="cursor-pointer select-none px-3 py-2 font-medium text-neutral-700">Routing: transcript source and default template (read-only)</summary>
          <div className="grid gap-4 border-t border-neutral-200 p-3 md:grid-cols-2">
            <div>
              <p className="section-title mb-1">Transcript source by channel</p>
              <ul className="space-y-1 text-sm text-neutral-700">
                {Object.entries(policy.transcription_source_preference).map(([channel, order]) => (
                  <li key={channel} className="flex items-center justify-between gap-3">
                    <span className="font-medium">{CHANNEL_SHORT[channel as Channel] ?? humanize(channel)}</span>
                    <span className="text-neutral-500">{order.map((item) => (item === 'azure_speech' ? 'Azure Speech' : 'Platform')).join(' → ')}</span>
                  </li>
                ))}
              </ul>
            </div>
            <div>
              <p className="section-title mb-1">Default template by business line</p>
              <ul className="space-y-1 text-sm text-neutral-700">
                {Object.entries(policy.default_template_by_business_line).map(([line, template]) => (
                  <li key={line} className="flex items-center justify-between gap-3">
                    <span className="font-medium">{businessLineLabel(line)}</span>
                    <code className="text-xs text-neutral-500">{template}</code>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </details>
      </CardBody>
    </Card>
  );
}

// ---- vault -----------------------------------------------------------------------------------------
const KIND_TONE = { audio: 'orange', transcript: 'violet', working: 'info' } as const;

/** Vault URIs are long (local file paths or blob URLs): show the part after the vault root; the full value stays in the tooltip. */
function shortUri(uri: string): string {
  const vaultAt = uri.search(/[\\/]vault[\\/]/i);
  if (vaultAt >= 0) return uri.slice(vaultAt + 1);
  return uri.replace(/^[a-z][a-z0-9+.-]*:\/\/[^/]*\/?/i, '');
}

function VaultCard() {
  const items = useVaultItems();
  const inbox = useConversationList(INBOX_PARAMS);
  const sweep = useSweep();
  const toast = useToast();
  const [confirm, setConfirm] = useState(false);
  const [deleted, setDeleted] = useState<VaultItem[] | null>(null);
  const known = new Set((inbox.data ?? []).map((item) => item.conversation_id));
  const now = Date.now();
  const expiredCount = (items.data ?? []).filter((item) => item.expires_at && Date.parse(item.expires_at) <= now).length;

  const runSweep = () => {
    sweep.mutate(undefined, {
      onSuccess: (result) => {
        setDeleted(result.deleted);
        setConfirm(false);
        toast.success(result.deleted.length === 0 ? 'Nothing had expired' : `Deleted ${pluralize(result.deleted.length, 'vault item')}`);
      },
      onError: (error) => {
        setConfirm(false);
        toast.error('Retention sweep failed', { message: getErrorMessage(error) });
      },
    });
  };

  return (
    <Card aria-labelledby="vault-title">
      <CardHeader
        id="vault-title"
        title="Transcript vault"
        subtitle="Audio, retained transcripts and short-lived working copies (never the CRM)"
        icon={ArchiveBoxIcon}
        actions={
          <Button variant="secondary" size="sm" icon={TrashIcon} onClick={() => setConfirm(true)}>
            Run retention sweep{expiredCount > 0 ? ` (${expiredCount} expired)` : ''}
          </Button>
        }
      />
      <CardBody className="!p-0">
        {deleted ? (
          <div role="status" className="border-b border-neutral-100 bg-emerald-50 px-4 py-3 text-sm text-emerald-900 sm:px-5">
            <p className="font-semibold">{deleted.length === 0 ? 'Sweep complete: nothing was past its expiry.' : `Sweep complete: ${pluralize(deleted.length, 'item')} deleted.`}</p>
            {deleted.length > 0 ? (
              <ul className="mt-1 list-inside list-disc text-xs text-emerald-800">
                {deleted.map((item) => (
                  <li key={`${item.conversation_id}-${item.kind}`}>
                    {humanize(item.kind)} · {item.conversation_id.slice(0, 12)} · expired {formatRelative(item.expires_at)}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
        <QueryState
          query={items}
          loading={<LoadingBlock label="Loading vault items…" />}
          isEmpty={(data) => data.length === 0}
          empty={<EmptyState icon={ArchiveBoxIcon} title="The vault is empty" description="No audio, transcript or working copy is currently stored." />}
        >
          {(data) => (
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-neutral-100 text-sm">
                <thead className="bg-neutral-50">
                  <tr>
                    <th scope="col" className="th">Conversation</th>
                    <th scope="col" className="th">Kind</th>
                    <th scope="col" className="th">Size</th>
                    <th scope="col" className="th">Created</th>
                    <th scope="col" className="th">Expires</th>
                    <th scope="col" className="th">Location</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-neutral-100">
                  {data.map((item) => {
                    const expiry = formatExpiry(item.expires_at, now);
                    return (
                      <tr key={`${item.conversation_id}-${item.kind}`}>
                        <td className="td font-mono text-xs">
                          {known.has(item.conversation_id) ? (
                            <Link to={`/conversations/${item.conversation_id}`} className="text-brand-700 hover:underline">
                              {item.conversation_id.slice(0, 12)}…
                            </Link>
                          ) : (
                            <span className="text-neutral-600">{item.conversation_id.length > 14 ? `${item.conversation_id.slice(0, 12)}…` : item.conversation_id}</span>
                          )}
                        </td>
                        <td className="td">
                          <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
                            <Badge tone={KIND_TONE[item.kind]}>{humanize(item.kind)}</Badge>
                            {item.redacted ? <LockClosedIcon title="Redacted" className="h-4 w-4 text-neutral-400" /> : null}
                          </span>
                        </td>
                        <td className="td tabular whitespace-nowrap text-neutral-600">{formatBytes(item.size_bytes)}</td>
                        <td className="td whitespace-nowrap text-neutral-600" title={formatDateTime(item.created_at)}>
                          {formatRelative(item.created_at)}
                        </td>
                        <td className="td whitespace-nowrap">
                          <span className={clsx(expiry.expired ? 'font-semibold text-red-700' : item.expires_at === null ? 'text-neutral-500' : 'text-neutral-700')} title={item.expires_at ? formatDateTime(item.expires_at) : undefined}>
                            {expiry.text}
                          </span>
                        </td>
                        <td className="td font-mono text-xs text-neutral-500">
                          <span className="block max-w-[16rem] truncate" title={item.uri}>
                            {shortUri(item.uri)}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </QueryState>
      </CardBody>
      <ConfirmDialog
        open={confirm}
        title="Run the retention sweep?"
        description="Permanently deletes every vault item that is past its expiry date (audio, transcripts and working copies). Summaries in the CRM are not touched."
        confirmLabel="Delete expired items"
        tone="danger"
        loading={sweep.isPending}
        onCancel={() => setConfirm(false)}
        onConfirm={runSweep}
      />
    </Card>
  );
}

export default function PoliciesPage() {
  useDocumentTitle('Policies');
  const policies = usePolicies();
  const config = useConfig();

  return (
    <div className="space-y-6">
      <PageHeader
        title="Policies"
        icon={AdjustmentsHorizontalIcon}
        description="Governance as configuration: per-market transcript retention, what may enter a note, and how much a human has to touch. Example policies, not Contoso Woodgrove's actual ones."
        actions={config.data ? <Badge tone="neutral">Effective engine: {config.data.engine}</Badge> : null}
      />
      {policies.isPending ? <ListSkeleton rows={3} /> : null}
      {policies.isError ? <ErrorState error={policies.error} title="Could not load the policies" onRetry={() => void policies.refetch()} /> : null}
      {policies.data ? (
        <>
          <section aria-labelledby="markets-heading" className="space-y-3">
            <h2 id="markets-heading" className="text-lg font-semibold text-neutral-900">
              Transcript retention by market
            </h2>
            {policies.data.markets.length === 0 ? (
              <div className="card">
                <EmptyState title="No markets configured" />
              </div>
            ) : (
              <div className="grid gap-4 xl:grid-cols-3 md:grid-cols-2">
                {policies.data.markets.map((market) => (
                  <MarketCard key={market.market} policy={market} />
                ))}
              </div>
            )}
          </section>
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
            <NotesPolicyCard policy={policies.data.notes} />
            <ProcessingCard policy={policies.data.processing} />
          </div>
          <VaultCard />
        </>
      ) : null}
    </div>
  );
}
