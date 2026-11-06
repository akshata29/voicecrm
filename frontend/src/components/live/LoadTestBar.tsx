import { BoltIcon } from '@heroicons/react/24/outline';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { getErrorMessage } from '@/api/client';
import { LOAD_ACTIVE_STATES, useClearLoad, useLoadStatus } from '@/api/hooks';
import { ConfirmDialog } from '@/components/ui/ConfirmDialog';
import { useToast } from '@/components/ui/Toast';
import { formatInt } from '@/lib/format';

/** Tells the Inbox when calls from a Live demo load test are in it, and lets you take them out again. */
export function LoadTestBar() {
  const status = useLoadStatus();
  const clear = useClearLoad();
  const toast = useToast();
  const [asking, setAsking] = useState(false);
  const data = status.data;
  if (!data) return null;
  const active = LOAD_ACTIVE_STATES.has(data.state);
  if (!active && data.stored_calls === 0) return null;

  const remove = () =>
    clear.mutate(undefined, {
      onSuccess: (result) => toast.success(`Removed ${formatInt(result.deleted)} load-test calls`),
      onError: (error) => toast.error('Could not remove them', { message: getErrorMessage(error) }),
      onSettled: () => setAsking(false),
    });

  return (
    <div role="status" className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-sky-200 bg-sky-50 px-4 py-2.5 text-sm text-sky-900">
      <BoltIcon aria-hidden="true" className="h-4 w-4 shrink-0" />
      <span>
        {active
          ? `A load test is running: ${formatInt(data.finished)} of ${formatInt(data.planned)} calls done.`
          : `${formatInt(data.stored_calls)} load-test calls are in this inbox, marked "Load test".`}
      </span>
      <Link to="/live" className="font-medium underline">
        Open the Live demo
      </Link>
      {!active ? (
        <button type="button" className="font-medium underline" onClick={() => setAsking(true)}>
          Remove them
        </button>
      ) : null}
      <ConfirmDialog
        open={asking}
        title="Remove the load-test calls?"
        description={`${formatInt(data.stored_calls)} calls are removed from the Inbox, the transcript vault, the state store and the local CRM simulator (what they wrote there). Calls made any other way are not touched.`}
        confirmLabel="Remove them"
        tone="danger"
        loading={clear.isPending}
        onConfirm={remove}
        onCancel={() => setAsking(false)}
      />
    </div>
  );
}
