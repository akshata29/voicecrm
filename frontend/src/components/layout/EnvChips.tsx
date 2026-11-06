import { ArrowPathIcon, BeakerIcon, ServerStackIcon } from '@heroicons/react/24/solid';
import clsx from 'clsx';
import { USE_MOCK } from '@/api/client';
import { useConfig } from '@/api/hooks';
import { SERVICE_KEYS, type ResolvedMode } from '@/api/types';
import { Skeleton } from '@/components/ui/StateViews';
import { LIVE_NAMES, SERVICE_LABELS } from '@/lib/labels';
import { humanize } from '@/lib/format';

const SHORT: Record<string, string> = {
  transcription: 'Speech',
  llm: 'Model',
  search: 'Search',
  pii: 'PII',
  crm: 'CRM',
  vault: 'Vault',
};

function tooltip(mode: ResolvedMode): string {
  const parts = [`${SERVICE_LABELS[mode.service] ?? humanize(mode.service)}: ${mode.live ? 'live' : 'mock / local'}`, `requested: ${mode.requested}`, `effective: ${mode.effective}`];
  if (mode.reason) parts.push(mode.reason);
  return parts.join(' \u00b7 ');
}

export function EnvChip({ mode }: { mode: ResolvedMode }) {
  const label = mode.live ? `Live: ${LIVE_NAMES[mode.effective] ?? humanize(mode.effective)}` : `Mock: ${SHORT[mode.service] ?? humanize(mode.service)}`;
  return (
    <span
      title={tooltip(mode)}
      className={clsx(
        'inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-1 text-xs font-medium ring-1 ring-inset',
        mode.live ? 'bg-emerald-50 text-emerald-800 ring-emerald-300' : 'bg-neutral-100 text-neutral-600 ring-neutral-200',
      )}
    >
      <span aria-hidden="true" className={clsx('h-1.5 w-1.5 rounded-full', mode.live ? 'bg-emerald-500' : 'bg-neutral-400')} />
      {label}
    </span>
  );
}

/** Per-service mode chips from GET /config: green "Live: Azure Speech" vs grey "Mock: Speech". */
export function EnvChips({ className }: { className?: string }) {
  const config = useConfig();

  if (config.isPending) {
    return (
      <div className={clsx('flex items-center gap-2', className)} role="status" aria-label="Loading environment">
        {Array.from({ length: 4 }, (_, index) => (
          <Skeleton key={index} className="h-6 w-24 rounded-full" />
        ))}
      </div>
    );
  }

  if (config.isError) {
    return (
      <div className={clsx('flex items-center gap-2', className)}>
        <button
          type="button"
          onClick={() => void config.refetch()}
          className="inline-flex items-center gap-1.5 rounded-full bg-red-50 px-2.5 py-1 text-xs font-medium text-red-800 ring-1 ring-inset ring-red-200 hover:bg-red-100"
          title="GET /api/v1/config failed - click to retry"
        >
          <ArrowPathIcon aria-hidden="true" className="h-3.5 w-3.5" />
          API unreachable
        </button>
      </div>
    );
  }

  const modes = config.data.modes;
  const ordered = [...SERVICE_KEYS.filter((key) => modes[key]), ...Object.keys(modes).filter((key) => !(SERVICE_KEYS as readonly string[]).includes(key))];

  return (
    <div className={clsx('flex items-center gap-1.5', className)} aria-label="Service modes">
      {USE_MOCK ? (
        <span
          title="The UI is serving canned data from an in-browser mock (VITE_USE_MOCK=1); no backend calls are made"
          className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full bg-brand-50 px-2.5 py-1 text-xs font-medium text-brand-700 ring-1 ring-inset ring-brand-200"
        >
          <ServerStackIcon aria-hidden="true" className="h-3.5 w-3.5" />
          Demo API
        </span>
      ) : null}
      {ordered.map((key) => {
        const mode = modes[key];
        return mode ? <EnvChip key={key} mode={mode} /> : null;
      })}
    </div>
  );
}

export function SyntheticBadge({ className }: { className?: string }) {
  return (
    <span
      title="Every call, product, customer and number in this demo is synthetic or illustrative"
      className={clsx('inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full bg-amber-100 px-2.5 py-1 text-xs font-semibold text-amber-900 ring-1 ring-inset ring-amber-300', className)}
    >
      <BeakerIcon aria-hidden="true" className="h-3.5 w-3.5" />
      <span className="hidden sm:inline">Synthetic demo data</span>
      <span className="sm:hidden">Synthetic data</span>
    </span>
  );
}
