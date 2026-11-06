import { CheckCircleIcon, ExclamationCircleIcon, ExclamationTriangleIcon, MinusCircleIcon } from '@heroicons/react/24/solid';
import clsx from 'clsx';
import type { PreflightCheck, PreflightReadiness, PreflightStatus } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import type { IconType, Tone } from '@/components/ui/types';
import { formatDurationSeconds } from '@/lib/format';

const STATUS: Record<PreflightStatus, { label: string; icon: IconType; color: string }> = {
  pass: { label: 'Pass', icon: CheckCircleIcon, color: 'text-emerald-500' },
  warn: { label: 'Warning', icon: ExclamationTriangleIcon, color: 'text-amber-500' },
  fail: { label: 'Fail', icon: ExclamationCircleIcon, color: 'text-red-500' },
  skip: { label: 'Skipped', icon: MinusCircleIcon, color: 'text-neutral-400' },
};

/** The probes of the preflight, one per line, with the concrete fix under every failure. */
export function CheckList({ checks, className }: { checks: PreflightCheck[]; className?: string }) {
  return (
    <ul className={clsx('divide-y divide-neutral-100 rounded-md border border-neutral-200 bg-white', className)} aria-label="Service checks">
      {checks.map((check) => {
        const { icon: Icon, color, label } = STATUS[check.status];
        return (
          <li key={check.name} className="flex gap-3 px-3 py-2.5 text-sm">
            <Icon aria-hidden="true" className={clsx('mt-0.5 h-5 w-5 shrink-0', color)} />
            <div className="min-w-0 flex-1">
              <p className="flex flex-wrap items-baseline justify-between gap-x-3">
                <span className="font-medium text-neutral-900">
                  <span className="sr-only">{label}: </span>
                  {check.name}
                </span>
                {check.seconds !== null ? <span className="tabular text-xs text-neutral-500">{formatDurationSeconds(check.seconds)}</span> : null}
              </p>
              {check.detail ? <p className="break-words text-xs text-neutral-600">{check.detail}</p> : null}
              {check.hint ? <p className="mt-0.5 break-words text-xs text-amber-800">Fix: {check.hint}</p> : null}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

const VERDICT_TONE: Record<string, Tone> = { READY: 'success', 'NOT READY': 'danger', 'NOT CONFIGURED': 'neutral' };

/** READY / NOT READY for the three live scenarios, derived from the checks (same verdicts as the preflight CLI). */
export function ReadinessList({ items, only }: { items: PreflightReadiness[]; only?: string[] }) {
  const shown = only ? items.filter((item) => only.includes(item.scenario.charAt(0))) : items;
  return (
    <ul className="space-y-1.5" aria-label="Scenario readiness">
      {shown.map((item) => (
        <li key={item.scenario} className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
          <Badge tone={VERDICT_TONE[item.verdict] ?? 'neutral'} className="w-32 justify-center">
            {item.verdict}
          </Badge>
          <span className="font-medium text-neutral-800">{item.scenario}</span>
          <span className="min-w-0 truncate text-xs text-neutral-500" title={item.why}>
            {item.why}
          </span>
        </li>
      ))}
    </ul>
  );
}
