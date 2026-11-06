import { ArrowPathIcon, ExclamationTriangleIcon, InboxIcon } from '@heroicons/react/24/outline';
import clsx from 'clsx';
import type { ReactNode } from 'react';
import { getErrorMessage } from '@/api/client';
import { Button } from './Button';
import { Spinner } from './Spinner';
import type { IconType } from './types';

export function Skeleton({ className }: { className?: string }) {
  return (
    <div aria-hidden="true" className={clsx('relative overflow-hidden rounded bg-neutral-200/70', className)}>
      <div className="absolute inset-0 -translate-x-full animate-shimmer bg-gradient-to-r from-transparent via-white/60 to-transparent" />
    </div>
  );
}

export function LoadingBlock({ label = 'Loading\u2026', className }: { label?: string; className?: string }) {
  return (
    <div role="status" aria-live="polite" className={clsx('flex items-center justify-center gap-2 px-4 py-10 text-sm text-neutral-500', className)}>
      <Spinner className="h-5 w-5 text-brand-500" />
      <span>{label}</span>
    </div>
  );
}

export function ListSkeleton({ rows = 4, className }: { rows?: number; className?: string }) {
  return (
    <div role="status" aria-label="Loading" className={clsx('space-y-3', className)}>
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="card flex items-center gap-4 p-4">
          <Skeleton className="h-9 w-9 shrink-0 rounded-lg" />
          <div className="flex-1 space-y-2">
            <Skeleton className="h-4 w-1/3" />
            <Skeleton className="h-3 w-2/3" />
          </div>
          <Skeleton className="hidden h-6 w-24 sm:block" />
        </div>
      ))}
    </div>
  );
}

interface ErrorStateProps {
  error: unknown;
  title?: string;
  onRetry?: () => void;
  className?: string;
  compact?: boolean;
}

export function ErrorState({ error, title = 'Something went wrong', onRetry, className, compact }: ErrorStateProps) {
  return (
    <div role="alert" className={clsx('rounded-lg border border-red-200 bg-red-50 text-red-900', compact ? 'p-3' : 'p-5', className)}>
      <div className="flex items-start gap-3">
        <ExclamationTriangleIcon aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0 text-red-600" />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold">{title}</p>
          <p className="mt-0.5 break-words text-sm text-red-800">{getErrorMessage(error)}</p>
          {onRetry ? (
            <Button variant="secondary" size="sm" icon={ArrowPathIcon} onClick={onRetry} className="mt-3">
              Try again
            </Button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

interface EmptyStateProps {
  icon?: IconType;
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}

export function EmptyState({ icon: Icon = InboxIcon, title, description, action, className }: EmptyStateProps) {
  return (
    <div className={clsx('flex flex-col items-center justify-center px-6 py-12 text-center', className)}>
      <span className="flex h-12 w-12 items-center justify-center rounded-full bg-neutral-100 text-neutral-400">
        <Icon aria-hidden="true" className="h-6 w-6" />
      </span>
      <h3 className="mt-3 text-sm font-semibold text-neutral-800">{title}</h3>
      {description ? <p className="mt-1 max-w-md text-sm text-neutral-500">{description}</p> : null}
      {action ? <div className="mt-4">{action}</div> : null}
    </div>
  );
}

interface QueryLike<T> {
  data: T | undefined;
  error: unknown;
  isPending: boolean;
  isError: boolean;
  refetch: () => unknown;
}

interface QueryStateProps<T> {
  query: QueryLike<T>;
  children: (data: T) => ReactNode;
  isEmpty?: (data: T) => boolean;
  empty?: ReactNode;
  loading?: ReactNode;
  errorTitle?: string;
}

/** Loading / error / empty / data in one place so every screen handles all four states the same way. */
export function QueryState<T>({ query, children, isEmpty, empty, loading, errorTitle }: QueryStateProps<T>) {
  if (query.isPending) return <>{loading ?? <LoadingBlock />}</>;
  if (query.isError || query.data === undefined) {
    return <ErrorState error={query.error} title={errorTitle} onRetry={() => void query.refetch()} />;
  }
  if (isEmpty?.(query.data)) return <>{empty ?? <EmptyState title="Nothing here yet" />}</>;
  return <>{children(query.data)}</>;
}
