/** Shared formatting helpers: dates, durations, percentages, cost ("est." - prices are illustrative). */

const dateTimeFmt = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });
const dateFmt = new Intl.DateTimeFormat(undefined, { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
const timeFmt = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' });
const relativeFmt = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });
const intFmt = new Intl.NumberFormat(undefined);

export const EM_DASH = '\u2014';

function toDate(value: string | number | Date | null | undefined): Date | null {
  if (value === null || value === undefined || value === '') return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDateTime(value: string | number | Date | null | undefined): string {
  const date = toDate(value);
  return date ? dateTimeFmt.format(date) : EM_DASH;
}

export function formatTime(value: string | number | Date | null | undefined): string {
  const date = toDate(value);
  return date ? timeFmt.format(date) : EM_DASH;
}

/** Calendar dates ("YYYY-MM-DD") are parsed as local dates so they never shift a day across timezones. */
export function formatDate(value: string | null | undefined): string {
  if (!value) return EM_DASH;
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  const date = match ? new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3])) : toDate(value);
  return date ? dateFmt.format(date) : EM_DASH;
}

export function formatRelative(value: string | number | Date | null | undefined, now: number = Date.now()): string {
  const date = toDate(value);
  if (!date) return EM_DASH;
  const seconds = Math.round((date.getTime() - now) / 1000);
  const abs = Math.abs(seconds);
  if (abs < 45) return seconds <= 0 ? 'just now' : 'in a few seconds';
  if (abs < 3600) return relativeFmt.format(Math.round(seconds / 60), 'minute');
  if (abs < 86400) return relativeFmt.format(Math.round(seconds / 3600), 'hour');
  if (abs < 86400 * 14) return relativeFmt.format(Math.round(seconds / 86400), 'day');
  return dateTimeFmt.format(date);
}

const shortDateFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric' });

/** "in 6 hours", "expired 3 hours ago", or a calendar date when the expiry is more than two weeks away. */
export function formatExpiry(value: string | null | undefined, now: number = Date.now()): { text: string; expired: boolean } {
  const date = toDate(value);
  if (!date) return { text: 'no expiry', expired: false };
  const diff = date.getTime() - now;
  const expired = diff <= 0;
  if (Math.abs(diff) < 14 * 86_400_000) {
    const relative = formatRelative(date, now);
    return { text: expired ? `expired ${relative}` : relative, expired };
  }
  const absolute = shortDateFmt.format(date);
  return { text: expired ? `expired ${absolute}` : absolute, expired };
}

export function formatDurationMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return EM_DASH;
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return formatDurationSeconds(ms / 1000);
}

export function formatDurationSeconds(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return EM_DASH;
  if (seconds < 10) return `${seconds.toFixed(1)} s`;
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds - minutes * 60);
  if (minutes < 60) return rest === 0 ? `${minutes} min` : `${minutes} min ${rest} s`;
  const hours = Math.floor(minutes / 60);
  return `${hours} h ${minutes - hours * 60} min`;
}

/** mm:ss clock for live counters and recording timers. */
export function formatClock(totalSeconds: number): string {
  const safe = Math.max(0, Math.floor(totalSeconds));
  const minutes = Math.floor(safe / 60);
  const seconds = safe % 60;
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
}

export function formatMsOffset(ms: number): string {
  return formatClock(ms / 1000);
}

/** Cost figures are illustrative until validated against Azure pricing, so they always read "est.". */
export function formatUsd(value: number | null | undefined, options: { est?: boolean } = {}): string {
  if (value === null || value === undefined) return EM_DASH;
  const { est = true } = options;
  const digits = value === 0 ? 2 : Math.abs(value) < 0.01 ? 4 : Math.abs(value) < 1 ? 3 : 2;
  const text = `$${value.toFixed(digits)}`;
  return est ? `${text} est.` : text;
}

export function formatPercent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EM_DASH;
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatInt(value: number | null | undefined): string {
  if (value === null || value === undefined) return EM_DASH;
  return intFmt.format(value);
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return EM_DASH;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function humanize(value: string | null | undefined): string {
  if (!value) return EM_DASH;
  const spaced = value.replace(/[_-]+/g, ' ').trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export function pluralize(count: number, singular: string, plural = `${singular}s`): string {
  return `${formatInt(count)} ${count === 1 ? singular : plural}`;
}

export function truncate(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, Math.max(0, max - 1)).trimEnd()}\u2026`;
}

export function initials(name: string | null | undefined): string {
  if (!name) return '?';
  const parts = name.split(/\s+/).filter(Boolean);
  return ((parts[0]?.[0] ?? '') + (parts.length > 1 ? (parts[parts.length - 1]?.[0] ?? '') : '')).toUpperCase() || '?';
}

/** Whole seconds between two instants, clamped at zero. */
export function secondsBetween(from: string | number | Date | null | undefined, to: number = Date.now()): number | null {
  const date = toDate(from);
  if (!date) return null;
  return Math.max(0, (to - date.getTime()) / 1000);
}

export function percentDelta(before: number, after: number): string {
  const diff = (after - before) * 100;
  const sign = diff > 0 ? '+' : diff < 0 ? '\u2212' : '';
  return `${sign}${Math.abs(diff).toFixed(1)} pts`;
}
