import { PlayIcon, VideoCameraIcon } from '@heroicons/react/24/outline';
import { useMemo, useState } from 'react';
import { getErrorMessage } from '@/api/client';
import { useFixtures, usePreflight, useSimulate } from '@/api/hooks';
import type { PreflightReadiness } from '@/api/types';
import { TeamsHistoryCard } from '@/components/conversation/TeamsHistoryCard';
import { TeamsLiveCard } from '@/components/conversation/TeamsLiveCard';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { Field } from '@/components/ui/Field';
import { ErrorState, LoadingBlock } from '@/components/ui/StateViews';
import { useToast } from '@/components/ui/Toast';
import { formatDurationSeconds } from '@/lib/format';
import { CheckList, ReadinessList } from './CheckList';
import { ConversationRun } from './ConversationRun';
import { ScenarioHeading } from './ScenarioHeading';

const DEFAULT_CALL = 'pb-teams-equity-class-c';

const TENANT_STEPS: Array<{ where: string; what: string; without: string }> = [
  { where: 'Teams admin centre, Meetings, Meeting settings', what: 'Transcript API access: Microsoft Graph access = On', without: '403 GraphAccessToTranscriptsDisabled' },
  { where: 'Same page', what: 'Include speaker attribution = On', without: '403 SpeakerAttributionNotAllowed: no speaker names' },
  { where: 'Entra ID', what: 'An app registration with application permissions OnlineMeetingTranscript.Read.All and OnlineMeetings.Read.All, admin consent, and a client secret', without: '403 or sign-in errors' },
  { where: 'Teams PowerShell', what: 'An application access policy for the app, granted to the seller who organises the meeting (up to 30 minutes to apply)', without: '403 "no application access policy"' },
];

interface Run {
  id: string;
  title: string;
}

/** Scenario B: a real Teams meeting lands in VoiceCRM through Microsoft Graph. Check the setup, watch the ingestion, or rehearse it. */
export function TeamsScenario({ verdict }: { verdict: PreflightReadiness | undefined }) {
  const fixtures = useFixtures();
  const check = usePreflight();
  const simulate = useSimulate();
  const toast = useToast();
  const [chosen, setChosen] = useState<string>(DEFAULT_CALL);
  const [run, setRun] = useState<Run | null>(null);

  const teamsCalls = useMemo(() => (fixtures.data ?? []).filter((fixture) => fixture.channel === 'teams'), [fixtures.data]);
  const call = teamsCalls.find((item) => item.fixture_id === chosen) ?? teamsCalls[0];

  const simulateCall = () => {
    if (!call) return;
    simulate.mutate(
      { fixture_id: call.fixture_id, via: 'transcript', asr_noise: false },
      {
        onSuccess: (response) => setRun({ id: response.duplicate_of ?? response.conversation_id, title: `${call.title} (simulated Teams call)` }),
        onError: (error) => toast.error('Could not start the call', { message: getErrorMessage(error) }),
      },
    );
  };

  return (
    <section id="scenario-b" aria-labelledby="scenario-b-title" className="scroll-mt-4 space-y-4">
      <ScenarioHeading
        id="scenario-b-title"
        letter="B"
        title="A Teams call, recorded and transcribed"
        description="Microsoft Graph publishes each scheduled meeting's transcript some minutes after the meeting ends (it lags the Teams chat) and VoiceCRM picks it up, so nobody uploads anything. Check what the tenant still needs, or rehearse the rest with a simulated call."
        verdict={verdict}
      />

      <Card aria-labelledby="teams-setup-title">
        <CardHeader
          id="teams-setup-title"
          icon={VideoCameraIcon}
          title="Is the tenant ready?"
          subtitle="Signs in to Microsoft Graph as the app and tests each step live ingestion depends on, with the fix for each failure"
          actions={
            <Button size="sm" variant="primary" icon={VideoCameraIcon} loading={check.isPending} onClick={() => check.mutate(['teams'])}>
              Check Teams setup
            </Button>
          }
        />
        <CardBody className="space-y-4">
          <div aria-live="polite" className="space-y-3">
            {check.isPending ? <LoadingBlock label="Signing in to Graph and listing transcripts…" className="py-6" /> : null}
            {check.isError ? <ErrorState error={check.error} title="The Teams check failed" compact /> : null}
            {check.data ? (
              <>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <ReadinessList items={check.data.readiness} only={['B']} />
                  <Badge tone="neutral">checked in {formatDurationSeconds(check.data.seconds)}</Badge>
                </div>
                <CheckList checks={check.data.checks} />
              </>
            ) : null}
          </div>
          <details className="rounded-lg border border-neutral-200 bg-white">
            <summary className="cursor-pointer select-none px-4 py-2.5 text-sm font-medium text-neutral-700">What the tenant needs (allow a day: this is the long pole)</summary>
            <div className="space-y-3 border-t border-neutral-100 p-4 text-sm text-neutral-700">
              <ol className="space-y-2">
                {TENANT_STEPS.map((step, index) => (
                  <li key={step.what} className="flex gap-3">
                    <span aria-hidden="true" className="tabular mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-brand-50 text-xs font-semibold text-brand-700">
                      {index + 1}
                    </span>
                    <span className="min-w-0">
                      <span className="block font-medium text-neutral-900">{step.what}</span>
                      <span className="block text-xs text-neutral-500">
                        {step.where}. Without it: {step.without}
                      </span>
                    </span>
                  </li>
                ))}
              </ol>
              <p className="text-xs text-neutral-500">
                Then set <code>VOICECRM_TEAMS_MODE=poll</code> with the tenant, client id, secret and organiser in <code>.env</code> and restart. The commands are in docs\live-demo.md, scenario B. A meeting has to be <strong>scheduled</strong> (not &ldquo;Meet now&rdquo;) with
                transcription started.
              </p>
            </div>
          </details>
        </CardBody>
      </Card>

      <TeamsLiveCard />
      <TeamsHistoryCard />

      <Card aria-labelledby="teams-sim-title">
        <CardHeader
          id="teams-sim-title"
          icon={PlayIcon}
          title="Rehearse it with a simulated Teams call"
          subtitle="Not a live meeting: a scripted call is rendered as the Teams transcript file (WebVTT) and goes through the same parser and pipeline"
          actions={<Badge tone="warning">Simulated</Badge>}
        />
        <CardBody>
          {fixtures.isPending ? (
            <LoadingBlock label="Loading the calls…" className="py-6" />
          ) : fixtures.isError ? (
            <ErrorState error={fixtures.error} title="Could not list the calls" compact onRetry={() => void fixtures.refetch()} />
          ) : (
            <div className="flex flex-wrap items-end gap-3">
              <Field label="Teams call" htmlFor="teams-sim-call" className="min-w-[16rem] flex-1">
                <select id="teams-sim-call" className="input" value={call?.fixture_id ?? ''} onChange={(event) => setChosen(event.target.value)}>
                  {teamsCalls.map((item) => (
                    <option key={item.fixture_id} value={item.fixture_id}>
                      {item.title}
                    </option>
                  ))}
                </select>
              </Field>
              <Button variant="primary" icon={PlayIcon} loading={simulate.isPending} disabled={!call} onClick={simulateCall}>
                Simulate a Teams call
              </Button>
            </div>
          )}
        </CardBody>
      </Card>

      {run ? <ConversationRun key={run.id} id={run.id} via="transcript" title={run.title} /> : null}
    </section>
  );
}
