import {
  ArrowRightIcon,
  BoltIcon,
  CheckCircleIcon,
  ClockIcon,
  CursorArrowRaysIcon,
  DocumentDuplicateIcon,
  ExclamationTriangleIcon,
  XCircleIcon,
} from '@heroicons/react/24/outline';
import { CheckIcon } from '@heroicons/react/24/solid';
import clsx from 'clsx';
import { useEffect, useState } from 'react';
import type { ConversationDetail, Engine, TemplateInfo } from '@/api/types';
import { useEngineOptions } from '@/api/hooks';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { ConfirmDialog } from '@/components/ui/ConfirmDialog';
import { formatClock, formatDurationSeconds, secondsBetween } from '@/lib/format';
import { useNow } from '@/lib/hooks';

interface KpiProps {
  clicks: number;
  readyAt: string | null;
}

/** The "fewer clicks" KPI: edits made so far plus the approve click, and how long the proposal has been waiting. */
export function ReviewKpis({ clicks, readyAt }: KpiProps) {
  const now = useNow(1000);
  const waiting = secondsBetween(readyAt, now);
  return (
    <dl className="grid grid-cols-2 gap-2 text-center" aria-label="Review effort">
      <div className="rounded-lg bg-neutral-50 px-2 py-2 ring-1 ring-inset ring-neutral-200" title="Every edit you make adds a click; the approve click is included. Zero edits = one click.">
        <dt className="flex items-center justify-center gap-1 text-[11px] font-semibold uppercase tracking-wide text-neutral-500">
          <CursorArrowRaysIcon aria-hidden="true" className="h-3.5 w-3.5" /> Clicks to approve
        </dt>
        <dd className={clsx('tabular text-xl font-semibold', clicks <= 1 ? 'text-emerald-700' : 'text-neutral-900')}>{clicks}</dd>
      </div>
      <div className="rounded-lg bg-neutral-50 px-2 py-2 ring-1 ring-inset ring-neutral-200" title="Seconds since the proposal became review-ready">
        <dt className="flex items-center justify-center gap-1 text-[11px] font-semibold uppercase tracking-wide text-neutral-500">
          <ClockIcon aria-hidden="true" className="h-3.5 w-3.5" /> Ready for
        </dt>
        <dd className="tabular text-xl font-semibold text-neutral-900">{waiting === null ? '—' : formatClock(waiting)}</dd>
      </div>
    </dl>
  );
}

interface ActionCardProps {
  detail: ConversationDetail;
  /** Set when a CRM write failed and the proposal can be retried: the error is shown and the button says "Retry". */
  retryError?: string | null;
  pendingPicks: number;
  /** Net edits that will be sent with the approval. */
  editCount: number;
  /** Clicks spent so far on edits, template switches and so on (the approve click is added on top). */
  clickCount: number;
  onApprove: () => void;
  approving: boolean;
  approveError: string | null;
  onReject: (reason: string | null) => void;
  rejecting: boolean;
  templates: TemplateInfo[];
  onReprocess: (templateId: string | null, engine: Engine | null) => void;
  reprocessing: boolean;
  /** Mobile: the same button is shown in a sticky bottom bar. */
  className?: string;
}

export function ApproveButton({
  pendingPicks,
  approving,
  onApprove,
  retry = false,
  size = 'lg',
  className,
}: {
  pendingPicks: number;
  approving: boolean;
  onApprove: () => void;
  retry?: boolean;
  size?: 'md' | 'lg';
  className?: string;
}) {
  return (
    <Button variant="primary" size={size} icon={CheckIcon} onClick={onApprove} loading={approving} disabled={pendingPicks > 0} className={clsx('w-full', className)}>
      {retry ? 'Retry & send to CRM' : 'Approve & send to CRM'}
    </Button>
  );
}

