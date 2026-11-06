import {
  BoltIcon,
  BuildingOffice2Icon,
  ChartBarIcon,
  CubeIcon,
  InboxIcon,
  MicrophoneIcon,
  RectangleGroupIcon,
  ShieldCheckIcon,
  SignalIcon,
} from '@heroicons/react/24/outline';
import clsx from 'clsx';
import { Link, useLocation } from 'react-router-dom';
import { INBOX_PARAMS, useConfig, useConversationList } from '@/api/hooks';
import type { IconType } from '@/components/ui/types';

interface NavItemDef {
  to: string;
  label: string;
  icon: IconType;
  /** Path rule that keeps this item highlighted (Review lives under the Inbox). */
  matches: (pathname: string) => boolean;
  showReviewCount?: boolean;
}

export const NAV_ITEMS: NavItemDef[] = [
  { to: '/', label: 'Inbox', icon: InboxIcon, matches: (p) => p === '/' || p.startsWith('/conversations'), showReviewCount: true },
  { to: '/live', label: 'Live demo', icon: BoltIcon, matches: (p) => p.startsWith('/live') },
  { to: '/sources', label: 'Sources', icon: SignalIcon, matches: (p) => p.startsWith('/sources') },
  { to: '/capture', label: 'Capture', icon: MicrophoneIcon, matches: (p) => p.startsWith('/capture') },
  { to: '/policies', label: 'Policies', icon: ShieldCheckIcon, matches: (p) => p.startsWith('/policies') },
  { to: '/catalogue', label: 'Catalogue', icon: CubeIcon, matches: (p) => p.startsWith('/catalogue') },
  { to: '/crm', label: 'CRM', icon: BuildingOffice2Icon, matches: (p) => p.startsWith('/crm') },
  { to: '/scoreboard', label: 'Scoreboard', icon: ChartBarIcon, matches: (p) => p.startsWith('/scoreboard') },
  { to: '/architecture', label: 'Architecture', icon: RectangleGroupIcon, matches: (p) => p.startsWith('/architecture') },
];

export function BrandMark({ className }: { className?: string }) {
  return (
    <span className={clsx('flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-brand-400 to-brand-800 text-white shadow-card', className)}>
      <MicrophoneIcon aria-hidden="true" className="h-5 w-5" />
    </span>
  );
}

function ReviewCount() {
  const list = useConversationList(INBOX_PARAMS);
  const count = list.data?.filter((item) => item.status === 'needs_review').length ?? 0;
  if (count === 0) return null;
  return (
    <span className="tabular ml-auto rounded-full bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-900 ring-1 ring-inset ring-amber-300" aria-label={`${count} conversations need review`}>
      {count}
    </span>
  );
}

export function NavList({ onNavigate }: { onNavigate?: () => void }) {
  const { pathname } = useLocation();
  return (
    <nav aria-label="Primary" className="flex-1 space-y-0.5 overflow-y-auto px-2 py-2">
      {NAV_ITEMS.map((item) => {
        const Icon = item.icon;
        const active = item.matches(pathname);
        return (
          <Link
            key={item.to}
            to={item.to}
            onClick={onNavigate}
            aria-current={active ? 'page' : undefined}
            className={clsx(
              'group relative flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors',
              active ? 'bg-brand-50 text-brand-700' : 'text-neutral-700 hover:bg-neutral-100 hover:text-neutral-900',
            )}
          >
            <span aria-hidden="true" className={clsx('absolute inset-y-1.5 left-0 w-[3px] rounded-r', active ? 'bg-brand-500' : 'bg-transparent')} />
            <Icon aria-hidden="true" className={clsx('h-5 w-5 shrink-0', active ? 'text-brand-600' : 'text-neutral-500 group-hover:text-neutral-700')} />
            <span>{item.label}</span>
            {item.showReviewCount ? <ReviewCount /> : null}
          </Link>
        );
      })}
    </nav>
  );
}

export function SideNavContent({ onNavigate }: { onNavigate?: () => void }) {
  const config = useConfig();
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 px-4 py-4">
        <BrandMark />
        <div className="min-w-0">
          <p className="text-base font-semibold leading-5 text-neutral-900">VoiceCRM</p>
          <p className="truncate text-xs text-neutral-500">Conversation to CRM</p>
        </div>
      </div>
      <NavList onNavigate={onNavigate} />
      <div className="border-t border-neutral-100 px-4 py-3 text-xs text-neutral-500">
        <p>Proof of concept{config.data ? ` · v${config.data.app_version}` : ''}</p>
        <p>English + Spanish · post-call</p>
      </div>
    </div>
  );
}
