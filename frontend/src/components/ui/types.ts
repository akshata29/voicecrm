import type { ComponentType } from 'react';

/** Any Heroicon (or compatible) component. */
export type IconType = ComponentType<{ className?: string; 'aria-hidden'?: boolean | 'true' | 'false' }>;

export type Tone =
  | 'neutral'
  | 'brand'
  | 'success'
  | 'warning'
  | 'danger'
  | 'info'
  | 'violet'
  | 'orange'
  | 'teal';