export function ActionCard({ detail, retryError = null, pendingPicks, editCount, clickCount, onApprove, approving, approveError, onReject, rejecting, templates, onReprocess, reprocessing, className }: ActionCardProps) {
  const { record } = detail;
  const proposal = record.proposal;
  const retry = retryError !== null;
  const { engines, modelLive } = useEngineOptions();
  const [rejectOpen, setRejectOpen] = useState(false);
  const [reason, setReason] = useState('');
  // Show the clicked template/engine immediately while the re-extraction is running.
  const [pendingTemplate, setPendingTemplate] = useState<string | null>(null);
  const [pendingEngine, setPendingEngine] = useState<Engine | null>(null);
  useEffect(() => {
    if (!reprocessing) {
      setPendingTemplate(null);
      setPendingEngine(null);
    }
  }, [reprocessing]);
  const shownTemplate = pendingTemplate ?? record.template_id;
  const shownEngine = pendingEngine ?? record.engine ?? '';
  const preview = proposal?.crm_preview;
  const taskCount = preview ? preview.tasks.length : (proposal?.result.tasks.length ?? 0);
  const regarding = proposal?.crm_match.account?.name;

  return (
    <Card className={className} aria-labelledby="action-title">
      <CardHeader
        id="action-title"
        title={retry ? 'The CRM write failed' : pendingPicks > 0 ? 'Almost there' : 'Ready to send'}
        subtitle={retry ? 'Nothing was lost: retry, or reject' : pendingPicks > 0 ? `${pendingPicks} product ${pendingPicks === 1 ? 'pick' : 'picks'} needed` : 'Review the proposal, then one click'}
        icon={retry ? ExclamationTriangleIcon : BoltIcon}
      />
      <CardBody className="space-y-4">
        <div className="space-y-2">
          {retry ? (
            <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-800 ring-1 ring-inset ring-red-200">
              {retryError}
            </p>
          ) : null}
          <ApproveButton pendingPicks={pendingPicks} approving={approving} onApprove={onApprove} retry={retry} />
          {pendingPicks > 0 ? (
            <p className="flex items-start gap-1.5 text-xs text-amber-800" role="status">
              <ExclamationTriangleIcon aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
              Pick the exact product for the highlighted mention{pendingPicks === 1 ? '' : 's'} to enable approval.
            </p>
          ) : (
            <p className="text-xs text-neutral-500">
              {editCount === 0 ? 'No edits needed: approving sends the proposal exactly as shown.' : `${editCount} edit${editCount === 1 ? '' : 's'} will be sent with your approval.`}
              {' '}
              Creates 1 phone call, {taskCount} task{taskCount === 1 ? '' : 's'} and 1 insight{regarding ? ` on ${regarding}` : ''}.
            </p>
          )}
          {approveError ? (
            <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-800 ring-1 ring-inset ring-red-200">
              {approveError}
            </p>
          ) : null}
        </div>

        <ReviewKpis clicks={clickCount + 1} readyAt={proposal?.ready_at ?? null} />

        <Button variant="danger-outline" icon={XCircleIcon} onClick={() => setRejectOpen(true)} className="w-full" disabled={approving}>
          Reject
        </Button>

        <fieldset className="border-t border-neutral-100 pt-4" disabled={reprocessing || approving}>
          <legend className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-neutral-500">
            <DocumentDuplicateIcon aria-hidden="true" className="h-4 w-4" /> Template
            {reprocessing ? <span className="font-normal normal-case text-brand-600">re-extracting…</span> : null}
          </legend>
          <div className="space-y-2" role="radiogroup" aria-label="Summary template">
            {templates.map((template) => {
              const checked = template.template_id === shownTemplate;
              return (
                <label
                  key={template.template_id}
                  className={clsx(
                    'block cursor-pointer rounded-lg border p-2.5 text-sm transition-colors focus-within:ring-2 focus-within:ring-brand-500',
                    checked ? 'border-brand-500 bg-brand-50' : 'border-neutral-200 hover:bg-neutral-50',
                  )}
                >
                  <span className="flex items-start gap-2">
                    <input
                      type="radio"
                      name="template"
                      value={template.template_id}
                      checked={checked}
                      onChange={() => {
                        setPendingTemplate(template.template_id);
                        onReprocess(template.template_id, null);
                      }}
                      className="mt-1 h-4 w-4 accent-brand-600 focus:outline-none"
                    />
                    <span className="min-w-0">
                      <span className="block font-medium text-neutral-900">{template.name}</span>
                      <span className="block text-xs text-neutral-500">{template.sections.length} sections: {template.sections.slice(0, 3).join(', ')}…</span>
                    </span>
                  </span>
                </label>
              );
            })}
          </div>
          <div className="mt-3 flex items-center gap-2">
            <label htmlFor="engine-select" className="text-xs font-semibold text-neutral-600">
              Engine
            </label>
            <select
              id="engine-select"
              className="input !w-auto !py-1"
              value={shownEngine}
              onChange={(event) => {
                const engine = event.target.value as Engine;
                setPendingEngine(engine);
                onReprocess(null, engine);
              }}
            >
              {[...new Set<Engine>([...engines, ...(shownEngine ? [shownEngine as Engine] : [])])].map((engine) => (
                <option key={engine} value={engine} disabled={!engines.includes(engine)}>
                  {engines.includes(engine) ? engine : `${engine} (no model configured)`}
                </option>
              ))}
            </select>
            <span className="text-[11px] text-neutral-400">{modelLive ? 're-runs from the working copy' : 'rules only: no model configured'}</span>
          </div>
        </fieldset>
      </CardBody>

      <ConfirmDialog
        open={rejectOpen}
        title="Reject this conversation?"
        description="Nothing is written to the CRM and the working copy is deleted. The call can be ingested again if needed."
        confirmLabel="Reject"
        tone="danger"
        loading={rejecting}
        onCancel={() => setRejectOpen(false)}
        onConfirm={() => {
          onReject(reason.trim() || null);
          setRejectOpen(false);
          setReason('');
        }}
      >
        <label htmlFor="reject-reason" className="label">
          Reason (optional)
        </label>
        <textarea id="reject-reason" rows={3} value={reason} onChange={(event) => setReason(event.target.value)} className="input" placeholder="e.g. wrong customer, duplicate note" />
      </ConfirmDialog>
    </Card>
  );
}

