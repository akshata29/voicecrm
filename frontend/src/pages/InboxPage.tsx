import {
  ArrowPathIcon,
  ClipboardDocumentCheckIcon,
  CubeIcon,
  InboxIcon,
  MicrophoneIcon,
  SignalIcon,
  TagIcon,
} from '@heroicons/react/24/outline';
import { CheckIcon } from '@heroicons/react/24/solid';
import { useQueryClient } from '@tanstack/react-query';
import clsx from 'clsx';
import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { getErrorMessage } from '@/api/client';
import { INBOX_PARAMS, qk, useApproveAny, useConversation, useConversationList, useMetricsSummary } from '@/api/hooks';
import type { ConversationListItem, ConversationStatus } from '@/api/types';
import { BusinessLineChip, ChannelIcon, EstCost, LanguageChip, MarketChip, StatusBadge } from '@/components/conversation/Badges';
import { PipelineLoadCard } from '@/components/conversation/PipelineLoadCard';
import { PipelineStepper } from '@/components/conversation/PipelineViews';
import { LoadTestBar } from '@/components/live/LoadTestBar';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { PageHeader } from '@/components/ui/Field';
import { EmptyState, ErrorState, ListSkeleton } from '@/components/ui/StateViews';
import { Stat } from '@/components/ui/Stat';
import { useToast } from '@/components/ui/Toast';
import { formatDurationSeconds, formatRelative } from '@/lib/format';
import { useDocumentTitle, useNow } from '@/lib/hooks';
import { hasPendingProductPick, reasonLabel } from '@/lib/labels';

type Filter = 'all' | 'review' | 'processing' | 'done' | 'other';

const GROUP_ORDER: Record<ConversationStatus, number> = {
  needs_review: 0,
  processing: 1,
  received: 1,
  approved: 2,
  written: 3,
  failed: 4,
  rejected: 5,
};

function filterOf(status: ConversationStatus): Exclude<Filter, 'all'> {
  if (status === 'needs_review') return 'review';
  if (status === 'processing' || status === 'received') return 'processing';
  if (status === 'written' || status === 'approved') return 'done';
  return 'other';
}

function Count({ icon: Icon, value, label }: { icon: typeof CubeIcon; value: number; label: string }) {
  return (
    <span className="inline-flex items-center gap-1 text-sm text-neutral-700" title={`${value} ${label}`}>
      <Icon aria-hidden="true" className="h-4 w-4 text-neutral-400" />
      <span className="tabular font-medium">{value}</span>
      <span className="sr-only">{label}</span>
    </span>
  );
}

