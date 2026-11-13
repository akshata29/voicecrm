/**
 * The Azure Communication Services Calling SDK behind the phone components. It is several MB, so it is imported on first use
 * and lands in its own chunks; nothing here runs in the offline demo. One CallClient lives per role (caller, seller): the SDK
 * allows one CallAgent per CallClient, and the Dialer and the seller console are two identities on the same page.
 */
import type { Call, CallAgent, CallClient, CallEndReason, CallState, ConnectionStateChangedEvent, IncomingCall } from '@azure/communication-calling';
import type { AzureCommunicationTokenCredential } from '@azure/communication-common';
import { phoneApi } from '@/api/client';
import type { PhoneCallerProfile, PhoneToken, PhoneTokenRequest } from '@/api/types';
import type { CallEnd, CallingBackend, CallPhase, CallSnapshot, IncomingRing, ListenHandlers, Listener, LiveCall } from './liveCall';

type Role = PhoneTokenRequest['role'];
type TokenOptions = Pick<PhoneTokenRequest, 'display_name' | 'profile'>;

interface Sdk {
  calling: typeof import('@azure/communication-calling');
  common: typeof import('@azure/communication-common');
}

interface Session {
  client: CallClient;
  agent: CallAgent;
  credential: AzureCommunicationTokenCredential;
  issued: PhoneToken;
}

interface SessionHandle {
  ready: Promise<Session>;
  close(): Promise<void>;
}

const IDENTITY_KEY: Record<Role, string> = { caller: 'voicecrm.phone.caller.identity', seller: 'voicecrm.phone.seller.identity' };
/** A held token with less than this left is replaced before it is used. */
const REFRESH_MARGIN_MS = 5 * 60_000;
/** How long a new session waits for the previous one of the same role to be closed before it tries anyway. */
const TURN_TIMEOUT_MS = 10_000;

// ---- the SDK, loaded once ------------------------------------------------------------------------------
let sdk: Promise<Sdk> | null = null;

function loadSdk(): Promise<Sdk> {
  sdk ??= Promise.all([import('@azure/communication-calling'), import('@azure/communication-common')]).then(
    ([calling, common]) => ({ calling, common }),
    () => {
      sdk = null; // a failed download (offline, a stale page after a deploy) can be retried
      throw new Error('Could not load the calling library. Check the network connection and reload the page.');
    },
  );
  return sdk;
}

const clients = new Map<Role, CallClient>();

function clientFor(calling: Sdk['calling'], role: Role): CallClient {
  const existing = clients.get(role);
  if (existing) return existing;
  const created = new calling.CallClient();
  clients.set(role, created);
  return created;
}

// ---- identities and tokens -----------------------------------------------------------------------------
function storedIdentity(role: Role): string | undefined {
  try {
    return window.localStorage.getItem(IDENTITY_KEY[role]) ?? undefined;
  } catch {
    return undefined;
  }
}

function rememberIdentity(role: Role, identity: string): void {
  try {
    window.localStorage.setItem(IDENTITY_KEY[role], identity);
  } catch {
    // storage may be blocked; the API then issues a new identity next time
  }
}

let sellerToken: { key: string; token: PhoneToken } | null = null;

/** Always asks the API; it hands back the identity it already knows for the one sent along, so identities are not piled up. */
async function requestToken(role: Role, options: TokenOptions): Promise<PhoneToken> {
  const identity = storedIdentity(role);
  const token = await phoneApi.token({ role, ...options, ...(identity ? { identity } : {}) });
  rememberIdentity(role, token.identity);
  if (role === 'seller') sellerToken = { key: JSON.stringify(options), token };
  return token;
}

/**
 * A caller always asks again: the line keeps the caller's details per identity, so what was typed since the last call (and
 * what a restarted API forgot) reaches it with every dial. The seller console reuses the token it holds unless that was
 * issued for another name or is about to expire.
 */
