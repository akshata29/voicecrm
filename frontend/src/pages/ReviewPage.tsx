import { ArrowLeftIcon, ArrowPathIcon, ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import { useReducer, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { getErrorMessage } from '@/api/client';
import {
  INBOX_PARAMS,
  useApprove,
  useConfig,
  useConversation,
  useConversationList,
  usePolicies,
  useReject,
  useReprocess,
  useTaxonomy,
  useTemplates,
} from '@/api/hooks';
import type { ConversationDetail } from '@/api/types';
import { ActionCard, ApproveButton, CrmResultCard, FailedCard, RejectedCard } from '@/components/conversation/ActionCard';
import { BusinessLineChip, ChannelIcon, LanguageChip, MarketChip, StatusBadge } from '@/components/conversation/Badges';
import { CrmMatchCard, RelationshipCard } from '@/components/conversation/ContextCards';
import { NoteCard } from '@/components/conversation/NoteCard';
import { PipelineTimeline } from '@/components/conversation/PipelineViews';
import { PolicyActionsCard, RetentionCard } from '@/components/conversation/PrivacyCards';
import { ProductsCard } from '@/components/conversation/ProductsCard';
import { TasksCard } from '@/components/conversation/TasksCard';
import { TopicsCard } from '@/components/conversation/TopicsCard';
import { TranscriptPanel } from '@/components/conversation/TranscriptPanel';
import { TranscriptPolicyProvider } from '@/components/conversation/TranscriptPolicy';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardBody } from '@/components/ui/Card';
import { ErrorState, Skeleton } from '@/components/ui/StateViews';
import { useToast } from '@/components/ui/Toast';
import { formatDateTime } from '@/lib/format';
import { useDocumentTitle } from '@/lib/hooks';
import { callTypeLabel, channelLabel } from '@/lib/labels';
import { buildApproveRequest, draftReducer, EMPTY_DRAFT, netEditCount } from '@/lib/reviewDraft';

export default function ReviewPage() {
  const { id } = useParams<{ id: string }>();
  const query = useConversation(id);

  if (query.isPending) return <ReviewSkeleton />;
  if (query.isError || !query.data) {
    return (
      <div className="space-y-4">
        <BackLink />
        <ErrorState error={query.error} title="Could not load this conversation" onRetry={() => void query.refetch()} />
      </div>
    );
  }
  // Remount per conversation so the draft and click counter never leak between reviews.
  return <ReviewContent key={query.data.record.meta.conversation_id} detail={query.data} />;
}

function BackLink() {
  return (
    <Link to="/" className="inline-flex items-center gap-1 text-sm font-medium text-brand-700 hover:underline">
      <ArrowLeftIcon aria-hidden="true" className="h-4 w-4" /> Inbox
    </Link>
  );
}

function ReviewSkeleton() {
  return (
    <div role="status" aria-label="Loading conversation" className="space-y-5">
      <Skeleton className="h-5 w-24" />
      <Skeleton className="h-8 w-2/3" />
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_24rem]">
        <div className="space-y-5">
          {[0, 1, 2].map((index) => (
            <Skeleton key={index} className="h-48 w-full rounded-lg" />
          ))}
        </div>
        <div className="space-y-5">
          <Skeleton className="h-64 w-full rounded-lg" />
          <Skeleton className="h-48 w-full rounded-lg" />
        </div>
      </div>
    </div>
  );
}

