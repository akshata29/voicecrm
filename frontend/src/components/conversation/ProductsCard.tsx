import { ArrowUturnLeftIcon, CubeIcon, ExclamationTriangleIcon, MagnifyingGlassIcon, PencilSquareIcon, XMarkIcon } from '@heroicons/react/24/outline';
import { CheckCircleIcon } from '@heroicons/react/24/solid';
import clsx from 'clsx';
import { useState } from 'react';
import { useCatalogueSearch } from '@/api/hooks';
import type { ProductCandidate, ProductMention } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { ConfidenceBar } from '@/components/ui/ConfidenceBar';
import { humanize } from '@/lib/format';
import { useDebouncedValue } from '@/lib/hooks';
import { MENTION_TYPE_LABELS, reasonLabel } from '@/lib/labels';
import { ShareClassBadge, shareClassText } from './Badges';
import { EvidenceQuotes } from './TranscriptPolicy';

interface MentionCardProps {
  mention: ProductMention;
  pickedId: string | undefined;
  dropped: boolean;
  readOnly: boolean;
  threshold: number;
  onPick: (mentionId: string, productId: string) => void;
  onToggleDrop: (mentionId: string) => void;
}

interface CandidateOptionProps {
  candidate: ProductCandidate;
  mentionId: string;
  checked: boolean;
  threshold: number;
  onPick: () => void;
  /** Radios of one mention share a name; catalogue results use their own so a product listed twice does not fight over the checked state. */
  groupName?: string;
  /** Catalogue-search results carry a relevance number that is not a 0..1 confidence, so it is not drawn as a bar. */
  showScore?: boolean;
  note?: string;
}

function CandidateOption({ candidate, mentionId, checked, threshold, onPick, groupName, showScore = true, note }: CandidateOptionProps) {
  return (
    <label
      className={clsx(
        'flex cursor-pointer flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border p-3 transition-colors focus-within:ring-2 focus-within:ring-brand-500 sm:flex-nowrap',
        checked ? 'border-brand-500 bg-brand-50 ring-1 ring-brand-500' : 'border-neutral-200 bg-white hover:border-neutral-300 hover:bg-neutral-50',
      )}
    >
      <input type="radio" name={groupName ?? `pick-${mentionId}`} value={candidate.product_id} checked={checked} onChange={onPick} className="h-4 w-4 shrink-0 accent-brand-600 focus:outline-none" />
      <ShareClassBadge shareClass={candidate.share_class} size="lg" className="min-w-[6.5rem]" />
      <div className="min-w-0 flex-1 basis-40">
        <p className="text-sm font-semibold leading-5 text-neutral-900">{candidate.fund_name}</p>
        <p className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-neutral-500">
          <span className="font-mono">{candidate.product_id}</span>
          {candidate.matched_on.length > 0 ? <span>matched on {candidate.matched_on.map((item) => humanize(item).toLowerCase()).join(', ')}</span> : null}
          {note ? <span>{note}</span> : null}
        </p>
      </div>
      {showScore ? (
        <div className="w-24 shrink-0 sm:w-28">
          <ConfidenceBar value={candidate.score} threshold={threshold} label={`Match score for ${candidate.display_name}`} />
        </div>
      ) : null}
    </label>
  );
}

