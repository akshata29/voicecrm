import { ArrowPathIcon, VideoCameraIcon } from '@heroicons/react/24/outline';
import { Link } from 'react-router-dom';
import { getErrorMessage } from '@/api/client';
import { usePollTeams, useTeamsStatus } from '@/api/hooks';
import type { TeamsRecent, TeamsStatus } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { useToast } from '@/components/ui/Toast';
import type { Tone } from '@/components/ui/types';
import { EM_DASH, formatRelative } from '@/lib/format';
import { useNow } from '@/lib/hooks';

const OUTCOME_TONE: Record<TeamsRecent['outcome'], Tone> = { ingested: 'success', duplicate: 'neutral', failed: 'danger' };

function health(status: TeamsStatus): { label: string; tone: Tone } {
  if (status.mode === 'off') return { label: 'Off', tone: 'neutral' };
  if (!status.configured) return { label: 'Not configured', tone: 'warning' };
  if (status.last_poll_ok === false || status.last_error) return { label: 'Needs attention', tone: 'danger' };
  return { label: status.mode === 'poll' ? 'Polling' : 'Listening for webhooks', tone: 'success' };
}

/**
 * Live Microsoft Teams ingestion: Graph delivers each meeting transcript after the meeting ends and it flows into the same
 * pipeline as every other channel. This is the operator view: is it healthy, what did it ingest, what is wrong.
 */
export function TeamsLiveCard() {
  const status = useTeamsStatus();
  const poll = usePollTeams();
  const toast = useToast();
  const now = useNow(5000);
  const data = status.data;
  if (!data) return null;
  const state = health(data);
  const running = data.mode !== 'off' && data.configured;

  const checkNow = () =>
    poll.mutate(undefined, {
      onSuccess: (result) =>
        toast.success(result.ingested > 0 ? `${result.ingested} new Teams transcript${result.ingested === 1 ? '' : 's'}` : 'No new transcripts yet', {
          message: result.ingested > 0 ? 'They are being processed now.' : 'Graph can take several minutes after a meeting ends (it lags the Teams chat).',
        }),
      onError: (error) => toast.error('Could not check Teams', { message: getErrorMessage(error) }),
    });

  return (
    <Card aria-labelledby="teams-live-title">
      <CardHeader
        id="teams-live-title"
        icon={VideoCameraIcon}
        title="Live Teams ingestion"
        subtitle="Microsoft Graph hands over each meeting transcript after the call; nobody uploads anything"
        actions={
          <>
            <Badge tone={state.tone}>{state.label}</Badge>
            {running ? (
              <Button size="sm" icon={ArrowPathIcon} loading={poll.isPending} onClick={checkNow}>
                Check now
              </Button>
            ) : null}
          </>
        }
      />
      <CardBody className="space-y-4">
        {!running ? (
          <p className="text-sm text-neutral-600">{data.problem ?? 'Live Teams ingestion is not running.'}</p>
        ) : (
          <>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-4">
              <div>
                <dt className="text-xs font-medium uppercase tracking-wide text-neutral-500">Mode</dt>
                <dd className="mt-0.5 text-sm font-semibold text-neutral-900">{data.mode === 'poll' ? `Poll every ${data.poll_seconds} s` : 'Webhook'}</dd>
              </div>
              <div>
                <dt className="text-xs font-medium uppercase tracking-wide text-neutral-500">Last check</dt>
                <dd className="mt-0.5 text-sm font-semibold text-neutral-900">{data.last_poll_at ? formatRelative(data.last_poll_at, now) : EM_DASH}</dd>
              </div>
              <div>
                <dt className="text-xs font-medium uppercase tracking-wide text-neutral-500">Ingested</dt>
                <dd className="tabular mt-0.5 text-sm font-semibold text-neutral-900">
                  {data.ingested}
                  {data.last_ingest_at ? <span className="ml-1 font-normal text-neutral-500">({formatRelative(data.last_ingest_at, now)})</span> : null}
                </dd>
              </div>
              <div>
                <dt className="text-xs font-medium uppercase tracking-wide text-neutral-500">Organisers</dt>
                <dd className="mt-0.5 truncate text-sm font-semibold text-neutral-900" title={data.organizers.join(', ')}>
                  {data.organizers.length > 0 ? data.organizers.join(', ') : EM_DASH}
                </dd>
              </div>
            </dl>

            {data.mode === 'webhook' && data.notification_url ? (
              <p className="text-xs text-neutral-500">
                Graph posts to <code className="rounded bg-neutral-100 px-1 py-0.5">{data.notification_url}</code>
              </p>
            ) : null}

            {data.last_error ? (
              <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-800">
                {data.last_error}
              </p>
            ) : null}

            {data.recent.length > 0 ? (
              <ul className="divide-y divide-neutral-100 rounded-md border border-neutral-200" aria-label="Recently seen transcripts">
                {data.recent.map((item) => (
                  <li key={`${item.transcript_id}-${item.at}`} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-sm">
                    <span className="min-w-0 flex-1 truncate text-neutral-800">{item.title ?? `Transcript ${item.transcript_id}`}</span>
                    <span className="flex items-center gap-2">
                      <Badge tone={OUTCOME_TONE[item.outcome]}>{item.outcome}</Badge>
                      <span className="text-xs text-neutral-500">{formatRelative(item.at, now)}</span>
                      {item.conversation_id ? (
                        <Link to={`/conversations/${item.conversation_id}`} className="text-xs font-medium text-brand-600 hover:underline">
                          Open
                        </Link>
                      ) : null}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-neutral-500">
                Nothing yet. Hold a <strong>scheduled</strong> Teams meeting (from the calendar, not Meet now) with transcription on. Graph can take several minutes after it ends to publish the transcript; it then appears here.
              </p>
            )}
          </>
        )}
      </CardBody>
    </Card>
  );
}
