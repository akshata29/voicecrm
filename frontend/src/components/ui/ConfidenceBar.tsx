import clsx from 'clsx';
import { formatPercent } from '@/lib/format';

interface ConfidenceBarProps {
  value: number;
  /** Review threshold: below it the bar turns red (a human pick is needed). */
  threshold?: number;
  label?: string;
  showValue?: boolean;
  className?: string;
}

export function confidenceColor(value: number, threshold = 0.75): string {
  if (value >= 0.9) return 'bg-emerald-500';
  if (value >= threshold) return 'bg-amber-400';
  return 'bg-red-500';
}

export function ConfidenceBar({ value, threshold = 0.75, label = 'Confidence', showValue = true, className }: ConfidenceBarProps) {
  const clamped = Math.max(0, Math.min(1, value));
  const pct = Math.round(clamped * 100);
  return (
    <div className={clsx('flex items-center gap-2', className)}>
      <div
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
        aria-valuetext={`${pct}%`}
        className="h-2 w-full min-w-[3.5rem] overflow-hidden rounded-full bg-neutral-200"
      >
        <div className={clsx('h-full rounded-full transition-all', confidenceColor(clamped, threshold))} style={{ width: `${pct}%` }} />
      </div>
      {showValue ? <span className="tabular w-10 shrink-0 text-right text-xs font-semibold text-neutral-700">{formatPercent(clamped)}</span> : null}
    </div>
  );
}