/** Lets the reviewer pick any product from the catalogue when the proposed top-3 does not contain it (or is empty). */
function CatalogueSearchPick({ mentionId, surface, chosenId, threshold, defaultOpen, onChoose }: { mentionId: string; surface: string; chosenId: string | null; threshold: number; defaultOpen: boolean; onChoose: (candidate: ProductCandidate) => void }) {
  const [query, setQuery] = useState('');
  const debounced = useDebouncedValue(query.trim(), 250);
  const search = useCatalogueSearch(debounced, 6);
  const inputId = `catalogue-search-${mentionId}`;

  return (
    <details open={defaultOpen} className="rounded-lg border border-dashed border-neutral-300 bg-neutral-50/60 text-sm">
      <summary className="cursor-pointer select-none rounded-lg px-3 py-2 font-medium text-neutral-700 hover:text-neutral-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500">
        Not listed? Search the catalogue
      </summary>
      <div className="space-y-2 border-t border-neutral-200 px-3 py-3">
        <label htmlFor={inputId} className="sr-only">
          Search the product catalogue for “{surface}”
        </label>
        <div className="relative">
          <MagnifyingGlassIcon aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-neutral-400" />
          <input
            id={inputId}
            type="search"
            value={query}
            autoComplete="off"
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Current fund name and share class, e.g. “equity fund class C”"
            className="input !pl-9"
          />
        </div>
        {debounced === '' ? (
          <p className="text-xs text-neutral-500">Type the fund's current name. Your pick teaches the resolver this phrase when the speaker named the share class.</p>
        ) : search.isError ? (
          <p role="alert" className="text-xs text-red-700">
            The catalogue search failed. Try again.
          </p>
        ) : search.isPending ? (
          <p className="text-xs text-neutral-500">Searching…</p>
        ) : search.data.length === 0 ? (
          <p className="text-xs text-neutral-500">No products match “{debounced}”.</p>
        ) : (
          <div className="space-y-2" role="radiogroup" aria-label={`Catalogue results for “${surface}”`}>
            {search.data.map((hit) => {
              const candidate: ProductCandidate = {
                product_id: hit.product_id,
                display_name: hit.display_name,
                fund_name: hit.fund_name,
                share_class: hit.share_class,
                score: hit.score,
                matched_on: ['catalogue_search'],
              };
              return (
                <CandidateOption
                  key={hit.product_id}
                  candidate={candidate}
                  mentionId={mentionId}
                  groupName={`search-${mentionId}`}
                  checked={chosenId === hit.product_id}
                  threshold={threshold}
                  showScore={false}
                  note={hit.asset_class}
                  onPick={() => onChoose(candidate)}
                />
              );
            })}
          </div>
        )}
      </div>
    </details>
  );
}

