import { CheckCircleIcon, GlobeAltIcon, InformationCircleIcon, PhoneArrowUpRightIcon, PhoneIcon } from '@heroicons/react/24/outline';
import { Link } from 'react-router-dom';
import { USE_MOCK } from '@/api/client';
import { usePhoneStatus } from '@/api/hooks';
import type { PhoneCallerProfile, PhoneStatus } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { Field } from '@/components/ui/Field';
import { Spinner } from '@/components/ui/Spinner';
import { useLocalStorageState } from '@/lib/hooks';
import { CallButtons, CallClock, PHASE_TEXT } from './CallControls';
import { callingUnsupported, useDialer } from './useAcsCalling';

type Caller = Required<PhoneCallerProfile>;

const DEFAULT_CALLER: Caller = {
  name: 'Dana Whitfield',
  org: 'Harborview Pension Fund',
  email: 'dana.whitfield@harborview-pension.example',
  phone: '+1 206 555 0142',
};

/** Why the Call button is off, or null when the line can be dialled. */
function blockedReason(line: PhoneStatus | undefined, unreadable: boolean, unsupported: string | null): string | null {
  if (!line) return unreadable ? 'Could not read the state of the phone line.' : 'Checking the phone line…';
  if (line.mode === 'off' || !line.configured) return line.problem ?? 'The phone line is not configured.';
  if (!line.line_identity) return 'The line has no Azure identity yet. See the Phone line card for what is missing.';
  return unsupported;
}

/** A heads-up that does not stop the call: with nobody to ring, the line takes a message instead. */
function sellerHint(line: PhoneStatus | undefined): string | null {
  if (!line || line.mode === 'off' || !line.configured) return null;
  const offline = line.seller.mode === 'none' || (line.seller.mode === 'console' && !line.seller.online);
  if (!offline) return null;
  return 'No seller is online, so the line will take a message instead of ringing anyone. To be answered, switch on "Answer calls in this browser" in the seller console on this page first; to leave a message, speak after the prompt.';
}

