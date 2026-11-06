import { CheckCircleIcon, ExclamationCircleIcon, InformationCircleIcon, XMarkIcon } from '@heroicons/react/24/solid';
import clsx from 'clsx';
import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';

type ToastTone = 'success' | 'error' | 'info';

interface ToastAction {
  label: string;
  to: string;
}

interface ToastItem {
  id: number;
  tone: ToastTone;
  title: string;
  message?: string;
  action?: ToastAction;
}

interface ToastApi {
  success: (title: string, options?: { message?: string; action?: ToastAction }) => void;
  error: (title: string, options?: { message?: string }) => void;
  info: (title: string, options?: { message?: string; action?: ToastAction }) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

const STYLES: Record<ToastTone, { box: string; icon: string; Icon: typeof CheckCircleIcon }> = {
  success: { box: 'border-emerald-200', icon: 'text-emerald-600', Icon: CheckCircleIcon },
  error: { box: 'border-red-200', icon: 'text-red-600', Icon: ExclamationCircleIcon },
  info: { box: 'border-sky-200', icon: 'text-sky-600', Icon: InformationCircleIcon },
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => setToasts((current) => current.filter((toast) => toast.id !== id)), []);

  const push = useCallback(
    (tone: ToastTone, title: string, options?: { message?: string; action?: ToastAction }) => {
      const id = nextId.current++;
      setToasts((current) => [...current.slice(-3), { id, tone, title, message: options?.message, action: options?.action }]);
      window.setTimeout(() => dismiss(id), tone === 'error' ? 9000 : 6000);
    },
    [dismiss],
  );

  const api = useMemo<ToastApi>(
    () => ({
      success: (title, options) => push('success', title, options),
      error: (title, options) => push('error', title, options),
      info: (title, options) => push('info', title, options),
    }),
    [push],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div aria-live="polite" aria-atomic="false" className="pointer-events-none fixed inset-x-3 top-16 z-[60] flex flex-col items-end gap-2 sm:left-auto sm:right-4 sm:w-96">
        {toasts.map((toast) => {
          const { box, icon, Icon } = STYLES[toast.tone];
          return (
            <div key={toast.id} role={toast.tone === 'error' ? 'alert' : 'status'} className={clsx('pointer-events-auto flex w-full animate-fade-in items-start gap-3 rounded-lg border bg-white p-3 shadow-pop', box)}>
              <Icon aria-hidden="true" className={clsx('mt-0.5 h-5 w-5 shrink-0', icon)} />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-semibold text-neutral-900">{toast.title}</p>
                {toast.message ? <p className="mt-0.5 break-words text-sm text-neutral-600">{toast.message}</p> : null}
                {toast.action ? (
                  <Link to={toast.action.to} onClick={() => dismiss(toast.id)} className="mt-1 inline-block text-sm font-semibold text-brand-600 hover:underline">
                    {toast.action.label}
                  </Link>
                ) : null}
              </div>
              <button type="button" onClick={() => dismiss(toast.id)} className="rounded p-0.5 text-neutral-400 hover:bg-neutral-100 hover:text-neutral-700" aria-label="Dismiss notification">
                <XMarkIcon aria-hidden="true" className="h-4 w-4" />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const context = useContext(ToastContext);
  if (!context) throw new Error('useToast must be used inside <ToastProvider>');
  return context;
}
