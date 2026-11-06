import clsx from 'clsx';
import type { ReactNode } from 'react';
import type { IconType, Tone } from './types';

const TONES: Record<Tone, string> = {
  neutral: 'bg-neutral-100 text-neutral-700 ring-neutral-200',
  brand: 'bg-brand-50 text-brand-700 ring-brand-200',
  success: 'bg-emerald-50 text-emerald-800 ring-emerald-200',
  warning: 'bg-amber-50 text-amber-900 ring-amber-300',
  danger: 'bg-red-50 text-red-800 ring-red-200',
  info: 'bg-sky-50 text-sky-800 ring-sky-200',
  violet: 'bg-violet-50 text-violet-800 ring-violet-200',
  orange: 'bg-orange-50 text-orange-900 ring-orange-300',
  teal: 'bg-teal-50 text-teal-800 ring-teal-200',
};

const DOTS: Record<Tone, string> = {
  neutral: 'bg-neutral-400',
  brand: 'bg-brand-500',
  success: 'bg-emerald-500',
  warning: 'bg-amber-500',
  danger: 'bg-red-500',
  info: 'bg-sky-500',
  violet: 'bg-violet-500',
  orange: 'bg-orange-500',
  teal: 'bg-teal-500',
};

interface BadgeProps {
  tone?: Tone;
  icon?: IconType;
  dot?: boolean;
  pulse?: boolean;
  size?: 'sm' | 'md';
  className?: string;
  title?: string;
  children: ReactNode;
}

export function Badge({ tone = 'neutral', icon: Icon, dot, pulse, size = 'sm', className, title, children }: BadgeProps) {
  return (
    <span
      title={title}
      className={clsx(
        'inline-flex max-w-full items-center gap-1 whitespace-nowrap rounded-full font-medium ring-1 ring-inset',
        size === 'sm' ? 'px-2 py-0.5 text-xs' : 'px-2.5 py-1 text-sm',
        TONES[tone],
        className,
      )}
    >
      {dot ? <span aria-hidden="true" className={clsx('h-1.5 w-1.5 shrink-0 rounded-full', DOTS[tone], pulse && 'animate-pulse-soft')} /> : null}
      {Icon ? <Icon aria-hidden="true" className={clsx('shrink-0', size === 'sm' ? 'h-3.5 w-3.5' : 'h-4 w-4')} /> : null}
      <span className="truncate">{children}</span>
    </span>
  );
}
