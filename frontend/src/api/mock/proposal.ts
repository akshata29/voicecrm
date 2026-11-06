/** Builds mock proposals (extraction result, rendered note, validation, CRM match) from a scripted call. */
import type {
  CrmMatch,
  Evidence,
  LearnedAlias,
  NoteSection,
  NotesPolicy,
  PoliciesResponse,
  PolicyAction,
  Proposal,
  ProductMention,
  RelationshipFact,
  RetentionDecision,
  MarketPolicy,
  SummaryTemplate,
  TaskItem,
  TopicTag,
  ValidationFinding,
  ValidationReport,
  Engine,
} from '../types';
import { findProduct, normalizeText, productById, toCandidate } from './catalogue';
import { accountRef, contactRef, opportunityRef, TAXONOMY, TEMPLATES } from './seed';
import type { MockScript, Turn } from './scripts';
import { iso } from './state';

export function dateOnly(ms: number, plusDays = 0): string {
  const date = new Date(ms);
  date.setUTCDate(date.getUTCDate() + plusDays);
  return date.toISOString().slice(0, 10);
}

export function templateById(templateId: string): SummaryTemplate {
  const template = TEMPLATES.find((item) => item.template_id === templateId);
  if (!template) throw new Error(`Unknown mock template ${templateId}`);
  return template;
}

function evidenceFor(turns: Turn[], seg: number[]): Evidence[] {
  return seg
    .filter((index) => (turns[index]?.text ?? '').length > 0)
    .slice(0, 2)
    .map((index) => ({ segment_ids: [index], quote: turns[index]?.text ?? '' }));
}

const NOISE_WORDS: Record<string, string> = {
  C: 'class see',
  A: 'class eh',
  I: 'class eye',
  R6: 'class are six',
  Advisor: 'advice or shares',
};

/** ASR noise: the spoken share class is misheard ("class see"), so the pick can no longer be automatic. */
function noisySurface(surface: string, shareClass: string): string {
  const noisy = NOISE_WORDS[shareClass] ?? 'class unknown';
  const replaced = surface.replace(/(class|clase)\s+\w+|\w+\s+shares/i, noisy);
  return replaced === surface ? `${surface} ${noisy}` : replaced;
}

interface MentionOptions {
  threshold: number;
  aliases: LearnedAlias[];
  noise: boolean;
}

export function buildMentions(script: MockScript, options: MentionOptions): ProductMention[] {
  const noiseTarget = options.noise ? script.mentions.findIndex((spec) => spec.stated !== null) : -1;

  return script.mentions.map((spec, index) => {
    let surface = spec.surface;
    let stated = spec.stated;
    let chosenClass = spec.chosen;
    let confidence = spec.confidence;
    let scores = spec.candidates.map((candidate) => candidate.score);
    const reasons = [...(spec.reasons ?? [])];

    if (index === noiseTarget && spec.stated) {
      surface = noisySurface(surface, spec.stated);
      stated = null;
      chosenClass = null;
      confidence = 0.52;
      scores = [0.52, 0.5, 0.47];
      reasons.push('share_class_misheard', 'low_margin');
    }

    const candidates = spec.candidates.map((candidate, candidateIndex) => {
      const product = findProduct(candidate.fund ?? spec.fund, candidate.cls);
      return toCandidate(product, scores[candidateIndex] ?? candidate.score, candidate.matched ?? ['fund_name']);
    });

    let chosenId: string | null = chosenClass ? findProduct(spec.fund, chosenClass).product_id : null;

    // Learned alias (from an earlier reviewer correction) wins: this is the "improves over time" loop.
    const learned = options.aliases.find((alias) => alias.surface === normalizeText(surface));
    const learnedProduct = learned ? productById(learned.product_id) : undefined;
    if (learned && learnedProduct) {
      chosenId = learned.product_id;
      confidence = Math.max(confidence, 0.94);
      reasons.length = 0;
      const hit = candidates.find((candidate) => candidate.product_id === learned.product_id);
      if (hit) {
        hit.matched_on = [...hit.matched_on, 'learned_alias'];
        hit.score = Math.max(hit.score, 0.94);
      } else {
        candidates.unshift(toCandidate(learnedProduct, 0.94, ['learned_alias']));
      }
    }

    if (chosenId && confidence < options.threshold) {
      chosenId = null;
      if (!reasons.includes('low_confidence')) reasons.push('low_confidence');
    }

    return {
      mention_id: `m${index + 1}`,
      surface,
      evidence: evidenceFor(script.turns, spec.seg),
      candidates: [...candidates].sort((left, right) => right.score - left.score),
      chosen_product_id: chosenId,
      confidence,
      mention_type: spec.type,
      share_class_stated: stated,
      needs_review: chosenId === null,
      reasons: chosenId === null ? reasons : [],
    };
  });
}

export function buildTopics(script: MockScript): TopicTag[] {
  return script.topics.map((spec) => ({
    topic_id: spec.id,
    label: TAXONOMY.topics.find((topic) => topic.topic_id === spec.id)?.label ?? spec.id,
    confidence: spec.confidence,
    evidence: evidenceFor(script.turns, spec.seg),
  }));
}

