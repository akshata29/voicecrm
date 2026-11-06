import clsx from 'clsx';
import type { ReactNode } from 'react';
import type { PreflightReadiness } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import type { Tone } from '@/components/ui/types';

const TONE: Record<string, Tone> = { READY: 'success', 'NOT READY': 'danger', 'NOT CONFIGURED': 'neutral' };

/** The A / B / C heading of a scenario, with its readiness verdict once "Check services now" has run. */
export function ScenarioHeading({ id, letter, title, description, verdict, className }: { id: string; letter: string; title: string; description: ReactNode; verdict?: PreflightReadiness | undefined; className?: string }) {
  return (
    <header className={clsx('mb-3 flex flex-wrap items-start justify-between gap-x-4 gap-y-2', className)}>
      <div className="flex min-w-0 items-start gap-3">
        <span aria-hidden="true" className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-brand-500 text-base font-bold text-white shadow-card">
          {letter}
        </span>
        <div className="min-w-0">
          <h2 id={id} className="text-lg font-semibold leading-7 text-neutral-900">
            {title}
          </h2>
          <p className="max-w-3xl text-sm text-neutral-600">{description}</p>
        </div>
      </div>
      {verdict ? (
        <Badge tone={TONE[verdict.verdict] ?? 'neutral'} size="md" title={verdict.why}>
          {verdict.verdict === 'READY' ? 'Ready' : verdict.verdict === 'NOT READY' ? `Not ready: ${verdict.why}` : 'Not configured'}
        </Badge>
      ) : null}
    </header>
  );
}
