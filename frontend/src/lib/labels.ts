/** Human-readable labels for the enums and data-driven keys the API returns. */
import type { Channel, ConversationStatus, MentionType, PolicyActionKind, Stage, StageStatus } from '@/api/types';
import { humanize } from './format';

export const CHANNEL_LABELS: Record<Channel, string> = {
  teams: 'Microsoft Teams',
  zoom: 'Zoom',
  genesys: 'Genesys Cloud',
  mobile: 'Mobile / in person',
  upload: 'Upload',
};

export const CHANNEL_SHORT: Record<Channel, string> = {
  teams: 'Teams',
  zoom: 'Zoom',
  genesys: 'Genesys',
  mobile: 'Mobile',
  upload: 'Upload',
};

export const STATUS_LABELS: Record<ConversationStatus, string> = {
  received: 'Received',
  processing: 'Processing',
  needs_review: 'Needs review',
  approved: 'Approved',
  written: 'Written to CRM',
  rejected: 'Rejected',
  failed: 'Failed',
};

export const STAGE_LABELS: Record<Stage, string> = {
  ingest: 'Ingest',
  transcribe: 'Transcribe',
  normalize: 'Normalize',
  policy: 'Privacy policy',
  extract: 'Extract',
  resolve: 'Resolve',
  validate: 'Validate',
  review: 'Review',
  write: 'CRM write',
  retention: 'Retention',
};

export const STAGE_HINTS: Record<Stage, string> = {
  ingest: 'Land the call from its channel and de-duplicate',
  transcribe: 'Speech to text (skipped when a platform transcript is used)',
  normalize: 'Canonical speakers, segments and language',
  policy: 'PII redaction, sensitive and small-talk filtering',
  extract: 'Summary sections, products, topics and tasks',
  resolve: 'Catalogue products, topic list and CRM matching',
  validate: 'Leak check, evidence check, catalogue check',
  review: 'Seller approval (or auto-approve by policy)',
  write: 'Idempotent upsert of call, tasks and insight',
  retention: 'Per-market transcript and audio lifecycle',
};

export const STAGE_STATUS_LABELS: Record<StageStatus, string> = {
  started: 'Running',
  completed: 'Done',
  skipped: 'Skipped',
  failed: 'Failed',
};

export const MENTION_TYPE_LABELS: Record<MentionType, string> = {
  interest: 'Interest',
  existing_holding: 'Existing holding',
  comparison: 'Comparison',
  objection: 'Objection',
  info_only: 'Information only',
};

export const POLICY_KIND_LABELS: Record<PolicyActionKind, string> = {
  pii_redacted: 'PII redacted',
  omitted_sensitive: 'Sensitive content omitted',
  dropped_small_talk: 'Small talk dropped',
  kept_relationship: 'Relationship fact kept',
};

export const BUSINESS_LINE_LABELS: Record<string, string> = {
  professional_buyer: 'Professional Buyer',
  retail: 'Retail',
  international: 'International',
};

export const CALL_TYPE_LABELS: Record<string, string> = {
  discovery: 'Discovery',
  service: 'Service',
  product_recommendation: 'Product recommendation',
  follow_up: 'Follow-up',
  relationship: 'Relationship',
};

export const SERVICE_LABELS: Record<string, string> = {
  transcription: 'Transcription',
  llm: 'Extraction model',
  search: 'Product search',
  pii: 'PII detection',
  crm: 'CRM',
  vault: 'Transcript vault',
};

/** Product name shown on a "Live" chip for each effective adapter. */
export const LIVE_NAMES: Record<string, string> = {
  azure_speech: 'Azure Speech',
  foundry: 'Foundry',
  hybrid: 'Foundry (hybrid)',
  azure_search: 'Azure AI Search',
  azure_language: 'Azure AI Language',
  dataverse: 'Dataverse',
  azure_blob: 'Azure Blob',
};

/** Fallback description for a non-live adapter. */
export const MOCK_NAMES: Record<string, string> = {
  mock: 'Scripted transcripts',
  rules: 'Rules engine (no LLM)',
  local: 'Local',
};

