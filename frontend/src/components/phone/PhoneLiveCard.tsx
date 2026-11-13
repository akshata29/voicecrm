import { PhoneArrowDownLeftIcon } from '@heroicons/react/24/outline';
import clsx from 'clsx';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { usePhoneStatus } from '@/api/hooks';
import type { PhoneCall, PhoneCallState, PhoneSeller, PhoneStatus } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { ErrorState, LoadingBlock } from '@/components/ui/StateViews';
import type { Tone } from '@/components/ui/types';
import { EM_DASH, formatClock, formatDateTime, formatDurationSeconds, formatRelative, formatTime, truncate } from '@/lib/format';
import { useNow } from '@/lib/hooks';

const STATE: Record<PhoneCallState, { label: string; tone: Tone; live?: boolean }> = {
  ringing: { label: 'Ringing', tone: 'info' },
  connecting: { label: 'Connecting', tone: 'info' },
  notice: { label: 'Recording notice', tone: 'info' },
  calling_seller: { label: 'Calling the seller', tone: 'info' },
  in_call: { label: 'In call', tone: 'success', live: true },
  leaving_message: { label: 'Leaving a message', tone: 'success', live: true },
  ended: { label: 'Ended', tone: 'warning' },
  processing: { label: 'Processing', tone: 'warning' },
  ingested: { label: 'Ingested', tone: 'success' },
  failed: { label: 'Failed', tone: 'danger' },
  missed: { label: 'Missed', tone: 'danger' },
};

const NOTICE: Record<PhoneCall['notice'], string> = { pending: 'not played yet', played: 'played', skipped: 'skipped', failed: 'failed' };
const RECORDING: Record<PhoneCall['recording'], string> = { none: 'not started', active: 'on', ready: 'ready', failed: 'failed' };

function health(status: PhoneStatus): { label: string; tone: Tone } {
  if (status.mode === 'off') return { label: 'Off', tone: 'neutral' };
  if (!status.configured) return { label: 'Not configured', tone: 'warning' };
  if (status.last_error || !status.ingress_listening) return { label: 'Needs attention', tone: 'danger' };
  return { label: 'Listening', tone: 'success' };
}

function sellerText(seller: PhoneSeller): string {
  if (seller.mode === 'teams') return seller.name ? `${seller.name} (Teams)` : 'Teams user';
  if (seller.mode === 'console') return `${seller.name ? `${seller.name} \u00b7 ` : ''}console ${seller.online ? 'online' : 'offline'}`;
  return 'none (voicemail)';
}

/** Only the host of the tunnel is shown: the scheme and path add nothing and the full URL is not needed on screen. */
function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url.replace(/^[a-z][a-z0-9+.-]*:\/\//i, '').replace(/[/?#].*$/, '');
  }
}

/** Seconds a call has lasted: until it ended, or until now while it goes on. */
function lastedSeconds(call: PhoneCall, now: number): number | null {
  if (call.ended_at && call.duration_seconds !== null) return call.duration_seconds;
  const seconds = ((call.ended_at ? Date.parse(call.ended_at) : now) - Date.parse(call.started_at)) / 1000;
  return Number.isFinite(seconds) ? Math.max(0, seconds) : null;
}

/** "+00:12": how long after the call arrived this happened. */
function offset(at: string, from: string): string {
  const seconds = (Date.parse(at) - Date.parse(from)) / 1000;
  return Number.isFinite(seconds) ? `+${formatClock(seconds)}` : formatTime(at);
}

function Fact({ label, children, title }: { label: string; children: ReactNode; title?: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs font-medium uppercase tracking-wide text-neutral-500">{label}</dt>
      <dd className="mt-0.5 text-sm font-semibold text-neutral-900" title={title}>
        {children}
      </dd>
    </div>
  );
}