function ReviewHeader({ detail }: { detail: ConversationDetail }) {
  const { record } = detail;
  const { meta } = record;
  const customer = meta.participants.find((participant) => participant.role === 'customer');
  return (
    <header className="mb-5">
      <div className="flex flex-wrap items-center gap-3">
        <BackLink />
        <StatusBadge status={record.status} size="md" />
        {record.proposal?.review.state === 'auto_approved' ? <Badge tone="violet">Auto-approved</Badge> : null}
      </div>
      <h1 className="mt-2 text-xl font-semibold leading-7 text-neutral-900 sm:text-2xl sm:leading-8">{meta.title ?? `Call with ${meta.customer_org ?? 'customer'}`}</h1>
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-2 text-sm text-neutral-600">
        <span className="inline-flex items-center gap-2">
          <ChannelIcon channel={meta.source} size="sm" />
          {channelLabel(meta.source)}
        </span>
        <MarketChip market={meta.market} />
        <BusinessLineChip value={meta.business_line} />
        <LanguageChip language={record.language ?? meta.language_hint} />
        {meta.call_type ? <Badge tone="neutral">{callTypeLabel(meta.call_type)}</Badge> : null}
        <span>{formatDateTime(meta.started_at)}</span>
        <span className="min-w-0">
          {meta.seller.display_name} → {customer?.display_name ?? 'customer'}
          {meta.customer_org ? <span className="text-neutral-400"> · {meta.customer_org}</span> : null}
        </span>
      </div>
    </header>
  );
}

