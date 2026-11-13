/** Small domain badges reused across Inbox, Review, Sources, Scoreboard. */
import {
  ArrowUpTrayIcon,
  CheckCircleIcon,
  CheckIcon,
  ClockIcon,
  DevicePhoneMobileIcon,
  EyeIcon,
  GlobeAltIcon,
  LanguageIcon,
  PhoneArrowDownLeftIcon,
  PhoneIcon,
  Squares2X2Icon,
  UserGroupIcon,
  VideoCameraIcon,
  XCircleIcon,
  ExclamationTriangleIcon,
} from '@heroicons/react/24/solid';
import clsx from 'clsx';
import type { Channel, ConversationStatus } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Spinner } from '@/components/ui/Spinner';
import type { IconType, Tone } from '@/components/ui/types';
import { formatUsd } from '@/lib/format';
import { businessLineLabel, CHANNEL_SHORT, languageLabel, STATUS_LABELS } from '@/lib/labels';

// ---- status ----------------------------------------------------------------------------------------
const STATUS_STYLE: Record<ConversationStatus, { tone: Tone; icon: IconType }> = {
  received: { tone: 'neutral', icon: ClockIcon },
  processing: { tone: 'info', icon: ClockIcon },
  needs_review: { tone: 'warning', icon: EyeIcon },
  approved: { tone: 'violet', icon: CheckIcon },
  written: { tone: 'success', icon: CheckCircleIcon },
  rejected: { tone: 'neutral', icon: XCircleIcon },
  failed: { tone: 'danger', icon: ExclamationTriangleIcon },
};

export function StatusBadge({ status, size = 'sm', className }: { status: ConversationStatus; size?: 'sm' | 'md'; className?: string }) {
  const { tone, icon } = STATUS_STYLE[status];
  if (status === 'processing') {
    return (
      <Badge tone={tone} size={size} className={className}>
        <span className="inline-flex items-center gap-1">
          <Spinner className="h-3 w-3" />
          {STATUS_LABELS[status]}
        </span>
      </Badge>
    );
  }
  return (
    <Badge tone={tone} icon={icon} size={size} className={className}>
      {STATUS_LABELS[status]}
    </Badge>
  );
}

// ---- channel ---------------------------------------------------------------------------------------
const CHANNEL_STYLE: Record<Channel, { icon: IconType; tile: string }> = {
  teams: { icon: UserGroupIcon, tile: 'bg-indigo-100 text-indigo-700' },
  zoom: { icon: VideoCameraIcon, tile: 'bg-blue-100 text-blue-700' },
  genesys: { icon: PhoneArrowDownLeftIcon, tile: 'bg-orange-100 text-orange-700' },
  phone: { icon: PhoneIcon, tile: 'bg-teal-100 text-teal-700' },
  mobile: { icon: DevicePhoneMobileIcon, tile: 'bg-emerald-100 text-emerald-700' },
  upload: { icon: ArrowUpTrayIcon, tile: 'bg-neutral-200 text-neutral-700' },
};

export function ChannelIcon({ channel, size = 'md', className }: { channel: Channel; size?: 'sm' | 'md' | 'lg'; className?: string }) {
  const style = CHANNEL_STYLE[channel] ?? { icon: Squares2X2Icon, tile: 'bg-neutral-200 text-neutral-700' };
  const Icon = style.icon;
  return (
    <span
      role="img"
      aria-label={`Channel: ${CHANNEL_SHORT[channel] ?? channel}`}
      title={CHANNEL_SHORT[channel] ?? channel}
      className={clsx('flex shrink-0 items-center justify-center rounded-lg', size === 'sm' ? 'h-7 w-7' : size === 'lg' ? 'h-11 w-11' : 'h-9 w-9', style.tile, className)}
    >
      <Icon aria-hidden="true" className={size === 'sm' ? 'h-4 w-4' : size === 'lg' ? 'h-6 w-6' : 'h-5 w-5'} />
    </span>
  );
}

// ---- chips -----------------------------------------------------------------------------------------
export function MarketChip({ market }: { market: string }) {
  return (
    <Badge tone="neutral" icon={GlobeAltIcon} title={`Market: ${market}`}>
      {market}
    </Badge>
  );
}

export function BusinessLineChip({ value }: { value: string }) {
  return (
    <Badge tone="violet" title="Business line">
      {businessLineLabel(value)}
    </Badge>
  );
}

export function LanguageChip({ language }: { language: string | null | undefined }) {
  if (!language) return null;
  return (
    <Badge tone="teal" icon={LanguageIcon} title={`Language: ${language}`}>
      {languageLabel(language)}
    </Badge>
  );
}

// ---- share class -----------------------------------------------------------------------------------
const CLASS_STYLES: Record<string, string> = {
  A: 'bg-sky-100 text-sky-900 ring-sky-500',
  C: 'bg-orange-100 text-orange-900 ring-orange-500',
  Advisor: 'bg-violet-100 text-violet-900 ring-violet-500',
  R6: 'bg-teal-100 text-teal-900 ring-teal-500',
  I: 'bg-emerald-100 text-emerald-900 ring-emerald-500',
  R: 'bg-slate-200 text-slate-900 ring-slate-500',
};

export function shareClassText(shareClass: string): string {
  return shareClass === 'Advisor' ? 'Advisor class' : `Class ${shareClass}`;
}

/** Share class is the whole point of the product pick, so it is loud and colour-coded (never colour alone). */
export function ShareClassBadge({ shareClass, size = 'md', className }: { shareClass: string; size?: 'sm' | 'md' | 'lg'; className?: string }) {
  return (
    <span
      className={clsx(
        'inline-flex shrink-0 items-center justify-center whitespace-nowrap rounded-md font-extrabold ring-2 ring-inset',
        size === 'sm' ? 'px-1.5 py-0.5 text-xs' : size === 'md' ? 'px-2.5 py-1 text-sm' : 'px-3.5 py-1.5 text-lg',
        CLASS_STYLES[shareClass] ?? 'bg-neutral-100 text-neutral-900 ring-neutral-400',
        className,
      )}
    >
      {shareClassText(shareClass)}
    </span>
  );
}

// ---- cost ------------------------------------------------------------------------------------------
/** Currency with an "est." suffix: every price in the demo is an illustrative placeholder. */
export function EstCost({ value, className }: { value: number | null | undefined; className?: string }) {
  if (value === null || value === undefined) return <span className={className}>{'\u2014'}</span>;
  return (
    <span className={clsx('tabular', className)} title="Illustrative price table - validate against current Azure pricing before quoting">
      {formatUsd(value)}
    </span>
  );
}