function ConversationRow({ item }: { item: ConversationListItem }) {
  const toast = useToast();
  const client = useQueryClient();
  const approve = useApproveAny();
  const now = useNow(30_000);
  const live = item.status === 'received' || item.status === 'processing';
  const detail = useConversation(live ? item.conversation_id : undefined);
  const liveStatus = detail.data?.record.status;

  useEffect(() => {
    if (live && liveStatus && liveStatus !== item.status) void client.invalidateQueries({ queryKey: qk.conversations });
  }, [live, liveStatus, item.status, client]);

  const needsPick = item.needs_review && hasPendingProductPick(item.review_reasons);
  const canApprove = item.status === 'needs_review' && !needsPick;
  const title = item.title ?? `Call with ${item.customer_org ?? 'customer'}`;
  const href = `/conversations/${item.conversation_id}`;

  const onApprove = () => {
    approve.mutate(
      { id: item.conversation_id, body: {} },
      {
        onSuccess: (result) => {
          const created = result.record.crm_result?.created.length ?? 0;
          toast.success('Approved and sent to the CRM', { message: `${title}: ${created} record${created === 1 ? '' : 's'} created`, action: { label: 'Open the record', to: href } });
        },
        onError: (error) => toast.error('Could not approve', { message: getErrorMessage(error) }),
      },
    );
  };

  return (
    <li className={clsx('card relative p-4 transition-shadow hover:shadow-pop', item.status === 'needs_review' && 'border-l-4 border-l-amber-400')}>
      <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center lg:grid-cols-[minmax(0,1fr)_15rem_13rem]">
        <div className="flex min-w-0 items-start gap-3">
          <ChannelIcon channel={item.source} />
          <div className="min-w-0 flex-1">
            <h3 className="truncate text-sm font-semibold text-neutral-900">
              <Link to={href} className="rounded after:absolute after:inset-0 after:content-[''] focus-visible:after:rounded-lg">
                {title}
              </Link>
            </h3>
            <p className="mt-0.5 truncate text-sm text-neutral-600">
              {item.customer_org ?? 'Unknown customer'} · {item.seller_name} · <span title={item.started_at}>{formatRelative(item.started_at, now)}</span>
            </p>
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <MarketChip market={item.market} />
              <BusinessLineChip value={item.business_line} />
              <LanguageChip language={item.language} />
              {item.template_id ? <Badge tone="neutral">{item.template_id === 'retail_advisor' ? 'Retail template' : 'Institutional template'}</Badge> : null}
              {item.fixture_id ? (
                <Badge tone="neutral" title={`Scripted call: ${item.fixture_id}`}>
                  Demo call
                </Badge>
              ) : null}
              {item.batch_id ? (
                <Badge tone="violet" title="Created by a load test on the Live demo page, which can remove it again">
                  Load test
                </Badge>
              ) : null}
            </div>
            {item.status === 'needs_review' ? (
              <p className="mt-2 flex flex-wrap items-center gap-1.5">
                {item.review_reasons.length > 0 ? (
                  item.review_reasons.map((reason) => (
                    <Badge key={reason} tone="warning">
                      {reasonLabel(reason)}
                    </Badge>
                  ))
                ) : (
                  <Badge tone="success">Ready: one click to approve</Badge>
                )}
              </p>
            ) : null}
          </div>
        </div>

        <div className="min-w-0 sm:col-span-1 lg:col-span-1">
          {live ? (
            <PipelineStepper stages={detail.data?.stages ?? []} />
          ) : (
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
              <Count icon={CubeIcon} value={item.product_count} label="products" />
              <Count icon={TagIcon} value={item.topic_count} label="topics" />
              <Count icon={ClipboardDocumentCheckIcon} value={item.task_count} label="tasks" />
              <span className="text-xs text-neutral-500" title="Ingest to review-ready">
                {item.latency_seconds === null ? '' : formatDurationSeconds(item.latency_seconds)}
              </span>
              {item.cost_usd !== null ? <EstCost value={item.cost_usd} className="text-xs text-neutral-500" /> : null}
            </div>
          )}
          {item.status === 'failed' ? <p className="mt-1 text-xs text-red-700">Processing failed. Open for details.</p> : null}
        </div>

        <div className="relative z-10 flex flex-wrap items-center gap-2 sm:col-span-2 lg:col-span-1 lg:justify-end">
          <StatusBadge status={item.status} />
          {canApprove ? (
            <Button variant="primary" size="sm" icon={CheckIcon} loading={approve.isPending} onClick={onApprove} aria-label={`Approve ${title}`}>
              Approve
            </Button>
          ) : null}
          {needsPick ? (
            <Link to={href} className="btn btn-secondary btn-sm relative z-10">
              Pick product
            </Link>
          ) : null}
        </div>
      </div>
    </li>
  );
}

const FILTERS: Array<{ id: Filter; label: string }> = [
  { id: 'all', label: 'All' },
  { id: 'review', label: 'Needs review' },
  { id: 'processing', label: 'Processing' },
  { id: 'done', label: 'Written' },
  { id: 'other', label: 'Rejected / failed' },
];

