/**
 * Offline stand-in for the phone line (npm run dev:mock): a configured line with two earlier calls, plus the calls the
 * in-browser simulation (components/phone/simulated.ts) places through it. Every number is in the fictional 555-01xx range.
 */
import { formatDurationSeconds } from '@/lib/format';
import type {
  PhoneCall,
  PhoneCallerProfile,
  PhoneCallState,
  PhoneSellerRegistered,
  PhoneSellerRegistration,
  PhoneStatus,
  PhoneToken,
  PhoneTokenRequest,
} from '../types';
import { upload } from './ingest';
import { db, HttpError, iso, uuidHex } from './state';

export const SELLER_NAME = 'Aisha West';
const SELLER_EMAIL = 'aisha.west@contoso.example';
const LINE_NUMBER = '+1 800 555 0100';
const LINE_IDENTITY = '8:acs:mock-line';
const TOKEN_LIFETIME_MS = 24 * 3_600_000;
const REGISTRATION_TTL_SECONDS = 60;

/** What the API lists as active; every other state (ended, processing, ingested, failed, missed) goes to the recent calls. */
const LIVE: ReadonlySet<PhoneCallState> = new Set(['ringing', 'connecting', 'notice', 'calling_seller', 'in_call', 'leaving_message']);

let calls: PhoneCall[] | null = null;
const profiles = new Map<string, PhoneCallerProfile>();
// Until the console on this page says otherwise, pretend one is open on another device.
let sellerOnline = true;

function history(): PhoneCall[] {
  if (calls) return calls;
  const now = Date.now();
  const at = (startedMsAgo: number, offsetSeconds: number) => iso(now - startedMsAgo + offsetSeconds * 1000);
  const twoHours = 2 * 3_600_000;
  const fortyMinutes = 41 * 60_000;
  // An inbound call that already sits in the Inbox, so the "Open" link has somewhere to go.
  const inbox = [...db().convs.values()].find((conv) => conv.record.meta.source === 'genesys');
  calls = [
    {
      call_id: 'mock-call-0002',
      state: 'missed',
      caller: '+1 312 555 0186',
      caller_kind: 'phone',
      caller_name: null,
      seller: null,
      started_at: at(fortyMinutes, 0),
      connected_at: at(fortyMinutes, 3),
      ended_at: at(fortyMinutes, 41),
      duration_seconds: null,
      notice: 'played',
      recording: 'none',
      conversation_id: null,
      error: null,
      timeline: [
        { at: at(fortyMinutes, 0), label: 'Call received from +1 312 555 0186' },
        { at: at(fortyMinutes, 3), label: 'Playing the recording notice' },
        { at: at(fortyMinutes, 11), label: `Ringing ${SELLER_NAME}` },
        { at: at(fortyMinutes, 41), label: 'Caller hung up before anyone answered' },
      ],
    },
    {
      call_id: 'mock-call-0001',
      state: 'ingested',
      caller: '+1 206 555 0142',
      caller_kind: 'phone',
      caller_name: 'Dana Whitfield',
      seller: SELLER_NAME,
      started_at: at(twoHours, 0),
      connected_at: at(twoHours, 3),
      ended_at: at(twoHours, 274),
      duration_seconds: 271,
      notice: 'played',
      recording: 'ready',
      conversation_id: inbox?.id ?? null,
      error: null,
      timeline: [
        { at: at(twoHours, 0), label: 'Call received from +1 206 555 0142' },
        { at: at(twoHours, 3), label: 'Playing the recording notice' },
        { at: at(twoHours, 11), label: `Ringing ${SELLER_NAME}` },
        { at: at(twoHours, 22), label: `${SELLER_NAME} answered; recording started` },
        { at: at(twoHours, 274), label: 'Call ended after 4 min 31 s' },
        { at: at(twoHours, 281), label: 'Recording ready; transcribing' },
        { at: at(twoHours, 331), label: 'Added to the Inbox' },
      ],
    },
  ];
  return calls;
}

const count = (state: PhoneCallState) => history().filter((call) => call.state === state).length;

export function phoneStatus(): PhoneStatus {
  const known = (id: string | null) => (id && db().convs.has(id) ? id : null);
  const all = history().map((call) => ({ ...call, conversation_id: known(call.conversation_id) }));
  const last = Math.max(...all.map((call) => Date.parse(call.timeline.at(-1)?.at ?? call.started_at)));
  return {
    mode: 'acs',
    configured: true,
    problem: null,
    number: LINE_NUMBER,
    line_identity: LINE_IDENTITY,
    public_url: 'https://voicecrm-demo.example',
    ingress_listening: true,
    seller: { mode: 'console', name: SELLER_NAME, online: sellerOnline },
    notice_enabled: true,
    active: all.filter((call) => LIVE.has(call.state)),
    recent: all.filter((call) => !LIVE.has(call.state)).slice(0, 10),
    calls: all.length,
    ingested: count('ingested'),
    failed: count('failed'),
    missed: count('missed'),
    last_event_at: iso(last),
    last_error: null,
  };
}

