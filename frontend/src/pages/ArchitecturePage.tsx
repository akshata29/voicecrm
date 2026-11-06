import { ArrowTopRightOnSquareIcon, RectangleGroupIcon } from '@heroicons/react/24/outline';
import clsx from 'clsx';
import type { ReactNode } from 'react';
import { useConfig } from '@/api/hooks';
import type { ConfigResponse, ResolvedMode } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { PageHeader } from '@/components/ui/Field';
import { ErrorState, LoadingBlock } from '@/components/ui/StateViews';
import { useDocumentTitle } from '@/lib/hooks';
import { humanize } from '@/lib/format';

interface ComponentRow {
  name: string;
  does: string;
  azure: string;
  /** Key into ConfigResponse.modes, or null for parts that have no switch. */
  service: string | null;
  env?: string;
  fixed?: { label: string; note: string };
}

const ROWS: ComponentRow[] = [
  {
    name: 'Channel adapters',
    does: 'Teams, Zoom, Genesys and mobile payloads normalised into one ingest contract',
    azure: 'APIM + Functions (production path)',
    service: null,
    fixed: { label: 'Simulated payloads', note: 'Format-faithful adapters; live connectors need accounts and admin consent' },
  },
  { name: 'Central transcription', does: 'Audio to text with language ID and diarization; platform transcript only when it is free', azure: 'Azure Speech', service: 'transcription', env: 'VOICECRM_TRANSCRIPTION_MODE' },
  { name: 'Notes policy', does: 'PII redaction, sensitive and small-talk filtering, relationship keeper, leak validator', azure: 'Azure AI Language (PII) + rules', service: 'pii', env: 'VOICECRM_PII_MODE' },
  { name: 'Templates and extraction', does: 'Schema-driven note, topics and tasks; rules, model or hybrid engine', azure: 'Microsoft Foundry model (structured output)', service: 'llm', env: 'VOICECRM_LLM_MODE' },
  { name: 'Product and topic resolver', does: 'Share-class catalogue index, candidate generation, reconciliation, top-3 fallback', azure: 'Azure AI Search + Foundry model', service: 'search', env: 'VOICECRM_SEARCH_MODE' },
  {
    name: 'Policy engine',
    does: 'Per-market retention, notes policy, auto-approve dial (runtime overrides persisted)',
    azure: 'Config store',
    service: null,
    fixed: { label: 'Local config store', note: 'YAML defaults with runtime overrides under .state/' },
  },
  { name: 'Transcript vault', does: 'Audio, retained transcripts and working copies with per-market lifecycle (never the CRM)', azure: 'Azure Blob Storage', service: 'vault', env: 'VOICECRM_VAULT_MODE' },
  {
    name: 'Review inbox',
    does: 'One-click approve, product pick-list, correction capture, offline mobile capture',
    azure: 'React web app (this UI)',
    service: null,
    fixed: { label: 'This app', note: 'React + TypeScript; installable PWA for capture' },
  },
  { name: 'CRM writer', does: 'Idempotent upsert of phone call, tasks and a structured insight', azure: 'Dataverse Web API', service: 'crm', env: 'VOICECRM_CRM_MODE' },
  {
    name: 'Metering and evaluation',
    does: 'Usage per call, illustrative cost model, golden-set scoreboard',
    azure: 'Application Insights / Cosmos DB',
    service: null,
    fixed: { label: 'In-process', note: 'Local meter and golden set; prices are illustrative' },
  },
];

function ModeChip({ mode }: { mode: ResolvedMode }) {
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-1 text-xs font-medium ring-1 ring-inset',
        mode.live ? 'bg-emerald-50 text-emerald-800 ring-emerald-300' : 'bg-neutral-100 text-neutral-600 ring-neutral-200',
      )}
    >
      <span aria-hidden="true" className={clsx('h-1.5 w-1.5 rounded-full', mode.live ? 'bg-emerald-500' : 'bg-neutral-400')} />
      {mode.live ? 'Live' : 'Mock / local'}
    </span>
  );
}

function ModeCell({ row, config }: { row: ComponentRow; config: ConfigResponse }) {
  if (row.service === null) {
    return (
      <div>
        <Badge tone="neutral">{row.fixed?.label}</Badge>
        <p className="mt-1 text-xs text-neutral-500">{row.fixed?.note}</p>
      </div>
    );
  }
  const mode = config.modes[row.service];
  if (!mode) return <Badge tone="warning">Not reported</Badge>;
  return (
    <div>
      <ModeChip mode={mode} />
      <p className="mt-1 text-xs text-neutral-600">
        {mode.requested === mode.effective ? (
          <>
            adapter: <code className="text-neutral-800">{mode.effective}</code>
          </>
        ) : (
          <>
            requested <code className="text-neutral-800">{mode.requested}</code> → running <code className="text-neutral-800">{mode.effective}</code>
          </>
        )}
      </p>
      {mode.reason ? <p className="mt-0.5 text-xs text-amber-800">{mode.reason}</p> : null}
    </div>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs font-semibold uppercase tracking-wide text-neutral-400">{label}</dt>
      <dd className="text-sm font-medium text-neutral-900">{children}</dd>
    </div>
  );
}

