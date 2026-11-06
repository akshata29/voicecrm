import { AcademicCapIcon, BeakerIcon, CubeIcon, MagnifyingGlassIcon, Square3Stack3DIcon, TrashIcon } from '@heroicons/react/24/outline';
import clsx from 'clsx';
import { useMemo, useState } from 'react';
import { getErrorMessage } from '@/api/client';
import { useAliases, useCatalogueSearch, useCatalogueStats, useResetAliases } from '@/api/hooks';
import type { CatalogueStats } from '@/api/types';
import { ShareClassBadge } from '@/components/conversation/Badges';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { ConfidenceBar } from '@/components/ui/ConfidenceBar';
import { ConfirmDialog } from '@/components/ui/ConfirmDialog';
import { PageHeader } from '@/components/ui/Field';
import { EmptyState, ErrorState, LoadingBlock, QueryState } from '@/components/ui/StateViews';
import { Stat } from '@/components/ui/Stat';
import { useToast } from '@/components/ui/Toast';
import { formatInt, formatRelative, humanize } from '@/lib/format';
import { useDebouncedValue, useDocumentTitle } from '@/lib/hooks';

const EXAMPLES = ['equity fund class C', 'growth opportunities', 'emerging markets I shares', 'short duration R6', 'balanced allocation advisor'];

