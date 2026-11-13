/**
 * The hooks behind the Dialer and the seller console. They own the call lifecycle (the lazily loaded backend, effects that
 * survive React's double run, cleanup on unmount) so the components only render state. In the offline demo the backend is the
 * simulation; otherwise it is the Azure Communication Services Calling SDK, imported on first use.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { phoneApi, USE_MOCK } from '@/api/client';
import type { PhoneCallerProfile } from '@/api/types';
import { useLiveCall, type CallingBackend, type CallPhase, type IncomingRing, type Listener, type LiveCall } from './liveCall';

/** The API forgets a seller console that has not been heard from for a while (ttl_seconds); this keeps it well inside that. */
const HEARTBEAT_MS = 20_000;

/** The mock build never imports the SDK glue, and the real build never imports the simulation. */
function loadBackend(): Promise<CallingBackend> {
  return USE_MOCK ? import('./simulated').then((module) => module.backend) : import('./acs').then((module) => module.backend);
}

/** Why this browser cannot place or answer calls at all, or null when it can. */
export function callingUnsupported(): string | null {
  if (USE_MOCK) return null;
  const { protocol, hostname, port } = window.location;
  // The calling library accepts only https, file: and the host name "localhost": not 127.0.0.1, although browsers treat it as secure.
  if (protocol !== 'https:' && protocol !== 'file:' && hostname !== 'localhost') {
    const loopback = hostname === '127.0.0.1' || hostname === '[::1]';
    return loopback
      ? `Open the app at http://localhost${port ? `:${port}` : ''} instead: the calling library does not accept ${hostname}.`
      : 'Calls need a secure connection: open the app over https, or on http://localhost.';
  }
  if (!window.isSecureContext) return 'Calls need a secure connection: open the app over https, or on http://localhost.';
  if (!navigator.mediaDevices?.getUserMedia || typeof RTCPeerConnection === 'undefined') return 'This browser cannot place calls: it needs WebRTC and a microphone.';
  return null;
}

function messageOf(error: unknown): string {
  return error instanceof Error && error.message ? error.message : 'Something went wrong while setting up the call.';
}

// ---- the customer ---------------------------------------------------------------------------------------
type DialPhase = 'idle' | 'preparing' | CallPhase;

export function useDialer() {
  const [call, setCall] = useState<LiveCall | null>(null);
  const [preparing, setPreparing] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const live = useLiveCall(call);
  const mounted = useRef(true);
  const placed = useRef<LiveCall | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      void placed.current?.hangUp(); // leaving the page ends a call that is still going
    };
  }, []);

  const start = useCallback(async (profile: PhoneCallerProfile) => {
    setProblem(null);
    setCall(null); // how the last call ended is history once a new one is placed
    placed.current = null;
    setPreparing(true);
    try {
      const backend = await loadBackend();
      const next = await backend.dial(profile);
      if (!mounted.current) {
        void next.hangUp();
        return;
      }
      placed.current = next;
      setCall(next);
    } catch (failure) {
      if (mounted.current) setProblem(messageOf(failure));
    } finally {
      if (mounted.current) setPreparing(false);
    }
  }, []);

  const hangUp = useCallback(() => void call?.hangUp(), [call]);
  const toggleMute = useCallback(() => {
    if (!call || !live) return;
    call.setMuted(!live.muted).catch(() => setProblem('The microphone could not be switched. Try again.'));
  }, [call, live]);

  const phase: DialPhase = preparing ? 'preparing' : (live?.phase ?? 'idle');
  const ended = live?.phase === 'ended';
  return {
    phase,
    muted: live?.muted ?? false,
    connectedAt: live?.connectedAt ?? null,
    /** Something went wrong before or during the call, in plain language. */
    problem: problem ?? (ended && live.end?.ok === false ? live.end.message : null),
    /** The call reached the line and was then ended normally, so a recording is on its way to the Inbox. */
    recorded: ended && live.connectedAt !== null && live.end?.ok !== false,
    start,
    hangUp,
    toggleMute,
  };
}

// ---- the seller -----------------------------------------------------------------------------------------
export type ConsolePhase = 'off' | 'starting' | 'waiting' | 'ringing' | 'in_call';

interface ConsoleOutcome {
  seconds: number;
  /** Set when the call ended badly or could not be answered. */
  message: string | null;
}

/**
 * Keeps this browser signed in as the seller while `enabled`: a CallAgent that rings for incoming calls, and a registration
 * with the API that is renewed every 20 s and removed again when the console goes off or the page closes. If it cannot start
 * it reports why and calls `onFailed` so the caller can switch the console off.
 */
