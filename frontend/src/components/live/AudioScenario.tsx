import { MicrophoneIcon, SparklesIcon } from '@heroicons/react/24/outline';
import clsx from 'clsx';
import { useMemo, useState } from 'react';
import { getErrorMessage, ingestApi } from '@/api/client';
import { useConfig, useFixtures, useSimulate } from '@/api/hooks';
import type { FixtureInfo, PreflightReadiness } from '@/api/types';
import { BusinessLineChip, ChannelIcon, LanguageChip, MarketChip } from '@/components/conversation/Badges';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { EmptyState, ErrorState, Skeleton } from '@/components/ui/StateViews';
import { useToast } from '@/components/ui/Toast';
import { formatClock } from '@/lib/format';
import { useLocalStorageState } from '@/lib/hooks';
import { CHANNEL_SHORT, languageLabel, tagLabel } from '@/lib/labels';
import { ConversationRun } from './ConversationRun';
import { ScenarioHeading } from './ScenarioHeading';

interface Run {
  id: string;
  title: string;
}

function CallChoice({ fixture, active, onSelect }: { fixture: FixtureInfo; active: boolean; onSelect: () => void }) {
  return (
    <label
      className={clsx(
        'flex cursor-pointer items-center gap-3 rounded-lg border p-2.5 text-sm transition-colors focus-within:ring-2 focus-within:ring-brand-500',
        active ? 'border-brand-500 bg-brand-50' : 'border-neutral-200 bg-white hover:bg-neutral-50',
      )}
    >
      <input type="radio" name="audio-call" className="sr-only" checked={active} onChange={onSelect} />
      <ChannelIcon channel={fixture.channel} size="sm" />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-medium text-neutral-900">{fixture.title}</span>
        <span className="block text-xs text-neutral-500">
          {formatClock(fixture.recording_seconds ?? 0)} &middot; {CHANNEL_SHORT[fixture.channel]} &middot; {languageLabel(fixture.language)}
        </span>
      </span>
    </label>
  );
}

/** Scenario A: pick a recorded call, play it, and send the audio through the real Speech, Language and model services. */
export function AudioScenario({ verdict }: { verdict: PreflightReadiness | undefined }) {
  const fixtures = useFixtures();
  const config = useConfig();
  const simulate = useSimulate();
  const toast = useToast();
  const [selected, setSelected] = useLocalStorageState<string | null>('voicecrm.live.audio', null);
  const [run, setRun] = useState<Run | null>(null);

  const recorded = useMemo(
    () =>
      (fixtures.data ?? [])
        .filter((item) => (item.recording_seconds ?? 0) > 0)
        .sort((left, right) => Number(right.tags.includes('hero')) - Number(left.tags.includes('hero'))),
    [fixtures.data],
  );
  const fixture = recorded.find((item) => item.fixture_id === selected) ?? recorded[0];
  const speechLive = config.data?.modes.transcription?.live === true;

  const start = () => {
    if (!fixture) return;
    simulate.mutate(
      { fixture_id: fixture.fixture_id, via: 'audio', asr_noise: false },
      {
        onSuccess: (response) => setRun({ id: response.duplicate_of ?? response.conversation_id, title: fixture.title }),
        onError: (error) => toast.error('Could not start the call', { message: getErrorMessage(error) }),
      },
    );
  };

  return (
    <section id="scenario-a" aria-labelledby="scenario-a-title" className="scroll-mt-4 space-y-4">
      <ScenarioHeading
        id="scenario-a-title"
        letter="A"
        title="Play a recorded call and watch Azure process it"
        description="A real speech recording goes to Azure Speech, Azure AI Language removes the sensitive parts, and the model writes the CRM note. After the audio leaves this page nothing is scripted."
        verdict={verdict}
      />
      <Card aria-labelledby="audio-picker-title">
        <CardHeader id="audio-picker-title" icon={MicrophoneIcon} title="Pick a recording" subtitle="Real neural-voice audio of the scripted calls, one voice per speaker" />
        <CardBody>
          {fixtures.isPending ? (
            <div className="grid gap-4 lg:grid-cols-[20rem_1fr]">
              <Skeleton className="h-44" />
              <Skeleton className="h-44" />
            </div>
          ) : fixtures.isError ? (
            <ErrorState error={fixtures.error} title="Could not list the calls" compact onRetry={() => void fixtures.refetch()} />
          ) : !fixture ? (
            <EmptyState
              icon={MicrophoneIcon}
              title="No recordings yet"
              description={
                <>
                  Make them once (about 25 seconds, well under a dollar): run <code className="rounded bg-neutral-100 px-1 py-0.5">scripts\make_audio.bat</code>, then reload this page. They are stored in{' '}
                  <code className="rounded bg-neutral-100 px-1 py-0.5">data\audio\live</code>.
                </>
              }
            />
          ) : (
            <div className="grid gap-5 lg:grid-cols-[minmax(0,20rem)_minmax(0,1fr)]">
              <fieldset className="min-w-0">
                <legend className="section-title mb-2">Recorded calls ({recorded.length})</legend>
                <div className="max-h-72 space-y-1.5 overflow-y-auto pr-1">
                  {recorded.map((item) => (
                    <CallChoice key={item.fixture_id} fixture={item} active={item.fixture_id === fixture.fixture_id} onSelect={() => setSelected(item.fixture_id)} />
                  ))}
                </div>
              </fieldset>

              <div className="min-w-0 space-y-3">
                <div>
                  <h3 className="text-base font-semibold text-neutral-900">{fixture.title}</h3>
                  <p className="mt-0.5 text-sm text-neutral-600">{fixture.description}</p>
                  <div className="mt-2 flex flex-wrap items-center gap-1.5">
                    <MarketChip market={fixture.market} />
                    <BusinessLineChip value={fixture.business_line} />
                    <LanguageChip language={fixture.language} />
                    {fixture.tags.slice(0, 3).map((tag) => (
                      <Badge key={tag} tone="neutral">
                        {tagLabel(tag)}
                      </Badge>
                    ))}
                  </div>
                </div>
                <audio key={fixture.fixture_id} controls preload="none" className="h-10 w-full" src={ingestApi.fixtureAudioUrl(fixture.fixture_id)} aria-label={`Recording of ${fixture.title}`} />
                <div className="flex flex-wrap items-center gap-3">
                  <Button variant="primary" size="lg" icon={SparklesIcon} loading={simulate.isPending} onClick={start}>
                    {speechLive ? 'Process with Azure' : 'Process (mock speech)'}
                  </Button>
                  <p className="max-w-sm text-xs text-neutral-500">Sends this recording through speech-to-text, the privacy filter and the model. About 20 seconds and a few cents at most.</p>
                </div>
                {!speechLive ? (
                  <p className="rounded-md bg-amber-50 p-2.5 text-xs text-amber-900">
                    Speech is a mock in this run, so the audio is not sent to Azure. Set <code>VOICECRM_TRANSCRIPTION_MODE=azure_speech</code> and the Speech endpoint and key (see docs\live-demo.md) for the real thing.
                  </p>
                ) : null}
              </div>
            </div>
          )}
        </CardBody>
      </Card>

      {run ? <ConversationRun key={run.id} id={run.id} via="audio" title={run.title} /> : null}
    </section>
  );
}
