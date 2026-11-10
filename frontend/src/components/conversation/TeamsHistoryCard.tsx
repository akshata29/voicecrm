import { ArrowDownTrayIcon, ClockIcon, MagnifyingGlassIcon } from '@heroicons/react/24/outline';
import clsx from 'clsx';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { getErrorMessage } from '@/api/client';
import { useFindTeamsMeetings, useImportTeams, useTeamsStatus } from '@/api/hooks';
import type { TeamsHistory, TeamsHistoryItem, TeamsImportResult } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { ErrorState, LoadingBlock } from '@/components/ui/StateViews';
import { useToast } from '@/components/ui/Toast';
import { formatDateTime, formatDurationSeconds, pluralize } from '@/lib/format';
import { useLocalStorageState } from '@/lib/hooks';
import { StatusBadge } from './Badges';

/** The most the server imports in one request (IMPORT_MAX in pipeline/teams_live.py). */
const IMPORT_MAX = 25;
const PERIODS = [7, 14, 30, 60, 90] as const;
const DEFAULT_DAYS = 30;

const keyOf = (item: Pick<TeamsHistoryItem, 'meeting_id' | 'transcript_id'>) => `${item.meeting_id}|${item.transcript_id}`;
const titleOf = (item: Pick<TeamsHistoryItem, 'title'>) => item.title ?? 'Teams meeting';

function summarise(result: TeamsImportResult): string {
  const parts: string[] = [];
  if (result.ingested > 0) parts.push(`${pluralize(result.ingested, 'meeting')} imported`);
  if (result.duplicates > 0) parts.push(`${result.duplicates} already in the Inbox`);
  if (result.failed > 0) parts.push(`${result.failed} failed`);
  return parts.join(', ');
}

function StateCell({ item }: { item: TeamsHistoryItem }) {
  if (item.state === 'in_inbox') {
    return (
      <span className="flex items-center gap-2">
        {item.conversation_status ? <StatusBadge status={item.conversation_status} /> : <Badge tone="success">In the Inbox</Badge>}
        {item.conversation_id ? (
          <Link to={`/conversations/${item.conversation_id}`} className="text-xs font-medium text-brand-600 hover:underline">
            Open
          </Link>
        ) : null}
      </span>
    );
  }
  if (item.state === 'unfinished') {
    return (
      <Badge tone="warning" title="Graph is still writing this transcript; try again in a few minutes">
        Still being written
      </Badge>
    );
  }
  return <Badge tone="info">Ready to import</Badge>;
}

/**
 * Short on time for a live meeting? Lists the transcripts Microsoft Graph still holds for the configured organisers (the same
 * `getAllTranscripts` call the live poller makes, with an earlier start) and brings the ones you tick through the same
 * download, parser and pipeline as a meeting that just ended. Imported meetings are not counted by the live card above.
 */