function MentionCard({ mention, pickedId, dropped, readOnly, threshold, onPick, onToggleDrop }: MentionCardProps) {
  const [changing, setChanging] = useState(false);
  // A product the reviewer found through the catalogue search (it is not one of the proposed candidates).
  const [searched, setSearched] = useState<ProductCandidate | null>(null);
  const candidates = searched && !mention.candidates.some((candidate) => candidate.product_id === searched.product_id) ? [searched, ...mention.candidates] : mention.candidates;
  const chosenId = pickedId ?? mention.chosen_product_id;
  const chosen = candidates.find((candidate) => candidate.product_id === chosenId);
  // The engine could not decide on its own (a pick is required), or it decided but is not sure (a confirmation is wanted).
  // In both cases the pick-list stays on screen so the reviewer can see and re-check the choice.
  const undecided = mention.chosen_product_id === null;
  const flagged = mention.needs_review && !undecided;
  const picked = pickedId !== undefined;
  const needsPick = !readOnly && chosenId === null;
  const showList = !readOnly && !dropped && (undecided || flagged || changing);
  const corrected = picked && !undecided && pickedId !== mention.chosen_product_id;
  // The backend only learns an alias from a phrase that actually named a share class.
  const learnsPhrase = !mention.reasons.includes('share_class_not_stated') && pickedId !== mention.chosen_product_id;

  if (dropped) {
    return (
      <li className="flex items-center justify-between gap-3 rounded-lg border border-dashed border-neutral-300 bg-neutral-50 px-4 py-3 text-sm text-neutral-500">
        <span>
          <span className="line-through">“{mention.surface}”</span> will not be added to the record
        </span>
        <Button size="sm" variant="ghost" icon={ArrowUturnLeftIcon} onClick={() => onToggleDrop(mention.mention_id)}>
          Undo
        </Button>
      </li>
    );
  }

  return (
    <li id={`mention-${mention.mention_id}`} className={clsx('scroll-mt-4 rounded-lg border bg-white', needsPick ? 'border-amber-400 ring-1 ring-amber-300' : 'border-neutral-200')}>
      <div className="flex flex-wrap items-start justify-between gap-2 border-b border-neutral-100 px-4 py-3">
        <div className="min-w-0">
          <p className="text-xs font-medium uppercase tracking-wide text-neutral-400">
            Heard as <span className="ml-1 font-mono normal-case">{mention.mention_id}</span>
          </p>
          <p className="text-sm font-semibold text-neutral-900">“{mention.surface}”</p>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge tone="neutral">{MENTION_TYPE_LABELS[mention.mention_type]}</Badge>
          {mention.share_class_stated ? (
            <Badge tone="brand" title="The speaker said this share class out loud">
              Stated: {mention.share_class_stated === 'Advisor' ? 'Advisor' : `Class ${mention.share_class_stated}`}
            </Badge>
          ) : (
            <Badge tone="warning">Share class not stated</Badge>
          )}
          {!readOnly ? (
            <button
              type="button"
              onClick={() => onToggleDrop(mention.mention_id)}
              className="rounded p-1 text-neutral-400 hover:bg-neutral-100 hover:text-red-600"
              aria-label={`Remove mention “${mention.surface}” from the record`}
              title="Not a product mention: remove"
            >
              <XMarkIcon aria-hidden="true" className="h-4 w-4" />
            </button>
          ) : null}
        </div>
      </div>

      <div className="space-y-3 px-4 py-3">
        {needsPick ? (
          <div className="flex items-start gap-2 rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-900" role="status">
            <ExclamationTriangleIcon aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0 text-amber-600" />
            <div>
              <p className="font-semibold">Your pick is needed: the exact share class matters.</p>
              {mention.reasons.length > 0 ? <p className="text-xs text-amber-800">{mention.reasons.map(reasonLabel).join(' · ')}</p> : null}
              {mention.candidates.length === 0 ? <p className="text-xs text-amber-800">Nothing in the catalogue matched this name. Search for the current product below, or remove the mention.</p> : null}
            </div>
          </div>
        ) : !readOnly && flagged && !picked ? (
          <div className="flex items-start gap-2 rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-900" role="status">
            <ExclamationTriangleIcon aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0 text-amber-600" />
            <div>
              <p className="font-semibold">
                Please check the share class: the match is only {Math.round(mention.confidence * 100)}% sure. Approving keeps the pre-selected choice.
              </p>
              {mention.reasons.length > 0 ? <p className="text-xs text-amber-800">{mention.reasons.map(reasonLabel).join(' · ')}</p> : null}
            </div>
          </div>
        ) : !readOnly && (undecided || flagged) ? (
          <p className="flex items-start gap-2 rounded-md bg-emerald-50 px-3 py-2 text-sm text-emerald-900" role="status">
            <CheckCircleIcon aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0 text-emerald-600" />
            <span>
              <strong>Picked: {chosen ? `${chosen.fund_name}, ${shareClassText(chosen.share_class)}` : chosenId}.</strong>{' '}
              {learnsPhrase ? 'The resolver will learn this phrase for next time.' : 'Recorded as your confirmed choice for this call.'}
            </span>
          </p>
        ) : (
          <div className="flex flex-wrap items-center gap-3">
            <CheckCircleIcon aria-hidden="true" className="h-5 w-5 shrink-0 text-emerald-500" />
            {chosen ? <ShareClassBadge shareClass={chosen.share_class} size="lg" /> : null}
            <div className="min-w-0 flex-1 basis-48">
              <p className="text-sm font-semibold text-neutral-900">{chosen?.fund_name ?? chosenId}</p>
              <p className="text-xs text-neutral-500">
                <span className="font-mono">{chosenId}</span>
                {corrected ? <span className="ml-2 font-semibold text-amber-800">{learnsPhrase ? 'corrected by you · the resolver will learn this' : 'corrected by you'}</span> : null}
                {picked && !undecided && !corrected ? <span className="ml-2 font-semibold text-emerald-700">confirmed by you</span> : null}
              </p>
            </div>
            <div className="w-32 shrink-0">
              <ConfidenceBar value={picked ? 1 : mention.confidence} threshold={threshold} label={`Confidence for ${mention.surface}`} />
            </div>
            {!readOnly && !changing && !undecided ? (
              <Button size="sm" variant="ghost" icon={PencilSquareIcon} onClick={() => setChanging(true)}>
                Change
              </Button>
            ) : null}
          </div>
        )}

        {showList ? (
          <fieldset>
            <legend className="mb-2 text-xs font-semibold text-neutral-600">
              {needsPick
                ? 'Pick the exact product (top candidates, best first)'
                : flagged && !picked
                  ? 'Confirm the product, or choose another (top candidates, best first)'
                  : undecided || flagged
                    ? 'Your pick (choose another to change it)'
                    : 'Choose a different product'}
            </legend>
            {candidates.length === 0 ? (
              <p className="rounded-lg border border-neutral-200 bg-neutral-50 px-3 py-2 text-xs text-neutral-600">No candidates were proposed for this phrase.</p>
            ) : (
              <div className="space-y-2" role="radiogroup" aria-label={`Candidates for “${mention.surface}”`}>
                {candidates.map((candidate) => (
                  <CandidateOption
                    key={candidate.product_id}
                    candidate={candidate}
                    mentionId={mention.mention_id}
                    checked={chosenId === candidate.product_id}
                    threshold={threshold}
                    note={searched?.product_id === candidate.product_id ? 'found in the catalogue' : undefined}
                    onPick={() => {
                      onPick(mention.mention_id, candidate.product_id);
                      setChanging(false);
                    }}
                  />
                ))}
              </div>
            )}
            <div className="mt-2">
              <CatalogueSearchPick
                mentionId={mention.mention_id}
                surface={mention.surface}
                chosenId={chosenId}
                threshold={threshold}
                defaultOpen={mention.candidates.length === 0}
                onChoose={(candidate) => {
                  setSearched(candidate);
                  onPick(mention.mention_id, candidate.product_id);
                  setChanging(false);
                }}
              />
            </div>
            {undecided || flagged ? (
              <button
                type="button"
                onClick={() => onToggleDrop(mention.mention_id)}
                className="mt-2 text-xs font-medium text-neutral-500 underline-offset-2 hover:text-red-700 hover:underline"
              >
                None of these: this is not a product mention
              </button>
            ) : (
              <button type="button" onClick={() => setChanging(false)} className="mt-2 text-xs font-medium text-neutral-500 hover:text-neutral-800">
                Keep the original choice
              </button>
            )}
          </fieldset>
        ) : null}

        <EvidenceQuotes evidence={mention.evidence} />
      </div>
    </li>
  );
}