/** Scenario D, the customer: a phone-style panel that places a VoIP call to the VoiceCRM line from this browser. */
export function Dialer() {
  const status = usePhoneStatus();
  const [stored, setCaller] = useLocalStorageState<Caller>('voicecrm.phone.caller', DEFAULT_CALLER);
  const caller: Caller = { ...DEFAULT_CALLER, ...stored };
  const dialer = useDialer();
  const line = status.data;
  const { phase } = dialer;

  const preparing = phase === 'preparing';
  const inCall = phase === 'connecting' || phase === 'ringing' || phase === 'connected';
  const blocked = blockedReason(line, status.isError, callingUnsupported());
  const hint = sellerHint(line);
  const set = (key: keyof Caller, value: string) => setCaller({ ...caller, [key]: value });

  return (
    <Card aria-labelledby="dialer-title">
      <CardHeader
        id="dialer-title"
        icon={PhoneArrowUpRightIcon}
        title="Call the VoiceCRM line"
        subtitle="You are the customer: the call goes out from this browser"
        actions={USE_MOCK ? <Badge tone="warning">Simulated</Badge> : null}
      />
      <CardBody className="space-y-4">
        <div className="rounded-xl bg-neutral-900 px-4 py-5 text-center text-white">
          <p className="text-xs font-medium uppercase tracking-wide text-neutral-400">{inCall ? 'On the line' : 'Dial'}</p>
          <p className="tabular mt-1 break-words text-2xl font-semibold tracking-tight sm:text-3xl">{line?.number ?? 'VoiceCRM line'}</p>
          <div className="mt-2 flex justify-center">
            <Badge tone="info" icon={GlobeAltIcon}>
              Browser call (VoIP)
            </Badge>
          </div>
          <p role="status" aria-live="polite" className="mt-3 flex min-h-7 items-center justify-center gap-2 text-base">
            {preparing ? (
              <>
                <Spinner className="h-4 w-4" />
                <span>Getting ready…</span>
              </>
            ) : phase === 'connected' ? (
              <>
                <span className="font-semibold text-emerald-300">{PHASE_TEXT.connected}</span>
                <CallClock since={dialer.connectedAt} className="text-neutral-200" />
              </>
            ) : inCall ? (
              <span className="animate-pulse-soft">{PHASE_TEXT[phase]}</span>
            ) : (
              <span className="text-neutral-300">{phase === 'ended' ? PHASE_TEXT.ended : 'Ready to call'}</span>
            )}
          </p>
        </div>

        {inCall ? (
          <div className="space-y-3">
            <CallButtons muted={dialer.muted} onToggleMute={dialer.toggleMute} onHangUp={dialer.hangUp} />
            {phase === 'connected' ? <p className="text-center text-xs text-neutral-500">Speak as the customer. Both sides of the call are being recorded.</p> : null}
          </div>
        ) : (
          <>
            <fieldset className="min-w-0" disabled={preparing}>
              <legend className="section-title mb-2">Who is calling</legend>
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Name" htmlFor="dialer-name">
                  <input id="dialer-name" className="input" autoComplete="off" maxLength={120} value={caller.name} onChange={(event) => set('name', event.target.value)} />
                </Field>
                <Field label="Organisation" htmlFor="dialer-org">
                  <input id="dialer-org" className="input" autoComplete="off" maxLength={160} value={caller.org} onChange={(event) => set('org', event.target.value)} />
                </Field>
                <Field label="Email" htmlFor="dialer-email">
                  <input id="dialer-email" type="email" className="input" autoComplete="off" maxLength={254} value={caller.email} onChange={(event) => set('email', event.target.value)} />
                </Field>
                <Field label="Phone" htmlFor="dialer-phone">
                  <input id="dialer-phone" type="tel" className="input" autoComplete="off" maxLength={40} value={caller.phone} onChange={(event) => set('phone', event.target.value)} />
                </Field>
              </div>
            </fieldset>

            <div className="flex flex-wrap items-center gap-3">
              <Button
                variant="success"
                size="lg"
                icon={PhoneIcon}
                loading={preparing}
                disabled={blocked !== null}
                aria-describedby={blocked ? 'dialer-blocked' : undefined}
                onClick={() => void dialer.start(caller)}
              >
                Call
              </Button>
              {blocked ? (
                <p id="dialer-blocked" className="min-w-0 flex-1 basis-56 text-xs text-amber-800">
                  {blocked}
                </p>
              ) : null}
            </div>
          </>
        )}

        {dialer.problem ? (
          <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-800 ring-1 ring-inset ring-red-200">
            {dialer.problem}
          </p>
        ) : null}
        {dialer.recorded ? (
          <p role="status" className="flex flex-wrap items-center gap-2 rounded-md bg-emerald-50 px-3 py-2 text-sm text-emerald-900 ring-1 ring-inset ring-emerald-200">
            <CheckCircleIcon aria-hidden="true" className="h-5 w-5 shrink-0 text-emerald-600" />
            <span>Your call is being processed; it appears in the Inbox in about a minute.</span>
            <Link to="/" className="font-semibold text-emerald-800 underline">
              Open the Inbox
            </Link>
          </p>
        ) : phase === 'ended' && !dialer.problem ? (
          <p role="status" className="rounded-md bg-neutral-100 px-3 py-2 text-sm text-neutral-700">
            The call ended before it connected, so there is nothing to record.
          </p>
        ) : null}
        {hint ? <p className="rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-900 ring-1 ring-inset ring-amber-200">{hint}</p> : null}

        <p className="flex items-start gap-2 rounded-md bg-sky-50 px-3 py-2 text-xs text-sky-900 ring-1 ring-inset ring-sky-200">
          <InformationCircleIcon aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-sky-600" />
          <span>Use headphones if the seller console is open on this computer, or you will hear an echo.</span>
        </p>
        {line && line.mode !== 'off' && line.configured ? (
          <p className="text-xs text-neutral-500">
            {line.notice_enabled ? 'The line plays a recording notice when it answers, then rings the seller.' : 'The recording notice is switched off on this line.'}
          </p>
        ) : null}
      </CardBody>
    </Card>
  );
}
