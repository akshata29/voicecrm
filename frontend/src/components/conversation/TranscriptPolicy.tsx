import { InformationCircleIcon } from '@heroicons/react/24/outline';
import { createContext, useContext, type ReactNode } from 'react';
import type { Evidence } from '@/api/types';

interface TranscriptPolicyValue {
  /** ConversationDetail.transcript_available */
  available: boolean;
  /** ConversationDetail.transcript_note: the market-policy explanation */
  note: string;
}

const TranscriptPolicyContext = createContext<TranscriptPolicyValue>({ available: false, note: 'Transcript availability is decided by the market policy.' });

export function TranscriptPolicyProvider({ available, note, children }: TranscriptPolicyValue & { children: ReactNode }) {
  return <TranscriptPolicyContext.Provider value={{ available, note }}>{children}</TranscriptPolicyContext.Provider>;
}

export function useTranscriptPolicy(): TranscriptPolicyValue {
  return useContext(TranscriptPolicyContext);
}

/**
 * Evidence quotes are transcript text, so they are only rendered when the market policy makes the transcript
 * available. Otherwise only the segment numbers are shown, with the policy note as the explanation.
 */
export function EvidenceQuotes({ evidence, className }: { evidence: Evidence[]; className?: string }) {
  const { available, note } = useTranscriptPolicy();
  if (evidence.length === 0) return null;
  const segmentIds = [...new Set(evidence.flatMap((item) => item.segment_ids))];
  const segments = segmentIds.map((id) => `#${id}`).join(', ');

  if (!available) {
    return (
      <p className={className} title={note}>
        <span className="inline-flex items-center gap-1 text-xs text-neutral-500">
          <InformationCircleIcon aria-hidden="true" className="h-3.5 w-3.5" />
          Evidence: segment {segments} · quotes hidden by market policy
        </span>
      </p>
    );
  }

  return (
    <details className={className}>
      <summary className="cursor-pointer select-none text-xs font-medium text-brand-700 hover:underline">Evidence ({segments})</summary>
      <ul className="mt-1 space-y-1">
        {evidence.map((item, index) => (
          <li key={index} className="border-l-2 border-neutral-200 pl-2 text-xs italic text-neutral-600">
            “{item.quote}”
          </li>
        ))}
      </ul>
    </details>
  );
}
