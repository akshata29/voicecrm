import { PhoneIcon } from '@heroicons/react/24/outline';
import { usePreflight } from '@/api/hooks';
import type { PreflightReadiness } from '@/api/types';
import { Dialer } from '@/components/phone/Dialer';
import { PhoneLiveCard } from '@/components/phone/PhoneLiveCard';
import { SellerConsole } from '@/components/phone/SellerConsole';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { ErrorState, LoadingBlock } from '@/components/ui/StateViews';
import { formatDurationSeconds } from '@/lib/format';
import { CheckList, ReadinessList } from './CheckList';
import { ScenarioHeading } from './ScenarioHeading';

const LINE_NEEDS: Array<{ what: string; why: string }> = [
  { what: 'An Azure Communication Services resource', why: 'It answers the call and records both sides on separate channels.' },
  { what: 'An Event Grid subscription on that resource', why: 'For the IncomingCall and RecordingFileStatusUpdated events, delivered to the public tunnel URL.' },
  { what: 'A dev tunnel to the ingress port', why: 'So that Azure can call back into this machine.' },
  { what: 'A seller to ring', why: 'A Teams user (Teams interop; the user must be voice enabled) or the seller console below, open in a browser.' },
  { what: 'A Foundry connection for the notice', why: 'So that the line can speak the recording notice when it answers.' },
];

/** Scenario D: a caller dials the VoiceCRM line from this browser; the call is answered, recorded and lands in the Inbox. */
export function PhoneScenario({ verdict }: { verdict: PreflightReadiness | undefined }) {
  const check = usePreflight();

  return (
    <section id="scenario-d" aria-labelledby="scenario-d-title" className="scroll-mt-4 space-y-4">
      <ScenarioHeading
        id="scenario-d-title"
        letter="D"
        title="A phone call, recorded and transcribed"
        description="Dial the VoiceCRM line from this browser (a VoIP call over Azure Communication Services). The service answers, plays the recording notice, rings the seller - a Teams user or the console below - and records both sides on separate channels. When the call ends, the recording is fetched, transcribed and sent through the same pipeline as every other channel."
        verdict={verdict}
      />

      <Card aria-labelledby="phone-setup-title">
        <CardHeader
          id="phone-setup-title"
          icon={PhoneIcon}
          title="Is the phone line ready?"
          subtitle="Tests each step a call depends on, from the Azure resource to the tunnel, with the fix for each failure"
          actions={
            <Button size="sm" variant="primary" icon={PhoneIcon} loading={check.isPending} onClick={() => check.mutate(['phone'])}>
              Check phone setup
            </Button>
          }
        />
        <CardBody className="space-y-4">
          <div aria-live="polite" className="space-y-3">
            {check.isPending ? <LoadingBlock label="Checking the phone line…" className="py-6" /> : null}
            {check.isError ? <ErrorState error={check.error} title="The phone check failed" compact /> : null}
            {check.data ? (
              <>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <ReadinessList items={check.data.readiness} only={['D']} />
                  <Badge tone="neutral">checked in {formatDurationSeconds(check.data.seconds)}</Badge>
                </div>
                <CheckList checks={check.data.checks} />
              </>
            ) : null}
          </div>
          <details className="rounded-lg border border-neutral-200 bg-white">
            <summary className="cursor-pointer select-none px-4 py-2.5 text-sm font-medium text-neutral-700">What the line needs</summary>
            <div className="space-y-3 border-t border-neutral-100 p-4 text-sm text-neutral-700">
              <ol className="space-y-2">
                {LINE_NEEDS.map((step, index) => (
                  <li key={step.what} className="flex gap-3">
                    <span aria-hidden="true" className="tabular mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-brand-50 text-xs font-semibold text-brand-700">
                      {index + 1}
                    </span>
                    <span className="min-w-0">
                      <span className="block font-medium text-neutral-900">{step.what}</span>
                      <span className="block text-xs text-neutral-500">{step.why}</span>
                    </span>
                  </li>
                ))}
              </ol>
              <p className="text-xs text-neutral-500">
                A real phone number needs a Teams Phone service number on a resource account (Teams Phone extensibility); the browser call uses the same recording and pipeline.
              </p>
            </div>
          </details>
        </CardBody>
      </Card>

      <PhoneLiveCard />

      <div className="grid gap-4 lg:grid-cols-2">
        <Dialer />
        <SellerConsole />
      </div>
    </section>
  );
}