interface ProductsCardProps {
  mentions: ProductMention[];
  picks: Record<string, string>;
  dropped: string[];
  readOnly: boolean;
  threshold: number;
  onPick: (mentionId: string, productId: string) => void;
  onToggleDrop: (mentionId: string) => void;
}

export function ProductsCard({ mentions, picks, dropped, readOnly, threshold, onPick, onToggleDrop }: ProductsCardProps) {
  const pending = readOnly ? 0 : mentions.filter((mention) => !dropped.includes(mention.mention_id) && picks[mention.mention_id] === undefined && mention.chosen_product_id === null).length;
  const toConfirm = readOnly
    ? 0
    : mentions.filter((mention) => !dropped.includes(mention.mention_id) && picks[mention.mention_id] === undefined && mention.chosen_product_id !== null && mention.needs_review).length;
  return (
    <Card aria-labelledby="products-title">
      <CardHeader
        id="products-title"
        title="Products discussed"
        subtitle="Exact product and share class, as the CRM will record them"
        icon={CubeIcon}
        actions={
          pending > 0 ? (
            <Badge tone="warning" icon={ExclamationTriangleIcon}>
              {pending} need{pending === 1 ? 's' : ''} your pick
            </Badge>
          ) : toConfirm > 0 ? (
            <Badge tone="warning" icon={ExclamationTriangleIcon}>
              {toConfirm} to confirm
            </Badge>
          ) : (
            <Badge tone="success">{mentions.length - dropped.length} resolved</Badge>
          )
        }
      />
      <CardBody>
        {mentions.length === 0 ? (
          <p className="text-sm text-neutral-500">No products were mentioned in this conversation.</p>
        ) : (
          <ul className="space-y-3">
            {mentions.map((mention) => (
              <MentionCard
                key={mention.mention_id}
                mention={mention}
                pickedId={picks[mention.mention_id]}
                dropped={dropped.includes(mention.mention_id)}
                readOnly={readOnly}
                threshold={threshold}
                onPick={onPick}
                onToggleDrop={onToggleDrop}
              />
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}