export function TeamsHistoryCard() {
  const status = useTeamsStatus();
  const find = useFindTeamsMeetings();
  const importer = useImportTeams();
  const toast = useToast();
  const [storedDays, setDays] = useLocalStorageState<number>('voicecrm.teams.history.days', DEFAULT_DAYS);
  const [found, setFound] = useState<TeamsHistory | null>(null);
  const [picked, setPicked] = useState<ReadonlySet<string>>(() => new Set());
  const [failures, setFailures] = useState<string[]>([]);
  const data = status.data;
  if (!data || data.mode === 'off' || !data.configured) return null;

  const days = (PERIODS as readonly number[]).includes(storedDays) ? storedDays : DEFAULT_DAYS;
  const items = found?.items ?? [];
  const ready = items.filter((item) => item.state === 'new');
  const inInbox = items.filter((item) => item.state === 'in_inbox').length;

  const search = (period: number) =>
    find.mutate(period, {
      onSuccess: (history) => {
        setFound(history);
        // Keep only the ticks that still point at an importable meeting (after an import the rows have moved on).
        setPicked((previous) => new Set(history.items.filter((item) => item.state === 'new' && previous.has(keyOf(item))).map(keyOf)));
      },
    });

  const findMeetings = () => {
    setFailures([]);
    search(days);
  };

  const toggle = (key: string) =>
    setPicked((previous) => {
      const next = new Set(previous);
      if (!next.delete(key)) next.add(key);
      return next;
    });

  const importPicked = () => {
    if (!found) return;
    const chosen = found.items.filter((item) => picked.has(keyOf(item)));
    importer.mutate(
      chosen.map(({ organizer, meeting_id, transcript_id }) => ({ organizer, meeting_id, transcript_id })),
      {
        onSuccess: (result) => {
          const titles = new Map(chosen.map((item) => [item.transcript_id, titleOf(item)]));
          setPicked(new Set());
          setFailures(
            result.outcomes
              .filter((outcome) => outcome.outcome === 'failed')
              .map((outcome) => `${titles.get(outcome.transcript_id) ?? 'A meeting'}: ${outcome.detail ?? 'it could not be imported'}`),
          );
          const message = `${summarise(result)}.${result.ingested > 0 ? ' They are being processed now.' : ''}`;
          if (result.ingested === 0 && result.failed > 0) toast.error('Nothing was imported', { message });
          else toast.success(result.ingested > 0 ? 'Imported from Teams' : 'Nothing new to import', { message });
          search(found.days); // the imported rows now show their Inbox status
        },
        onError: (error) => toast.error('Could not import the meetings', { message: getErrorMessage(error) }),
      },
    );
  };

  return (
    <Card aria-labelledby="teams-history-title">
      <CardHeader
        id="teams-history-title"
        icon={ClockIcon}
        title="Import earlier meetings"
        subtitle="No time for a live meeting? Bring an earlier one through the same Graph download, parser and pipeline"
        actions={
          <>
            <label htmlFor="teams-history-days" className="sr-only">
              How far back to look
            </label>
            <select id="teams-history-days" className="input !w-auto py-1 text-xs" value={days} onChange={(event) => setDays(Number(event.target.value))}>
              {PERIODS.map((period) => (
                <option key={period} value={period}>
                  Last {period} days
                </option>
              ))}
            </select>
            <Button size="sm" variant="primary" icon={MagnifyingGlassIcon} loading={find.isPending} onClick={findMeetings}>
              Find meetings
            </Button>
          </>
        }
      />
      <CardBody className="space-y-4">
        {find.isError ? <ErrorState error={find.error} title="Could not search Teams" compact onRetry={findMeetings} /> : null}

        {!found && !find.isPending && !find.isError ? (
          <p className="text-sm text-neutral-600">
            Pick a period and <strong>Find meetings</strong>. VoiceCRM asks Microsoft Graph which transcripts the organisers still have and lists them; nothing is imported until you tick a meeting and import it. It then
            appears in the Inbox like a meeting that just ended.
          </p>
        ) : null}

        {find.isPending && !found ? <LoadingBlock label="Asking Microsoft Graph for transcripts…" className="py-6" /> : null}

        {found ? (
          <div aria-live="polite" className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-sm text-neutral-700">
                {items.length === 0 ? (
                  <>No Teams transcripts in the last {found.days} days.</>
                ) : (
                  <>
                    <strong>{pluralize(items.length, 'meeting')}</strong> in the last {found.days} days
                    {inInbox > 0 ? `, ${inInbox} already in the Inbox` : ''}
                    {ready.length > 0 ? `, ${ready.length} ready to import` : ''}.
                  </>
                )}
              </p>
              {items.length > 0 ? (
                <span className="flex items-center gap-2">
                  {picked.size > 0 ? (
                    <Button size="sm" variant="ghost" onClick={() => setPicked(new Set())}>
                      Clear
                    </Button>
                  ) : null}
                  <Button size="sm" disabled={ready.length === 0} onClick={() => setPicked(new Set(ready.slice(0, IMPORT_MAX).map(keyOf)))}>
                    {ready.length > IMPORT_MAX ? `Select newest ${IMPORT_MAX}` : 'Select all new'}
                  </Button>
                  <Button size="sm" variant="primary" icon={ArrowDownTrayIcon} loading={importer.isPending} disabled={picked.size === 0} onClick={importPicked}>
                    Import {picked.size > 0 ? picked.size : ''} selected
                  </Button>
                </span>
              ) : null}
            </div>

            {items.length === 0 ? (
              <p className="text-sm text-neutral-500">
                Only scheduled meetings (not Meet now) with transcription started have a transcript. Try a longer period, or check that the organisers above host the meetings.
              </p>
            ) : (
              <div className="overflow-x-auto rounded-md border border-neutral-200">
                <table className="min-w-full divide-y divide-neutral-100 text-sm">
                  <caption className="sr-only">Earlier Teams meetings that have a transcript</caption>
                  <thead className="bg-neutral-50">
                    <tr>
                      <th scope="col" className="th w-10">
                        <span className="sr-only">Select</span>
                      </th>
                      <th scope="col" className="th">
                        Meeting
                      </th>
                      <th scope="col" className="th">
                        When
                      </th>
                      <th scope="col" className="th">
                        Organiser
                      </th>
                      <th scope="col" className="th">
                        In VoiceCRM
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-neutral-100 bg-white">
                    {items.map((item) => {
                      const key = keyOf(item);
                      const ticked = picked.has(key);
                      return (
                        <tr key={key} className={clsx(ticked ? 'bg-brand-50/40' : 'hover:bg-neutral-50')}>
                          <td className="td w-10">
                            <input
                              type="checkbox"
                              className="h-4 w-4 rounded border-neutral-300 accent-brand-600"
                              checked={ticked}
                              disabled={item.state !== 'new' || (!ticked && picked.size >= IMPORT_MAX)}
                              onChange={() => toggle(key)}
                              aria-label={`Select ${titleOf(item)}`}
                            />
                          </td>
                          <td className="td max-w-xs">
                            <span className="block truncate font-medium text-neutral-900" title={titleOf(item)}>
                              {titleOf(item)}
                            </span>
                            {item.attendees.length > 0 ? (
                              <span className="block truncate text-xs text-neutral-500" title={item.attendees.join(', ')}>
                                With {item.attendees.join(', ')}
                              </span>
                            ) : null}
                          </td>
                          <td className="td whitespace-nowrap text-neutral-700">
                            {formatDateTime(item.started_at)}
                            <span className="block text-xs text-neutral-500">{formatDurationSeconds(item.duration_seconds)}</span>
                          </td>
                          <td className="td max-w-[10rem] truncate text-neutral-700" title={item.organizer}>
                            {item.organizer_name ?? item.organizer}
                          </td>
                          <td className="td whitespace-nowrap">
                            <StateCell item={item} />
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}

            {found.truncated ? (
              <p className="text-xs text-neutral-500">Showing the newest {items.length}. Choose a shorter period to see the rest.</p>
            ) : null}
            {found.errors.length > 0 ? (
              <p role="status" className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                Could not search every organiser: {found.errors.join(' ')}
              </p>
            ) : null}
          </div>
        ) : null}

        {failures.length > 0 ? (
          <div role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-800">
            <p className="font-semibold">Not imported</p>
            <ul className="mt-1 list-inside list-disc space-y-0.5">
              {failures.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}