export function useSellerConsole(enabled: boolean, displayName: string, onFailed: () => void) {
  const [ready, setReady] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [connection, setConnection] = useState<string | null>(null);
  const [contactLost, setContactLost] = useState(false);
  const [ring, setRing] = useState<IncomingRing | null>(null);
  const [call, setCall] = useState<LiveCall | null>(null);
  const [outcome, setOutcome] = useState<ConsoleOutcome | null>(null);
  const [answeredCaller, setAnsweredCaller] = useState<string | null>(null);
  const live = useLiveCall(call);

  // The effect outlives renders, so what it reads later goes through refs.
  const active = useRef(false);
  const ringing = useRef<IncomingRing | null>(null);
  const answering = useRef<LiveCall | null>(null);
  const unwatch = useRef<() => void>(() => undefined);
  const latest = useRef({ displayName, onFailed });
  latest.current = { displayName, onFailed };

  useEffect(() => {
    if (!enabled) return;
    active.current = true;
    let cancelled = false;
    let listener: Listener | null = null;
    let registered: string | null = null;
    let beat: number | undefined;

    const register = (identity: string) => phoneApi.registerSeller({ identity, name: latest.current.displayName.trim() || undefined });
    const leaveOnUnload = () => {
      if (registered) phoneApi.unregisterSellerOnUnload(registered);
    };

    const onRing = (incoming: IncomingRing) => {
      if (ringing.current || answering.current) {
        void incoming.decline().catch(() => undefined); // one call at a time
        return;
      }
      ringing.current = incoming;
      setOutcome(null);
      setRing(incoming);
      unwatch.current = incoming.onEnded(() => {
        if (ringing.current !== incoming) return;
        ringing.current = null;
        setRing(null);
      });
    };

    const start = async () => {
      setProblem(null);
      setOutcome(null);
      try {
        const backend = await loadBackend();
        if (cancelled) return;
        const created = await backend.listen(latest.current.displayName, { onRing, onTrouble: setConnection });
        if (cancelled) {
          await created.stop();
          return;
        }
        listener = created;
        registered = created.identity;
        await register(created.identity);
        if (cancelled) {
          void phoneApi.unregisterSeller(created.identity).catch(() => undefined); // the first removal may have run before this registration landed
          return;
        }
        beat = window.setInterval(() => void register(created.identity).then(() => setContactLost(false), () => setContactLost(true)), HEARTBEAT_MS);
        window.addEventListener('pagehide', leaveOnUnload);
        setReady(true);
      } catch (failure) {
        if (cancelled) return;
        setProblem(messageOf(failure));
        latest.current.onFailed();
      }
    };
    void start();

    return () => {
      cancelled = true;
      active.current = false;
      window.clearInterval(beat);
      window.removeEventListener('pagehide', leaveOnUnload);
      unwatch.current();
      void answering.current?.hangUp();
      void ringing.current?.decline().catch(() => undefined);
      answering.current = null;
      ringing.current = null;
      setReady(false);
      setRing(null);
      setCall(null);
      setConnection(null);
      setContactLost(false);
      if (registered) void phoneApi.unregisterSeller(registered).catch(() => undefined);
      void listener?.stop();
    };
  }, [enabled]);

  useEffect(() => {
    if (!call || live?.phase !== 'ended') return;
    answering.current = null;
    setCall(null);
    setOutcome({ seconds: live.connectedAt ? (Date.now() - live.connectedAt) / 1000 : 0, message: live.end?.ok === false ? live.end.message : null });
  }, [call, live]);

  const accept = useCallback(async () => {
    const incoming = ringing.current;
    if (!incoming) return;
    ringing.current = null;
    unwatch.current();
    setRing(null);
    setAnsweredCaller(incoming.callerName);
    try {
      const next = await incoming.accept();
      if (!active.current) {
        void next.hangUp(); // switched off while the microphone prompt was open
        return;
      }
      answering.current = next;
      setCall(next);
    } catch (failure) {
      setOutcome({ seconds: 0, message: messageOf(failure) });
    }
  }, []);

  const decline = useCallback(async () => {
    const incoming = ringing.current;
    if (!incoming) return;
    ringing.current = null;
    unwatch.current();
    setRing(null);
    await incoming.decline().catch(() => undefined);
  }, []);

  const hangUp = useCallback(() => void answering.current?.hangUp(), []);
  const toggleMute = useCallback(() => {
    if (!call || !live) return;
    call.setMuted(!live.muted).catch(() => setOutcome({ seconds: 0, message: 'The microphone could not be switched. Try again.' }));
  }, [call, live]);

  const phase: ConsolePhase = !enabled ? 'off' : call ? 'in_call' : ring ? 'ringing' : ready ? 'waiting' : 'starting';
  return {
    phase,
    /** Why the console could not start; it has then switched itself off. */
    problem,
    /** Calls cannot reach this console right now; it keeps trying. */
    trouble: connection ?? (contactLost ? 'Lost contact with VoiceCRM, so the line may not see this console. Trying again.' : null),
    ring,
    /** Who is on the line while a call is being answered or is going on. */
    callerName: answeredCaller,
    call: live,
    outcome,
    accept,
    decline,
    hangUp,
    toggleMute,
  };
}
