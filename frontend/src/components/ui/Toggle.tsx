import clsx from 'clsx';
import { useId } from 'react';

interface ToggleProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  description?: string;
  disabled?: boolean;
  /** Hide the visible label (the switch keeps an accessible name). */
  hideLabel?: boolean;
  className?: string;
}

export function Toggle({ checked, onChange, label, description, disabled, hideLabel, className }: ToggleProps) {
  const id = useId();
  return (
    <div className={clsx('flex items-start justify-between gap-3', className)}>
      {hideLabel ? null : (
        <div className="min-w-0">
          <label htmlFor={id} className="block cursor-pointer text-sm font-medium text-neutral-800">
            {label}
          </label>
          {description ? <p className="text-xs text-neutral-500">{description}</p> : null}
        </div>
      )}
      <button
        id={id}
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={hideLabel ? label : undefined}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={clsx(
          'relative mt-0.5 inline-flex h-6 w-11 shrink-0 items-center rounded-full border border-transparent transition-colors disabled:cursor-not-allowed disabled:opacity-50',
          checked ? 'bg-brand-500' : 'bg-neutral-300',
        )}
      >
        <span
          aria-hidden="true"
          className={clsx('inline-block rounded-full bg-white shadow transition-transform', checked ? 'translate-x-[1.375rem]' : 'translate-x-0.5')}
          style={{ height: '1.125rem', width: '1.125rem' }}
        />
      </button>
    </div>
  );
}