export const REASON_LABELS: Record<string, string> = {
  product_needs_pick: 'Product needs a pick',
  share_class_not_stated: 'Share class not stated',
  share_class_misheard: 'Share class unclear in the audio',
  low_margin: 'Top candidates too close to call',
  no_candidate: 'No catalogue match',
  low_confidence: 'Low confidence',
  unknown_product: 'Product not in the catalogue',
  share_class_mismatch: 'Share class does not match what was said',
  no_evidence: 'No supporting evidence in the call',
  learned_alias: 'Matched a learned alias',
  reviewer_selected: 'Picked by a reviewer',
  crm_match_ambiguous: 'CRM match is ambiguous',
  crm_match_missing: 'No CRM match',
  validation_failed: 'Validation failed',
};

export const TASK_BASIS_LABELS: Record<string, string> = {
  stated: 'Date stated',
  inferred: 'Date inferred',
  none: 'No date',
};

const TAG_LABELS: Record<string, string> = {
  top3: 'Top-3 pick',
  hero: 'Hero call',
  crm_ambiguous: 'CRM ambiguous',
  es_twin: 'Spanish twin',
  pb: 'PB',
};

// Acronyms and proper nouns that plain sentence-casing would mangle.
const TAG_WORDS: Record<string, string> = {
  uk: 'UK',
  eu: 'EU',
  us: 'US',
  asr: 'ASR',
  esg: 'ESG',
  etf: 'ETF',
  ucits: 'UCITS',
  gdpr: 'GDPR',
  ddq: 'DDQ',
  pii: 'PII',
  crm: 'CRM',
  pb: 'PB',
  es: 'ES',
  english: 'English',
  spanish: 'Spanish',
  british: 'British',
  euro: 'Euro',
  seattle: 'Seattle',
};

/** Fixture / eval case tags are snake_case keys; a few are acronyms that humanize() would mangle. */
export function tagLabel(tag: string): string {
  const override = TAG_LABELS[tag];
  if (override) return override;
  const words = tag.split(/[_\-\s]+/).filter(Boolean);
  return words
    .map((word, index) => {
      const lower = word.toLowerCase();
      const fixed = TAG_WORDS[lower];
      if (fixed) return fixed;
      if (index > 0 && words[index - 1]?.toLowerCase() === 'class' && lower.length <= 2) return lower.toUpperCase();
      return index === 0 ? lower.charAt(0).toUpperCase() + lower.slice(1) : lower;
    })
    .join(' ');
}

export const MARKET_FLAGS: Record<string, string> = { US: 'US', UK: 'UK', EU: 'EU' };

export function channelLabel(channel: string): string {
  return CHANNEL_LABELS[channel as Channel] ?? humanize(channel);
}

export function businessLineLabel(value: string | null | undefined): string {
  if (!value) return humanize(value);
  return BUSINESS_LINE_LABELS[value] ?? humanize(value);
}

export function callTypeLabel(value: string | null | undefined): string {
  if (!value) return humanize(value);
  return CALL_TYPE_LABELS[value] ?? humanize(value);
}

export function reasonLabel(reason: string): string {
  return REASON_LABELS[reason] ?? humanize(reason);
}

export function languageLabel(language: string | null | undefined): string {
  if (!language) return 'Auto';
  const base = language.toLowerCase().split('-')[0];
  if (base === 'es') return 'Spanish';
  if (base === 'en') return 'English';
  return language;
}

/** True when the list item still needs a human to pick a product (the Inbox one-click approve is hidden). */
export function hasPendingProductPick(reasons: string[]): boolean {
  return reasons.some((reason) => /product/i.test(reason));
}

export function serviceModeLabel(service: string, effective: string, live: boolean): string {
  if (live) return `Live: ${LIVE_NAMES[effective] ?? humanize(effective)}`;
  return `Mock: ${SERVICE_LABELS[service] ?? humanize(service)}`;
}
