/** Small, generic React hooks. */
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';

/** Re-renders every `intervalMs` and returns the current time (ms). */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}

function subscribeOnline(callback: () => void) {
  window.addEventListener('online', callback);
  window.addEventListener('offline', callback);
  return () => {
    window.removeEventListener('online', callback);
    window.removeEventListener('offline', callback);
  };
}

export function useOnlineStatus(): boolean {
  return useSyncExternalStore(
    subscribeOnline,
    () => navigator.onLine,
    () => true,
  );
}

export function useDebouncedValue<T>(value: T, delayMs = 250): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(id);
  }, [value, delayMs]);
  return debounced;
}

/** useState persisted to localStorage (JSON). Falls back silently when storage is unavailable. */
export function useLocalStorageState<T>(key: string, initial: T): [T, (value: T | ((previous: T) => T)) => void] {
  const [state, setState] = useState<T>(() => {
    try {
      const raw = window.localStorage.getItem(key);
      return raw === null ? initial : (JSON.parse(raw) as T);
    } catch {
      return initial;
    }
  });
  const update = useCallback(
    (value: T | ((previous: T) => T)) => {
      setState((previous) => {
        const next = typeof value === 'function' ? (value as (previous: T) => T)(previous) : value;
        try {
          window.localStorage.setItem(key, JSON.stringify(next));
        } catch {
          // storage may be full or blocked; the in-memory value still works
        }
        return next;
      });
    },
    [key],
  );
  return [state, update];
}

/** Calls `callback` when `value` changes after the first render. */
export function useOnChange<T>(value: T, callback: (value: T, previous: T) => void) {
  const previous = useRef(value);
  const latest = useRef(callback);
  latest.current = callback;
  useEffect(() => {
    if (previous.current !== value) {
      const before = previous.current;
      previous.current = value;
      latest.current(value, before);
    }
  }, [value]);
}

/** Scrolls the element behind the returned ref into view once, when it first appears (a result that opens below the fold). */
export function useScrollIntoViewOnMount<T extends HTMLElement>(block: ScrollLogicalPosition = 'start') {
  const ref = useRef<T>(null);
  useEffect(() => {
    ref.current?.scrollIntoView({ behavior: 'smooth', block });
  }, [block]);
  return ref;
}

export function useDocumentTitle(title: string) {
  useEffect(() => {
    const previous = document.title;
    document.title = title ? `${title} \u00b7 VoiceCRM` : 'VoiceCRM';
    return () => {
      document.title = previous;
    };
  }, [title]);
}
