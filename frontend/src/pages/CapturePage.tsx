import {
  ArrowDownTrayIcon,
  ArrowPathIcon,
  CheckCircleIcon,
  CloudArrowUpIcon,
  ExclamationTriangleIcon,
  MicrophoneIcon,
  PauseIcon,
  PlayIcon,
  SignalSlashIcon,
  StopIcon,
  TrashIcon,
  WifiIcon,
} from '@heroicons/react/24/solid';
import { FolderOpenIcon } from '@heroicons/react/24/outline';
import clsx from 'clsx';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useConfig } from '@/api/hooks';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { Field, PageHeader } from '@/components/ui/Field';
import { EmptyState } from '@/components/ui/StateViews';
import { Spinner } from '@/components/ui/Spinner';
import { Toggle } from '@/components/ui/Toggle';
import { useToast } from '@/components/ui/Toast';
import { formatBytes, formatClock, formatDurationSeconds, formatRelative } from '@/lib/format';
import { useDocumentTitle, useLocalStorageState, useNow, useOnlineStatus } from '@/lib/hooks';
import { businessLineLabel } from '@/lib/labels';
import { addRecording, extensionFor, newRecordingId, type CaptureMetadata, type QueuedRecording } from '@/offline/queue';
import { recorderSupported, useRecorder } from '@/offline/useRecorder';
import { useRecordingQueue } from '@/offline/useRecordingQueue';

const DEFAULT_METADATA: CaptureMetadata = {
  customer_org: '',
  customer_name: '',
  market: 'US',
  business_line: 'professional_buyer',
  language_hint: '',
  seller_name: 'Demo Seller',
  seller_email: 'demo.seller@contoso.example',
};

const LANGUAGES = [
  { value: '', label: 'Auto-detect' },
  { value: 'en-US', label: 'English' },
  { value: 'es-US', label: 'Spanish' },
];

function LevelMeter({ level, active }: { level: number; active: boolean }) {
  const bars = 24;
  return (
    <div className="flex h-8 items-end gap-0.5" aria-hidden="true">
      {Array.from({ length: bars }, (_, index) => {
        const lit = active && level * bars * 1.2 > index;
        return <span key={index} className={clsx('w-1.5 rounded-sm transition-all', lit ? 'bg-emerald-500' : 'bg-neutral-200')} style={{ height: `${25 + (index % 6) * 12}%` }} />;
      })}
    </div>
  );
}