export function buildTasks(script: MockScript, callStartMs: number): TaskItem[] {
  return script.tasks.map((spec, index) => ({
    task_id: `t${index + 1}`,
    title: spec.title,
    description: spec.description ?? null,
    owner_role: spec.owner ?? 'seller',
    owner_ref: 'seller',
    due_date: spec.dueDays === null ? null : dateOnly(callStartMs, spec.dueDays),
    due_text: spec.dueText ?? null,
    due_basis: spec.basis,
    confidence: spec.confidence,
    evidence: evidenceFor(script.turns, spec.seg),
  }));
}

export function buildRelationshipFacts(script: MockScript): RelationshipFact[] {
  const kept = script.policyActions.filter((action) => action.kind === 'kept_relationship');
  return script.facts.relationship.map((text, index) => ({
    text,
    category: kept[index]?.category ?? 'interest',
    evidence: evidenceFor(script.turns, kept[index]?.segment_ids ?? []),
  }));
}

const STANCE: Record<string, string> = {
  interest: 'client interested',
  existing_holding: 'existing holding',
  comparison: 'being compared',
  objection: 'objection raised',
  info_only: 'mentioned',
};

const OWNER: Record<string, string> = { seller: 'seller', customer: 'customer', internal: 'internal', unknown: 'owner unknown' };

export interface Draft {
  products: ProductMention[];
  topics: TopicTag[];
  tasks: TaskItem[];
  relationshipFacts: RelationshipFact[];
}

function productLine(mention: ProductMention): string {
  const stance = STANCE[mention.mention_type] ?? 'mentioned';
  if (mention.chosen_product_id) {
    const product = productById(mention.chosen_product_id);
    return `${product?.display_name ?? mention.surface} - ${stance}`;
  }
  return `${mention.surface} - share class pending reviewer pick`;
}

function taskLine(task: TaskItem): string {
  return `${task.title} (${OWNER[task.owner_role] ?? task.owner_role}${task.due_date ? `, due ${task.due_date}` : ''})`;
}

/** Same call, different template: the sections change, the underlying structured fields do not. */
export function renderSections(template: SummaryTemplate, script: MockScript, draft: Draft): NoteSection[] {
  const products = draft.products.map(productLine);
  const tasks = draft.tasks.map(taskLine);
  const relationship = draft.relationshipFacts.map((fact) => fact.text);
  const context = script.facts.context;
  const concerns = script.facts.concerns;

  const lists: Record<string, string[]> = {
    client_context: context,
    advisor_situation: context,
    products_strategies: products,
    products_discussed: products,
    concerns,
    questions_raised: concerns,
    relationship_notes: relationship,
    decisions_next_steps: tasks,
    follow_ups: tasks,
  };

  const sections: NoteSection[] = [];
  for (const section of template.sections) {
    if (section.key === 'overview') {
      sections.push({ key: section.key, title: section.title, text: script.facts.overview, items: [] });
      continue;
    }
    const items = lists[section.key] ?? [];
    if (items.length === 0 && !section.required) continue;
    sections.push({ key: section.key, title: section.title, text: null, items });
  }
  return sections;
}

export function toMarkdown(sections: NoteSection[]): string {
  return sections.map((section) => `## ${section.title}\n${section.text ?? section.items.map((item) => `- ${item}`).join('\n')}`).join('\n\n');
}

export function buildMatch(script: MockScript): CrmMatch {
  if (!script.accountId) {
    return { account: null, contact: null, opportunity: null, confidence: 0, method: 'none', ambiguous: false, candidates: [] };
  }
  return {
    account: accountRef(script.accountId),
    contact: script.match.ambiguous ? null : script.contactId ? contactRef(script.contactId) : null,
    opportunity: script.opportunityId ? opportunityRef(script.opportunityId) : null,
    confidence: script.match.confidence,
    method: script.match.method,
    ambiguous: script.match.ambiguous ?? false,
    candidates: (script.match.candidateContactIds ?? []).map(contactRef),
  };
}

/** Policy actions as the notes policy would report them for this template and policy switches. */
export function filterPolicyActions(script: MockScript, template: SummaryTemplate, notes: NotesPolicy): PolicyAction[] {
  const keepRelationship = template.include_relationship_context && notes.keep_relationship_context;
  return script.policyActions
    .filter((action) => {
      if (action.kind === 'kept_relationship') return keepRelationship;
      if (action.kind === 'dropped_small_talk') return notes.drop_small_talk;
      return true;
    })
    .map((action) => ({ ...action, segment_ids: [...action.segment_ids] }));
}

export function buildValidation(products: ProductMention[], match: CrmMatch): ValidationReport {
  const findings: ValidationFinding[] = [];
  for (const mention of products) {
    if (mention.needs_review) {
      findings.push({
        severity: 'warning',
        code: 'product.share_class_unconfirmed',
        message: `Mention ${mention.mention_id}: the share class is not certain, so a reviewer pick is required.`,
        where: `products.${mention.mention_id}`,
      });
    }
  }
  if (match.ambiguous) {
    findings.push({ severity: 'warning', code: 'crm.match_ambiguous', message: 'More than one CRM contact matches this call.', where: 'crm_match' });
  }
  return { passed: true, findings, leak_count: 0, unsupported_claims: 0 };
}

