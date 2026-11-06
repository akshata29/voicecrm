import { BriefcaseIcon, BuildingOffice2Icon, ChatBubbleBottomCenterTextIcon, ClipboardDocumentCheckIcon, LightBulbIcon, MagnifyingGlassIcon, PhoneIcon, ShieldCheckIcon, UserCircleIcon } from '@heroicons/react/24/outline';
import { useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useCrmHealth, useCrmRecords, useTaxonomy } from '@/api/hooks';
import { CRM_ENTITIES, type CrmEntity, type CrmRow } from '@/api/types';
import { LanguageChip } from '@/components/conversation/Badges';
import { Badge } from '@/components/ui/Badge';
import { ConfidenceBar } from '@/components/ui/ConfidenceBar';
import { PageHeader } from '@/components/ui/Field';
import { EmptyState, LoadingBlock, ErrorState } from '@/components/ui/StateViews';
import { Tabs, tabIds, type TabItem } from '@/components/ui/Tabs';
import type { Tone } from '@/components/ui/types';
import { formatDate, formatDateTime, humanize, truncate } from '@/lib/format';
import { useDocumentTitle } from '@/lib/hooks';

// ---- tolerant row helpers (the contract only says "dict[] rows") -----------------------------------
function parseMaybeJson(value: unknown): unknown {
  if (typeof value !== 'string') return value;
  const trimmed = value.trim();
  if ((trimmed.startsWith('[') && trimmed.endsWith(']')) || (trimmed.startsWith('{') && trimmed.endsWith('}'))) {
    try {
      return JSON.parse(trimmed) as unknown;
    } catch {
      return value;
    }
  }
  return value;
}

function pick(row: CrmRow, keys: string[]): unknown {
  for (const key of keys) {
    const value = row[key];
    if (value !== undefined && value !== null && value !== '') return parseMaybeJson(value);
  }
  return undefined;
}

function pickList(row: CrmRow, keys: string[]): string[] {
  const value = pick(row, keys);
  if (Array.isArray(value)) return value.map((item) => (item && typeof item === 'object' && 'name' in item ? String(item.name) : String(item)));
  if (typeof value === 'string') return value.split(/[;,]/).map((item) => item.trim()).filter(Boolean);
  return [];
}

function pickText(row: CrmRow, keys: string[]): string | undefined {
  const value = pick(row, keys);
  if (value === undefined) return undefined;
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (value && typeof value === 'object' && 'name' in value) return String(value.name);
  return undefined;
}

const ISO_DATETIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const BADGE_KEYS: Record<string, Record<string, Tone>> = {
  status: { open: 'info', completed: 'success', cancelled: 'neutral' },
  priority: { high: 'danger', normal: 'neutral', low: 'neutral' },
  direction: { inbound: 'info', outbound: 'brand' },
  stage: {},
  segment: {},
  review_status: { approved: 'success', auto_approved: 'violet' },
};

function CellValue({ name, value }: { name: string; value: unknown }) {
  const parsed = parseMaybeJson(value);
  if (parsed === null || parsed === undefined || parsed === '') return <span className="text-neutral-300">{'\u2014'}</span>;
  if (typeof parsed === 'boolean') return <span>{parsed ? 'Yes' : 'No'}</span>;
  if (typeof parsed === 'number') return <span className="tabular">{Number.isInteger(parsed) ? parsed : parsed.toFixed(2)}</span>;
  if (Array.isArray(parsed)) {
    const items = parsed.map((item) => (item && typeof item === 'object' && 'name' in item ? String(item.name) : String(item)));
    return (
      <span className="flex flex-wrap gap-1">
        {items.slice(0, 3).map((item) => (
          <Badge key={item} tone="neutral">
            {truncate(item, 28)}
          </Badge>
        ))}
        {items.length > 3 ? <span className="text-xs text-neutral-400">+{items.length - 3}</span> : null}
      </span>
    );
  }
  if (typeof parsed === 'object') {
    const label = 'name' in parsed ? String(parsed.name) : JSON.stringify(parsed);
    return <span title={JSON.stringify(parsed)}>{truncate(label, 40)}</span>;
  }
  const text = String(parsed);
  if (name in BADGE_KEYS) return <Badge tone={BADGE_KEYS[name]?.[text.toLowerCase()] ?? 'neutral'}>{humanize(text)}</Badge>;
  if (ISO_DATETIME.test(text)) return <span className="tabular whitespace-nowrap">{formatDateTime(text)}</span>;
  if (ISO_DATE.test(text)) return <span className="tabular whitespace-nowrap">{formatDate(text)}</span>;
  if (name.endsWith('_id') || name === 'id' || name === 'idempotency_key' || name.endsWith('_uri')) return <code className="text-xs text-neutral-500">{truncate(text, 24)}</code>;
  return <span title={text.length > 60 ? text : undefined}>{truncate(text, 60)}</span>;
}

