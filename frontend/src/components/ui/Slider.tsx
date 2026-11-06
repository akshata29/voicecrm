import clsx from 'clsx';
import { useEffect, useId, useState } from 'react';

interface SliderProps {
  value: number;
  min: number;
  max: number;
  step: number;
  label: string;
  /** Called once when the user releases the handle / key, not on every drag tick. */
  onCommit: (value: number) => void;
  format?: (value: number) => string;
  disabled?: boolean;
  className?: string;
  description?: string;
}

export function Slider({ value, min, max, step, label, onCommit, format, disabled, className, description }: SliderProps) {
  const id = useId();
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);

  const commit = () => {
    if (draft !== value) onCommit(draft);
  };
  const pct = ((draft - min) / (max - min)) * 100;

  return (
    <div className={className}>
      <div className="flex items-baseline justify-between gap-3">
        <label htmlFor={id} className="text-sm font-medium text-neutral-800">
          {label}
        </label>
        <output htmlFor={id} className="tabular text-sm font-semibold text-brand-700">
          {format ? format(draft) : draft}
        </output>
      </div>
      {description ? <p className="text-xs text-neutral-500">{description}</p> : null}
      <input
        id={id}
        type="range"
        min={min}
        max={max}
        step={step}
        value={draft}
        disabled={disabled}
        onChange={(event) => setDraft(Number(event.target.value))}
        onPointerUp={commit}
        onKeyUp={commit}
        onBlur={commit}
        className={clsx('mt-2 h-2 w-full cursor-pointer appearance-none rounded-full accent-brand-500 disabled:cursor-not-allowed disabled:opacity-50')}
        style={{ background: `linear-gradient(to right, #0f6cbd ${pct}%, #d4d4d4 ${pct}%)` }}
      />
      <div className="mt-1 flex justify-between text-[11px] text-neutral-400">
        <span>{format ? format(min) : min}</span>
        <span>{format ? format(max) : max}</span>
      </div>
    </div>
  );
}
