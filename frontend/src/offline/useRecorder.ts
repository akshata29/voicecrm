import { useCallback, useEffect, useRef, useState } from 'react';

export type RecorderState = 'idle' | 'requesting' | 'recording' | 'paused' | 'recorded';

export interface RecordingResult {
  blob: Blob;
  mimeType: string;
  durationSeconds: number;
  startedAt: string;
}

const MIME_CANDIDATES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus'];

export function recorderSupported(): boolean {
  return typeof navigator !== 'undefined' && Boolean(navigator.mediaDevices?.getUserMedia) && typeof MediaRecorder !== 'undefined';
}

function pickMimeType(): string {
  return MIME_CANDIDATES.find((type) => MediaRecorder.isTypeSupported(type)) ?? '';
}

function friendlyError(error: unknown): string {
  if (error instanceof DOMException) {
    if (error.name === 'NotAllowedError' || error.name === 'SecurityError') return 'Microphone access was blocked. Allow the microphone for this site, or choose an audio file instead.';
    if (error.name === 'NotFoundError') return 'No microphone was found on this device. Choose an audio file instead.';
    if (error.name === 'NotReadableError') return 'The microphone is in use by another app.';
  }
  return error instanceof Error ? error.message : 'Could not start the recorder.';
}

/** Reads an audio file's length from its metadata (best effort; 0 when unknown). */
export function probeDuration(blob: Blob): Promise<number> {
  return new Promise((resolve) => {
    const audio = document.createElement('audio');
    const url = URL.createObjectURL(blob);
    const done = (seconds: number) => {
      URL.revokeObjectURL(url);
      resolve(Number.isFinite(seconds) ? Math.round(seconds) : 0);
    };
    const timeout = window.setTimeout(() => done(0), 2000);
    audio.preload = 'metadata';
    audio.onloadedmetadata = () => {
      window.clearTimeout(timeout);
      done(audio.duration);
    };
    audio.onerror = () => {
      window.clearTimeout(timeout);
      done(0);
    };
    audio.src = url;
  });
}

/** MediaRecorder wrapper: start / pause / resume / stop, a live level meter, and a screen wake lock while recording. */
export function useRecorder() {
  const [state, setState] = useState<RecorderState>('idle');
  const [error, setError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [level, setLevel] = useState(0);
  const [result, setResult] = useState<RecordingResult | null>(null);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const startedAtRef = useRef('');
  const accumulatedRef = useRef(0);
  const segmentStartRef = useRef<number | null>(null);
  const finalMsRef = useRef(0);
  const timerRef = useRef<number | null>(null);
  const meterRef = useRef<{ ctx: AudioContext; raf: number } | null>(null);
  const wakeLockRef = useRef<WakeLockSentinel | null>(null);

  const elapsedMs = useCallback(() => accumulatedRef.current + (segmentStartRef.current === null ? 0 : performance.now() - segmentStartRef.current), []);

  const stopMeter = useCallback(() => {
    const meter = meterRef.current;
    if (meter) {
      cancelAnimationFrame(meter.raf);
      void meter.ctx.close().catch(() => undefined);
      meterRef.current = null;
    }
    setLevel(0);
  }, []);

  const teardown = useCallback(() => {
    if (timerRef.current !== null) window.clearInterval(timerRef.current);
    timerRef.current = null;
    stopMeter();
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    void wakeLockRef.current?.release().catch(() => undefined);
    wakeLockRef.current = null;
  }, [stopMeter]);

  useEffect(() => teardown, [teardown]);

  const startMeter = useCallback((stream: MediaStream) => {
    try {
      const Ctx = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!Ctx) return;
      const ctx = new Ctx();
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      ctx.createMediaStreamSource(stream).connect(analyser);
      const data = new Uint8Array(analyser.fftSize);
      let last = 0;
      const loop = () => {
        analyser.getByteTimeDomainData(data);
        let sum = 0;
        for (const value of data) {
          const centred = (value - 128) / 128;
          sum += centred * centred;
        }
        const next = Math.min(1, Math.sqrt(sum / data.length) * 3.2);
        if (Math.abs(next - last) > 0.03) {
          last = next;
          setLevel(next);
        }
        if (meterRef.current) meterRef.current.raf = requestAnimationFrame(loop);
      };
      meterRef.current = { ctx, raf: requestAnimationFrame(loop) };
    } catch {
      // The meter is cosmetic; recording works without it.
    }
  }, []);

  const start = useCallback(async () => {
    setError(null);
    setResult(null);
    if (!recorderSupported()) {
      setError('This browser cannot record audio here (a secure connection is required). Choose an audio file instead.');
      return;
    }
    setState('requesting');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      streamRef.current = stream;
      const mimeType = pickMimeType();
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      chunksRef.current = [];
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) chunksRef.current.push(event.data);
      };
      recorder.onerror = () => {
        setError('The recorder stopped unexpectedly. Your audio so far was kept.');
      };
      recorder.onstop = () => {
        const type = recorder.mimeType || mimeType || 'audio/webm';
        const blob = new Blob(chunksRef.current, { type });
        teardown();
        recorderRef.current = null;
        if (blob.size === 0) {
          setError('Nothing was recorded. Try again.');
          setState('idle');
          return;
        }
        setResult({ blob, mimeType: type, durationSeconds: Math.max(1, Math.round(finalMsRef.current / 1000)), startedAt: startedAtRef.current });
        setState('recorded');
      };
      recorderRef.current = recorder;
      accumulatedRef.current = 0;
      segmentStartRef.current = performance.now();
      startedAtRef.current = new Date().toISOString();
      setElapsed(0);
      recorder.start(1000);
      startMeter(stream);
      timerRef.current = window.setInterval(() => setElapsed(elapsedMs() / 1000), 250);
      void navigator.wakeLock?.request('screen').then((lock) => (wakeLockRef.current = lock)).catch(() => undefined);
      setState('recording');
    } catch (failure) {
      teardown();
      setError(friendlyError(failure));
      setState('idle');
    }
  }, [elapsedMs, startMeter, teardown]);

  const pause = useCallback(() => {
    const recorder = recorderRef.current;
    if (!recorder || recorder.state !== 'recording') return;
    recorder.pause();
    accumulatedRef.current = elapsedMs();
    segmentStartRef.current = null;
    setState('paused');
  }, [elapsedMs]);

  const resume = useCallback(() => {
    const recorder = recorderRef.current;
    if (!recorder || recorder.state !== 'paused') return;
    recorder.resume();
    segmentStartRef.current = performance.now();
    setState('recording');
  }, []);

  const stop = useCallback(() => {
    const recorder = recorderRef.current;
    if (!recorder || recorder.state === 'inactive') return;
    finalMsRef.current = elapsedMs();
    segmentStartRef.current = null;
    recorder.stop();
  }, [elapsedMs]);

  const discard = useCallback(() => {
    setResult(null);
    setError(null);
    setElapsed(0);
    setState('idle');
  }, []);

  /** Fallback when the microphone is unavailable or blocked: use an existing audio file. */
  const loadFile = useCallback(async (file: File) => {
    setError(null);
    const durationSeconds = await probeDuration(file);
    setResult({ blob: file, mimeType: file.type || 'audio/webm', durationSeconds, startedAt: new Date().toISOString() });
    setElapsed(durationSeconds);
    setState('recorded');
  }, []);

  return { state, error, elapsed, level, result, start, pause, resume, stop, discard, loadFile };
}