export function issueToken(body: PhoneTokenRequest): PhoneToken {
  if (body.role !== 'caller' && body.role !== 'seller') throw new HttpError(422, 'role must be "caller" or "seller"');
  return {
    identity: body.identity ?? `8:acs:mock-${body.role}-${uuidHex().slice(0, 8)}`,
    token: `mock-token-${uuidHex().slice(0, 12)}`,
    expires_on: iso(Date.now() + TOKEN_LIFETIME_MS),
    display_name: body.display_name?.trim() || (body.role === 'seller' ? SELLER_NAME : 'Browser caller'),
    line_identity: LINE_IDENTITY,
    number: LINE_NUMBER,
  };
}

export function registerSeller(body: PhoneSellerRegistration): PhoneSellerRegistered {
  if (!body.identity) throw new HttpError(422, 'identity is required');
  sellerOnline = true;
  return { registered: true, ttl_seconds: REGISTRATION_TTL_SECONDS };
}

export function unregisterSeller(identity: string | undefined): Pick<PhoneSellerRegistered, 'registered'> {
  if (!identity) throw new HttpError(422, 'identity is required');
  sellerOnline = false;
  return { registered: false };
}

// ---- the simulated calls ---------------------------------------------------------------------------
export const isSellerOnline = (): boolean => sellerOnline;

const find = (id: string) => history().find((call) => call.call_id === id);

function move(call: PhoneCall, state: PhoneCallState, label: string, patch: Partial<PhoneCall> = {}): void {
  Object.assign(call, patch, { state });
  call.timeline.push({ at: iso(Date.now()), label });
}

/** The line sees a browser call arrive. Returns the id the other functions take. */
export function beginCall(profile: PhoneCallerProfile): string {
  const id = `sim-${uuidHex().slice(0, 8)}`;
  const name = profile.name?.trim() || null;
  const now = iso(Date.now());
  history().unshift({
    call_id: id,
    state: 'ringing',
    caller: name ?? 'Browser caller',
    caller_kind: 'voip',
    caller_name: name,
    seller: null,
    started_at: now,
    connected_at: null,
    ended_at: null,
    duration_seconds: null,
    notice: 'pending',
    recording: 'none',
    conversation_id: null,
    error: null,
    timeline: [{ at: now, label: 'Call received from the browser dialer' }],
  });
  profiles.set(id, profile);
  return id;
}

export function advanceCall(id: string, state: PhoneCallState, label: string, patch: Partial<PhoneCall> = {}): void {
  const call = find(id);
  if (call && LIVE.has(call.state)) move(call, state, label, patch);
}

function ingest(call: PhoneCall): void {
  const profile = profiles.get(call.call_id) ?? {};
  const fields: Record<string, string | undefined> = {
    channel: 'phone',
    market: 'US',
    business_line: 'professional_buyer',
    seller_name: SELLER_NAME,
    seller_email: SELLER_EMAIL,
    fixture_id: 'pb-genesys-inbound-service',
    source_interaction_id: call.call_id,
    title: profile.org ? `${profile.org} - phone call` : undefined,
    customer_name: profile.name,
    customer_org: profile.org,
    customer_email: profile.email,
  };
  try {
    const form = new FormData();
    form.append('file', new File([], `${call.call_id}.wav`, { type: 'audio/wav' }));
    for (const [key, value] of Object.entries(fields)) if (value) form.append(key, value);
    move(call, 'ingested', 'Added to the Inbox', { conversation_id: upload(form).conversation_id });
  } catch (error) {
    move(call, 'failed', 'Could not add the call to the Inbox', { error: error instanceof Error ? error.message : 'The call could not be ingested' });
  }
}

/** The caller (or the seller) hung up: a call somebody took is recorded and travels on to the Inbox, any other is missed. */
export function endCall(id: string): void {
  const call = find(id);
  if (!call || !LIVE.has(call.state)) return;
  const now = Date.now();
  const ended_at = iso(now);
  if (call.state !== 'in_call' && call.state !== 'leaving_message') {
    move(call, 'missed', 'Caller hung up before anyone answered', { ended_at });
    return;
  }
  const seconds = Math.round((now - Date.parse(call.connected_at ?? call.started_at)) / 1000);
  move(call, 'ended', `Call ended after ${formatDurationSeconds(seconds)}`, { ended_at, duration_seconds: seconds });
  window.setTimeout(() => move(call, 'processing', 'Recording ready; transcribing', { recording: 'ready' }), 2500);
  window.setTimeout(() => ingest(call), 6500);
}