// ---- after approval -------------------------------------------------------------------------------
const ENTITY_LABELS: Record<string, string> = { phonecall: 'Phone call', task: 'Task', insight: 'Conversation insight', account: 'Account', contact: 'Contact', opportunity: 'Opportunity' };

interface ResultProps {
  detail: ConversationDetail;
  nextId: string | null;
  onNext: () => void;
  onInbox: () => void;
  onViewCrm: () => void;
}

export function CrmResultCard({ detail, nextId, onNext, onInbox, onViewCrm }: ResultProps) {
  const { record } = detail;
  const result = record.crm_result;
  const review = record.proposal?.review;
  const auto = review?.state === 'auto_approved';

  return (
    <Card aria-labelledby="result-title" className="border-emerald-300 ring-1 ring-emerald-200">
      <CardHeader
        id="result-title"
        title={result?.ok === false ? 'CRM write failed' : record.status === 'approved' ? 'Approved: writing to the CRM' : 'Written to the CRM'}
        subtitle={auto ? 'Auto-approved by policy: zero clicks' : review ? `Approved in ${review.clicks} click${review.clicks === 1 ? '' : 's'}${review.seconds_to_review !== null ? `, ${formatDurationSeconds(review.seconds_to_review)} after ready` : ''}` : undefined}
        icon={result?.ok === false ? XCircleIcon : CheckCircleIcon}
        className="bg-emerald-50/60"
      />
      <CardBody className="space-y-3">
        {result ? (
          <>
            {result.error ? (
              <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-800 ring-1 ring-inset ring-red-200">
                {result.error}
              </p>
            ) : null}
            {result.skipped_duplicate ? <Badge tone="info">Duplicate skipped: already in the CRM (idempotent write)</Badge> : null}
            <ul className="divide-y divide-neutral-100 rounded-lg border border-neutral-200">
              {result.created.map((ref) => (
                <li key={`${ref.entity}-${ref.id}`} className="flex items-start gap-3 px-3 py-2">
                  <Badge className="mt-0.5 shrink-0" tone={ref.entity === 'task' ? 'brand' : ref.entity === 'insight' ? 'violet' : 'success'}>
                    {ENTITY_LABELS[ref.entity] ?? ref.entity}
                  </Badge>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm text-neutral-800" title={ref.name ?? undefined}>
                      {ref.name ?? ref.id}
                    </span>
                    <span className="block truncate font-mono text-[11px] text-neutral-400" title={ref.id}>
                      {ref.id}
                    </span>
                  </span>
                </li>
              ))}
              {result.created.length === 0 ? <li className="px-3 py-2 text-sm text-neutral-500">No new records were created.</li> : null}
            </ul>
            {result.degraded_fields.length > 0 ? (
              <div role="status" className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                <p className="flex items-center gap-2 font-semibold">
                  <ExclamationTriangleIcon aria-hidden="true" className="h-4 w-4 text-amber-600" />
                  Degraded fields
                </p>
                <p className="mt-0.5 text-xs text-amber-800">These fields were written in fallback form (for example the custom CRM column is missing, so the content went into the call description):</p>
                <ul className="mt-1 flex flex-wrap gap-1.5">
                  {result.degraded_fields.map((field) => (
                    <li key={field}>
                      <code className="rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-900">{field}</code>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </>
        ) : (
          <p className="text-sm text-neutral-600">The CRM write is in progress…</p>
        )}
        <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap">
          {nextId ? (
            <Button variant="primary" icon={ArrowRightIcon} onClick={onNext}>
              Next to review
            </Button>
          ) : null}
          <Button variant="secondary" onClick={onViewCrm}>
            View in CRM
          </Button>
          <Button variant="ghost" onClick={onInbox}>
            Back to inbox
          </Button>
        </div>
      </CardBody>
    </Card>
  );
}

export function RejectedCard({ detail, onInbox }: { detail: ConversationDetail; onInbox: () => void }) {
  const reason = detail.stages.find((stage) => stage.stage === 'review')?.detail.reason;
  return (
    <Card aria-labelledby="rejected-title" className="border-neutral-300">
      <CardHeader id="rejected-title" title="Rejected" subtitle="Nothing was written to the CRM" icon={XCircleIcon} />
      <CardBody className="space-y-3">
        {typeof reason === 'string' && reason ? <p className="text-sm text-neutral-700">Reason: {reason}</p> : <p className="text-sm text-neutral-500">No reason was given.</p>}
        <Button variant="secondary" onClick={onInbox}>
          Back to inbox
        </Button>
      </CardBody>
    </Card>
  );
}

export function FailedCard({ detail, onInbox }: { detail: ConversationDetail; onInbox: () => void }) {
  return (
    <Card aria-labelledby="failed-title" className="border-red-300">
      <CardHeader id="failed-title" title="Processing failed" subtitle="No proposal was produced" icon={ExclamationTriangleIcon} className="bg-red-50/60" />
      <CardBody className="space-y-3">
        <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-800 ring-1 ring-inset ring-red-200">
          {detail.record.error ?? 'The pipeline failed before a proposal could be built.'}
        </p>
        <Button variant="secondary" onClick={onInbox}>
          Back to inbox
        </Button>
      </CardBody>
    </Card>
  );
}