function ReviewContent({ detail }: { detail: ConversationDetail }) {
  const { record } = detail;
  const proposal = record.proposal;
  const id = record.meta.conversation_id;
  const navigate = useNavigate();
  const toast = useToast();

  const config = useConfig();
  const taxonomy = useTaxonomy();
  const templates = useTemplates();
  const policies = usePolicies();
  const list = useConversationList(INBOX_PARAMS);

  const approve = useApprove(id);
  const reject = useReject(id);
  const reprocess = useReprocess(id);

  const [draft, dispatch] = useReducer(draftReducer, EMPTY_DRAFT);
  const [approveError, setApproveError] = useState<string | null>(null);

  // A reprocess swaps the proposal: drop the old edits (the click total is kept).
  const proposalKey = proposal ? `${record.version}|${proposal.template_id}|${record.engine}|${proposal.ready_at}` : 'none';
  const [seenKey, setSeenKey] = useState(proposalKey);
  if (seenKey !== proposalKey) {
    setSeenKey(proposalKey);
    dispatch({ type: 'reset' });
  }

  useDocumentTitle(record.meta.title ?? 'Review');

  const status = record.status;
  // A failed CRM write keeps its proposal and the backend accepts approve/reject/reprocess again: treat it as a retry.
  const retryable = status === 'failed' && proposal !== null;
  const reviewable = status === 'needs_review' || retryable;
  const readOnly = !reviewable;
  const mentions = proposal?.result.products ?? [];
  const pending = readOnly
    ? 0
    : mentions.filter((mention) => !draft.dropped.includes(mention.mention_id) && draft.picks[mention.mention_id] === undefined && mention.chosen_product_id === null).length;
  const originalTopics = proposal?.result.topics.map((topic) => topic.topic_id) ?? [];
  const selectedTopics = draft.topicIds ?? originalTopics;
  const template = templates.data?.find((item) => item.template_id === record.template_id);
  // The backend rebuilds the "products" and "next steps" sections from the products and tasks, so edits there would be lost.
  const lockedSections: Record<string, string> = {};
  for (const section of template?.sections ?? []) {
    if (section.crm_field === 'vcrm_productsdiscussed') lockedSections[section.key] = 'built from the products below';
    if (section.crm_field === 'vcrm_nextsteps') lockedSections[section.key] = 'built from the tasks below';
  }
  const templateName = config.data?.templates.find((item) => item.template_id === record.template_id)?.name ?? template?.name ?? record.template_id ?? 'default';
  const showRelationship = (template?.include_relationship_context ?? false) || (proposal?.result.relationship_facts.length ?? 0) > 0;
  const threshold = policies.data?.processing.product_review_threshold ?? 0.75;
  const nextId = list.data?.find((item) => item.status === 'needs_review' && item.conversation_id !== id)?.conversation_id ?? null;

  const onApprove = () => {
    setApproveError(null);
    approve.mutate(buildApproveRequest(draft), {
      onSuccess: (result) => {
        const created = result.record.crm_result?.created.length ?? 0;
        toast.success('Sent to the CRM', { message: `${created} record${created === 1 ? '' : 's'} created. The CRM holds no transcript.` });
        document.getElementById('main')?.scrollTo({ top: 0, behavior: 'smooth' });
      },
      onError: (error) => setApproveError(getErrorMessage(error)),
    });
  };

  const onReject = (reason: string | null) => {
    reject.mutate(
      { reason },
      {
        onSuccess: () => toast.info('Conversation rejected', { message: 'Nothing was written to the CRM.' }),
        onError: (error) => toast.error('Could not reject', { message: getErrorMessage(error) }),
      },
    );
  };

  const onReprocess = (templateId: string | null, engine: 'rules' | 'llm' | 'hybrid' | null) => {
    dispatch({ type: 'click' });
    reprocess.mutate(
      { template_id: templateId, engine },
      { onError: (error) => toast.error('Could not re-run extraction', { message: getErrorMessage(error) }) },
    );
  };

  const goInbox = () => navigate('/');
  const goNext = () => navigate(nextId ? `/conversations/${nextId}` : '/');

  const statusPanel = (() => {
    if (reviewable) {
      return (
        <ActionCard
          detail={detail}
          retryError={retryable ? (record.error ?? 'The CRM write failed.') : null}
          pendingPicks={pending}
          editCount={netEditCount(draft)}
          clickCount={draft.actions}
          onApprove={onApprove}
          approving={approve.isPending}
          approveError={approveError}
          onReject={onReject}
          rejecting={reject.isPending}
          templates={config.data?.templates ?? []}
          onReprocess={onReprocess}
          reprocessing={reprocess.isPending}
        />
      );
    }
    if (status === 'written' || status === 'approved') {
      return <CrmResultCard detail={detail} nextId={nextId} onNext={goNext} onInbox={goInbox} onViewCrm={() => navigate('/crm')} />;
    }
    if (status === 'rejected') return <RejectedCard detail={detail} onInbox={goInbox} />;
    if (status === 'failed') return <FailedCard detail={detail} onInbox={goInbox} />;
    return (
      <Card>
        <CardBody className="flex items-start gap-3">
          <ArrowPathIcon aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0 animate-spin text-brand-500" />
          <div>
            <p className="text-sm font-semibold text-neutral-900">{status === 'received' ? 'Received: starting the pipeline' : 'Processing the conversation'}</p>
            <p className="mt-0.5 text-sm text-neutral-600">The proposal appears here as soon as extraction and validation are done. This page updates by itself.</p>
          </div>
        </CardBody>
      </Card>
    );
  })();

  return (
    <TranscriptPolicyProvider available={detail.transcript_available} note={detail.transcript_note}>
      <div className="pb-32 lg:pb-0">
        <ReviewHeader detail={detail} />

        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_24rem] xl:grid-cols-[minmax(0,1fr)_26rem]">
          <div className="min-w-0 space-y-5" key={proposalKey}>
            {proposal ? (
              <>
                {pending > 0 ? (
                  <div role="status" className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3">
                    <p className="flex items-center gap-2 text-sm font-semibold text-amber-900">
                      <ExclamationTriangleIcon aria-hidden="true" className="h-5 w-5 shrink-0 text-amber-600" />
                      {pending} product {pending === 1 ? 'pick is' : 'picks are'} needed before this can go to the CRM
                    </p>
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => {
                        const target = mentions.find((mention) => !draft.dropped.includes(mention.mention_id) && draft.picks[mention.mention_id] === undefined && mention.chosen_product_id === null);
                        if (target) document.getElementById(`mention-${target.mention_id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
                      }}
                    >
                      Go to the pick
                    </Button>
                  </div>
                ) : null}
                <NoteCard
                  sections={proposal.result.sections}
                  markdown={proposal.note_markdown}
                  templateName={templateName}
                  flags={proposal.result.flags}
                  edits={draft.sectionEdits}
                  editable={!readOnly}
                  locked={lockedSections}
                  onEdit={(key, text) => dispatch({ type: 'section', key, text })}
                />
                <ProductsCard
                  mentions={mentions}
                  picks={draft.picks}
                  dropped={draft.dropped}
                  readOnly={readOnly}
                  threshold={threshold}
                  onPick={(mentionId, productId) =>
                    dispatch({ type: 'pick', mentionId, productId, original: mentions.find((mention) => mention.mention_id === mentionId)?.chosen_product_id ?? null })
                  }
                  onToggleDrop={(mentionId) => dispatch({ type: 'toggleDrop', mentionId })}
                />
                <TopicsCard
                  tags={proposal.result.topics}
                  selected={selectedTopics}
                  taxonomy={taxonomy.data?.topics}
                  businessLine={record.meta.business_line}
                  readOnly={readOnly}
                  onChange={(ids) => dispatch({ type: 'topics', ids, original: originalTopics })}
                />
                <TasksCard
                  tasks={proposal.result.tasks}
                  edits={draft.taskEdits}
                  readOnly={readOnly}
                  onEdit={(taskId, patch) => {
                    const original = proposal.result.tasks.find((task) => task.task_id === taskId);
                    if (original) dispatch({ type: 'task', taskId, patch, original });
                  }}
                />
                {showRelationship ? <RelationshipCard facts={proposal.result.relationship_facts} /> : null}
                <CrmMatchCard match={proposal.crm_match} />
              </>
            ) : status === 'failed' || status === 'rejected' ? (
              <Card>
                <CardBody>
                  <p className="text-sm text-neutral-600">No proposal is available for this conversation.</p>
                </CardBody>
              </Card>
            ) : (
              [0, 1, 2].map((index) => <Skeleton key={index} className="h-44 w-full rounded-lg" />)
            )}
          </div>

          <aside aria-label="Pipeline, privacy and retention" className="min-w-0 space-y-5 lg:sticky lg:top-4 lg:max-h-[calc(100dvh-6.5rem)] lg:self-start lg:overflow-y-auto lg:overscroll-contain lg:pr-1">
            {statusPanel}
            <PipelineTimeline detail={detail} />
            {proposal ? <PolicyActionsCard proposal={proposal} /> : null}
            {record.retention || status !== 'failed' ? (
              <RetentionCard
                retention={record.retention}
                transcriptNote={detail.transcript_note}
                market={config.data?.markets.find((item) => item.market === record.meta.market)}
              />
            ) : null}
            <TranscriptPanel
              conversationId={id}
              revision={`${record.status}|${record.updated_at}`}
              available={detail.transcript_available}
              note={detail.transcript_note}
              policyActions={proposal?.policy_actions ?? []}
            />
          </aside>
        </div>

        {reviewable ? (
          <div className="fixed inset-x-0 bottom-0 z-40 border-t border-neutral-200 bg-white/95 px-3 pt-3 pb-safe shadow-[0_-4px_12px_rgba(0,0,0,0.08)] backdrop-blur lg:hidden">
            <div className="mx-auto flex max-w-2xl items-center gap-3">
              <div className="shrink-0 text-center leading-tight" title="Clicks to approve (the approve click is included)">
                <p className="tabular text-lg font-semibold text-neutral-900">{draft.actions + 1}</p>
                <p className="text-[11px] text-neutral-500">{draft.actions === 0 ? 'click' : 'clicks'}</p>
              </div>
              <ApproveButton pendingPicks={pending} approving={approve.isPending} onApprove={onApprove} retry={retryable} size="md" className="flex-1" />
            </div>
            {pending > 0 ? (
              <p className="mt-1.5 text-center text-xs text-amber-800">
                Pick {pending} product{pending === 1 ? '' : 's'} to continue
              </p>
            ) : null}
            {approveError ? (
              <p role="alert" className="mt-1.5 text-center text-xs text-red-700">
                {approveError}
              </p>
            ) : null}
          </div>
        ) : null}
        {status !== 'needs_review' && nextId === null && status === 'written' ? (
          <p className="mt-6 text-center text-sm text-neutral-500">
            Inbox zero: nothing else needs review. <Button variant="ghost" size="sm" onClick={goInbox}>Back to inbox</Button>
          </p>
        ) : null}
      </div>
    </TranscriptPolicyProvider>
  );
}
