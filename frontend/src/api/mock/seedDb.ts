/** Seeds the in-memory mock with conversations in every status, learned aliases, vault items and eval runs. */
import type { LearnedAlias, VaultItem } from '../types';
import { findProduct } from './catalogue';
import { seedEvalRuns } from './evaluation';
import { approve, createConversation, createFailedConversation, reject } from './pipeline';
import { scriptByFixture, type MockScript } from './scripts';
import { defaultPolicies } from './seed';
import { iso, setDb, type MockConv, type MockDb } from './state';

const MINUTE = 60_000;
const HOUR = 3_600_000;
const DAY = 86_400_000;

function script(fixtureId: string): MockScript {
  const found = scriptByFixture(fixtureId);
  if (!found) throw new Error(`Missing mock script ${fixtureId}`);
  return found;
}

function readyAt(conv: MockConv): number {
  const ready = conv.record.proposal?.ready_at;
  return ready ? Date.parse(ready) : Date.now();
}

function seedAliases(now: number): LearnedAlias[] {
  return [
    {
      surface: 'income fund a shares',
      product_id: findProduct('Contoso Equity Income Fund', 'A').product_id,
      count: 3,
      source: 'reviewer_correction',
      created_at: iso(now - 3 * DAY),
    },
    {
      surface: 'small cap value class see',
      product_id: findProduct('Contoso Small Cap Value Fund', 'C').product_id,
      count: 1,
      source: 'reviewer_correction',
      created_at: iso(now - 26 * HOUR),
    },
  ];
}

function seedVault(now: number): VaultItem[] {
  const item = (id: string, kind: VaultItem['kind'], createdAt: number, expiresAt: number | null, size: number): VaultItem => ({
    conversation_id: id,
    kind,
    uri: `.state/vault/${id}/${kind}.json`,
    created_at: iso(createdAt),
    expires_at: expiresAt === null ? null : iso(expiresAt),
    size_bytes: size,
    redacted: true,
    filename: `${kind}.json`,
  });
  return [
    item('archive-0007', 'working', now - 9 * HOUR, now - 3 * HOUR, 6_120),
    item('archive-0003', 'transcript', now - 400 * DAY, now - 35 * DAY, 9_840),
    item('archive-0011', 'transcript', now - 20 * DAY, now + 345 * DAY, 11_260),
    item('archive-0012', 'transcript', now - 120 * DAY, now + 1705 * DAY, 8_430),
  ];
}

export function initMockDb(): void {
  const now = Date.now();
  const state: MockDb = {
    convs: new Map(),
    aliases: seedAliases(now),
    policies: defaultPolicies(),
    vault: seedVault(now),
    crm: { phonecall: [], task: [], insight: [] },
    evalRuns: [],
  };
  setDb(state);

  // Written to the CRM after a single click.
  const hero = createConversation({ script: script('pb-teams-equity-class-c'), via: 'transcript', noise: false, createdAtMs: now - 190 * MINUTE });
  approve(hero.id, {}, { atMs: readyAt(hero) + 14_200 });

  // Written by the auto-approve policy; one custom CRM column was missing, so two fields were written in fallback form.
  const service = createConversation({ script: script('pb-genesys-inbound-service'), via: 'transcript', noise: false, createdAtMs: now - 140 * MINUTE });
  approve(service.id, {}, { atMs: readyAt(service) + 700, auto: true, degraded: ['vcrm_productsdiscussed', 'vcrm_clientcontext'] });

  // Rejected by the seller.
  const visit = createConversation({ script: script('pb-mobile-inperson-visit'), via: 'audio', noise: false, createdAtMs: now - 100 * MINUTE });
  reject(visit.id, 'Wrong customer attached - duplicate of the earlier Lakeshore visit note', readyAt(visit) + 71_000);

  // Failed before any proposal existed.
  createFailedConversation(
    { script: script('pb-genesys-near-identical-names'), via: 'transcript', noise: false, createdAtMs: now - 75 * MINUTE },
    'Genesys transcript URL expired (10-minute window) and no audio recording was attached, so there was nothing to transcribe.',
    'transcribe',
  );

  // Needs review: UK market keeps a redacted transcript, so the transcript panel is available.
  createConversation({ script: script('int-teams-uk-multi-asset'), via: 'transcript', noise: false, createdAtMs: now - 55 * MINUTE });

  // Needs review: Spanish call, ambiguous CRM contact.
  createConversation({ script: script('pb-zoom-es-mandato-renta-variable'), via: 'audio', noise: false, createdAtMs: now - 38 * MINUTE });

  // Needs review: privacy call with policy actions, ready for one-click approval.
  createConversation({ script: script('rt-genesys-privacy-seahawks'), via: 'transcript', noise: false, createdAtMs: now - 22 * MINUTE });

  // Needs review: share class never stated, so a top-3 pick (Class C / Class A / Advisor) is required.
  createConversation({ script: script('pb-ambiguous-equity-fund'), via: 'transcript', noise: false, createdAtMs: now - 9 * MINUTE });

  // Needs review: a former fund name the catalogue does not know, so the reviewer searches the catalogue and teaches the alias.
  createConversation({ script: script('pb-teams-legacy-blue-chip'), via: 'transcript', noise: false, createdAtMs: now - 14 * MINUTE });

  // Processing right now (visible for ~25 seconds after the page loads).
  createConversation({ script: script('pb-zoom-fixed-income-followup'), via: 'audio', noise: false, createdAtMs: now - 3_000, pace: 1.8 });

  state.evalRuns = seedEvalRuns(now, state.aliases.length);
}