const PREFERRED: Record<Exclude<CrmEntity, 'insight'>, string[]> = {
  phonecall: ['subject', 'regarding_name', 'contact_name', 'direction', 'start', 'duration_minutes', 'owner_name'],
  task: ['subject', 'due', 'priority', 'regarding_name', 'owner_name'],
  account: ['name', 'domain', 'segment'],
  contact: ['name', 'title', 'email', 'phone', 'account_name'],
  opportunity: ['name', 'stage', 'account_name', 'contact_name'],
};

const DETAIL_ONLY = new Set([
  'description',
  'summary_markdown',
  'idempotency_key',
  'conversation_id',
  'source_interaction_id',
  'created_at',
  'id',
  'regarding_id',
  'regarding_entity',
  'phonecall_id',
  'source_task_id',
  'contact_id',
  'account_id',
  'owner_email',
]);

function columnsFor(entity: Exclude<CrmEntity, 'insight'>, rows: CrmRow[]): string[] {
  const present = new Set(rows.flatMap((row) => Object.keys(row)));
  const preferred = PREFERRED[entity].filter((key) => present.has(key));
  const extras = [...present].filter((key) => !preferred.includes(key) && !DETAIL_ONLY.has(key)).slice(0, Math.max(0, 7 - preferred.length));
  return [...preferred, ...extras];
}

function matches(row: CrmRow, needle: string): boolean {
  if (!needle) return true;
  return JSON.stringify(Object.values(row)).toLowerCase().includes(needle.toLowerCase());
}

// ---- generic table ---------------------------------------------------------------------------------
function RowDetails({ row }: { row: CrmRow }) {
  const entries = Object.entries(row);
  return (
    <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
      {entries.map(([key, value]) => {
        const long = typeof value === 'string' && value.length > 80;
        return (
          <div key={key} className={long ? 'sm:col-span-2' : undefined}>
            <dt className="text-xs font-semibold uppercase tracking-wide text-neutral-400">{humanize(key)}</dt>
            <dd className={long ? 'whitespace-pre-wrap text-neutral-700' : 'text-neutral-800'}>{long ? String(value) : <CellValue name={key} value={value} />}</dd>
          </div>
        );
      })}
    </dl>
  );
}