function currentToken(role: Role, options: TokenOptions): Promise<PhoneToken> {
  if (role === 'seller' && sellerToken && sellerToken.key === JSON.stringify(options) && Date.parse(sellerToken.token.expires_on) - Date.now() > REFRESH_MARGIN_MS) {
    return Promise.resolve(sellerToken.token);
  }
  return requestToken(role, options);
}

// ---- sessions: one CallAgent per role, opened and closed strictly in turn -------------------------------
const turns = new Map<Role, Promise<void>>();

async function createSession(role: Role, options: TokenOptions): Promise<Session> {
  const { calling, common } = await loadSdk();
  const issued = await currentToken(role, options);
  const credential = new common.AzureCommunicationTokenCredential({
    token: issued.token,
    tokenRefresher: async () => (await requestToken(role, options)).token,
    refreshProactively: true,
  });
  try {
    const client = clientFor(calling, role);
    const agent = await client.createCallAgent(credential, { displayName: issued.display_name || undefined });
    return { client, agent, credential, issued };
  } catch (error) {
    credential.dispose();
    throw error;
  }
}

/**
 * The SDK refuses a second CallAgent while the first is still being disposed, which React's double-run effects and a quick
 * redial would otherwise hit. Each open therefore waits for the close of the one before it.
 */
function openSession(role: Role, options: TokenOptions): SessionHandle {
  const previous = turns.get(role) ?? Promise.resolve();
  let release: () => void = () => undefined;
  turns.set(
    role,
    new Promise<void>((resolve) => {
      release = resolve;
    }),
  );

  const wait = Promise.race([previous, new Promise<void>((resolve) => window.setTimeout(resolve, TURN_TIMEOUT_MS))]);
  const ready = wait.then(() => createSession(role, options));
  ready.catch(() => release()); // a failed open leaves nothing to close, so the next one may go

  let closing: Promise<void> | null = null;
  const close = () =>
    (closing ??= (async () => {
      try {
        const session = await ready;
        try {
          await session.agent.dispose();
        } finally {
          session.credential.dispose();
        }
      } catch {
        // it never opened, or is already gone
      } finally {
        release();
      }
    })());
  return { ready, close };
}

async function allowMicrophone(client: CallClient): Promise<void> {
  const access = await (await client.getDeviceManager()).askDevicePermission({ audio: true, video: false });
  if (!access.audio) {
    throw new Error('The microphone is blocked or missing. Allow microphone access for this site (the lock icon in the address bar) and try again.');
  }
}

// ---- calls ---------------------------------------------------------------------------------------------
const PHASES: Record<CallState, CallPhase> = {
  None: 'connecting',
  Connecting: 'connecting',
  InLobby: 'connecting',
  Ringing: 'ringing',
  EarlyMedia: 'ringing',
  Connected: 'connected',
  LocalHold: 'connected',
  RemoteHold: 'connected',
  Disconnecting: 'ended',
  Disconnected: 'ended',
};

/** Plain-language outcome from the code the SDK reports when a call ends (0 and 487 are hang-ups, not failures). */
function endOf(reason: CallEndReason | undefined): CallEnd {
  const code = reason?.code ?? 0;
  const subCode = reason?.subCode ?? 0;
  if (code < 400 || code === 487) return { ok: true, message: null };
  if (code === 603) return { ok: false, message: 'The call was declined.' };
  if (code === 486) return { ok: false, message: 'The line is busy right now. Try again in a moment.' };
  if (code === 410 || code === 490 || code === 496) {
    return { ok: false, message: 'The call could not be kept up because of a network problem on this computer. Check the connection and try again.' };
  }
  if (code === 404 || code === 408 || code === 480) {
    return { ok: false, message: 'The VoiceCRM line did not answer. Check that the API is running and that Azure can reach it through the tunnel (see the Phone line card).' };
  }
  return { ok: false, message: `The call ended with an error (code ${code}${subCode ? `, subcode ${subCode}` : ''}). See the Phone line card for the state of the line.` };
}