function Distribution({ title, data }: { title: string; data: Record<string, number> }) {
  const entries = Object.entries(data).sort((left, right) => right[1] - left[1]);
  const max = Math.max(1, ...entries.map(([, count]) => count));
  return (
    <div>
      <p className="section-title mb-2">{title}</p>
      <ul className="space-y-1.5">
        {entries.map(([name, count]) => (
          <li key={name} className="flex items-center gap-2 text-sm">
            <span className="w-28 shrink-0 truncate text-neutral-700">{name}</span>
            <div className="h-2 flex-1 overflow-hidden rounded-full bg-neutral-100">
              <div className="h-full rounded-full bg-brand-400" style={{ width: `${(count / max) * 100}%` }} />
            </div>
            <span className="tabular w-10 shrink-0 text-right text-xs text-neutral-500">{formatInt(count)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function StatsSection({ stats }: { stats: CatalogueStats }) {
  return (
    <section aria-label="Catalogue statistics" className="space-y-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Products" value={formatInt(stats.products)} icon={CubeIcon} hint="every share class is its own product" />
        <Stat label="Fund families" value={formatInt(stats.families)} icon={Square3Stack3DIcon} tone="violet" hint="same fund, several classes" />
        <Stat label="Share classes" value={Object.keys(stats.share_classes).length} icon={CubeIcon} tone="orange" hint={Object.keys(stats.share_classes).join(' · ')} />
        <Stat label="Learned aliases" value={formatInt(stats.learned_aliases)} icon={AcademicCapIcon} tone="success" hint="from reviewer corrections" />
      </div>
      <Card>
        <CardBody className="grid gap-6 md:grid-cols-2">
          <Distribution title="Products by share class" data={stats.share_classes} />
          <Distribution title="Products by asset class" data={stats.asset_classes} />
        </CardBody>
      </Card>
    </section>
  );
}

function SearchSection({ stats }: { stats: CatalogueStats | undefined }) {
  const [query, setQuery] = useState('');
  const [shareClass, setShareClass] = useState<string | null>(null);
  const debounced = useDebouncedValue(query.trim(), 250);
  const search = useCatalogueSearch(debounced, 40);

  const hits = useMemo(() => (search.data ?? []).filter((hit) => !shareClass || hit.share_class === shareClass), [search.data, shareClass]);
  const classes = stats ? Object.keys(stats.share_classes) : [];
  const counts = useMemo(() => {
    const result: Record<string, number> = {};
    for (const hit of search.data ?? []) result[hit.share_class] = (result[hit.share_class] ?? 0) + 1;
    return result;
  }, [search.data]);

  return (
    <Card aria-labelledby="search-title">
      <CardHeader id="search-title" title="Search the catalogue" subtitle="Share class is part of the product: Class C and Class A are different rows" icon={MagnifyingGlassIcon} />
      <CardBody className="space-y-4">
        <div>
          <label htmlFor="catalogue-q" className="label">
            Fund name and share class
          </label>
          <div className="relative">
            <MagnifyingGlassIcon aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 h-5 w-5 -translate-y-1/2 text-neutral-400" />
            <input id="catalogue-q" type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="e.g. equity fund class C" className="input !pl-10" autoComplete="off" />
          </div>
          <p className="mt-2 flex flex-wrap items-center gap-1.5 text-xs text-neutral-500">
            Try:
            {EXAMPLES.map((example) => (
              <button key={example} type="button" onClick={() => setQuery(example)} className="rounded-full border border-neutral-300 bg-white px-2 py-0.5 text-neutral-700 hover:border-brand-400 hover:bg-brand-50">
                {example}
              </button>
            ))}
          </p>
        </div>

        {classes.length > 0 ? (
          <div role="group" aria-label="Filter by share class" className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              aria-pressed={shareClass === null}
              onClick={() => setShareClass(null)}
              className={clsx('rounded-full border px-3 py-1 text-sm font-medium', shareClass === null ? 'border-brand-500 bg-brand-500 text-white' : 'border-neutral-300 bg-white text-neutral-700 hover:bg-neutral-50')}
            >
              All classes
            </button>
            {classes.map((item) => (
              <button
                key={item}
                type="button"
                aria-pressed={shareClass === item}
                onClick={() => setShareClass(shareClass === item ? null : item)}
                className={clsx('inline-flex items-center gap-1.5 rounded-full p-0.5 transition-opacity', shareClass !== null && shareClass !== item && 'opacity-50')}
              >
                <ShareClassBadge shareClass={item} size="sm" className={clsx(shareClass === item && 'ring-4')} />
                {debounced ? <span className="tabular pr-1 text-xs text-neutral-500">{counts[item] ?? 0}</span> : null}
              </button>
            ))}
          </div>
        ) : null}

        {!debounced ? (
          <EmptyState icon={MagnifyingGlassIcon} title="Search to see products" description="Type a fund name (and a share class) to see the ranked candidates the resolver works with." className="!py-8" />
        ) : null}
        {debounced && search.isPending ? <LoadingBlock label="Searching…" className="!py-6" /> : null}
        {debounced && search.isError ? <ErrorState error={search.error} title="Search failed" onRetry={() => void search.refetch()} compact /> : null}
        {debounced && search.data && hits.length === 0 ? (
          <EmptyState icon={MagnifyingGlassIcon} title="No matching products" description={shareClass ? `Nothing in Class ${shareClass} matches “${debounced}”. Try All classes.` : `Nothing matches “${debounced}”.`} className="!py-8" />
        ) : null}
        {hits.length > 0 ? (
          <div className="overflow-x-auto rounded-lg border border-neutral-200">
            <table className="min-w-full divide-y divide-neutral-100 text-sm">
              <thead className="bg-neutral-50">
                <tr>
                  <th scope="col" className="th">Product</th>
                  <th scope="col" className="th">Share class</th>
                  <th scope="col" className="th">Asset class</th>
                  <th scope="col" className="th w-40">Match</th>
                  <th scope="col" className="th">Id</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-neutral-100 bg-white">
                {hits.map((hit) => (
                  <tr key={hit.product_id} className="hover:bg-neutral-50">
                    <td className="td font-medium text-neutral-900">{hit.fund_name}</td>
                    <td className="td">
                      <ShareClassBadge shareClass={hit.share_class} size="md" />
                    </td>
                    <td className="td text-neutral-600">{hit.asset_class}</td>
                    <td className="td">
                      <ConfidenceBar value={hit.score} label={`Match score for ${hit.display_name}`} />
                    </td>
                    <td className="td font-mono text-xs text-neutral-500">{hit.product_id}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}

function AliasesSection() {
  const aliases = useAliases();
  const reset = useResetAliases();
  const toast = useToast();
  const [confirm, setConfirm] = useState(false);

  return (
    <Card aria-labelledby="aliases-title">
      <CardHeader
        id="aliases-title"
        title="Learned aliases"
        subtitle="Created when a reviewer corrects a product pick; applied on later calls"
        icon={AcademicCapIcon}
        actions={
          <Button variant="danger-outline" size="sm" icon={TrashIcon} disabled={!aliases.data || aliases.data.length === 0} onClick={() => setConfirm(true)}>
            Reset learned aliases
          </Button>
        }
      />
      <CardBody className="!p-0">
        <QueryState
          query={aliases}
          loading={<LoadingBlock label="Loading aliases…" />}
          isEmpty={(data) => data.length === 0}
          empty={<EmptyState icon={AcademicCapIcon} title="Nothing learned yet" description="Pick a product on a Review screen: the correction becomes an alias and the same phrase resolves automatically next time." />}
        >
          {(data) => (
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-neutral-100 text-sm">
                <thead className="bg-neutral-50">
                  <tr>
                    <th scope="col" className="th">Heard as</th>
                    <th scope="col" className="th">Resolves to</th>
                    <th scope="col" className="th">Corrections</th>
                    <th scope="col" className="th">Source</th>
                    <th scope="col" className="th">Learned</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-neutral-100">
                  {data.map((alias) => (
                    <tr key={`${alias.surface}-${alias.product_id}`}>
                      <td className="td font-medium text-neutral-900">“{alias.surface}”</td>
                      <td className="td font-mono text-xs text-neutral-700">{alias.product_id}</td>
                      <td className="td">
                        <Badge tone="brand">×{alias.count}</Badge>
                      </td>
                      <td className="td text-neutral-600">{humanize(alias.source)}</td>
                      <td className="td text-neutral-600">{formatRelative(alias.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </QueryState>
      </CardBody>
      <ConfirmDialog
        open={confirm}
        title="Reset all learned aliases?"
        description="The resolver forgets every reviewer correction. Run the scoreboard before and after to see the effect."
        confirmLabel="Reset aliases"
        tone="danger"
        loading={reset.isPending}
        onCancel={() => setConfirm(false)}
        onConfirm={() =>
          reset.mutate(undefined, {
            onSuccess: (result) => {
              setConfirm(false);
              toast.success(`Reset: ${result.deleted} alias${result.deleted === 1 ? '' : 'es'} removed`);
            },
            onError: (error) => {
              setConfirm(false);
              toast.error('Could not reset the aliases', { message: getErrorMessage(error) });
            },
          })
        }
      />
    </Card>
  );
}

export default function CataloguePage() {
  useDocumentTitle('Catalogue');
  const stats = useCatalogueStats();

  return (
    <div className="space-y-6">
      <PageHeader
        title="Product catalogue"
        icon={CubeIcon}
        description="Every share class is its own product. The resolver ranks candidates from this index; below the confidence threshold the seller picks from the top three."
        actions={
          stats.data ? (
            <Badge tone={stats.data.synthetic ? 'warning' : 'success'} icon={BeakerIcon} title={stats.data.note}>
              {stats.data.synthetic ? 'Synthetic catalogue' : 'Live catalogue'}
            </Badge>
          ) : null
        }
      />
      {stats.data?.synthetic ? <p className="-mt-2 rounded-lg border border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-900">{stats.data.note}</p> : null}
      {stats.isPending ? <LoadingBlock label="Loading catalogue statistics…" /> : null}
      {stats.isError ? <ErrorState error={stats.error} title="Could not load the catalogue statistics" onRetry={() => void stats.refetch()} /> : null}
      {stats.data ? <StatsSection stats={stats.data} /> : null}
      <SearchSection stats={stats.data} />
      <AliasesSection />
    </div>
  );
}