function GenericTable({ entity, rows }: { entity: Exclude<CrmEntity, 'insight'>; rows: CrmRow[] }) {
  const columns = columnsFor(entity, rows);
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full divide-y divide-neutral-100 text-sm">
        <thead className="bg-neutral-50">
          <tr>
            {columns.map((column) => (
              <th key={column} scope="col" className="th">
                {humanize(column)}
              </th>
            ))}
            <th scope="col" className="th">
              <span className="sr-only">Details</span>
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-neutral-100 bg-white">
          {rows.map((row, index) => {
            const key = String(row.id ?? index);
            const expanded = open === key;
            return (
              <FragmentRow key={key} expanded={expanded} columns={columns.length + 1} detail={<RowDetails row={row} />}>
                {columns.map((column) => (
                  <td key={column} className="td max-w-xs text-neutral-800">
                    <CellValue name={column} value={row[column]} />
                  </td>
                ))}
                <td className="td text-right">
                  <button type="button" onClick={() => setOpen(expanded ? null : key)} aria-expanded={expanded} className="rounded px-2 py-0.5 text-xs font-medium text-brand-700 hover:bg-brand-50">
                    {expanded ? 'Hide' : 'Details'}
                  </button>
                </td>
              </FragmentRow>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function FragmentRow({ children, expanded, columns, detail }: { children: React.ReactNode; expanded: boolean; columns: number; detail: React.ReactNode }) {
  return (
    <>
      <tr className={expanded ? 'bg-brand-50/40' : 'hover:bg-neutral-50'}>{children}</tr>
      {expanded ? (
        <tr className="bg-neutral-50">
          <td colSpan={columns} className="px-4 py-3">
            {detail}
          </td>
        </tr>
      ) : null}
    </>
  );
}

// ---- insights (structured) -------------------------------------------------------------------------
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Product lines for an insight: names when the row has them, else the "products" note section, else the raw ids. */
function productLines(row: CrmRow): string[] {
  const names = pickList(row, ['products', 'product_names']);
  if (names.length > 0) return names;
  const sections = pick(row, ['sections']);
  if (Array.isArray(sections)) {
    for (const section of sections) {
      if (!isRecord(section)) continue;
      const label = `${String(section.key ?? '')} ${String(section.title ?? '')}`;
      if (/product/i.test(label) && Array.isArray(section.items) && section.items.length > 0) return section.items.map((item) => String(item));
    }
  }
  return pickList(row, ['product_ids']);
}

function InsightCard({ row }: { row: CrmRow }) {
  const taxonomy = useTaxonomy();
  const products = productLines(row);
  const productIds = pickList(row, ['product_ids']);
  const topicLabel = (value: string) => taxonomy.data?.topics.find((topic) => topic.topic_id === value)?.label ?? humanize(value);
  const topics = pickList(row, ['topics', 'topic_labels', 'topic_ids']);
  const facts = pickList(row, ['relationship_facts']);
  const confidence = pick(row, ['confidence']);
  const summary = pickText(row, ['summary_markdown', 'summary']);
  const pointer = pickText(row, ['transcript_pointer']);
  const template = pickText(row, ['template_id']);
  const engine = pickText(row, ['engine']);
  const model = pickText(row, ['model']);
  const review = pickText(row, ['review_status']);
  const created = pickText(row, ['created_at']);
  const regarding = pickText(row, ['regarding_name', 'regarding', 'account']);

  return (
    <li className="rounded-lg border border-neutral-200 bg-white p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-neutral-900">{regarding ?? 'Conversation insight'}</p>
          <p className="text-xs text-neutral-500">
            {pickText(row, ['source']) ? `${humanize(pickText(row, ['source']))} · ` : ''}
            {created ? formatDateTime(created) : ''}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {template ? <Badge tone="brand">{template}</Badge> : null}
          {engine ? (
            <Badge tone="neutral" title={model ? `model: ${model}` : undefined}>
              engine: {engine}
            </Badge>
          ) : null}
          {review ? <Badge tone={review === 'auto_approved' ? 'violet' : 'success'}>{humanize(review)}</Badge> : null}
          <LanguageChip language={pickText(row, ['language'])} />
        </div>
      </div>
      <div className="mt-3 grid gap-4 md:grid-cols-2">
        <div>
          <p className="section-title mb-1">Products</p>
          {products.length === 0 ? (
            <p className="text-sm text-neutral-400">None</p>
          ) : (
            <ul className="space-y-1 text-sm text-neutral-800">
              {products.map((product) => (
                <li key={product} className="flex items-center gap-1.5">
                  <ClipboardDocumentCheckIcon aria-hidden="true" className="h-4 w-4 shrink-0 text-neutral-400" />
                  {product}
                </li>
              ))}
            </ul>
          )}
          {productIds.length > 0 && productIds.join(',') !== products.join(',') ? (
            <p className="mt-1 font-mono text-[11px] text-neutral-400" title="Catalogue product ids written to the CRM">
              {productIds.join(' · ')}
            </p>
          ) : null}
        </div>
        <div>
          <p className="section-title mb-1">Topics</p>
          {topics.length === 0 ? (
            <p className="text-sm text-neutral-400">None</p>
          ) : (
            <p className="flex flex-wrap gap-1.5">
              {topics.map((topic) => (
                <Badge key={topic} tone="info">
                  {topicLabel(topic)}
                </Badge>
              ))}
            </p>
          )}
        </div>
      </div>
      {facts.length > 0 ? (
        <p className="mt-3 flex flex-wrap items-center gap-1.5 text-sm text-neutral-700">
          <span className="section-title">Relationship</span>
          {facts.map((fact) => (
            <Badge key={fact} tone="success">
              {fact}
            </Badge>
          ))}
        </p>
      ) : null}
      <div className="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-neutral-100 pt-3">
        <div className="w-48">
          {typeof confidence === 'number' ? <ConfidenceBar value={confidence} label="Overall confidence" /> : null}
        </div>
        {pointer ? (
          <Badge tone="violet" title="Pointer into the vault; the text itself is never in the CRM">
            vault pointer: {truncate(pointer, 34)}
          </Badge>
        ) : (
          <Badge tone="success" icon={ShieldCheckIcon}>
            No transcript pointer
          </Badge>
        )}
      </div>
      {summary ? (
        <details className="mt-3 rounded-md border border-neutral-200 bg-neutral-50 text-sm">
          <summary className="cursor-pointer select-none px-3 py-2 font-medium text-neutral-700">Summary note</summary>
          <pre className="whitespace-pre-wrap border-t border-neutral-200 px-3 py-2 font-sans text-xs leading-5 text-neutral-700">{summary}</pre>
        </details>
      ) : null}
    </li>
  );
}

// ---- page ------------------------------------------------------------------------------------------
const TABS: Array<TabItem<CrmEntity>> = [
  { id: 'phonecall', label: 'Phone calls', icon: PhoneIcon },
  { id: 'task', label: 'Tasks', icon: ClipboardDocumentCheckIcon },
  { id: 'insight', label: 'Insights', icon: LightBulbIcon },
  { id: 'account', label: 'Accounts', icon: BuildingOffice2Icon },
  { id: 'contact', label: 'Contacts', icon: UserCircleIcon },
  { id: 'opportunity', label: 'Opportunities', icon: BriefcaseIcon },
];

const EMPTY_HINT: Record<CrmEntity, string> = {
  phonecall: 'Approve a conversation and its phone call appears here.',
  task: 'Follow-up tasks from approved conversations appear here.',
  insight: 'Each approved conversation writes one structured insight.',
  account: 'No accounts in the CRM view.',
  contact: 'No contacts in the CRM view.',
  opportunity: 'No opportunities in the CRM view.',
};

function EntityPanel({ entity }: { entity: CrmEntity }) {
  const query = useCrmRecords(entity, 100);
  const [filter, setFilter] = useState('');
  const rows = useMemo(() => (query.data ?? []).filter((row) => matches(row, filter)), [query.data, filter]);

  if (query.isPending) return <LoadingBlock label="Loading CRM rows…" />;
  if (query.isError) return <ErrorState error={query.error} title="Could not read the CRM" onRetry={() => void query.refetch()} />;
  const total = query.data?.length ?? 0;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="relative w-full max-w-xs">
          <label htmlFor={`crm-filter-${entity}`} className="sr-only">
            Filter rows
          </label>
          <MagnifyingGlassIcon aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-neutral-400" />
          <input id={`crm-filter-${entity}`} type="search" value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="Filter rows…" className="input !pl-9" />
        </div>
        <p className="text-xs text-neutral-500">
          {rows.length === total ? `${total} row${total === 1 ? '' : 's'}` : `${rows.length} of ${total} rows`} · refreshes every 5 s
        </p>
      </div>
      {total === 0 ? (
        <div className="card">
          <EmptyState icon={ChatBubbleBottomCenterTextIcon} title={`No ${TABS.find((tab) => tab.id === entity)?.label.toLowerCase() ?? 'rows'} yet`} description={EMPTY_HINT[entity]} />
        </div>
      ) : rows.length === 0 ? (
        <div className="card">
          <EmptyState icon={MagnifyingGlassIcon} title="No rows match the filter" />
        </div>
      ) : entity === 'insight' ? (
        <ul className="grid gap-3 xl:grid-cols-2">
          {rows.map((row, index) => (
            <InsightCard key={String(row.id ?? index)} row={row} />
          ))}
        </ul>
      ) : (
        <div className="card overflow-hidden">
          <GenericTable entity={entity} rows={rows} />
        </div>
      )}
    </div>
  );
}

export default function CrmPage() {
  useDocumentTitle('CRM');
  const [params, setParams] = useSearchParams();
  const requested = params.get('tab');
  const tab: CrmEntity = (CRM_ENTITIES as readonly string[]).includes(requested ?? '') ? (requested as CrmEntity) : 'phonecall';
  const health = useCrmHealth();
  const adapter = health.data ? (typeof health.data.adapter === 'string' ? health.data.adapter : null) : null;

  return (
    <div className="space-y-5">
      <PageHeader
        title="CRM"
        icon={BuildingOffice2Icon}
        description="What actually landed in the CRM: the call, its tasks and one structured insight per conversation. A read-only view of the simulator tables or recent Dataverse rows."
        actions={
          health.data ? (
            <Badge
              tone={health.data.status === 'ok' || health.data.ok === true ? 'success' : 'warning'}
              dot
              title={isRecord(health.data.counts) ? Object.entries(health.data.counts).map(([key, value]) => `${key}: ${String(value)}`).join(' · ') : undefined}
            >
              {adapter ? `${adapter}: ` : 'Adapter: '}
              {health.data.status === 'ok' || health.data.ok === true ? 'ok' : String(health.data.status ?? 'degraded')}
            </Badge>
          ) : health.isError ? (
            <Badge tone="danger" dot>
              Adapter unreachable
            </Badge>
          ) : null
        }
      />
      <p role="note" className="flex items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-2.5 text-sm font-medium text-emerald-900">
        <ShieldCheckIcon aria-hidden="true" className="h-5 w-5 shrink-0 text-emerald-600" />
        Transcripts are never stored in the CRM. Rows hold the summary, structured fields and tasks only.
      </p>
      <div>
        <Tabs tabs={TABS} value={tab} onChange={(next) => setParams({ tab: next }, { replace: true })} label="CRM tables" idPrefix="crm" />
        <div role="tabpanel" id={tabIds('crm', tab).panel} aria-labelledby={tabIds('crm', tab).tab} className="pt-4">
          <EntityPanel key={tab} entity={tab} />
        </div>
      </div>
    </div>
  );
}
