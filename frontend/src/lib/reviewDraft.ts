/**
 * Draft state for the Review screen. The approve request carries ONLY the edits the reviewer actually made, so the
 * one-click path is an empty body. `actions` is the click counter behind the "fewer clicks" KPI.
 */
import type { ApproveRequest, TaskItem } from '@/api/types';
import type { TaskDraft } from '@/components/conversation/TasksCard';

export interface ReviewDraft {
  picks: Record<string, string>;
  dropped: string[];
  topicIds: string[] | null;
  taskEdits: Record<string, TaskDraft>;
  sectionEdits: Record<string, string>;
  actions: number;
}

export const EMPTY_DRAFT: ReviewDraft = { picks: {}, dropped: [], topicIds: null, taskEdits: {}, sectionEdits: {}, actions: 0 };

export type DraftAction =
  /** A new proposal replaced the old one (reprocess): clear edits but keep the click total. */
  | { type: 'reset' }
  | { type: 'click' }
  | { type: 'pick'; mentionId: string; productId: string; original: string | null }
  | { type: 'toggleDrop'; mentionId: string }
  | { type: 'topics'; ids: string[]; original: string[] }
  | { type: 'task'; taskId: string; patch: TaskDraft; original: TaskItem }
  | { type: 'section'; key: string; text: string | null };

export function draftReducer(state: ReviewDraft, action: DraftAction): ReviewDraft {
  const actions = state.actions + 1;
  switch (action.type) {
    case 'reset':
      return { ...EMPTY_DRAFT, actions: state.actions };
    case 'click':
      return { ...state, actions };
    case 'pick': {
      const picks = { ...state.picks };
      if (action.productId === action.original) delete picks[action.mentionId];
      else picks[action.mentionId] = action.productId;
      return { ...state, picks, actions };
    }
    case 'toggleDrop': {
      const removing = !state.dropped.includes(action.mentionId);
      const picks = { ...state.picks };
      if (removing) delete picks[action.mentionId];
      return {
        ...state,
        picks,
        dropped: removing ? [...state.dropped, action.mentionId] : state.dropped.filter((id) => id !== action.mentionId),
        actions,
      };
    }
    case 'topics': {
      const same = action.ids.length === action.original.length && action.ids.every((id) => action.original.includes(id));
      return { ...state, topicIds: same ? null : action.ids, actions };
    }
    case 'task': {
      const merged: TaskDraft = { ...state.taskEdits[action.taskId], ...action.patch };
      if ('title' in merged && merged.title === action.original.title) delete merged.title;
      if ('due_date' in merged && merged.due_date === action.original.due_date) delete merged.due_date;
      if (merged.drop === false) delete merged.drop;
      const taskEdits = { ...state.taskEdits };
      if (Object.keys(merged).length === 0) delete taskEdits[action.taskId];
      else taskEdits[action.taskId] = merged;
      return { ...state, taskEdits, actions };
    }
    case 'section': {
      const sectionEdits = { ...state.sectionEdits };
      if (action.text === null) delete sectionEdits[action.key];
      else sectionEdits[action.key] = action.text;
      return { ...state, sectionEdits, actions };
    }
  }
}

/** Number of net edits that will travel with the approval. */
export function netEditCount(draft: ReviewDraft): number {
  return (
    Object.keys(draft.picks).length +
    draft.dropped.length +
    (draft.topicIds ? 1 : 0) +
    Object.keys(draft.taskEdits).length +
    Object.keys(draft.sectionEdits).length
  );
}

/** `{}` for the pure one-click path; otherwise only what the reviewer changed (plus `clicks` when it is not 1). */
export function buildApproveRequest(draft: ReviewDraft): ApproveRequest {
  const body: ApproveRequest = {};
  if (Object.keys(draft.picks).length > 0) body.product_choices = { ...draft.picks };
  if (draft.dropped.length > 0) body.drop_mentions = [...draft.dropped];
  if (draft.topicIds) body.topic_ids = [...draft.topicIds];
  const taskEdits = Object.entries(draft.taskEdits).map(([task_id, edit]) => ({ task_id, ...edit }));
  if (taskEdits.length > 0) body.task_edits = taskEdits;
  if (Object.keys(draft.sectionEdits).length > 0) body.section_edits = { ...draft.sectionEdits };
  const clicks = draft.actions + 1;
  if (clicks !== 1) body.clicks = clicks;
  return body;
}
