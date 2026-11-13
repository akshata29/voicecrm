import { InformationCircleIcon, PhoneArrowDownLeftIcon, PhoneIcon, PhoneXMarkIcon } from '@heroicons/react/24/outline';
import { USE_MOCK } from '@/api/client';
import { usePhoneStatus } from '@/api/hooks';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { Field } from '@/components/ui/Field';
import { Spinner } from '@/components/ui/Spinner';
import { Toggle } from '@/components/ui/Toggle';
import type { Tone } from '@/components/ui/types';
import { formatDurationSeconds } from '@/lib/format';
import { useLocalStorageState } from '@/lib/hooks';
import { CallButtons, CallClock } from './CallControls';
import { callingUnsupported, useSellerConsole, type ConsolePhase } from './useAcsCalling';

interface ConsoleSettings {
  enabled: boolean;
  name: string;
}

const DEFAULT_SETTINGS: ConsoleSettings = { enabled: false, name: 'Aisha West' };

const BADGE: Record<ConsolePhase, { label: string; tone: Tone; live?: boolean }> = {
  off: { label: 'Off', tone: 'neutral' },
  starting: { label: 'Starting', tone: 'info' },
  waiting: { label: 'Online', tone: 'success', live: true },
  ringing: { label: 'Ringing', tone: 'warning', live: true },
  in_call: { label: 'In call', tone: 'success', live: true },
};

/** Scenario D, the seller: takes the call in this browser, so no Teams client is needed to see the whole flow. */
export function SellerConsole() {
  const status = usePhoneStatus();
  const [stored, setSettings] = useLocalStorageState<ConsoleSettings>('voicecrm.phone.seller', DEFAULT_SETTINGS);
  const settings: ConsoleSettings = { ...DEFAULT_SETTINGS, ...stored };
  const unsupported = callingUnsupported();
  const line = status.data;
  const available = line !== undefined && line.mode !== 'off' && line.configured;
  // A console that cannot start switches itself off, so it does not try again on every page load.
  const seller = useSellerConsole(settings.enabled && unsupported === null, settings.name, () => setSettings((current) => ({ ...DEFAULT_SETTINGS, ...current, enabled: false })));
  const badge = BADGE[seller.phase];

  return (
    <Card aria-labelledby="console-title">
      <CardHeader
        id="console-title"
        icon={PhoneArrowDownLeftIcon}
        title="Seller console"
        subtitle="You are the seller: take the call in this browser instead of Teams"
        actions={
          <>
            {USE_MOCK ? <Badge tone="warning">Simulated</Badge> : null}
            <Badge tone={badge.tone} dot={badge.live} pulse={badge.live}>
              {badge.label}
            </Badge>
          </>
        }
      />
      <CardBody className="space-y-4">
        <Toggle
          label="Answer calls in this browser"
          description="Off by default. While it is on, the line rings this browser."
          checked={settings.enabled}
          disabled={!settings.enabled && (!available || unsupported !== null)}
          onChange={(enabled) => setSettings({ ...settings, enabled })}
        />
        <Field label="Display name" htmlFor="console-name" hint={settings.enabled ? 'Turn the console off to change the name.' : 'The name the line shows for the seller.'}>
          <input id="console-name" className="input" autoComplete="off" maxLength={120} value={settings.name} disabled={settings.enabled} onChange={(event) => setSettings({ ...settings, name: event.target.value })} />
        </Field>

        {!settings.enabled && !available ? (
          <p className="text-xs text-amber-800">{line ? (line.problem ?? 'The phone line is not configured.') : status.isError ? 'Could not read the state of the phone line.' : 'Checking the phone line…'}</p>
        ) : null}
        {unsupported ? <p className="text-xs text-amber-800">{unsupported}</p> : null}
        {line?.seller.mode === 'teams' ? (
          <p className="flex items-start gap-2 rounded-md bg-sky-50 px-3 py-2 text-xs text-sky-900 ring-1 ring-inset ring-sky-200">
            <InformationCircleIcon aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-sky-600" />
            <span>Calls ring the Teams user{line.seller.name ? ` ${line.seller.name}` : ''}. The console is only a fallback.</span>
          </p>
        ) : null}

        <div aria-live="polite" className="space-y-3">
          {seller.problem ? (
            <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-800 ring-1 ring-inset ring-red-200">
              The console could not start and is switched off. {seller.problem}
            </p>
          ) : null}
          {seller.trouble ? <p className="rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-900 ring-1 ring-inset ring-amber-200">{seller.trouble}</p> : null}

          {seller.phase === 'starting' ? (
            <p className="flex items-center gap-2 text-sm text-neutral-600">
              <Spinner className="h-4 w-4 text-brand-500" />
              Connecting the console…
            </p>
          ) : null}

          {seller.phase === 'waiting' ? (
            <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3">
              <p className="text-sm font-semibold text-emerald-900">Waiting for calls…</p>
              <p className="text-xs text-emerald-800">Registered as the seller. When a customer calls the line, this browser rings.</p>
            </div>
          ) : null}

          {seller.phase === 'ringing' && seller.ring ? (
            <div role="alert" className="rounded-lg border border-amber-300 bg-amber-50 p-4">
              <p className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-amber-900">
                <PhoneIcon aria-hidden="true" className="h-4 w-4 animate-pulse-soft" />
                Incoming call
              </p>
              <p className="mt-1 break-words text-lg font-semibold text-neutral-900">{seller.ring.callerName}</p>
              <div className="mt-3 flex flex-wrap gap-2">
                <Button variant="success" size="lg" icon={PhoneIcon} onClick={() => void seller.accept()}>
                  Accept
                </Button>
                <Button variant="danger-outline" size="lg" icon={PhoneXMarkIcon} onClick={() => void seller.decline()}>
                  Decline
                </Button>
              </div>
            </div>
          ) : null}

          {seller.phase === 'in_call' && seller.call ? (
            <div className="space-y-3 rounded-lg bg-neutral-900 px-4 py-4 text-center text-white">
              <p className="text-xs font-medium uppercase tracking-wide text-neutral-400">In call with</p>
              <p className="break-words text-xl font-semibold">{seller.callerName ?? 'the customer'}</p>
              <p className="text-base">
                {seller.call.phase === 'connected' ? <CallClock since={seller.call.connectedAt} className="text-emerald-300" /> : <span className="animate-pulse-soft text-neutral-200">Connecting…</span>}
              </p>
              <CallButtons muted={seller.call.muted} onToggleMute={seller.toggleMute} onHangUp={seller.hangUp} />
            </div>
          ) : null}

          {seller.outcome && (seller.phase === 'waiting' || seller.phase === 'ringing') ? (
            <p role="status" className={seller.outcome.message ? 'rounded-md bg-red-50 px-3 py-2 text-sm text-red-800 ring-1 ring-inset ring-red-200' : 'text-xs text-neutral-500'}>
              {seller.outcome.message ?? `The last call ended after ${formatDurationSeconds(seller.outcome.seconds)}.`}
            </p>
          ) : null}
        </div>

        <p className="text-xs text-neutral-500">
          Set <code className="rounded bg-neutral-100 px-1 py-0.5">VOICECRM_PHONE_SELLER</code> to a Teams user to ring Teams instead; the console is then only a fallback.
        </p>
      </CardBody>
    </Card>
  );
}
