/**
 * What the phone components need from a call, whether it runs on the Azure Communication Services Calling SDK (acs.ts) or is
 * simulated in the browser (simulated.ts, npm run dev:mock). The components never touch the SDK, so it stays in its own chunk.
 */
import { useCallback, useSyncExternalStore } from 'react';
import type { PhoneCallerProfile } from '@/api/types';

export type CallPhase = 'connecting' | 'ringing' | 'connected' | 'ended';

export interface CallEnd {
  /** False when the call ended because something went wrong; `message` then says what, in plain language. */
  ok: boolean;
  message: string | null;
}

export interface CallSnapshot {
  phase: CallPhase;
  muted: boolean;
  /** When audio first flowed (ms since the epoch); null while the call is still being set up. */
  connectedAt: number | null;
  /** Set once `phase` is 'ended'. */
  end: CallEnd | null;
}

export interface LiveCall {
  /** The same object until something changes: useSyncExternalStore depends on it. */
  snapshot(): CallSnapshot;
  subscribe(listener: () => void): () => void;
  setMuted(muted: boolean): Promise<void>;
  hangUp(): Promise<void>;
}

export interface IncomingRing {
  callerName: string;
  accept(): Promise<LiveCall>;
  decline(): Promise<void>;
  /** The caller gave up before anybody answered. Returns the function that stops listening. */
  onEnded(listener: () => void): () => void;
}

export interface ListenHandlers {
  onRing(ring: IncomingRing): void;
  /** A plain-language reason calls cannot ring here right now, or null once they can again. */
  onTrouble(message: string | null): void;
}

export interface Listener {
  /** The ACS identity this browser answers as; the API needs it to register the seller console. */
  identity: string;
  stop(): Promise<void>;
}

export interface CallingBackend {
  /** Customer side: ask for the microphone, sign in as a caller and ring the line. Rejects with a plain-language Error. */
  dial(profile: PhoneCallerProfile): Promise<LiveCall>;
  /** Seller side: sign in as the seller and hand every incoming call to `handlers`. Rejects with a plain-language Error. */
  listen(displayName: string, handlers: ListenHandlers): Promise<Listener>;
}

const noop = () => undefined;

/** The live state of `call`, or null when there is none. */
export function useLiveCall(call: LiveCall | null): CallSnapshot | null {
  const subscribe = useCallback((listener: () => void) => (call ? call.subscribe(listener) : noop), [call]);
  const getSnapshot = useCallback(() => (call ? call.snapshot() : null), [call]);
  return useSyncExternalStore(subscribe, getSnapshot);
}
