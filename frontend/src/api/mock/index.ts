/**
 * In-browser mock of the /api/v1 contract (enabled with VITE_USE_MOCK=1). An axios adapter routes each request to
 * handlers that read and mutate the in-memory state in ./state, so approvals, rejections, reprocessing, simulated
 * calls and policy changes behave live. All data is synthetic.
 */
import { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from 'axios';
import type {
  ApproveRequest,
  ConversationStatus,
  EvalRunRequest,
  LoadRequest,
  MarketPolicy,
  NotesPolicy,
  PreflightProbe,
  ProcessingPolicy,
  RejectRequest,
  ReprocessRequest,
  SimulateRequest,
} from '../types';
import { CONVERSATION_STATUSES } from '../types';
import * as admin from './admin';
import { searchCatalogue } from './catalogue';
import * as demo from './demo';
import * as evaluation from './evaluation';
import { simulate, upload } from './ingest';
import * as live from './live';
import * as pipeline from './pipeline';
import { fixtureInfos } from './scripts';
import { TAXONOMY, TEMPLATES } from './seed';
import { initMockDb } from './seedDb';
import { HttpError } from './state';

interface RouteContext {
  params: string[];
  query: Record<string, unknown>;
  body: unknown;
  form: FormData | null;
}

class Reply {
  readonly data: unknown;
  readonly headers: Record<string, string>;

  constructor(data: unknown, headers: Record<string, string> = {}) {
    this.data = data;
    this.headers = headers;
  }
}

interface Route {
  method: string;
  pattern: RegExp;
  handler: (context: RouteContext) => unknown;
  extraDelayMs?: number;
}

const asBody = <T>(body: unknown): T => (body && typeof body === 'object' ? (body as T) : ({} as T));

function queryString(query: Record<string, unknown>, key: string): string | undefined {
  const value = query[key];
  if (Array.isArray(value)) return value.length > 0 ? String(value[0]) : undefined;
  return value === undefined || value === null || value === '' ? undefined : String(value);
}

function queryList(query: Record<string, unknown>, key: string): string[] {
  const value = query[key];
  if (value === undefined || value === null || value === '') return [];
  return (Array.isArray(value) ? value : [value]).map(String);
}

function queryNumber(query: Record<string, unknown>, key: string, fallback: number): number {
  const parsed = Number(queryString(query, key));
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
}

const routes: Route[] = [
  { method: 'GET', pattern: /^\/health$/, handler: () => ({ status: 'ok' }) },
  { method: 'GET', pattern: /^\/config$/, handler: () => admin.configResponse() },

  { method: 'GET', pattern: /^\/fixtures$/, handler: () => fixtureInfos() },
  { method: 'POST', pattern: /^\/ingest\/simulate$/, handler: ({ body }) => simulate(asBody<SimulateRequest>(body)) },
  { method: 'POST', pattern: /^\/ingest\/upload$/, handler: ({ form }) => upload(form), extraDelayMs: 500 },

  {
    method: 'GET',
    pattern: /^\/conversations$/,
    handler: ({ query }) => {
      const statuses = queryList(query, 'status');
      const unknown = statuses.find((status) => !CONVERSATION_STATUSES.includes(status as ConversationStatus));
      if (unknown) throw new HttpError(422, `Unknown status "${unknown}"`);
      return pipeline.listItems(statuses as ConversationStatus[], queryNumber(query, 'limit', 50), queryNumber(query, 'offset', 0));
    },
  },
  { method: 'GET', pattern: /^\/conversations\/([^/]+)\/transcript$/, handler: ({ params }) => {
      const { data, source } = pipeline.transcriptOf(params[0] ?? '');
      return new Reply(data, { 'x-transcript-source': source });
    } },
  { method: 'GET', pattern: /^\/conversations\/([^/]+)$/, handler: ({ params }) => pipeline.detailOf(params[0] ?? '') },
  { method: 'POST', pattern: /^\/conversations\/([^/]+)\/approve$/, handler: ({ params, body }) => pipeline.approve(params[0] ?? '', asBody<ApproveRequest>(body)) },
  {
    method: 'POST',
    pattern: /^\/conversations\/([^/]+)\/reject$/,
    handler: ({ params, body }) => pipeline.reject(params[0] ?? '', asBody<RejectRequest>(body).reason),
  },
  {
    method: 'POST',
    pattern: /^\/conversations\/([^/]+)\/reprocess$/,
    handler: ({ params, body }) => pipeline.reprocess(params[0] ?? '', asBody<ReprocessRequest>(body)),
    extraDelayMs: 700,
  },

  { method: 'GET', pattern: /^\/policies$/, handler: () => admin.policies() },
  { method: 'PUT', pattern: /^\/policies\/markets\/([^/]+)$/, handler: ({ params, body }) => admin.updateMarket(decodeURIComponent(params[0] ?? ''), asBody<MarketPolicy>(body)) },
  { method: 'PUT', pattern: /^\/policies\/processing$/, handler: ({ body }) => admin.updateProcessing(asBody<ProcessingPolicy>(body)) },
  { method: 'PUT', pattern: /^\/policies\/notes$/, handler: ({ body }) => admin.updateNotes(asBody<NotesPolicy>(body)) },
  { method: 'GET', pattern: /^\/policies\/retention-preview$/, handler: ({ query }) => admin.retentionPreview(queryString(query, 'market')) },
  { method: 'GET', pattern: /^\/templates$/, handler: () => TEMPLATES },
  { method: 'GET', pattern: /^\/taxonomy$/, handler: () => TAXONOMY },

  { method: 'GET', pattern: /^\/catalogue\/stats$/, handler: () => admin.stats() },
  { method: 'GET', pattern: /^\/catalogue\/search$/, handler: ({ query }) => searchCatalogue(queryString(query, 'q') ?? '', Math.min(50, queryNumber(query, 'top', 10) || 10)) },
  { method: 'GET', pattern: /^\/aliases$/, handler: () => admin.aliases() },
  { method: 'DELETE', pattern: /^\/aliases$/, handler: () => admin.resetAliases() },

  { method: 'GET', pattern: /^\/crm\/records$/, handler: ({ query }) => admin.crmRecords(queryString(query, 'entity'), queryNumber(query, 'limit', 100)) },
  { method: 'GET', pattern: /^\/crm\/health$/, handler: () => admin.crmHealth() },

  { method: 'GET', pattern: /^\/vault\/items$/, handler: ({ query }) => admin.vaultItems(queryString(query, 'conversation_id')) },
  { method: 'POST', pattern: /^\/retention\/sweep$/, handler: () => admin.sweepVault(), extraDelayMs: 400 },

  { method: 'GET', pattern: /^\/metrics\/summary$/, handler: () => admin.metricsSummary() },
  { method: 'GET', pattern: /^\/metrics\/pipeline$/, handler: () => admin.pipelineStats() },
  { method: 'GET', pattern: /^\/sources\/teams$/, handler: () => admin.teamsStatus() },
  { method: 'POST', pattern: /^\/sources\/teams\/poll$/, handler: () => { throw new HttpError(409, admin.teamsStatus().problem ?? 'Live Teams ingestion is off.'); } },

  { method: 'GET', pattern: /^\/live\/load$/, handler: () => live.loadStatus() },
  { method: 'POST', pattern: /^\/live\/load\/estimate$/, handler: ({ body }) => live.loadEstimate(asBody<LoadRequest>(body)) },
  { method: 'POST', pattern: /^\/live\/load$/, handler: () => live.startLoad() },
  { method: 'POST', pattern: /^\/live\/load\/stop$/, handler: () => live.loadStatus() },
  { method: 'DELETE', pattern: /^\/live\/load\/calls$/, handler: () => ({ deleted: 0 }) },
  { method: 'POST', pattern: /^\/live\/preflight$/, handler: ({ body }) => live.preflight(asBody<{ only?: PreflightProbe[] }>(body).only) },

  { method: 'GET', pattern: /^\/demo\/data$/, handler: () => demo.demoData() },
  { method: 'POST', pattern: /^\/demo\/reset$/, handler: () => demo.resetDemo(), extraDelayMs: 400 },

  { method: 'POST', pattern: /^\/eval\/run$/, handler: ({ body }) => evaluation.runEvaluation(asBody<EvalRunRequest>(body)), extraDelayMs: 1800 },
  { method: 'GET', pattern: /^\/eval\/runs$/, handler: () => evaluation.listRuns() },
  { method: 'GET', pattern: /^\/eval\/runs\/([^/]+)$/, handler: ({ params }) => evaluation.getRun(decodeURIComponent(params[0] ?? '')) },
];

const STATUS_TEXT: Record<number, string> = { 200: 'OK', 404: 'Not Found', 409: 'Conflict', 422: 'Unprocessable Entity', 500: 'Internal Server Error' };

let seeded = false;

function ensureSeeded() {
  if (!seeded) {
    initMockDb();
    seeded = true;
  }
}

function parseBody(data: unknown): { body: unknown; form: FormData | null } {
  if (typeof FormData !== 'undefined' && data instanceof FormData) return { body: null, form: data };
  if (typeof data === 'string' && data.length > 0) {
    try {
      return { body: JSON.parse(data), form: null };
    } catch {
      return { body: {}, form: null };
    }
  }
  return { body: data ?? {}, form: null };
}

function pathOf(config: InternalAxiosRequestConfig): { path: string; query: Record<string, unknown> } {
  const raw = config.url ?? '';
  const base = config.baseURL ?? '';
  const withoutBase = base && raw.startsWith(base) ? raw.slice(base.length) : raw;
  const [path = '', search = ''] = withoutBase.split('?');
  const query: Record<string, unknown> = {};
  for (const [key, value] of new URLSearchParams(search)) {
    const current = query[key];
    query[key] = current === undefined ? value : Array.isArray(current) ? [...current, value] : [current, value];
  }
  const params: unknown = config.params;
  if (params && typeof params === 'object') Object.assign(query, params);
  return { path: path.startsWith('/') ? path : `/${path}`, query };
}

function failure(config: InternalAxiosRequestConfig, error: HttpError): AxiosError {
  const response: AxiosResponse = {
    data: { detail: error.detail },
    status: error.status,
    statusText: STATUS_TEXT[error.status] ?? 'Error',
    headers: {},
    config,
    request: {},
  };
  return new AxiosError(error.detail, error.status >= 500 ? 'ERR_BAD_RESPONSE' : 'ERR_BAD_REQUEST', config, {}, response);
}

const sleep = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));

export async function mockAdapter(config: InternalAxiosRequestConfig): Promise<AxiosResponse> {
  ensureSeeded();
  const method = (config.method ?? 'get').toUpperCase();
  const { path, query } = pathOf(config);
  const { body, form } = parseBody(config.data);
  const route = routes
    .map((candidate) => ({ candidate, match: candidate.method === method ? candidate.pattern.exec(path) : null }))
    .find((entry) => entry.match !== null);

  await sleep(50 + Math.random() * 110 + (route?.candidate.extraDelayMs ?? 0));

  if (!route || !route.match) throw failure(config, new HttpError(404, 'Not Found'));
  try {
    const result = route.candidate.handler({ params: route.match.slice(1), query, body, form });
    const reply = result instanceof Reply ? result : new Reply(result);
    return { data: structuredClone(reply.data), status: 200, statusText: 'OK', headers: reply.headers, config, request: {} };
  } catch (error) {
    if (error instanceof HttpError) throw failure(config, error);
    throw error;
  }
}
