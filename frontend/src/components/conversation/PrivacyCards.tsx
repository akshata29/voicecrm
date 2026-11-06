import {
  CheckBadgeIcon,
  EyeSlashIcon,
  ExclamationTriangleIcon,
  HeartIcon,
  LockClosedIcon,
  NoSymbolIcon,
  ShieldCheckIcon,
  ShieldExclamationIcon,
  ChatBubbleLeftRightIcon,
  ArchiveBoxIcon,
} from '@heroicons/react/24/outline';
import clsx from 'clsx';
import type { MarketPolicy, PolicyAction, PolicyActionKind, Proposal, RetentionDecision } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import type { IconType, Tone } from '@/components/ui/types';
import { formatDate, humanize, pluralize } from '@/lib/format';
import { POLICY_KIND_LABELS } from '@/lib/labels';
import { MarketChip } from './Badges';

const KIND_STYLE: Record<PolicyActionKind, { icon: IconType; tone: Tone }> = {
  pii_redacted: { icon: LockClosedIcon, tone: 'info' },
  omitted_sensitive: { icon: EyeSlashIcon, tone: 'danger' },
  dropped_small_talk: { icon: NoSymbolIcon, tone: 'neutral' },
  kept_relationship: { icon: HeartIcon, tone: 'success' },
};

const KIND_ORDER: PolicyActionKind[] = ['omitted_sensitive', 'pii_redacted', 'dropped_small_talk', 'kept_relationship'];

function segmentLabel(ids: number[]): string {
  if (ids.length === 0) return 'no segments';
  const shown = ids.slice(0, 6).map((id) => `#${id}`).join(', ');
  return `${pluralize(ids.length, 'segment')} (${shown}${ids.length > 6 ? ', \u2026' : ''})`;
}