function Caller({ call }: { call: PhoneCall }) {
  const phone = call.caller_kind === 'phone';
  const name = call.caller_name ?? (phone ? null : call.caller);
  return (
    <span className="min-w-0 break-words">
      {name ? <span className="font-semibold text-neutral-900">{name}</span> : null}
      {phone ? (
        <>
          {name ? ' ' : null}
          <span className={clsx('font-mono', name ? 'ml-1 text-xs text-neutral-500' : 'font-semibold text-neutral-900')}>{call.caller}</span>
        </>
      ) : null}{' '}
      <span className="ml-1 text-xs text-neutral-500">{phone ? 'phone' : 'browser'}</span>
    </span>
  );
}

function Timeline({ call }: { call: PhoneCall }) {
  return (
    <ol aria-label="Call timeline" className="space-y-1 border-l border-neutral-200 pl-3">
      {call.timeline.map((event, index) => (
        <li key={`${event.at}-${index}`} className="relative flex gap-2 text-xs text-neutral-600">
          <span aria-hidden="true" className="absolute -left-[0.9rem] top-1.5 h-1.5 w-1.5 rounded-full bg-neutral-300" />
          <time dateTime={event.at} title={formatDateTime(event.at)} className="tabular w-12 shrink-0 text-neutral-400">
            {offset(event.at, call.started_at)}
          </time>
          <span className="min-w-0 break-words">{event.label}</span>
        </li>
      ))}
    </ol>
  );
}

function StateBadge({ state }: { state: PhoneCallState }) {
  const { label, tone, live } = STATE[state];
  return (
    <Badge tone={tone} dot={live} pulse={live}>
      {label}
    </Badge>
  );
}

function ActiveCall({ call, now }: { call: PhoneCall; now: number }) {
  const seconds = lastedSeconds(call, now);
  return (
    <li className="space-y-2 px-3 py-3 text-sm">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <Caller call={call} />
        <StateBadge state={call.state} />
        {seconds !== null ? (
          <span className="tabular text-xs text-neutral-500" title="Time since the call arrived">
            {formatClock(seconds)}
          </span>
        ) : null}
        {call.seller ? <span className="text-xs text-neutral-500">with {call.seller}</span> : null}
      </div>
      <p className="text-xs text-neutral-500">
        Recording notice <span className={clsx(call.notice === 'failed' && 'font-semibold text-red-700')}>{NOTICE[call.notice]}</span>
        {' \u00b7 '}recording <span className={clsx(call.recording === 'failed' && 'font-semibold text-red-700')}>{RECORDING[call.recording]}</span>
      </p>
      {call.error ? (
        <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-xs text-red-800">
          {call.error}
        </p>
      ) : null}
      <Timeline call={call} />
    </li>
  );
}

/** Why a call was missed, in the words the line wrote last: a missed call is not an error, so it has no error text of its own. */
function missedReason(call: PhoneCall): string | null {
  if (call.state !== 'missed' || call.error) return null;
  return call.timeline[call.timeline.length - 1]?.label ?? null;
}

function RecentCall({ call }: { call: PhoneCall }) {
  const reason = missedReason(call);
  return (
    <li className="px-3 py-2.5 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1.5">
          <Caller call={call} />
          <StateBadge state={call.state} />
        </div>
        <div className="flex items-center gap-3 text-xs text-neutral-500">
          <span className="tabular" title="How long the call lasted">
            {formatDurationSeconds(call.duration_seconds)}
          </span>
          <span title={formatDateTime(call.started_at)}>{formatRelative(call.started_at)}</span>
          {call.conversation_id ? (
            <Link to={`/conversations/${call.conversation_id}`} className="font-medium text-brand-600 hover:underline">
              Open
            </Link>
          ) : null}
        </div>
      </div>
      {call.error ? <p className="mt-1 break-words text-xs text-red-700">{call.error}</p> : null}
      {reason ? <p className="mt-1 break-words text-xs text-neutral-600">{reason}</p> : null}
      {call.timeline.length > 0 ? (
        <details className="mt-1.5">
          <summary className="cursor-pointer select-none text-xs text-neutral-500 hover:text-neutral-700">Timeline</summary>
          <div className="mt-1.5">
            <Timeline call={call} />
          </div>
        </details>
      ) : null}
    </li>
  );
}

