import clsx from 'clsx';
import { useRef, type KeyboardEvent } from 'react';
import type { IconType } from './types';

export interface TabItem<T extends string> {
  id: T;
  label: string;
  count?: number;
  icon?: IconType;
}

export const tabIds = (prefix: string, id: string) => ({ tab: `${prefix}-tab-${id}`, panel: `${prefix}-panel-${id}` });

interface TabsProps<T extends string> {
  tabs: readonly TabItem<T>[];
  value: T;
  onChange: (value: T) => void;
  label: string;
  idPrefix: string;
  className?: string;
}

/** Accessible tablist: roving tabindex + arrow/Home/End keys. Render the matching role="tabpanel" yourself. */
export function Tabs<T extends string>({ tabs, value, onChange, label, idPrefix, className }: TabsProps<T>) {
  const refs = useRef<Array<HTMLButtonElement | null>>([]);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const index = tabs.findIndex((tab) => tab.id === value);
    let next = index;
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
    else if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = tabs.length - 1;
    else return;
    event.preventDefault();
    const target = tabs[next];
    if (target) {
      onChange(target.id);
      refs.current[next]?.focus();
    }
  };

  return (
    <div role="tablist" aria-label={label} onKeyDown={onKeyDown} className={clsx('no-scrollbar -mb-px flex gap-1 overflow-x-auto border-b border-neutral-200', className)}>
      {tabs.map((tab, index) => {
        const selected = tab.id === value;
        const Icon = tab.icon;
        return (
          <button
            key={tab.id}
            ref={(node) => {
              refs.current[index] = node;
            }}
            id={tabIds(idPrefix, tab.id).tab}
            role="tab"
            type="button"
            aria-selected={selected}
            aria-controls={tabIds(idPrefix, tab.id).panel}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(tab.id)}
            className={clsx(
              'inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap border-b-2 px-3 py-2.5 text-sm font-semibold transition-colors',
              selected ? 'border-brand-500 text-brand-700' : 'border-transparent text-neutral-600 hover:border-neutral-300 hover:text-neutral-900',
            )}
          >
            {Icon ? <Icon aria-hidden="true" className="h-4 w-4" /> : null}
            {tab.label}
            {tab.count !== undefined ? (
              <span className={clsx('tabular rounded-full px-1.5 text-xs', selected ? 'bg-brand-100 text-brand-700' : 'bg-neutral-100 text-neutral-600')}>{tab.count}</span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}