/** Aggregates policy actions by kind and category. Never shows the omitted text: the API only sends categories and segment ids. */
export function PolicyActionsCard({ proposal }: { proposal: Proposal }) {
  const actions = proposal.policy_actions;
  const validation = proposal.validation;
  const grouped = KIND_ORDER.map((kind) => ({ kind, items: actions.filter((action) => action.kind === kind) })).filter((group) => group.items.length > 0);
  const totalSegments = new Set(actions.flatMap((action) => action.segment_ids)).size;

  return (
    <Card aria-labelledby="privacy-title">
      <CardHeader
        id="privacy-title"
        title="What the privacy policy did"
        subtitle={actions.length > 0 ? `${pluralize(actions.length, 'action')} across ${pluralize(totalSegments, 'segment')}` : 'Applied before any model saw the text'}
        icon={ShieldCheckIcon}
      />
      <CardBody className="space-y-3">
        {grouped.length === 0 ? (
          <p className="flex items-start gap-2 text-sm text-neutral-600">
            <CheckBadgeIcon aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0 text-emerald-600" />
            Nothing in this call needed redaction, omission or filtering.
          </p>
        ) : (
          grouped.map(({ kind, items }) => {
            const { icon: Icon, tone } = KIND_STYLE[kind];
            return (
              <section key={kind} aria-label={POLICY_KIND_LABELS[kind]}>
                <h3 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-neutral-500">
                  <Icon aria-hidden="true" className="h-4 w-4" />
                  {POLICY_KIND_LABELS[kind]}
                  <span className="tabular rounded-full bg-neutral-100 px-1.5 text-neutral-600">{items.length}</span>
                </h3>
                <ul className="mt-1.5 space-y-1.5">
                  {items.map((action: PolicyAction, index) => (
                    <li key={`${kind}-${index}`} className="rounded-md border border-neutral-200 bg-neutral-50 px-3 py-2">
                      <div className="flex flex-wrap items-center gap-1.5">
                        {action.category ? (
                          <Badge tone={tone} icon={Icon}>
                            {humanize(action.category)}
                          </Badge>
                        ) : null}
                        <span className="tabular text-xs font-medium text-neutral-600">{segmentLabel(action.segment_ids)}</span>
                      </div>
                      <p className="mt-1 text-xs text-neutral-600">{action.detail}</p>
                    </li>
                  ))}
                </ul>
              </section>
            );
          })
        )}

        <div
          className={clsx(
            'flex items-start gap-2 rounded-md border px-3 py-2 text-xs',
            validation.passed ? 'border-emerald-200 bg-emerald-50 text-emerald-900' : 'border-red-200 bg-red-50 text-red-900',
          )}
          role={validation.passed ? undefined : 'alert'}
        >
          {validation.passed ? <ShieldCheckIcon aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" /> : <ShieldExclamationIcon aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />}
          <p className="font-semibold">
            Leak check {validation.passed ? 'passed' : 'failed'}: {pluralize(validation.leak_count, 'leak')} · {pluralize(validation.unsupported_claims, 'unsupported claim')}
          </p>
        </div>
        {validation.findings.length > 0 ? (
          <ul className="space-y-1" aria-label="Validation findings">
            {validation.findings.map((finding) => (
              <li
                key={`${finding.code}-${finding.where ?? ''}`}
                className={clsx(
                  'flex items-start gap-2 rounded-md border px-3 py-2 text-xs',
                  finding.severity === 'error' ? 'border-red-200 bg-red-50 text-red-900' : 'border-amber-200 bg-amber-50 text-amber-900',
                )}
              >
                <ExclamationTriangleIcon aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
                <span>
                  <span className="font-semibold">{finding.severity === 'error' ? 'Error' : 'Warning'}:</span> {finding.message}
                </span>
              </li>
            ))}
          </ul>
        ) : null}
        <p className="text-[11px] leading-4 text-neutral-500">Omitted content never reaches the browser: only categories and segment numbers are shown here.</p>
      </CardBody>
    </Card>
  );
}

/** Per-market retention decision: the toggle's effect, with the "CRM holds no transcript" guarantee. */
export function RetentionCard({ retention, transcriptNote, market }: { retention: RetentionDecision | null; transcriptNote: string; market?: MarketPolicy | undefined }) {
  return (
    <Card aria-labelledby="retention-title">
      <CardHeader
        id="retention-title"
        title="Retention decision"
        subtitle={retention ? 'Set per market by policy' : 'Applied when the call is approved'}
        icon={ArchiveBoxIcon}
        actions={retention ? <MarketChip market={retention.market} /> : market ? <MarketChip market={market.market} /> : null}
      />
      <CardBody className="space-y-3">
        {retention ? (
          <>
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
              <dt className="text-neutral-500">Transcript</dt>
              <dd className="font-medium text-neutral-900">
                {retention.transcript_stored ? (
                  <>
                    Kept in the vault{retention.transcript_redacted ? ' (redacted)' : ''}
                    {retention.transcript_expires_at ? <span className="font-normal text-neutral-600"> until {formatDate(retention.transcript_expires_at)}</span> : null}
                  </>
                ) : (
                  'Not retained: discarded after processing'
                )}
              </dd>
              <dt className="text-neutral-500">Audio</dt>
              <dd className="font-medium text-neutral-900">
                {retention.audio_expires_at ? <>Kept until {formatDate(retention.audio_expires_at)}</> : 'Deleted right after transcription'}
              </dd>
            </dl>
            <p className="text-sm text-neutral-600">{retention.explanation}</p>
          </>
        ) : (
          <>
            {market ? (
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
                <dt className="text-neutral-500">Transcript</dt>
                <dd className="font-medium text-neutral-900">
                  {market.transcript_retention === 'on'
                    ? `Will be kept in the vault${market.store_redacted_only ? ' (redacted)' : ''}${market.transcript_retention_days ? ` for ${pluralize(market.transcript_retention_days, 'day')}` : ''}`
                    : 'Will be discarded after review'}
                </dd>
                <dt className="text-neutral-500">Audio</dt>
                <dd className="font-medium text-neutral-900">
                  {market.audio_retention_days > 0 ? `Kept for ${pluralize(market.audio_retention_days, 'day')}` : 'Deleted right after transcription'}
                </dd>
              </dl>
            ) : null}
            <p className="text-sm text-neutral-600">{transcriptNote}</p>
          </>
        )}
        {retention?.crm_contains_transcript ? (
          <p role="alert" className="flex items-center gap-2 rounded-md bg-red-600 px-3 py-2 text-sm font-semibold text-white">
            <ShieldExclamationIcon aria-hidden="true" className="h-5 w-5" /> Invariant violated: the CRM must never hold a transcript
          </p>
        ) : (
          <p className="inline-flex items-center gap-1.5 rounded-full bg-emerald-50 px-2.5 py-1 text-xs font-semibold text-emerald-800 ring-1 ring-inset ring-emerald-200">
            <ShieldCheckIcon aria-hidden="true" className="h-4 w-4" />
            CRM holds no transcript
          </p>
        )}
      </CardBody>
    </Card>
  );
}

export function TranscriptUnavailable({ note }: { note: string }) {
  return (
    <Card aria-labelledby="transcript-title">
      <CardHeader id="transcript-title" title="Transcript" subtitle="Governed by the market policy" icon={ChatBubbleLeftRightIcon} />
      <CardBody>
        <p className="flex items-start gap-2 text-sm text-neutral-600">
          <EyeSlashIcon aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0 text-neutral-400" />
          <span>{note}</span>
        </p>
      </CardBody>
    </Card>
  );
}
