import {
  ArrowUpTrayIcon,
  BeakerIcon,
  CheckCircleIcon,
  CpuChipIcon,
  DocumentArrowUpIcon,
  PlayIcon,
  SignalIcon,
  SpeakerWaveIcon,
} from '@heroicons/react/24/outline';
import clsx from 'clsx';
import { useMemo, useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { getErrorMessage, ingestApi } from '@/api/client';
import { useConfig, useConversation, useFixtures, usePolicies, useSimulate, useUpload } from '@/api/hooks';
import { CHANNELS, type Channel, type FixtureInfo, type SimulateVia } from '@/api/types';
import { BusinessLineChip, ChannelIcon, LanguageChip, MarketChip, StatusBadge } from '@/components/conversation/Badges';
import { PipelineStepper } from '@/components/conversation/PipelineViews';
import { TeamsLiveCard } from '@/components/conversation/TeamsLiveCard';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { Field, PageHeader } from '@/components/ui/Field';
import { EmptyState, ErrorState, ListSkeleton } from '@/components/ui/StateViews';
import { Toggle } from '@/components/ui/Toggle';
import type { Tone } from '@/components/ui/types';
import { useToast } from '@/components/ui/Toast';
import { formatClock, formatRelative, humanize } from '@/lib/format';
import { useDocumentTitle, useLocalStorageState, useNow } from '@/lib/hooks';
import { businessLineLabel, CALL_TYPE_LABELS, CHANNEL_LABELS, CHANNEL_SHORT, tagLabel } from '@/lib/labels';

interface SourceInfo {
  channel: Channel;
  how: string;
  endpoint: string;
  points: string[];
}

const SOURCES: SourceInfo[] = [
  {
    channel: 'teams',
    how: 'Microsoft Graph change-notification subscription',
    endpoint: 'POST /api/v1/webhooks/teams',
    points: [
      'Subscribe to call transcript and recording notifications before the meeting starts',
      'The endpoint answers Graph validation tokens',
      'Transcript from Graph when available; otherwise the recording is transcribed centrally',
    ],
  },
  {
    channel: 'zoom',
    how: 'Webhook: recording.completed (with endpoint.url_validation)',
    endpoint: 'POST /api/v1/webhooks/zoom',
    points: ['Audio-first: the cloud-recording audio is transcribed centrally', "Zoom's own transcript is used only if it already exists", 'Meetings and Zoom Phone share the same ingest contract'],
  },
  {
    channel: 'genesys',
    how: 'Notification subscription (speech and text analytics)',
    endpoint: 'POST /api/v1/webhooks/genesys',
    points: ['The transcript URL expires after 10 minutes, so it is fetched and persisted at once', 'Platform transcript is free only within the contract allowance: audio is the fallback', 'Inbound calls land here'],
  },
  {
    channel: 'mobile',
    how: 'App upload from the installable web app',
    endpoint: 'POST /api/v1/ingest/upload',
    points: ['Record in person, even offline; uploads when the phone reconnects', 'Audio goes through the same transcription, policy and review', 'Retries are idempotent (source_interaction_id)'],
  },
];

const SOURCE_LABEL: Record<string, string> = { platform: 'Platform transcript', azure_speech: 'Azure Speech (audio)' };

const TAG_TONES: Record<string, Tone> = {
  hero: 'brand',
  class_trap: 'orange',
  ambiguous_product: 'warning',
  top3: 'warning',
  near_identical_names: 'orange',
  privacy: 'danger',
  small_talk: 'neutral',
  relationship: 'success',
  spanish: 'teal',
  mobile: 'neutral',
  offline: 'neutral',
  market_policy: 'violet',
  crm_ambiguous: 'warning',
};

function SourceCard({ source }: { source: SourceInfo }) {
  const config = useConfig();
  const policies = usePolicies();
  const transcription = config.data?.modes.transcription;
  const order = policies.data?.processing.transcription_source_preference[source.channel] ?? [];
  return (
    <Card className="flex flex-col" aria-labelledby={`source-${source.channel}`}>
      <div className="flex items-start gap-3 border-b border-neutral-100 px-4 py-3 sm:px-5">
        <ChannelIcon channel={source.channel} size="lg" />
        <div className="min-w-0">
          <h3 id={`source-${source.channel}`} className="text-base font-semibold text-neutral-900">
            {CHANNEL_LABELS[source.channel]}
          </h3>
          <p className="text-sm text-neutral-600">{source.how}</p>
        </div>
      </div>
      <CardBody className="flex flex-1 flex-col gap-3">
        <code className="block overflow-x-auto rounded-md bg-neutral-900 px-3 py-2 text-xs text-emerald-300">{source.endpoint}</code>
        <ul className="list-disc space-y-1 pl-5 text-sm text-neutral-700 marker:text-neutral-400">
          {source.points.map((point) => (
            <li key={point}>{point}</li>
          ))}
        </ul>
        <div className="mt-auto space-y-2 border-t border-neutral-100 pt-3">
          <div className="flex flex-wrap items-center gap-1.5 text-xs">
            <span className="font-semibold text-neutral-500">Mode</span>
            <Badge tone="neutral" icon={BeakerIcon} title="Fixtures are rendered as this channel's native payload and ingested through the real adapter path">
              Simulated payloads
            </Badge>
            {transcription ? (
              <Badge tone={transcription.live ? 'success' : 'neutral'} icon={SpeakerWaveIcon} title={transcription.reason ?? undefined}>
                {transcription.live ? 'Live: Azure Speech' : 'Mock transcriber'}
              </Badge>
            ) : null}
          </div>
          {order.length > 0 ? (
            <p className="flex flex-wrap items-center gap-1.5 text-xs text-neutral-600">
              <span className="font-semibold text-neutral-500">Transcript order</span>
              {order.map((item, index) => (
                <span key={item} className="inline-flex items-center gap-1">
                  {index > 0 ? <span aria-hidden="true">→</span> : null}
                  <Badge tone={index === 0 ? 'brand' : 'neutral'}>{SOURCE_LABEL[item] ?? humanize(item)}</Badge>
                </span>
              ))}
            </p>
          ) : null}
          {source.channel === 'mobile' ? (
            <Link to="/capture" className="btn btn-secondary btn-sm">
              Open Capture
            </Link>
          ) : null}
        </div>
      </CardBody>
    </Card>
  );
}

interface RunRecord {
  id: string;
  title: string;
  via: SimulateVia;
  at: number;
  note?: string;
}

function RecentRun({ run }: { run: RunRecord }) {
  const detail = useConversation(run.id);
  const now = useNow(15_000);
  const status = detail.data?.record.status;
  const live = status === 'received' || status === 'processing';
  return (
    <li className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 sm:px-5">
      <div className="min-w-0 flex-1 basis-60">
        <p className="truncate text-sm font-medium text-neutral-900">{run.title}</p>
        <p className="text-xs text-neutral-500">
          via {run.via} · {formatRelative(run.at, now)}
          {run.note ? ` · ${run.note}` : ''}
        </p>
      </div>
      <div className="w-44 shrink-0">{live ? <PipelineStepper stages={detail.data?.stages ?? []} /> : status ? <StatusBadge status={status} /> : <span className="text-xs text-neutral-400">Loading…</span>}</div>
      <Link to={`/conversations/${run.id}`} className={clsx('btn btn-sm', status === 'needs_review' ? 'btn-primary' : 'btn-secondary')}>
        {status === 'needs_review' ? 'Review' : 'Open'}
      </Link>
    </li>
  );
}

function FixtureCard({ fixture, onRun, busy }: { fixture: FixtureInfo; onRun: (fixture: FixtureInfo, via: SimulateVia) => void; busy: SimulateVia | null }) {
  const recorded = typeof fixture.recording_seconds === 'number' && fixture.recording_seconds > 0;
  return (
    <li className="flex flex-col rounded-lg border border-neutral-200 bg-white p-4">
      <div className="flex items-start justify-between gap-2">
        <h4 className="text-sm font-semibold leading-5 text-neutral-900">{fixture.title}</h4>
        <span className="shrink-0 text-xs text-neutral-400" title={recorded ? 'Length of the real speech recording' : 'Scripted length'}>
          {recorded ? `${formatClock(fixture.recording_seconds ?? 0)} recorded` : `${fixture.duration_minutes} min`} · {fixture.turns} turns
        </span>
      </div>
      <p className="mt-1 text-sm text-neutral-600">{fixture.description}</p>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <MarketChip market={fixture.market} />
        <BusinessLineChip value={fixture.business_line} />
        <LanguageChip language={fixture.language} />
        {fixture.call_type ? <Badge tone="neutral">{CALL_TYPE_LABELS[fixture.call_type] ?? humanize(fixture.call_type)}</Badge> : null}
        {recorded ? <Badge tone="success">Real speech</Badge> : null}
      </div>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {fixture.tags.map((tag) => (
          <Badge key={tag} tone={TAG_TONES[tag] ?? 'neutral'}>
            {tagLabel(tag)}
          </Badge>
        ))}
      </div>
      {recorded ? (
        <audio controls preload="none" className="mt-3 h-9 w-full" src={ingestApi.fixtureAudioUrl(fixture.fixture_id)} aria-label={`Recording of ${fixture.title}`} />
      ) : null}
      <div className="mt-auto flex flex-wrap gap-2 pt-3">
        <Button variant="primary" size="sm" icon={PlayIcon} loading={busy === 'transcript'} disabled={busy !== null} onClick={() => onRun(fixture, 'transcript')}>
          Run via transcript
        </Button>
        <Button
          variant="secondary"
          size="sm"
          icon={SpeakerWaveIcon}
          loading={busy === 'audio'}
          disabled={busy !== null}
          onClick={() => onRun(fixture, 'audio')}
          title={recorded ? 'Sends the real recording to speech-to-text' : 'No recording for this call: a placeholder is sent'}
        >
          Run via audio
        </Button>
      </div>
    </li>
  );
}

function SimulateGallery() {
  const fixtures = useFixtures();
  const config = useConfig();
  const simulate = useSimulate();
  const toast = useToast();
  const [asrNoise, setAsrNoise] = useState(false);
  const [market, setMarket] = useState('');
  const [businessLine, setBusinessLine] = useState('');
  const [templateId, setTemplateId] = useState('');
  const [busy, setBusy] = useState<{ id: string; via: SimulateVia } | null>(null);
  const [runs, setRuns] = useState<RunRecord[]>([]);

  const grouped = useMemo(() => {
    const result = new Map<Channel, FixtureInfo[]>();
    for (const channel of CHANNELS) result.set(channel, []);
    for (const fixture of fixtures.data ?? []) result.get(fixture.channel)?.push(fixture);
    return [...result.entries()].filter(([, items]) => items.length > 0);
  }, [fixtures.data]);

  const run = (fixture: FixtureInfo, via: SimulateVia) => {
    setBusy({ id: fixture.fixture_id, via });
    simulate.mutate(
      {
        fixture_id: fixture.fixture_id,
        via,
        asr_noise: asrNoise,
        market: market || null,
        business_line: businessLine || null,
        template_id: templateId || null,
      },
      {
        onSuccess: (response) => {
          setRuns((current) => [
            {
              id: response.duplicate_of ?? response.conversation_id,
              title: fixture.title,
              via,
              at: Date.now(),
              note: response.duplicate_of ? 'duplicate of an earlier run' : asrNoise ? 'ASR noise on' : undefined,
            },
            ...current,
          ].slice(0, 5));
          toast.success(response.duplicate_of ? 'Already ingested' : 'Call ingested', { message: `${fixture.title} (via ${via})`, action: { label: 'Open the review', to: `/conversations/${response.duplicate_of ?? response.conversation_id}` } });
        },
        onError: (error) => toast.error('Could not run the call', { message: getErrorMessage(error) }),
        onSettled: () => setBusy(null),
      },
    );
  };

  return (
    <section aria-labelledby="simulate-title" className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 id="simulate-title" className="text-lg font-semibold text-neutral-900">
            Simulate a call
          </h2>
          <p className="max-w-3xl text-sm text-neutral-600">
            Each scripted call is rendered as the channel's native payload (Teams VTT, Zoom VTT/JSON, Genesys JSON, or an audio sidecar) and ingested through the real adapter path.
          </p>
        </div>
        <Toggle label="ASR noise" description='Mishears the share class ("class see")' checked={asrNoise} onChange={setAsrNoise} className="max-w-xs items-center gap-4 rounded-lg border border-neutral-200 bg-white px-3 py-2" />
      </div>

      <details className="rounded-lg border border-neutral-200 bg-white">
        <summary className="cursor-pointer select-none px-4 py-2.5 text-sm font-medium text-neutral-700">Overrides (optional): market, business line, template</summary>
        <div className="grid gap-3 border-t border-neutral-100 p-4 sm:grid-cols-3">
          <Field label="Market" htmlFor="sim-market" hint="Flip US / UK / EU to see the retention policy decide.">
            <select id="sim-market" className="input" value={market} onChange={(event) => setMarket(event.target.value)}>
              <option value="">As scripted</option>
              {(config.data?.markets ?? []).map((item) => (
                <option key={item.market} value={item.market}>
                  {item.display_name} ({item.market})
                </option>
              ))}
            </select>
          </Field>
          <Field label="Business line" htmlFor="sim-bl">
            <select id="sim-bl" className="input" value={businessLine} onChange={(event) => setBusinessLine(event.target.value)}>
              <option value="">As scripted</option>
              {(config.data?.business_lines ?? []).map((item) => (
                <option key={item} value={item}>
                  {businessLineLabel(item)}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Summary template" htmlFor="sim-template">
            <select id="sim-template" className="input" value={templateId} onChange={(event) => setTemplateId(event.target.value)}>
              <option value="">Chosen by business line</option>
              {(config.data?.templates ?? []).map((item) => (
                <option key={item.template_id} value={item.template_id}>
                  {item.name}
                </option>
              ))}
            </select>
          </Field>
        </div>
      </details>

      {runs.length > 0 ? (
        <Card aria-labelledby="recent-runs-title">
          <CardHeader id="recent-runs-title" title="Just ingested" subtitle="Live pipeline progress; open the review when it is ready" icon={SignalIcon} />
          <ul className="divide-y divide-neutral-100">
            {runs.map((item) => (
              <RecentRun key={`${item.id}-${item.at}`} run={item} />
            ))}
          </ul>
        </Card>
      ) : null}

      {fixtures.isPending ? <ListSkeleton rows={3} /> : null}
      {fixtures.isError ? <ErrorState error={fixtures.error} title="Could not load the scripted calls" onRetry={() => void fixtures.refetch()} /> : null}
      {fixtures.data && fixtures.data.length === 0 ? (
        <div className="card">
          <EmptyState title="No scripted calls available" description="The backend returned an empty fixtures list." />
        </div>
      ) : null}

      <div className="space-y-6">
        {grouped.map(([channel, items]) => (
          <div key={channel}>
            <h3 className="mb-2 flex items-center gap-2 text-sm font-semibold text-neutral-800">
              <ChannelIcon channel={channel} size="sm" />
              {CHANNEL_LABELS[channel]}
              <span className="tabular rounded-full bg-neutral-200 px-1.5 text-xs text-neutral-700">{items.length}</span>
            </h3>
            <ul className="grid gap-3 md:grid-cols-2 2xl:grid-cols-3">
              {items.map((fixture) => (
                <FixtureCard key={fixture.fixture_id} fixture={fixture} onRun={run} busy={busy?.id === fixture.fixture_id ? busy.via : null} />
              ))}
            </ul>
          </div>
        ))}
      </div>
    </section>
  );
}

interface UploadValues {
  channel: Channel;
  market: string;
  business_line: string;
  call_type: string;
  seller_name: string;
  seller_email: string;
  customer_name: string;
  customer_org: string;
  customer_email: string;
  language_hint: string;
  title: string;
  started_at: string;
  source_interaction_id: string;
  fixture_id: string;
}

const DEFAULT_UPLOAD: UploadValues = {
  channel: 'upload',
  market: 'US',
  business_line: 'professional_buyer',
  call_type: '',
  seller_name: 'Demo Seller',
  seller_email: 'demo.seller@contoso.example',
  customer_name: '',
  customer_org: '',
  customer_email: '',
  language_hint: '',
  title: '',
  started_at: '',
  source_interaction_id: '',
  fixture_id: '',
};

function UploadForm() {
  const config = useConfig();
  const fixtures = useFixtures();
  const upload = useUpload();
  const toast = useToast();
  const [values, setValues] = useLocalStorageState<UploadValues>('voicecrm.sources.upload', DEFAULT_UPLOAD);
  const [file, setFile] = useState<File | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [result, setResult] = useState<{ id: string; duplicate: boolean } | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  const set = <K extends keyof UploadValues>(key: K, value: UploadValues[K]) => setValues((current) => ({ ...current, [key]: value }));

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setFormError(null);
    setResult(null);
    if (!file) {
      setFormError('Choose an audio file (.wav, .mp3, .m4a, .ogg, .webm) or a transcript (.vtt, .json).');
      return;
    }
    const form = new FormData();
    form.append('file', file, file.name);
    const required: Array<[string, string]> = [
      ['channel', values.channel],
      ['market', values.market],
      ['business_line', values.business_line],
      ['seller_name', values.seller_name],
      ['seller_email', values.seller_email],
    ];
    for (const [key, value] of required) form.append(key, value);
    const optional: Array<[string, string]> = [
      ['call_type', values.call_type],
      ['customer_name', values.customer_name],
      ['customer_org', values.customer_org],
      ['customer_email', values.customer_email],
      ['language_hint', values.language_hint],
      ['title', values.title],
      ['started_at', values.started_at ? new Date(values.started_at).toISOString() : ''],
      ['source_interaction_id', values.source_interaction_id],
      ['fixture_id', values.fixture_id],
    ];
    for (const [key, value] of optional) if (value.trim()) form.append(key, value.trim());

    setProgress(0);
    upload.mutate(
      { form, onProgress: setProgress },
      {
        onSuccess: (response) => {
          const id = response.duplicate_of ?? response.conversation_id;
          setResult({ id, duplicate: response.duplicate_of !== null });
          toast.success(response.duplicate_of ? 'Already ingested' : 'Upload accepted', { action: { label: 'Open the review', to: `/conversations/${id}` } });
        },
        onError: (error) => setFormError(getErrorMessage(error)),
        onSettled: () => setProgress(null),
      },
    );
  };

  return (
    <Card aria-labelledby="upload-title">
      <CardHeader id="upload-title" title="Upload a recording or transcript" subtitle="POST /ingest/upload (multipart)" icon={DocumentArrowUpIcon} />
      <form onSubmit={submit} noValidate>
        <CardBody className="space-y-4">
          <Field label="File" htmlFor="up-file" required hint="Audio: .wav .mp3 .m4a .ogg .webm. Transcript: .vtt or .json.">
            <input
              id="up-file"
              type="file"
              accept=".wav,.mp3,.m4a,.ogg,.webm,.vtt,.json,audio/*"
              className="input file:mr-3 file:rounded-md file:border-0 file:bg-brand-50 file:px-3 file:py-1 file:text-sm file:font-semibold file:text-brand-700"
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
            />
          </Field>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Channel" htmlFor="up-channel" required>
              <select id="up-channel" className="input" value={values.channel} onChange={(event) => set('channel', event.target.value as Channel)}>
                {CHANNELS.map((channel) => (
                  <option key={channel} value={channel}>
                    {CHANNEL_SHORT[channel]}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Market" htmlFor="up-market" required>
              <select id="up-market" className="input" value={values.market} onChange={(event) => set('market', event.target.value)}>
                {(config.data?.markets ?? [{ market: 'US', display_name: 'United States' }]).map((item) => (
                  <option key={item.market} value={item.market}>
                    {item.display_name} ({item.market})
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Business line" htmlFor="up-bl" required>
              <select id="up-bl" className="input" value={values.business_line} onChange={(event) => set('business_line', event.target.value)}>
                {(config.data?.business_lines ?? ['professional_buyer', 'retail', 'international']).map((line) => (
                  <option key={line} value={line}>
                    {businessLineLabel(line)}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Call type" htmlFor="up-calltype">
              <select id="up-calltype" className="input" value={values.call_type} onChange={(event) => set('call_type', event.target.value)}>
                <option value="">Not specified</option>
                {Object.entries(CALL_TYPE_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Seller name" htmlFor="up-seller" required>
              <input id="up-seller" className="input" value={values.seller_name} onChange={(event) => set('seller_name', event.target.value)} required />
            </Field>
            <Field label="Seller email" htmlFor="up-seller-email" required>
              <input id="up-seller-email" type="email" className="input" value={values.seller_email} onChange={(event) => set('seller_email', event.target.value)} required />
            </Field>
            <Field label="Customer name" htmlFor="up-cust">
              <input id="up-cust" className="input" value={values.customer_name} onChange={(event) => set('customer_name', event.target.value)} />
            </Field>
            <Field label="Customer organisation" htmlFor="up-org">
              <input id="up-org" className="input" value={values.customer_org} onChange={(event) => set('customer_org', event.target.value)} />
            </Field>
            <Field label="Customer email" htmlFor="up-cust-email">
              <input id="up-cust-email" type="email" className="input" value={values.customer_email} onChange={(event) => set('customer_email', event.target.value)} />
            </Field>
            <Field label="Language hint" htmlFor="up-lang">
              <select id="up-lang" className="input" value={values.language_hint} onChange={(event) => set('language_hint', event.target.value)}>
                <option value="">Auto-detect</option>
                <option value="en-US">English (en-US)</option>
                <option value="es-US">Spanish (es-US)</option>
              </select>
            </Field>
          </div>
          <details className="rounded-md border border-neutral-200 bg-neutral-50 text-sm">
            <summary className="cursor-pointer select-none px-3 py-2 font-medium text-neutral-700">Advanced</summary>
            <div className="grid gap-3 border-t border-neutral-200 p-3 sm:grid-cols-2">
              <Field label="Title" htmlFor="up-title">
                <input id="up-title" className="input" value={values.title} onChange={(event) => set('title', event.target.value)} />
              </Field>
              <Field label="Call started at" htmlFor="up-started">
                <input id="up-started" type="datetime-local" className="input" value={values.started_at} onChange={(event) => set('started_at', event.target.value)} />
              </Field>
              <Field label="Source interaction id" htmlFor="up-source" hint="Same id twice = the second upload is recognised as a duplicate.">
                <input id="up-source" className="input" value={values.source_interaction_id} onChange={(event) => set('source_interaction_id', event.target.value)} />
              </Field>
              <Field label="Scripted call (fixture)" htmlFor="up-fixture" hint="Pairs the audio with a scripted call for the mock transcriber.">
                <select id="up-fixture" className="input" value={values.fixture_id} onChange={(event) => set('fixture_id', event.target.value)}>
                  <option value="">None</option>
                  {(fixtures.data ?? []).map((fixture) => (
                    <option key={fixture.fixture_id} value={fixture.fixture_id}>
                      {fixture.title}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
          </details>

          {formError ? (
            <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-800 ring-1 ring-inset ring-red-200">
              {formError}
            </p>
          ) : null}
          {result ? (
            <p role="status" className="flex flex-wrap items-center gap-2 rounded-md bg-emerald-50 px-3 py-2 text-sm text-emerald-900 ring-1 ring-inset ring-emerald-200">
              <CheckCircleIcon aria-hidden="true" className="h-5 w-5 text-emerald-600" />
              {result.duplicate ? 'This recording was already ingested.' : 'Accepted: the pipeline is running.'}
              <Link to={`/conversations/${result.id}`} className="font-semibold text-emerald-800 underline">
                Open the review
              </Link>
            </p>
          ) : null}
          {progress !== null ? (
            <div role="progressbar" aria-label="Upload progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress} className="h-2 overflow-hidden rounded-full bg-neutral-200">
              <div className="h-full bg-brand-500 transition-all" style={{ width: `${Math.max(progress, 8)}%` }} />
            </div>
          ) : null}
          <div className="flex flex-wrap gap-2">
            <Button type="submit" variant="primary" icon={ArrowUpTrayIcon} loading={upload.isPending}>
              Upload and process
            </Button>
            <Button type="button" variant="ghost" onClick={() => setValues(DEFAULT_UPLOAD)} disabled={upload.isPending}>
              Reset form
            </Button>
          </div>
        </CardBody>
      </form>
    </Card>
  );
}

export default function SourcesPage() {
  useDocumentTitle('Sources');
  return (
    <div className="space-y-8">
      <PageHeader
        title="Sources"
        icon={SignalIcon}
        description="Teams, Zoom, Genesys and in-person recordings all converge on one Microsoft-controlled pipeline: the same transcription, privacy policy, extraction and review, whatever the channel."
      />
      <section aria-labelledby="channels-title" className="space-y-3">
        <h2 id="channels-title" className="flex items-center gap-2 text-lg font-semibold text-neutral-900">
          <CpuChipIcon aria-hidden="true" className="h-5 w-5 text-neutral-400" /> How each channel connects
        </h2>
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          {SOURCES.map((source) => (
            <SourceCard key={source.channel} source={source} />
          ))}
        </div>
      </section>
      <TeamsLiveCard />
      <SimulateGallery />
      <UploadForm />
    </div>
  );
}
