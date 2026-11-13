/**
 * The calling backend of the offline demo (npm run dev:mock): no Azure and no microphone. Phases advance on timers and every
 * step is reported to the mock phone line (api/mock/phone.ts), so the Phone line card follows this call as it would a real one.
 */
import * as line from '@/api/mock/phone';
import type { PhoneCallerProfile } from '@/api/types';
import type { CallingBackend, CallPhase, CallSnapshot, IncomingRing, ListenHandlers, Listener, LiveCall } from './liveCall';

/** How long the simulated console rings before the line gives up on it and takes a message. */
const RING_MS = 20_000;
/** How long the pretend seller on another device takes to pick up when no console in this page is listening. */
const OTHER_DEVICE_MS = 2500;

type Later = (ms: number, action: () => void) => void;

class SimulatedCall implements LiveCall {
  private current: CallSnapshot = { phase: 'connecting', muted: false, connectedAt: null, end: null };
  private readonly listeners = new Set<() => void>();
  private readonly endedCallbacks: Array<() => void> = [];

  snapshot(): CallSnapshot {
    return this.current;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  private update(next: Partial<CallSnapshot>): void {
    this.current = { ...this.current, ...next };
    for (const listener of [...this.listeners]) listener();
  }

  advance(phase: CallPhase): void {
    if (this.current.phase === 'ended') return;
    this.update({
      phase,
      connectedAt: phase === 'connected' ? (this.current.connectedAt ?? Date.now()) : this.current.connectedAt,
      end: phase === 'ended' ? { ok: true, message: null } : null,
    });
    if (phase === 'ended') for (const callback of this.endedCallbacks) callback();
  }

  onEnded(callback: () => void): void {
    this.endedCallbacks.push(callback);
  }

  async setMuted(muted: boolean): Promise<void> {
    this.update({ muted });
  }

  async hangUp(): Promise<void> {
    this.advance('ended');
  }
}

/** Whoever hangs up first ends both legs, as on a real call. */
function bridge(first: SimulatedCall, second: SimulatedCall): void {
  first.onEnded(() => second.advance('ended'));
  second.onEnded(() => first.advance('ended'));
}

/** The seller console of this page while it is switched on; the line rings it instead of a pretend seller elsewhere. */
let seat: { name: string; onRing: (ring: IncomingRing) => void } | null = null;

function routeToSeller(id: string, profile: PhoneCallerProfile, caller: SimulatedCall, later: Later): void {
  const answered = (name: string) => line.advanceCall(id, 'in_call', `${name} answered; recording started`, { seller: name, recording: 'active' });
  const takeMessage = () => line.advanceCall(id, 'leaving_message', 'Nobody answered; taking a message', { recording: 'active' });

  const desk = seat;
  if (!desk) {
    if (line.isSellerOnline()) later(OTHER_DEVICE_MS, () => answered(line.SELLER_NAME));
    else takeMessage();
    return;
  }

  let ringing = true;
  const dropped = new Set<() => void>();
  const stopRinging = () => {
    if (!ringing) return;
    ringing = false;
    for (const listener of dropped) listener();
  };
  caller.onEnded(stopRinging);
  later(RING_MS, () => {
    if (!ringing) return;
    stopRinging();
    takeMessage();
  });

  desk.onRing({
    callerName: profile.name?.trim() || 'A customer',
    accept: async () => {
      if (!ringing) throw new Error('The caller hung up.');
      ringing = false;
      const seller = new SimulatedCall();
      seller.advance('connected');
      bridge(caller, seller);
      answered(desk.name);
      return seller;
    },
    decline: async () => {
      if (!ringing) return;
      ringing = false;
      takeMessage();
    },
    onEnded: (listener) => {
      dropped.add(listener);
      return () => {
        dropped.delete(listener);
      };
    },
  });
}

async function dial(profile: PhoneCallerProfile): Promise<LiveCall> {
  const id = line.beginCall(profile);
  const caller = new SimulatedCall();
  const timers: number[] = [];
  const later: Later = (ms, action) => {
    timers.push(window.setTimeout(action, ms));
  };
  caller.onEnded(() => {
    for (const timer of timers) window.clearTimeout(timer);
    line.endCall(id);
  });

  later(900, () => {
    caller.advance('ringing');
    line.advanceCall(id, 'connecting', 'Answering the call');
  });
  later(1800, () => {
    caller.advance('connected');
    line.advanceCall(id, 'notice', 'Playing the recording notice', { connected_at: new Date().toISOString() });
  });
  later(4300, () => {
    line.advanceCall(id, 'calling_seller', `Ringing ${seat?.name ?? line.SELLER_NAME}`, { notice: 'played' });
    routeToSeller(id, profile, caller, later);
  });
  return caller;
}

async function listen(displayName: string, handlers: ListenHandlers): Promise<Listener> {
  const mine = { name: displayName.trim() || line.SELLER_NAME, onRing: handlers.onRing };
  seat = mine;
  return {
    identity: 'mock-seller-console',
    stop: async () => {
      if (seat === mine) seat = null;
    },
  };
}

export const backend: CallingBackend = { dial, listen };