/**
 * The phone line, as an operator sees it: is it listening, who takes the calls, which calls are on right now (each with the
 * timeline the service writes) and how the latest ones ended. A finished call links to its conversation in the Inbox.
 */
export function PhoneLiveCard() {
  const status = usePhoneStatus();
  const data = status.data;
  const now = useNow(data && data.active.length > 0 ? 1000 : 10_000);
  const state = data ? health(data) : null;
  const running = data !== undefined && data.mode !== 'off' && data.configured;

  return (
    <Card aria-labelledby="phone-live-title">
      <CardHeader
        id="phone-live-title"
        icon={PhoneArrowDownLeftIcon}
        title="Phone line"
        subtitle="Calls to the VoiceCRM line are answered, recorded on separate channels and sent through the same pipeline"
        actions={state ? <Badge tone={state.tone}>{state.label}</Badge> : null}
      />
      <CardBody className="space-y-4">
        {!data ? (
          status.isError ? (
            <ErrorState error={status.error} title="Could not read the phone line" compact onRetry={() => void status.refetch()} />
          ) : (
            <LoadingBlock label="Reading the phone line…" className="py-6" />
          )
        ) : !running ? (
          <p className="text-sm text-neutral-600">{data.problem ?? 'The phone line is not running.'}</p>
        ) : (
          <>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-4">
              <Fact label="Number">{data.number ?? 'Browser line'}</Fact>
              <Fact label="Seller">
                {data.seller.mode === 'console' ? <span aria-hidden="true" className={clsx('mr-1.5 inline-block h-2 w-2 rounded-full', data.seller.online ? 'bg-emerald-500' : 'bg-neutral-400')} /> : null}
                {sellerText(data.seller)}
              </Fact>
              <Fact label="Public URL" title={data.public_url ? hostOf(data.public_url) : undefined}>
                <span className="block truncate">{data.public_url ? truncate(hostOf(data.public_url), 36) : EM_DASH}</span>
              </Fact>
              <Fact label="Calls">
                <span className="tabular">{data.calls}</span>{' '}
                <span className="tabular font-normal text-neutral-500">
                  ({data.ingested} ingested, {data.failed} failed{data.missed > 0 ? `, ${data.missed} missed` : ''})
                </span>
              </Fact>
            </dl>

            <p className="text-xs text-neutral-500">
              Every call is recorded on separate channels. The recording notice is {data.notice_enabled ? 'played before the seller is rung' : 'switched off'}.
              {data.last_event_at ? ` Last call event ${formatRelative(data.last_event_at)}.` : ''}
            </p>

            {!data.ingress_listening ? (
              <p role="status" className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                Nothing is listening for call events, so Azure cannot report calls to VoiceCRM. Check that the API is running and that the dev tunnel points at it.
              </p>
            ) : null}

            {data.last_error ? (
              <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-800">
                {data.last_error}
              </p>
            ) : null}

            {data.active.length > 0 ? (
              <section aria-labelledby="phone-active-title">
                <h3 id="phone-active-title" className="section-title mb-1.5">
                  Active calls ({data.active.length})
                </h3>
                <ul className="divide-y divide-neutral-100 rounded-md border border-neutral-200" aria-label="Active calls">
                  {data.active.map((call) => (
                    <ActiveCall key={call.call_id} call={call} now={now} />
                  ))}
                </ul>
              </section>
            ) : null}

            {data.recent.length > 0 ? (
              <section aria-labelledby="phone-recent-title">
                <h3 id="phone-recent-title" className="section-title mb-1.5">
                  Recent calls
                </h3>
                <ul className="divide-y divide-neutral-100 rounded-md border border-neutral-200" aria-label="Recent calls">
                  {data.recent.slice(0, 10).map((call) => (
                    <RecentCall key={call.call_id} call={call} />
                  ))}
                </ul>
              </section>
            ) : null}

            {data.active.length === 0 && data.recent.length === 0 ? (
              <p className="text-sm text-neutral-500">No calls yet. Dial the line from the browser (Live demo, section D) or from a phone once a number is attached.</p>
            ) : null}
          </>
        )}
      </CardBody>
    </Card>
  );
}