export default function CapturePage() {
  useDocumentTitle('Capture');
  const toast = useToast();
  const config = useConfig();
  const browserOnline = useOnlineStatus();
  const [simulateOffline, setSimulateOffline] = useLocalStorageState('voicecrm.capture.simulateOffline', false);
  const online = browserOnline && !simulateOffline;
  const [metadata, setMetadata] = useLocalStorageState<CaptureMetadata>('voicecrm.capture.metadata', DEFAULT_METADATA);
  const recorder = useRecorder();
  const queue = useRecordingQueue(online);
  const now = useNow(15_000);
  const fileInput = useRef<HTMLInputElement>(null);
  const [saving, setSaving] = useState(false);
  const [installEvent, setInstallEvent] = useState<BeforeInstallPromptEvent | null>(null);

  useEffect(() => {
    const onPrompt = (event: BeforeInstallPromptEvent) => {
      event.preventDefault();
      setInstallEvent(event);
    };
    window.addEventListener('beforeinstallprompt', onPrompt);
    return () => window.removeEventListener('beforeinstallprompt', onPrompt);
  }, []);

  const markets = config.data?.markets ?? [];
  const businessLines = config.data?.business_lines ?? ['professional_buyer', 'retail', 'international'];
  const set = <K extends keyof CaptureMetadata>(key: K, value: CaptureMetadata[K]) => setMetadata((current) => ({ ...current, [key]: value }));

  const previewUrl = useMemo(() => (recorder.result ? URL.createObjectURL(recorder.result.blob) : null), [recorder.result]);
  useEffect(() => {
    return () => {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
    };
  }, [previewUrl]);

  const saveRecording = async () => {
    const result = recorder.result;
    if (!result) return;
    setSaving(true);
    try {
      const stamp = new Date(result.startedAt).toISOString().replace(/[:.]/g, '-').slice(0, 19);
      const recording: QueuedRecording = {
        id: newRecordingId(),
        created_at: new Date().toISOString(),
        started_at: result.startedAt,
        duration_seconds: result.durationSeconds,
        mime_type: result.mimeType,
        filename: `visit-${stamp}.${extensionFor(result.mimeType)}`,
        size_bytes: result.blob.size,
        blob: result.blob,
        metadata: { ...metadata },
        status: 'queued',
        attempts: 0,
        error: null,
        conversation_id: null,
        duplicate_of: null,
        uploaded_at: null,
      };
      await addRecording(recording);
      recorder.discard();
      if (online) toast.info('Saved: uploading now');
      else toast.info('Saved offline: will upload when you are back online');
    } catch (error) {
      toast.error('Could not save the recording on this device', { message: error instanceof Error ? error.message : undefined });
    } finally {
      setSaving(false);
    }
  };

  const recording = recorder.state === 'recording' || recorder.state === 'paused';
  const supported = recorderSupported();

  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader
        title="Capture"
        icon={MicrophoneIcon}
        description="Record an in-person conversation. No signal? It is saved on this device and uploads by itself when you reconnect. The processor turns it into a note, products and tasks for your approval."
        actions={
          installEvent ? (
            <Button
              variant="secondary"
              icon={ArrowDownTrayIcon}
              onClick={() => {
                void installEvent.prompt();
                setInstallEvent(null);
              }}
            >
              Install app
            </Button>
          ) : null
        }
      />

      <div
        role="status"
        className={clsx(
          'mb-5 flex flex-wrap items-center justify-between gap-3 rounded-lg border px-4 py-3 text-sm',
          online ? 'border-emerald-200 bg-emerald-50 text-emerald-900' : 'border-amber-300 bg-amber-50 text-amber-900',
        )}
      >
        <p className="flex items-center gap-2 font-medium">
          {online ? <WifiIcon aria-hidden="true" className="h-5 w-5 text-emerald-600" /> : <SignalSlashIcon aria-hidden="true" className="h-5 w-5 text-amber-600" />}
          {online ? 'Online: recordings upload as soon as you save them' : browserOnline ? 'Offline mode (simulated): recordings are saved here and will upload later' : 'You are offline: recordings are saved on this device and will upload automatically'}
        </p>
        <Toggle label="Simulate offline (demo)" checked={simulateOffline} onChange={setSimulateOffline} className="items-center gap-3" />
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <div className="space-y-5">
          <Card aria-labelledby="rec-title">
            <CardHeader
              id="rec-title"
              title="Recorder"
              subtitle={recorder.state === 'recording' ? 'Recording…' : recorder.state === 'paused' ? 'Paused' : recorder.result ? 'Review, then save' : 'Tap to start'}
              icon={MicrophoneIcon}
              actions={recording ? <Badge tone="danger" dot pulse>REC</Badge> : null}
            />
            <CardBody className="space-y-4">
              {recorder.error ? (
                <p role="alert" className="flex items-start gap-2 rounded-md bg-red-50 px-3 py-2 text-sm text-red-800 ring-1 ring-inset ring-red-200">
                  <ExclamationTriangleIcon aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
                  {recorder.error}
                </p>
              ) : null}
              {!supported ? (
                <p className="rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-900 ring-1 ring-inset ring-amber-200">
                  Live recording needs a secure (https or localhost) page and a microphone. You can still upload an audio file below.
                </p>
              ) : null}

              {recorder.result ? (
                <div className="space-y-3">
                  <div className="flex items-center justify-between text-sm text-neutral-700">
                    <span className="font-medium">{recorder.result.durationSeconds > 0 ? formatDurationSeconds(recorder.result.durationSeconds) : 'Recorded audio'}</span>
                    <span className="text-neutral-500">{formatBytes(recorder.result.blob.size)}</span>
                  </div>
                  {previewUrl ? <audio controls src={previewUrl} className="w-full" aria-label="Playback of your recording" /> : null}
                  <div className="flex flex-col gap-2 sm:flex-row">
                    <Button variant="primary" size="lg" icon={CloudArrowUpIcon} loading={saving} onClick={() => void saveRecording()} className="flex-1">
                      {online ? 'Save & upload' : 'Save (upload later)'}
                    </Button>
                    <Button variant="secondary" size="lg" icon={TrashIcon} onClick={recorder.discard} disabled={saving}>
                      Discard
                    </Button>
                  </div>
                </div>
              ) : (
                <div className="flex flex-col items-center gap-4 py-2">
                  <p className="tabular text-4xl font-semibold tracking-tight text-neutral-900" aria-live="off">
                    {formatClock(recorder.elapsed)}
                  </p>
                  <LevelMeter level={recorder.level} active={recorder.state === 'recording'} />
                  {recorder.state === 'idle' || recorder.state === 'requesting' ? (
                    <button
                      type="button"
                      onClick={() => void recorder.start()}
                      disabled={recorder.state === 'requesting' || !supported}
                      aria-label="Start recording"
                      className="flex h-24 w-24 items-center justify-center rounded-full bg-red-600 text-white shadow-pop transition-transform hover:bg-red-700 active:scale-95 disabled:cursor-not-allowed disabled:bg-neutral-300"
                    >
                      {recorder.state === 'requesting' ? <Spinner className="h-9 w-9" /> : <MicrophoneIcon aria-hidden="true" className="h-11 w-11" />}
                    </button>
                  ) : (
                    <div className="flex items-center gap-4">
                      {recorder.state === 'recording' ? (
                        <button type="button" onClick={recorder.pause} aria-label="Pause recording" className="flex h-14 w-14 items-center justify-center rounded-full bg-neutral-200 text-neutral-800 hover:bg-neutral-300">
                          <PauseIcon aria-hidden="true" className="h-7 w-7" />
                        </button>
                      ) : (
                        <button type="button" onClick={recorder.resume} aria-label="Resume recording" className="flex h-14 w-14 items-center justify-center rounded-full bg-neutral-200 text-neutral-800 hover:bg-neutral-300">
                          <PlayIcon aria-hidden="true" className="h-7 w-7" />
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={recorder.stop}
                        aria-label="Stop recording"
                        className={clsx('flex h-24 w-24 items-center justify-center rounded-full bg-red-600 text-white shadow-pop hover:bg-red-700 active:scale-95', recorder.state === 'recording' && 'animate-record-ring')}
                      >
                        <StopIcon aria-hidden="true" className="h-11 w-11" />
                      </button>
                    </div>
                  )}
                  <p className="text-center text-xs text-neutral-500">
                    {recorder.state === 'idle' ? 'Keep the phone near the conversation. Stop when you are done.' : recorder.state === 'requesting' ? 'Waiting for microphone permission…' : 'Recording stays on this device until it is uploaded.'}
                  </p>
                  {recorder.state === 'idle' ? (
                    <>
                      <input
                        ref={fileInput}
                        type="file"
                        accept="audio/*"
                        className="sr-only"
                        aria-label="Choose an audio file"
                        onChange={(event) => {
                          const file = event.target.files?.[0];
                          if (file) void recorder.loadFile(file);
                          event.target.value = '';
                        }}
                      />
                      <Button variant="ghost" size="sm" icon={FolderOpenIcon} onClick={() => fileInput.current?.click()}>
                        Choose an audio file instead
                      </Button>
                    </>
                  ) : null}
                </div>
              )}
            </CardBody>
          </Card>

          <Card aria-labelledby="meta-title">
            <CardHeader id="meta-title" title="Who is it with?" subtitle="Optional: helps match the CRM account. Fill it in before or after recording." />
            <CardBody className="space-y-3">
              <Field label="Client organisation" htmlFor="cap-org" hint="Used to match the CRM account.">
                <input id="cap-org" className="input" value={metadata.customer_org} onChange={(event) => set('customer_org', event.target.value)} placeholder="e.g. Cedar Ridge Pension Trust" autoComplete="off" />
              </Field>
              <Field label="Client name" htmlFor="cap-name">
                <input id="cap-name" className="input" value={metadata.customer_name} onChange={(event) => set('customer_name', event.target.value)} placeholder="e.g. Marcus Whitfield" autoComplete="off" />
              </Field>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Market" htmlFor="cap-market">
                  <select id="cap-market" className="input" value={metadata.market} onChange={(event) => set('market', event.target.value)}>
                    {(markets.length > 0 ? markets.map((market) => ({ value: market.market, label: market.display_name })) : [{ value: 'US', label: 'United States' }]).map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Business line" htmlFor="cap-bl">
                  <select id="cap-bl" className="input" value={metadata.business_line} onChange={(event) => set('business_line', event.target.value)}>
                    {businessLines.map((line) => (
                      <option key={line} value={line}>
                        {businessLineLabel(line)}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>
              <Field label="Spoken language" htmlFor="cap-lang">
                <select id="cap-lang" className="input" value={metadata.language_hint} onChange={(event) => set('language_hint', event.target.value)}>
                  {LANGUAGES.map((language) => (
                    <option key={language.value} value={language.value}>
                      {language.label}
                    </option>
                  ))}
                </select>
              </Field>
              <details className="rounded-md border border-neutral-200 bg-neutral-50 text-sm">
                <summary className="cursor-pointer select-none px-3 py-2 font-medium text-neutral-700">Your details (saved on this device)</summary>
                <div className="grid gap-3 border-t border-neutral-200 p-3 sm:grid-cols-2">
                  <Field label="Your name" htmlFor="cap-seller-name">
                    <input id="cap-seller-name" className="input" value={metadata.seller_name} onChange={(event) => set('seller_name', event.target.value)} />
                  </Field>
                  <Field label="Your email" htmlFor="cap-seller-email">
                    <input id="cap-seller-email" type="email" className="input" value={metadata.seller_email} onChange={(event) => set('seller_email', event.target.value)} />
                  </Field>
                </div>
              </details>
            </CardBody>
          </Card>
        </div>

        <Card aria-labelledby="queue-title" className="self-start">
          <CardHeader
            id="queue-title"
            title="Upload queue"
            subtitle="Stored on this device (IndexedDB)"
            icon={CloudArrowUpIcon}
            actions={
              online && queue.items.some((item) => item.status === 'queued') ? (
                <Button size="sm" variant="secondary" icon={ArrowPathIcon} onClick={() => void queue.flush()}>
                  Upload now
                </Button>
              ) : null
            }
          />
          <CardBody className="!p-0">
            {queue.storageError ? (
              <p role="alert" className="m-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-800 ring-1 ring-inset ring-red-200">
                This browser blocked local storage, so offline saving is unavailable: {queue.storageError}
              </p>
            ) : null}
            {queue.loaded && queue.items.length === 0 ? (
              <EmptyState title="Nothing recorded yet" description="Your recordings and their upload status will appear here." />
            ) : (
              <ul className="divide-y divide-neutral-100" aria-label="Recordings">
                {queue.items.map((item) => (
                  <li key={item.id} className="px-4 py-3 sm:px-5">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="truncate text-sm font-semibold text-neutral-900">{item.metadata.customer_org || item.metadata.customer_name || 'In-person conversation'}</p>
                        <p className="mt-0.5 text-xs text-neutral-500">
                          {formatDurationSeconds(item.duration_seconds)} · {formatBytes(item.size_bytes)} · {formatRelative(item.created_at, now)}
                        </p>
                      </div>
                      <div className="shrink-0">
                        {item.status === 'queued' ? (
                          <Badge tone={online ? 'info' : 'warning'} icon={online ? undefined : SignalSlashIcon}>
                            {online ? 'Queued' : 'Offline: will upload'}
                          </Badge>
                        ) : null}
                        {item.status === 'uploading' ? (
                          <Badge tone="info">
                            <span className="inline-flex items-center gap-1">
                              <Spinner className="h-3 w-3" /> Uploading…
                            </span>
                          </Badge>
                        ) : null}
                        {item.status === 'uploaded' ? (
                          <Badge tone="success" icon={CheckCircleIcon}>
                            Uploaded
                          </Badge>
                        ) : null}
                        {item.status === 'failed' ? (
                          <Badge tone="danger" icon={ExclamationTriangleIcon}>
                            Failed
                          </Badge>
                        ) : null}
                      </div>
                    </div>
                    {item.error && item.status !== 'uploaded' ? <p className="mt-1 text-xs text-neutral-600">{item.error}</p> : null}
                    {item.duplicate_of ? <p className="mt-1 text-xs text-neutral-600">Already uploaded earlier: the existing conversation was reused.</p> : null}
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      {item.status === 'uploaded' && item.conversation_id ? (
                        <Link to={`/conversations/${item.conversation_id}`} className="btn btn-primary btn-sm">
                          Open the review
                        </Link>
                      ) : null}
                      {item.status === 'failed' ? (
                        <Button size="sm" variant="secondary" icon={ArrowPathIcon} onClick={() => void queue.retry(item.id)}>
                          Retry
                        </Button>
                      ) : null}
                      {item.status !== 'uploading' ? (
                        <Button size="sm" variant="ghost" icon={TrashIcon} onClick={() => void queue.remove(item.id)} aria-label={`Remove ${item.metadata.customer_org || 'recording'} from the list`}>
                          {item.status === 'uploaded' ? 'Clear' : 'Delete'}
                        </Button>
                      ) : null}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </CardBody>
        </Card>
      </div>
    </div>
  );
}
