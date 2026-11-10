import { ArrowPathIcon, TrashIcon } from '@heroicons/react/24/outline';
import { useState } from 'react';
import { getErrorMessage } from '@/api/client';
import { useDemoData, useResetDemo } from '@/api/hooks';
import type { DemoCounts } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { ConfirmDialog } from '@/components/ui/ConfirmDialog';
import { ErrorState, Skeleton } from '@/components/ui/StateViews';
import { useToast } from '@/components/ui/Toast';
import { formatInt } from '@/lib/format';

const count = (n: number, one: string, many = `${one}s`) => `${formatInt(n)} ${n === 1 ? one : many}`;

/** One line per kind of thing a reset removes; kinds with nothing in them are left out. */
export function removedLines(c: DemoCounts): string[] {
  const lines: string[] = [];
  if (c.conversations > 0) lines.push(`${count(c.conversations, 'call')} in the Inbox, with their review history and cost`);
  const crm: string[] = [];
  if (c.crm_phone_calls > 0) crm.push(count(c.crm_phone_calls, 'phone call'));
  if (c.crm_tasks > 0) crm.push(count(c.crm_tasks, 'task'));
  if (c.crm_insights > 0) crm.push(count(c.crm_insights, 'insight'));
  if (crm.length > 0) lines.push(`${crm.join(', ')} in the CRM simulator`);
  if (c.vault_items > 0) lines.push(`${count(c.vault_items, 'copy', 'copies')} in the transcript vault`);
  if (c.learned_aliases > 0) lines.push(count(c.learned_aliases, 'learned product name'));
  if (c.evaluation_runs > 0) lines.push(count(c.evaluation_runs, 'scoreboard run'));
  if (c.policy_changes) lines.push('the changes saved on the Policies page (back to the shipped defaults)');
  return lines;
}

const KEPT =
  'Kept: the catalogue, topics and templates; the CRM accounts, contacts and opportunities; the recordings in data\\audio; and the list of Teams meetings already ingested, so the live poller does not bring them back (Import earlier meetings can).';

/** Live demo, last section: clear what earlier runs left behind, so the next demo starts from an empty Inbox. */
export function DemoReset({ onReset }: { onReset: () => void }) {
  const data = useDemoData();
  const reset = useResetDemo();
  const toast = useToast();
  const [asking, setAsking] = useState(false);
  const current = data.data;
  const lines = current ? removedLines(current) : [];
  const blocked = current ? !current.can_reset : false;

  const confirm = () =>
    reset.mutate(undefined, {
      onSuccess: (removed) => {
        setAsking(false);
        toast.success('Demo data reset', { message: removedLines(removed).join('; ') || 'There was nothing to remove.' });
        onReset();
      },
      onError: (error) => {
        setAsking(false);
        toast.error('Could not reset the demo data', { message: getErrorMessage(error) });
      },
    });

  return (
    <section id="demo-data" aria-labelledby="demo-data-title" className="scroll-mt-4 space-y-4">
      <header className="mb-3 flex items-start gap-3">
        <span aria-hidden="true" className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-neutral-700 text-white shadow-card">
          <ArrowPathIcon className="h-5 w-5" />
        </span>
        <div className="min-w-0">
          <h2 id="demo-data-title" className="text-lg font-semibold leading-7 text-neutral-900">
            Start clean
          </h2>
          <p className="max-w-3xl text-sm text-neutral-600">
            Earlier runs leave calls, CRM rows, learned product names and policy edits behind. Clear them here before a demo, so the Inbox, the CRM and the scoreboard start empty.
          </p>
        </div>
      </header>
      <Card aria-labelledby="demo-reset-title">
        <CardHeader
          id="demo-reset-title"
          icon={TrashIcon}
          title="What is stored in this app"
          subtitle="Reset removes it all in one step; nothing here is a real customer record"
          actions={
            <Button variant="danger-outline" size="sm" icon={TrashIcon} disabled={!current || blocked || lines.length === 0} onClick={() => setAsking(true)}>
              Reset demo data
            </Button>
          }
        />
        <CardBody className="space-y-3">
          {data.isPending ? (
            <Skeleton className="h-24" />
          ) : data.isError ? (
            <ErrorState error={data.error} title="Could not read what is stored" compact onRetry={() => void data.refetch()} />
          ) : (
            <>
              {lines.length === 0 ? (
                <p className="text-sm text-neutral-700">Nothing to reset: the Inbox, the CRM simulator and the vault are empty, and the policies are the shipped ones.</p>
              ) : (
                <ul aria-label="Stored demo data" className="list-inside list-disc space-y-0.5 text-sm text-neutral-800">
                  {lines.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              )}
              {blocked && current?.reason ? (
                <p role="status" className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                  {current.reason}
                </p>
              ) : null}
              <p className="text-xs text-neutral-500">{KEPT}</p>
            </>
          )}
        </CardBody>
      </Card>
      <ConfirmDialog
        open={asking}
        title="Reset the demo data?"
        description="This cannot be undone. The next run starts from an empty Inbox."
        confirmLabel="Reset demo data"
        tone="danger"
        loading={reset.isPending}
        onConfirm={confirm}
        onCancel={() => setAsking(false)}
      >
        <p className="text-sm font-medium text-neutral-800">This removes</p>
        <ul className="mt-1 list-inside list-disc space-y-0.5 text-sm text-neutral-700">
          {lines.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
        <p className="mt-3 text-xs text-neutral-500">{KEPT}</p>
      </ConfirmDialog>
    </section>
  );
}
