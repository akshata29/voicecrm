/** In-memory state for the mock API. Mutations from the UI (approve, reject, reprocess, policies...) land here. */
import type {
  ConversationRecord,
  CostBreakdown,
  CrmRow,
  Engine,
  EvalRun,
  LearnedAlias,
  PoliciesResponse,
  Stage,
  StageEvent,
  StageRecordView,
  UsageRecord,
  VaultItem,
} from '../types';
import type { MockScript } from './scripts';

export class HttpError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = 'HttpError';
    this.status = status;
    this.detail = detail;
  }
}

export type SimVia = 'transcript' | 'audio';

export interface StagePlanItem {
  stage: Stage;
  ms: number;
  skipped: boolean;
  detail: Record<string, unknown>;
}

/** A pipeline run in progress: stage statuses are derived from elapsed wall-clock time on every read. */
export interface SimState {
  startedAt: number;
  plan: StagePlanItem[];
  engine: Engine;
  templateId: string;
}

export interface MockConv {
  id: string;
  /** The scripted call behind this conversation (uploads get an adapted copy with their own names). */
  script: MockScript;
  via: SimVia;
  noise: boolean;
  record: ConversationRecord;
  stages: StageRecordView[];
  events: StageEvent[];
  usage: UsageRecord[];
  cost: CostBreakdown;
  sim: SimState | null;
  rejectReason: string | null;
}

export interface MockDb {
  convs: Map<string, MockConv>;
  aliases: LearnedAlias[];
  policies: PoliciesResponse;
  vault: VaultItem[];
  crm: { phonecall: CrmRow[]; task: CrmRow[]; insight: CrmRow[] };
  evalRuns: EvalRun[];
}

let current: MockDb | null = null;

export function setDb(next: MockDb) {
  current = next;
}

export function db(): MockDb {
  if (!current) throw new Error('Mock database has not been seeded');
  return current;
}

export function uuidHex(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID().replace(/-/g, '');
  return Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join('');
}

export const iso = (ms: number): string => new Date(ms).toISOString();