export interface ProposalInput {
  script: MockScript;
  templateId: string;
  engine: Engine;
  readyAtMs: number;
  callStartMs: number;
  policies: PoliciesResponse;
  aliases: LearnedAlias[];
  noise: boolean;
}

export function buildProposal(input: ProposalInput): Proposal {
  const { script, policies } = input;
  const template = templateById(input.templateId);
  const processing = policies.processing;

  const products = buildMentions(script, { threshold: processing.product_review_threshold, aliases: input.aliases, noise: input.noise });
  const topics = buildTopics(script);
  const tasks = buildTasks(script, input.callStartMs);
  const relationshipFacts = template.include_relationship_context && policies.notes.keep_relationship_context ? buildRelationshipFacts(script) : [];
  const draft: Draft = { products, topics, tasks, relationshipFacts };
  const sections = renderSections(template, script, draft);
  const match = buildMatch(script);

  const reasons: string[] = [];
  if (products.some((mention) => mention.needs_review)) reasons.push('product_needs_pick');
  if (match.ambiguous) reasons.push('crm_match_ambiguous');
  const overall = products.every((mention) => !mention.needs_review) ? script.confidence : Math.min(script.confidence, 0.7);
  const eligible = reasons.length === 0 && overall >= processing.auto_approve_min_confidence && (!processing.auto_approve_requires_crm_match || match.account !== null);

  return {
    template_id: template.template_id,
    note_markdown: toMarkdown(sections),
    result: {
      template_id: template.template_id,
      language_out: template.language_out,
      sections,
      products,
      topics,
      tasks,
      relationship_facts: relationshipFacts,
      flags: script.flags ?? [],
      overall_confidence: overall,
      engine: input.engine,
      model: input.engine === 'rules' ? null : 'gpt-4.1-mini',
    },
    validation: buildValidation(products, match),
    crm_match: match,
    crm_preview: null,
    review: {
      state: 'pending',
      reasons,
      auto_approve_eligible: eligible,
      reviewed_at: null,
      reviewed_by: null,
      clicks: 0,
      seconds_to_review: null,
    },
    policy_actions: filterPolicyActions(script, template, policies.notes),
    ready_at: iso(input.readyAtMs),
  };
}

/** Re-renders note sections after the reviewer's edits (picks, drops, topic and task changes). */
export function rerender(proposal: Proposal, script: MockScript): void {
  const template = templateById(proposal.template_id);
  const draft: Draft = {
    products: proposal.result.products,
    topics: proposal.result.topics,
    tasks: proposal.result.tasks,
    relationshipFacts: proposal.result.relationship_facts,
  };
  proposal.result.sections = renderSections(template, script, draft);
  proposal.note_markdown = toMarkdown(proposal.result.sections);
}

export function recomputeReview(proposal: Proposal, policies: PoliciesResponse): void {
  const reasons: string[] = [];
  if (proposal.result.products.some((mention) => mention.needs_review)) reasons.push('product_needs_pick');
  if (proposal.crm_match.ambiguous) reasons.push('crm_match_ambiguous');
  proposal.review.reasons = reasons;
  const processing = policies.processing;
  proposal.review.auto_approve_eligible =
    reasons.length === 0 &&
    proposal.result.overall_confidence >= processing.auto_approve_min_confidence &&
    (!processing.auto_approve_requires_crm_match || proposal.crm_match.account !== null);
  proposal.validation = buildValidation(proposal.result.products, proposal.crm_match);
}

// ---- retention -------------------------------------------------------------------------------------
export function marketOf(policies: PoliciesResponse, market: string): MarketPolicy {
  const found = policies.markets.find((item) => item.market === market);
  if (found) return found;
  return {
    market,
    display_name: market,
    transcript_retention: 'off',
    transcript_retention_days: null,
    store_redacted_only: true,
    audio_retention_days: 0,
    summary_retention_days: null,
    notes: null,
  };
}

export function retentionDecision(policy: MarketPolicy, atMs: number): RetentionDecision {
  const on = policy.transcript_retention === 'on';
  const days = policy.transcript_retention_days ?? 0;
  const explanation = on
    ? `${policy.display_name}: a ${policy.store_redacted_only ? 'redacted ' : ''}transcript is kept in the vault for ${days} days. The CRM only receives the summary, structured fields and tasks.`
    : `${policy.display_name}: transcripts are not retained. The working copy is deleted after review; the CRM only receives the summary, structured fields and tasks.`;
  return {
    market: policy.market,
    transcript_stored: on,
    transcript_redacted: policy.store_redacted_only,
    transcript_expires_at: on ? iso(atMs + days * 86_400_000) : null,
    audio_expires_at: policy.audio_retention_days > 0 ? iso(atMs + policy.audio_retention_days * 86_400_000) : null,
    crm_contains_transcript: false,
    explanation,
  };
}
