import { MicrophoneIcon, PhoneXMarkIcon } from '@heroicons/react/24/outline';
import clsx from 'clsx';
import { Button } from '@/components/ui/Button';
import { formatClock } from '@/lib/format';
import { useNow } from '@/lib/hooks';
import type { CallPhase } from './liveCall';

export const PHASE_TEXT: Record<CallPhase, string> = {
  connecting: 'Connecting\u2026',
  ringing: 'Ringing\u2026',
  connected: 'Connected',
  ended: 'Call ended',
};

/** A running mm:ss since `since`. Mounted only while a call is on, so nothing ticks the rest of the time. */
export function CallClock({ since, className }: { since: number | null; className?: string }) {
  const now = useNow(1000);
  return <span className={clsx('tabular', className)}>{formatClock(since === null ? 0 : (now - since) / 1000)}</span>;
}

export function CallButtons({ muted, onToggleMute, onHangUp }: { muted: boolean; onToggleMute: () => void; onHangUp: () => void }) {
  // These replace the button that started or accepted the call, so focus moves here instead of falling back to the page.
  return (
    <div className="flex flex-wrap items-center justify-center gap-2">
      <Button variant={muted ? 'primary' : 'secondary'} icon={MicrophoneIcon} onClick={onToggleMute}>
        {muted ? 'Unmute' : 'Mute'}
      </Button>
      <Button variant="danger" icon={PhoneXMarkIcon} onClick={onHangUp} autoFocus>
        Hang up
      </Button>
    </div>
  );
}
