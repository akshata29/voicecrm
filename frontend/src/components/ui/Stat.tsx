import clsx from 'clsx';
import type { ReactNode } from 'react';
import type { IconType, Tone } from './types';

const ICON_TONES: Record<Tone, string> = {
  neutral: 'bg-neutral-100 text-neutral-600',
  brand: 'bg-brand-50 text-brand-600',
  success: 'bg-emerald-50 text-emerald-600',
  warning: 'bg-amber-50 text-amber-600',
  danger: 'bg-red-50 text-red-600',
  info: 'bg-sky-50 text-sky-600',
  violet: 'bg-violet-50 text-violet-600',
  orange: 'bg-orange-50 text-orange-600',
  teal: 'bg-teal-50 text-teal-600',
};

interface StatProps {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  icon?: IconType;
  tone?: Tone;
  className?: string;
  /** Marks a figure as illustrative / estimated. */
  illustrative?: boolean;
}

export function Stat({ label, value, hint, icon: Icon, tone = 'brand', className, illustrative }: StatProps) {
  return (
    <div className={clsx('card flex items-start justify-between gap-3 p-4', className)}>
      <div className="min-w-0">
        <p className="text-xs font-semibold uppercase tracking-wide text-neutral-500">{label}</p>
        <p className="tabular mt-1 truncate text-2xl font-semibold leading-8 text-neutral-900">{value}</p>
        {hint || illustrative ? (
          <p className="mt-0.5 text-xs text-neutral-500">
            {hint}
            {illustrative ? <span className="ml-1 rounded bg-amber-50 px-1 py-px font-medium text-amber-800 ring-1 ring-inset ring-amber-200">illustrative</span> : null}
          </p>
        ) : null}
      </div>
      {Icon ? (
        <span className={clsx('flex h-9 w-9 shrink-0 items-center justify-center rounded-lg', ICON_TONES[tone])}>
          <Icon aria-hidden="true" className="h-5 w-5" />
        </span>
      ) : null}
    </div>
  );
}