export default function InboxPage() {
  useDocumentTitle('Inbox');
  const list = useConversationList(INBOX_PARAMS);
  const metrics = useMetricsSummary();
  const [filter, setFilter] = useState<Filter>('all');

  const items = list.data;
  const counts = useMemo(() => {
    const result: Record<Filter, number> = { all: 0, review: 0, processing: 0, done: 0, other: 0 };
    for (const item of items ?? []) {
      result.all += 1;
      result[filterOf(item.status)] += 1;
    }
    return result;
  }, [items]);

  const sorted = useMemo(() => {
    const visible = (items ?? []).filter((item) => filter === 'all' || filterOf(item.status) === filter);
    return [...visible].sort((left, right) => GROUP_ORDER[left.status] - GROUP_ORDER[right.status] || Date.parse(right.created_at) - Date.parse(left.created_at));
  }, [items, filter]);

  const groups = useMemo(() => {
    const result: Array<{ key: string; title: string; items: ConversationListItem[] }> = [];
    const push = (key: string, title: string, predicate: (item: ConversationListItem) => boolean) => {
      const matching = sorted.filter(predicate);
      if (matching.length > 0) result.push({ key, title, items: matching });
    };
    push('review', 'Needs your review', (item) => item.status === 'needs_review');
    push('processing', 'Processing now', (item) => item.status === 'processing' || item.status === 'received');
    push('done', 'Written to the CRM', (item) => item.status === 'written' || item.status === 'approved');
    push('other', 'Rejected or failed', (item) => item.status === 'rejected' || item.status === 'failed');
    return result;
  }, [sorted]);

  const m = metrics.data;
  // The list shows the newest 100; during a bulk run the summary has the true totals.
  const reviewTotal = m ? m.needs_review : counts.review;
  const processingTotal = m ? (m.by_status.processing ?? 0) + (m.by_status.received ?? 0) : counts.processing;
  const truncated = m && items ? m.conversations > items.length : false;

  return (
    <div>
      <PageHeader
        title="Review inbox"
        icon={InboxIcon}
        description="Every conversation becomes a governed, CRM-ready proposal. Approve it with one click; sellers only step in where the system is unsure."
        actions={
          <>
            <Link to="/sources" className="btn btn-secondary">
              <SignalIcon aria-hidden="true" className="h-4 w-4" /> Simulate a call
            </Link>
            <Link to="/capture" className="btn btn-secondary">
              <MicrophoneIcon aria-hidden="true" className="h-4 w-4" /> Record in person
            </Link>
          </>
        }
      />

      <LoadTestBar />
      <PipelineLoadCard />

      <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
        <Stat label="Needs review" value={reviewTotal} tone="warning" icon={InboxIcon} hint="waiting for a seller" />
        <Stat label="Processing" value={processingTotal} tone="info" icon={ArrowPathIcon} hint="live pipeline" />
        <Stat label="Written to CRM" value={m ? m.written : counts.done} tone="success" icon={CheckIcon} hint={m && m.auto_approved > 0 ? `${m.auto_approved} auto-approved` : undefined} />
        <Stat label="Ready in (median)" value={m?.latency_p50_seconds != null ? formatDurationSeconds(m.latency_p50_seconds) : '—'} hint="ingest to review-ready" />
        <Stat label="Clicks to approve" value={m?.mean_clicks_to_approve != null ? m.mean_clicks_to_approve.toFixed(1) : '—'} hint={m?.mean_seconds_to_review != null ? `${formatDurationSeconds(m.mean_seconds_to_review)} to review` : 'mean per approval'} />
      </div>

      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div role="group" aria-label="Filter conversations" className="flex flex-wrap gap-2">
          {FILTERS.map((option) => (
            <button
              key={option.id}
              type="button"
              aria-pressed={filter === option.id}
              onClick={() => setFilter(option.id)}
              className={clsx(
                'inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm font-medium transition-colors',
                filter === option.id ? 'border-brand-500 bg-brand-500 text-white' : 'border-neutral-300 bg-white text-neutral-700 hover:bg-neutral-50',
              )}
            >
              {option.label}
              <span className={clsx('tabular rounded-full px-1.5 text-xs', filter === option.id ? 'bg-white/25 text-white' : 'bg-neutral-100 text-neutral-600')}>{counts[option.id]}</span>
            </button>
          ))}
        </div>
        <p className="flex items-center gap-1.5 text-xs text-neutral-500" aria-live="off">
          <span aria-hidden="true" className={clsx('h-2 w-2 rounded-full', list.isFetching ? 'animate-pulse-soft bg-brand-500' : 'bg-emerald-500')} />
          Live: refreshes every 2 s{truncated && m && items ? `, showing the newest ${items.length} of ${m.conversations}` : ''}
        </p>
      </div>

      {list.isPending ? <ListSkeleton rows={5} /> : null}
      {list.isError && !items ? <ErrorState error={list.error} title="Could not load the inbox" onRetry={() => void list.refetch()} /> : null}
      {items && items.length === 0 ? (
        <div className="card">
          <EmptyState
            title="No conversations yet"
            description="Run a scripted call from Sources, or record a conversation on your phone, and it will show up here within seconds."
            action={
              <Link to="/sources" className="btn btn-primary">
                Simulate a call
              </Link>
            }
          />
        </div>
      ) : null}
      {items && items.length > 0 && sorted.length === 0 ? (
        <div className="card">
          <EmptyState title="Nothing in this view" description="No conversations match the selected filter." action={<Button onClick={() => setFilter('all')}>Show all</Button>} />
        </div>
      ) : null}

      <div className="space-y-6">
        {groups.map((group) => (
          <section key={group.key} aria-labelledby={`group-${group.key}`}>
            <h2 id={`group-${group.key}`} className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-neutral-500">
              {group.title}
              <span className="tabular rounded-full bg-neutral-200 px-1.5 text-neutral-700">{group.items.length}</span>
            </h2>
            <ul className="space-y-3">
              {group.items.map((item) => (
                <ConversationRow key={item.conversation_id} item={item} />
              ))}
            </ul>
          </section>
        ))}
      </div>
    </div>
  );
}
