import { SignalIcon } from '@heroicons/react/24/outline';
import type { UseMutationResult } from '@tanstack/react-query';
import clsx from 'clsx';
import { useConfig } from '@/api/hooks';
import type { ConfigResponse, PreflightProbe, PreflightResponse, ResolvedMode } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { ErrorState, LoadingBlock, Skeleton } from '@/components/ui/StateViews';
import { formatDurationSeconds } from '@/lib/format';
import { LIVE_NAMES, SERVICE_LABELS } from '@/lib/labels';
import { CheckList, ReadinessList } from './CheckList';

export type PreflightMutation = UseMutationResult<PreflightResponse, Error, PreflightProbe[] | undefined>;

interface Tile {
  key: string;
  live: (config: ConfigResponse) => string;
  mock: string;
}

const TILES: Tile[] = [
  { key: 'transcription', live: () => 'Fast transcription with speaker separation', mock: 'Scripted transcripts: no audio goes to Azure' },
  { key: 'pii', live: () => 'Language PII detection plus a local card and credential check', mock: 'Local pattern detector' },
  { key: 'llm', live: (config) => `${config.engine} engine${config.llm_deployment ? ` on deployment ${config.llm_deployment}` : ''}`, mock: 'Rules engine: no model' },
  { key: 'crm', live: () => 'Dataverse: calls, tasks and insights are really written', mock: 'Local CRM simulator (SQLite), a Dataverse stand-in' },
  { key: 'vault', live: () => 'Azure Blob storage', mock: 'Local file vault' },
  { key: 'search', live: () => 'Azure AI Search', mock: 'Local catalogue index' },
];

function ServiceTile({ tile, mode, config }: { tile: Tile; mode: ResolvedMode; config: ConfigResponse }) {
  const name = mode.live ? (LIVE_NAMES[mode.effective] ?? mode.effective) : 'Mock';
  return (
    <li className="rounded-lg border border-neutral-200 bg-white p-3">
      <p className="text-xs font-semibold uppercase tracking-wide text-neutral-500">{SERVICE_LABELS[tile.key] ?? tile.key}</p>
      <p className="mt-1 flex items-center gap-1.5 text-sm font-semibold text-neutral-900">
        <span aria-hidden="true" className={clsx('h-2 w-2 shrink-0 rounded-full', mode.live ? 'bg-emerald-500' : 'bg-neutral-400')} />
        {mode.live ? `Live: ${name}` : 'Mock / local'}
      </p>
      <p className="mt-0.5 text-xs text-neutral-500">{mode.live ? tile.live(config) : tile.mock}</p>
      {mode.reason ? <p className="mt-1 text-xs text-amber-800">Requested {mode.requested}, but {mode.reason}</p> : null}
    </li>
  );
}

/** What is live and what is a mock right now, plus the same service probes as `scripts\preflight.bat`, run from the page. */
export function ServiceReadiness({ check }: { check: PreflightMutation }) {
  const config = useConfig();
  const modes = config.data?.modes;

  return (
    <Card aria-labelledby="services-title">
      <CardHeader
        id="services-title"
        icon={SignalIcon}
        title="Services behind these runs"
        subtitle="Set in .env; the top bar shows the same chips. Say out loud which ones are live."
        actions={
          <Button size="sm" variant="primary" icon={SignalIcon} loading={check.isPending} onClick={() => check.mutate(undefined)}>
            Check services now
          </Button>
        }
      />
      <CardBody className="space-y-4">
        {config.isPending ? (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {Array.from({ length: 3 }, (_, index) => (
              <Skeleton key={index} className="h-20 rounded-lg" />
            ))}
          </div>
        ) : config.isError || !modes || !config.data ? (
          <ErrorState error={config.error} title="Could not read the configuration" compact onRetry={() => void config.refetch()} />
        ) : (
          <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3" aria-label="Service modes">
            {TILES.map((tile) => {
              const mode = modes[tile.key];
              return mode ? <ServiceTile key={tile.key} tile={tile} mode={mode} config={config.data} /> : null;
            })}
          </ul>
        )}

        <div aria-live="polite">
          {check.isPending ? <LoadingBlock label="Probing the live services: a short text-to-speech clip, a transcription, a PII sample and one model call (about 20 s)…" className="py-6" /> : null}
          {check.isError ? <ErrorState error={check.error} title="The service check failed" compact /> : null}
          {check.data ? (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-neutral-500">Scenario readiness</h3>
                <Badge tone="neutral">checked in {formatDurationSeconds(check.data.seconds)}</Badge>
              </div>
              <ReadinessList items={check.data.readiness} />
              <CheckList checks={check.data.checks} />
            </div>
          ) : check.isPending || check.isError ? null : (
            <p className="text-xs text-neutral-500">
              &ldquo;Check services now&rdquo; sends a few tiny synthetic requests to each live service (under one cent) and tells you which scenarios are ready, with the fix for anything that is not.
            </p>
          )}
        </div>
      </CardBody>
    </Card>
  );
}