export default function ArchitecturePage() {
  useDocumentTitle('Architecture');
  const config = useConfig();
  const live = config.data ? Object.values(config.data.modes).filter((mode) => mode.live).length : 0;
  const total = config.data ? Object.keys(config.data.modes).length : 0;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Architecture"
        icon={RectangleGroupIcon}
        description="Any source, one canonical conversation, governed AI extraction, a controlled CRM write-back. Every Azure service sits behind a port with a live and a mock adapter, so the demo runs with whatever is configured."
        actions={
          <a href="/architecture.png" target="_blank" rel="noreferrer" className="btn btn-secondary">
            <ArrowTopRightOnSquareIcon aria-hidden="true" className="h-4 w-4" /> Open full size
          </a>
        }
      />

      <Card aria-labelledby="diagram-title">
        <CardHeader id="diagram-title" title="VoiceCRM: conversation-to-CRM hub on Azure" subtitle="Capture, ingest and transcribe, understand, review and write, where it lands" />
        <CardBody>
          <div className="overflow-x-auto rounded-lg border border-neutral-200 bg-white">
            <img src="/architecture.png" alt="Architecture diagram: five stages from capture (Teams, Zoom, Genesys, mobile, other tools) through ingest and transcribe, understand, and one-click review and write, to where it lands in Dynamics 365 / Dataverse, with cross-cutting governance, quality and economics." className="mx-auto block h-auto w-full min-w-[56rem]" loading="lazy" />
          </div>
        </CardBody>
      </Card>

      <Card aria-labelledby="modes-title">
        <CardHeader
          id="modes-title"
          title="Where each component runs today"
          subtitle="From GET /config"
          actions={config.data ? <Badge tone={live > 0 ? 'success' : 'neutral'}>{live} of {total} services live</Badge> : null}
        />
        <CardBody className="!p-0">
          {config.isPending ? <LoadingBlock label="Reading the configuration…" /> : null}
          {config.isError ? (
            <div className="p-4">
              <ErrorState error={config.error} title="Could not read the configuration" onRetry={() => void config.refetch()} />
            </div>
          ) : null}
          {config.data ? (
            <>
              <dl className="grid grid-cols-2 gap-4 border-b border-neutral-100 px-4 py-4 sm:grid-cols-3 sm:px-5 lg:grid-cols-6">
                <Fact label="App version">{config.data.app_version}</Fact>
                <Fact label="Demo mode">{config.data.demo_mode ? 'On (fallbacks allowed)' : 'Off (strict)'}</Fact>
                <Fact label="Effective engine">{config.data.engine}</Fact>
                <Fact label="Catalogue">
                  {config.data.catalogue_products.toLocaleString()} products
                  {config.data.catalogue_synthetic ? <span className="ml-1 text-xs font-normal text-amber-700">synthetic</span> : null}
                </Fact>
                <Fact label="Pricing">{config.data.pricing_illustrative ? 'Illustrative' : 'Validated'}</Fact>
                <Fact label="Markets">{config.data.markets.map((market) => market.market).join(' · ')}</Fact>
              </dl>
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-neutral-100 text-sm">
                  <caption className="sr-only">Components and their current mode</caption>
                  <thead className="bg-neutral-50">
                    <tr>
                      <th scope="col" className="th">Component</th>
                      <th scope="col" className="th">What it does</th>
                      <th scope="col" className="th">Current mode</th>
                      <th scope="col" className="th">Azure service when live</th>
                      <th scope="col" className="th hidden xl:table-cell">Switch with</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-neutral-100 bg-white">
                    {ROWS.map((row) => (
                      <tr key={row.name}>
                        <th scope="row" className="td whitespace-nowrap text-left font-semibold text-neutral-900">{row.name}</th>
                        <td className="td max-w-xs text-neutral-600">{row.does}</td>
                        <td className="td min-w-[14rem]">
                          <ModeCell row={row} config={config.data} />
                        </td>
                        <td className="td text-neutral-700">{row.azure}</td>
                        <td className="td hidden xl:table-cell">{row.env ? <code className="text-xs text-neutral-500">{row.env}</code> : <span className="text-neutral-300">n/a</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {Object.keys(config.data.modes).some((key) => !ROWS.some((row) => row.service === key)) ? (
                <p className="border-t border-neutral-100 px-5 py-3 text-xs text-neutral-500">
                  Also reported by the API: {Object.keys(config.data.modes).filter((key) => !ROWS.some((row) => row.service === key)).map(humanize).join(', ')}.
                </p>
              ) : null}
            </>
          ) : null}
        </CardBody>
      </Card>
    </div>
  );
}
