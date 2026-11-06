import clsx from 'clsx';
import type { ReactNode } from 'react';
import type { IconType } from './types';

export function Card({
  className,
  children,
  as: Tag = 'section',
  ...rest
}: {
  className?: string;
  children: ReactNode;
  as?: 'section' | 'div' | 'article';
  'aria-labelledby'?: string;
  id?: string;
}) {
  return (
    <Tag className={clsx('card', className)} {...rest}>
      {children}
    </Tag>
  );
}

interface CardHeaderProps {
  title: ReactNode;
  subtitle?: ReactNode;
  icon?: IconType;
  actions?: ReactNode;
  className?: string;
  id?: string;
}

export function CardHeader({ title, subtitle, icon: Icon, actions, className, id }: CardHeaderProps) {
  return (
    <header className={clsx('flex flex-wrap items-start justify-between gap-x-3 gap-y-2 border-b border-neutral-100 px-4 py-3 sm:px-5', className)}>
      <div className="flex min-w-0 items-start gap-2.5">
        {Icon ? (
          <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-brand-50 text-brand-600">
            <Icon aria-hidden="true" className="h-4 w-4" />
          </span>
        ) : null}
        <div className="min-w-0">
          <h2 id={id} className="text-sm font-semibold leading-6 text-neutral-900">
            {title}
          </h2>
          {subtitle ? <p className="text-xs leading-5 text-neutral-500">{subtitle}</p> : null}
        </div>
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </header>
  );
}

export function CardBody({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={clsx('p-4 sm:p-5', className)}>{children}</div>;
}
