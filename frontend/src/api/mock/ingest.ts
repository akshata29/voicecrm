/** Mock ingestion: POST /ingest/simulate and POST /ingest/upload. */
import type { Channel, IngestResponse, SimulateRequest } from '../types';
import { CHANNELS } from '../types';
import { createConversation, findByInteraction } from './pipeline';
import { scriptByFixture, type MockScript } from './scripts';
import { SEED_ACCOUNTS, SEED_CONTACTS, SEED_OPPORTUNITIES, TEMPLATES } from './seed';
import { HttpError, type SimVia } from './state';

export function simulate(body: SimulateRequest): IngestResponse {
  const script = scriptByFixture(body.fixture_id);
  if (!script) throw new HttpError(404, `Unknown fixture "${body.fixture_id}"`);
  if (body.via !== undefined && body.via !== 'transcript' && body.via !== 'audio') throw new HttpError(422, 'via must be "transcript" or "audio"');
  if (body.template_id && !TEMPLATES.some((template) => template.template_id === body.template_id)) throw new HttpError(404, `Unknown template "${body.template_id}"`);
  if (body.channel && !CHANNELS.includes(body.channel)) throw new HttpError(422, `Unknown channel "${body.channel}"`);

  const conv = createConversation({
    script,
    channel: body.channel ?? null,
    market: body.market ?? null,
    businessLine: body.business_line ?? null,
    templateId: body.template_id ?? null,
    via: body.via ?? 'transcript',
    noise: Boolean(body.asr_noise),
  });
  return { conversation_id: conv.id, duplicate_of: null };
}

interface Overrides {
  sellerName: string;
  sellerEmail: string;
  customerName: string | null;
  customerOrg: string | null;
  customerEmail: string | null;
}

/** Uploads reuse a scripted call's content but carry the uploader's own names and CRM match. */
function adaptScript(base: MockScript, overrides: Overrides): MockScript {
  const replacements: Array<[string, string]> = [[base.seller.name, overrides.sellerName]];
  if (overrides.customerName) replacements.push([base.customer.name, overrides.customerName]);
  if (overrides.customerOrg) replacements.push([base.org, overrides.customerOrg]);
  const swap = (text: string) => replacements.reduce((current, [from, to]) => current.split(from).join(to), text);

  const org = overrides.customerOrg ?? base.org;
  const lowerOrg = org.toLowerCase();
  const account = overrides.customerOrg
    ? SEED_ACCOUNTS.find((item) => item.name.toLowerCase() === lowerOrg || lowerOrg.includes(item.name.toLowerCase()) || item.name.toLowerCase().includes(lowerOrg))
    : SEED_ACCOUNTS.find((item) => item.id === base.accountId);
  const contact = account
    ? (SEED_CONTACTS.find((item) => item.account_id === account.id && overrides.customerName && item.name.toLowerCase() === overrides.customerName.toLowerCase()) ??
      SEED_CONTACTS.find((item) => item.account_id === account.id))
    : undefined;
  const opportunity = account ? SEED_OPPORTUNITIES.find((item) => item.account_id === account.id) : undefined;
  const keepsOriginalMatch = !overrides.customerOrg;

  return {
    ...base,
    seller: { ...base.seller, name: overrides.sellerName, email: overrides.sellerEmail },
    customer: { ...base.customer, name: overrides.customerName ?? base.customer.name, email: overrides.customerEmail ?? base.customer.email },
    org,
    accountId: account?.id ?? '',
    contactId: keepsOriginalMatch ? base.contactId : (contact?.id ?? null),
    opportunityId: keepsOriginalMatch ? base.opportunityId : (opportunity?.id ?? null),
    match: account
      ? keepsOriginalMatch
        ? base.match
        : { method: 'name_org', confidence: 0.86 }
      : { method: 'none', confidence: 0 },
    facts: {
      ...base.facts,
      overview: swap(base.facts.overview),
      context: base.facts.context.map(swap),
      concerns: base.facts.concerns.map(swap),
    },
    tasks: base.tasks.map((task) => ({ ...task, title: swap(task.title) })),
  };
}

function text(form: FormData, key: string): string | null {
  const value = form.get(key);
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

export function upload(form: FormData | null): IngestResponse {
  if (!form) throw new HttpError(422, 'multipart form data is required');
  const file = form.get('file');
  if (!(file instanceof File)) throw new HttpError(422, 'file is required (audio .wav/.mp3/.m4a/.ogg/.webm, or transcript .vtt/.json)');
  if (!/\.(wav|mp3|m4a|ogg|webm|vtt|json)$/i.test(file.name)) throw new HttpError(422, `Unsupported file type "${file.name}"`);

  const channelText = text(form, 'channel') ?? 'upload';
  if (!CHANNELS.includes(channelText as Channel)) throw new HttpError(422, `Unknown channel "${channelText}"`);
  const channel = channelText as Channel;
  const market = text(form, 'market');
  const businessLine = text(form, 'business_line');
  const sellerName = text(form, 'seller_name');
  const sellerEmail = text(form, 'seller_email');
  if (!market || !businessLine || !sellerName || !sellerEmail) throw new HttpError(422, 'market, business_line, seller_name and seller_email are required');

  const interactionId = text(form, 'source_interaction_id');
  if (interactionId) {
    const existing = findByInteraction(channel, interactionId);
    if (existing) return { conversation_id: existing.id, duplicate_of: existing.id };
  }

  const language = text(form, 'language_hint') ?? '';
  const fixtureId = text(form, 'fixture_id');
  let base = fixtureId ? scriptByFixture(fixtureId) : undefined;
  if (fixtureId && !base) throw new HttpError(404, `Unknown fixture "${fixtureId}"`);
  if (!base) {
    const wantsSpanish = language.toLowerCase().startsWith('es');
    const retail = businessLine === 'retail';
    base = scriptByFixture(wantsSpanish ? (retail ? 'rt-mobile-es-advisor-visit' : 'pb-zoom-es-mandato-renta-variable') : retail ? 'rt-genesys-privacy-seahawks' : 'pb-mobile-inperson-visit');
  }
  if (!base) throw new HttpError(500, 'Mock scripts are missing');

  const customerOrg = text(form, 'customer_org');
  const script = adaptScript(base, {
    sellerName,
    sellerEmail,
    customerName: text(form, 'customer_name'),
    customerOrg,
    customerEmail: text(form, 'customer_email'),
  });
  const via: SimVia = /\.(vtt|json)$/i.test(file.name) ? 'transcript' : 'audio';
  const callType = text(form, 'call_type');

  const conv = createConversation({
    script,
    channel,
    market,
    businessLine,
    callType: callType ?? base.info.call_type,
    title: text(form, 'title') ?? (customerOrg ? `${customerOrg} - ${channel === 'mobile' ? 'in-person visit' : 'conversation'}` : base.info.title),
    languageHint: language || base.info.language,
    sourceInteractionId: interactionId,
    via,
    noise: false,
    fixtureId: fixtureId ?? null,
  });
  return { conversation_id: conv.id, duplicate_of: null };
}