class SdkCall implements LiveCall {
  private current: CallSnapshot;
  private over = false;
  private readonly call: Call;
  private readonly onOver: () => void;
  private readonly listeners = new Set<() => void>();

  constructor(call: Call, onOver: () => void) {
    this.call = call;
    this.onOver = onOver;
    this.current = this.read(null);
    call.on('stateChanged', this.refresh);
    call.on('isMutedChanged', this.refresh);
    this.refresh();
  }

  private read(previous: CallSnapshot | null): CallSnapshot {
    const phase = PHASES[this.call.state];
    return {
      phase,
      muted: this.call.isMuted,
      connectedAt: previous?.connectedAt ?? (phase === 'connected' ? Date.now() : null),
      end: phase === 'ended' ? endOf(this.call.callEndReason) : null,
    };
  }

  private readonly refresh = (): void => {
    this.current = this.read(this.current);
    for (const listener of [...this.listeners]) listener();
    if (this.call.state === 'Disconnected' && !this.over) {
      this.over = true;
      this.call.off('stateChanged', this.refresh);
      this.call.off('isMutedChanged', this.refresh);
      this.onOver();
    }
  };

  snapshot(): CallSnapshot {
    return this.current;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  async setMuted(muted: boolean): Promise<void> {
    await (muted ? this.call.mute() : this.call.unmute());
    this.refresh();
  }

  /** Ends the call for everybody, as a person hanging up a phone would; where that is not allowed, leaves it. */
  async hangUp(): Promise<void> {
    try {
      await this.call.hangUp({ forEveryone: true });
    } catch {
      await this.call.hangUp().catch(() => undefined);
    }
  }
}

class SdkRing implements IncomingRing {
  readonly callerName: string;
  private readonly incoming: IncomingCall;

  constructor(incoming: IncomingCall) {
    this.incoming = incoming;
    this.callerName = incoming.callerInfo.displayName?.trim() || 'A customer';
  }

  async accept(): Promise<LiveCall> {
    return new SdkCall(await this.incoming.accept({ audioOptions: { muted: false } }), () => undefined);
  }

  decline(): Promise<void> {
    return this.incoming.reject();
  }

  onEnded(listener: () => void): () => void {
    this.incoming.on('callEnded', listener);
    return () => this.incoming.off('callEnded', listener);
  }
}

// ---- the two sides -------------------------------------------------------------------------------------
async function dial(profile: PhoneCallerProfile): Promise<LiveCall> {
  const handle = openSession('caller', { display_name: profile.name?.trim() || undefined, profile });
  try {
    const session = await handle.ready;
    await allowMicrophone(session.client);
    const line = session.issued.line_identity;
    if (!line) throw new Error('The line has no Azure identity yet. See the Phone line card for what is missing.');
    const call = session.agent.startCall([{ communicationUserId: line }], { audioOptions: { muted: false } });
    return new SdkCall(call, () => void handle.close());
  } catch (error) {
    await handle.close();
    throw error;
  }
}

async function listen(displayName: string, handlers: ListenHandlers): Promise<Listener> {
  const handle = openSession('seller', { display_name: displayName.trim() || undefined });
  try {
    const session = await handle.ready;
    await allowMicrophone(session.client);
    const onIncoming = ({ incomingCall }: { incomingCall: IncomingCall }) => handlers.onRing(new SdkRing(incomingCall));
    const onConnection: ConnectionStateChangedEvent = ({ newValue }) =>
      handlers.onTrouble(newValue === 'Disconnected' ? 'Lost the connection to Azure Communication Services, so calls cannot ring here until it is back.' : null);
    session.agent.on('incomingCall', onIncoming);
    session.agent.on('connectionStateChanged', onConnection);
    return {
      identity: session.issued.identity,
      stop: async () => {
        session.agent.off('incomingCall', onIncoming);
        session.agent.off('connectionStateChanged', onConnection);
        await handle.close();
      },
    };
  } catch (error) {
    await handle.close();
    throw error;
  }
}

export const backend: CallingBackend = { dial, listen };
