import { ArrowUturnLeftIcon, CalendarDaysIcon, ClipboardDocumentCheckIcon, TrashIcon } from '@heroicons/react/24/outline';
import clsx from 'clsx';
import { useLayoutEffect, useRef, useState } from 'react';
import type { TaskItem } from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card, CardBody, CardHeader } from '@/components/ui/Card';
import { formatDate, formatPercent, humanize } from '@/lib/format';
import { TASK_BASIS_LABELS } from '@/lib/labels';
import { EvidenceQuotes } from './TranscriptPolicy';

export interface TaskDraft {
  title?: string;
  due_date?: string | null;
  drop?: boolean;
}

interface TaskRowProps {
  task: TaskItem;
  draft: TaskDraft | undefined;
  readOnly: boolean;
  onEdit: (taskId: string, patch: TaskDraft) => void;
}

function TaskRow({ task, draft, readOnly, onEdit }: TaskRowProps) {
  const dropped = draft?.drop === true;
  const title = draft?.title ?? task.title;
  const due = draft && 'due_date' in draft ? draft.due_date : task.due_date;
  const [text, setText] = useState(title);
  const areaRef = useRef<HTMLTextAreaElement>(null);
  const titleId = `task-title-${task.task_id}`;
  const dueId = `task-due-${task.task_id}`;

  // Grow the title box with its content so long task titles are never cut off.
  useLayoutEffect(() => {
    const area = areaRef.current;
    if (!area) return;
    area.style.height = 'auto';
    area.style.height = `${area.scrollHeight}px`;
  }, [text, dropped, readOnly]);

  if (dropped) {
    return (
      <li className="flex items-center justify-between gap-3 rounded-lg border border-dashed border-neutral-300 bg-neutral-50 px-4 py-3 text-sm text-neutral-500">
        <span className="line-through">{title}</span>
        <Button size="sm" variant="ghost" icon={ArrowUturnLeftIcon} onClick={() => onEdit(task.task_id, { drop: false })}>
          Restore
        </Button>
      </li>
    );
  }

  return (
    <li className="rounded-lg border border-neutral-200 bg-white p-3 sm:p-4">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1 space-y-2">
          {readOnly ? (
            <p className="text-sm font-medium text-neutral-900">{title}</p>
          ) : (
            <div>
              <label htmlFor={titleId} className="sr-only">
                Task title
              </label>
              <textarea
                id={titleId}
                ref={areaRef}
                rows={1}
                value={text}
                onChange={(event) => setText(event.target.value)}
                onBlur={() => {
                  const next = text.trim();
                  if (next && next !== title) onEdit(task.task_id, { title: next });
                  else setText(title);
                }}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && !event.shiftKey) {
                    event.preventDefault();
                    event.currentTarget.blur();
                  }
                }}
                className="input resize-none overflow-hidden font-medium leading-5"
              />
            </div>
          )}
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-xs">
            {readOnly ? (
              <span className="inline-flex items-center gap-1 text-neutral-700">
                <CalendarDaysIcon aria-hidden="true" className="h-4 w-4 text-neutral-400" />
                {due ? formatDate(due) : 'No due date'}
              </span>
            ) : (
              <div className="flex items-center gap-1.5">
                <label htmlFor={dueId} className="font-semibold text-neutral-600">
                  Due
                </label>
                <input
                  id={dueId}
                  type="date"
                  value={due ?? ''}
                  onChange={(event) => onEdit(task.task_id, { due_date: event.target.value === '' ? null : event.target.value })}
                  className="input !w-auto !py-1"
                />
              </div>
            )}
            <Badge tone={task.due_basis === 'stated' ? 'success' : task.due_basis === 'inferred' ? 'warning' : 'neutral'}>{TASK_BASIS_LABELS[task.due_basis] ?? humanize(task.due_basis)}</Badge>
            {task.due_text ? <span className="text-neutral-500">said “{task.due_text}”</span> : null}
            <Badge tone="neutral">Owner: {humanize(task.owner_role)}</Badge>
            <span className="tabular text-neutral-400" title="Model confidence">
              {formatPercent(task.confidence)}
            </span>
          </div>
          <EvidenceQuotes evidence={task.evidence} />
        </div>
        {!readOnly ? (
          <button
            type="button"
            onClick={() => onEdit(task.task_id, { drop: true })}
            className={clsx('shrink-0 rounded-md p-2 text-neutral-400 hover:bg-red-50 hover:text-red-600')}
            aria-label={`Drop task: ${title}`}
            title="Drop this task"
          >
            <TrashIcon aria-hidden="true" className="h-5 w-5" />
          </button>
        ) : null}
      </div>
    </li>
  );
}

interface TasksCardProps {
  tasks: TaskItem[];
  edits: Record<string, TaskDraft>;
  readOnly: boolean;
  onEdit: (taskId: string, patch: TaskDraft) => void;
}

export function TasksCard({ tasks, edits, readOnly, onEdit }: TasksCardProps) {
  const active = tasks.filter((task) => edits[task.task_id]?.drop !== true).length;
  return (
    <Card aria-labelledby="tasks-title">
      <CardHeader id="tasks-title" title="CRM tasks" subtitle="Follow-up commitments, created as tasks on the account" icon={ClipboardDocumentCheckIcon} actions={<Badge tone="brand">{active} to create</Badge>} />
      <CardBody>
        {tasks.length === 0 ? (
          <p className="text-sm text-neutral-500">No follow-up commitments were detected.</p>
        ) : (
          <ul className="space-y-2.5">
            {tasks.map((task) => (
              <TaskRow key={task.task_id} task={task} draft={edits[task.task_id]} readOnly={readOnly} onEdit={onEdit} />
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}
