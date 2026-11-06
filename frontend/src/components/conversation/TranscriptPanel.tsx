import { ChatBubbleLeftRightIcon, EyeSlashIcon } from '@heroicons/react/24/outline';
import clsx from 'clsx';
import { Fragment } from 'react';
import { useTranscript } from '@/api/hooks';
import type { CanonicalConversation, PolicyAction, PolicyActionKind, Segment } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { ErrorState, LoadingBlock } from '@/components/ui/StateViews';
import type { Tone } from '@/components/ui/types';
import { formatMsOffset } from '@/lib/format';
import { TranscriptUnavailable } from './PrivacyCards';

const KIND_PRIORITY: PolicyActionKind[] = ['omitted_sensitive', 'pii_redacted', 'dropped_small_talk', 'kept_relationship'];

const ANNOTATION: Record<PolicyActionKind, { label: string; tone: Tone }> = {
  omitted_sensitive: { label: 'omitted', tone: 'danger' },
  pii_redacted: { label: 'redacted', tone: 'info' },
  dropped_small_talk: { label: 'small talk: not in note', tone: 'neutral' },
  kept_relationship: { label: 'kept as relationship fact', tone: 'success' },
};

function annotations(actions: PolicyAction[]): Map<number, PolicyActionKind> {
  const map = new Map<number, PolicyActionKind>();
  for (const action of actions) {
    for (const id of action.segment_ids) {
      const current = map.get(id);
      if (!current || KIND_PRIORITY.indexOf(action.kind) < KIND_PRIORITY.indexOf(current)) map.set(id, action.kind);
    }
  }
  return map;
}

function speakerName(transcript: CanonicalConversation, segment: Segment): { name: string; role: string } {
  const { seller, participants } = transcript.meta;
  if (segment.speaker_id === seller.id) return { name: seller.display_name, role: 'seller' };
  const match = participants.find((participant) => participant.id === segment.speaker_id);
  if (match) return { name: match.display_name, role: match.role };
  return { name: segment.speaker_id, role: 'unknown' };
}

function RedactedText({ text }: { text: string }) {
  const parts = text.split(/(\[[A-Z0-9_]+\])/g);
  return (
    <>
      {parts.map((part, index) =>
        /^\[[A-Z0-9_]+\]$/.test(part) ? (
          <mark key={index} className="mx-0.5 rounded bg-sky-100 px-1 py-px font-mono text-[11px] font-semibold text-sky-900">
            {part}
          </mark>
        ) : (
          <Fragment key={index}>{part}</Fragment>
        ),
      )}
    </>
  );
}

interface TranscriptPanelProps {
  conversationId: string;
  /** Changes whenever the record changes, so a stale copy is never refetched after approve/reprocess. */
  revision: string;
  available: boolean;
  note: string;
  policyActions: PolicyAction[];
}

/** Shown only when ConversationDetail.transcript_available is true; otherwise the market-policy note is shown instead. */
export function TranscriptPanel({ conversationId, revision, available, note, policyActions }: TranscriptPanelProps) {
  if (!available) return <TranscriptUnavailable note={note} />;
  return <AvailableTranscript conversationId={conversationId} revision={revision} note={note} policyActions={policyActions} />;
}

function AvailableTranscript({ conversationId, revision, note, policyActions }: Omit<TranscriptPanelProps, 'available'>) {
  const query = useTranscript(conversationId, true, revision);
  const marks = annotations(policyActions);
  const source = query.data?.source;
  const subtitle =
    source === 'vault'
      ? "Retained in the vault under this market's retention policy"
      : source === 'working'
        ? 'Review copy: kept only until you decide, unless the market policy retains it'
        : 'Redacted text only: the CRM never holds it';

  return (
    <Card aria-labelledby="transcript-title">
      <CardHeader
        id="transcript-title"
        title="Transcript (redacted)"
        subtitle={subtitle}
        icon={ChatBubbleLeftRightIcon}
        actions={
          source ? (
            <Badge tone={source === 'vault' ? 'violet' : 'brand'} title="X-Transcript-Source response header">
              {source === 'vault' ? 'Vault copy' : 'Review working copy'}
            </Badge>
          ) : null
        }
      />
      <CardBody className="!p-0">
        {query.isPending ? <LoadingBlock label="Loading transcript…" /> : null}
        {query.isError ? (
          <div className="p-4">
            <ErrorState error={query.error} title="Transcript not available" compact />
          </div>
        ) : null}
        {query.data ? (
          <>
            <ol className="max-h-[26rem] divide-y divide-neutral-100 overflow-y-auto" aria-label="Transcript segments">
              {query.data.transcript.segments.map((segment) => {
                const speaker = speakerName(query.data.transcript, segment);
                const kind = marks.get(segment.id);
                const omitted = segment.text.trim() === '';
                return (
                  <li key={segment.id} className={clsx('px-4 py-2.5 text-sm sm:px-5', speaker.role === 'seller' ? 'bg-white' : 'bg-neutral-50/70')}>
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
                      <span className="font-semibold text-neutral-800">{speaker.name}</span>
                      <span className="text-neutral-400">{speaker.role}</span>
                      <span className="tabular text-neutral-400">
                        {formatMsOffset(segment.start_ms)} · #{segment.id}
                      </span>
                      {kind ? (
                        <Badge tone={ANNOTATION[kind].tone} className="ml-auto">
                          {ANNOTATION[kind].label}
                        </Badge>
                      ) : null}
                    </div>
                    {omitted ? (
                      <p className="mt-1 flex items-center gap-1.5 text-xs italic text-neutral-500">
                        <EyeSlashIcon aria-hidden="true" className="h-3.5 w-3.5" /> Omitted by the privacy policy
                      </p>
                    ) : (
                      <p className="mt-0.5 text-neutral-800" lang={segment.locale ?? undefined}>
                        <RedactedText text={segment.text} />
                      </p>
                    )}
                  </li>
                );
              })}
            </ol>
            <p className="border-t border-neutral-100 px-4 py-2 text-[11px] leading-4 text-neutral-500 sm:px-5">{note}</p>
          </>
        ) : null}
      </CardBody>
    </Card>
  );
}
