/**
 * Axios client for /api/v1 with one namespace per domain (configApi, ingestApi, conversationsApi, policiesApi,
 * catalogueApi, crmApi, evalApi, metricsApi, vaultApi, liveApi, demoApi, phoneApi). With VITE_USE_MOCK=1 an in-browser
 * adapter serves canned data from ./mock instead of the network (npm run dev:mock).
 */
import axios, { type AxiosInstance, type AxiosProgressEvent, type AxiosRequestConfig } from 'axios';
import type {
  ApproveRequest,
  CanonicalConversation,
  CatalogueStats,
  ConfigResponse,
  ConversationDetail,
  ConversationListItem,
  ConversationStatus,
  CrmEntity,
  CrmHealth,
  CrmRow,
  DeletedCount,
  DemoCounts,
  DemoData,
  EvalRun,
  EvalRunRequest,
  FixtureInfo,
  HealthResponse,
  IngestResponse,
  LearnedAlias,
  LoadEstimate,
  LoadRequest,
  LoadStatus,
  MarketPolicy,
  MetricsSummary,
  NotesPolicy,
  PhoneSellerRegistered,
  PhoneSellerRegistration,
  PhoneStatus,
  PhoneToken,
  PhoneTokenRequest,
  PipelineStats,
  PoliciesResponse,
  PreflightProbe,
  PreflightResponse,
  ProcessingPolicy,
  ProductSearchHit,
  RejectRequest,
  ReprocessRequest,
  RetentionPreview,
  SimulateRequest,
  SummaryTemplate,
  SweepResponse,
  Taxonomy,
  TeamsHistory,
  TeamsImportItem,
  TeamsImportResult,
  TeamsPollResult,
  TeamsStatus,
  TranscriptResponse,
  TranscriptSource,
  VaultItem,
} from './types';

export const USE_MOCK = import.meta.env.VITE_USE_MOCK === '1';

export const http: AxiosInstance = axios.create({
  baseURL: '/api/v1',
  headers: { Accept: 'application/json' },
  // FastAPI expects repeated keys (status=a&status=b), not status[]=a.
  paramsSerializer: { indexes: null },
});

if (USE_MOCK) {
  // The mock bundle is loaded lazily, but the adapter is installed synchronously so no request can slip through.
  http.defaults.adapter = (config) => import('./mock').then((mock) => mock.mockAdapter(config));
}

// ---- errors ----------------------------------------------------------------------------------------
export class ApiError extends Error {
  readonly status: number | null;

  constructor(message: string, status: number | null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

function detailToText(detail: unknown): string | null {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((item) => {
      if (item && typeof item === 'object' && 'msg' in item) {
        const loc = 'loc' in item && Array.isArray(item.loc) ? `${item.loc.slice(1).join('.')}: ` : '';
        return `${loc}${String(item.msg)}`;
      }
      return typeof item === 'string' ? item : JSON.stringify(item);
    });
    return parts.join('; ');
  }
  if (detail && typeof detail === 'object') return JSON.stringify(detail);
  return null;
}

export function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error;
  if (axios.isAxiosError(error)) {
    const status = error.response?.status ?? null;
    if (!error.response) {
      return new ApiError(
        USE_MOCK
          ? error.message
          : 'Cannot reach the VoiceCRM API. Start the backend on :8010, or run "npm run dev:mock" for the offline demo.',
        null,
      );
    }
    const data: unknown = error.response.data;
    const detail = data && typeof data === 'object' && 'detail' in data ? detailToText(data.detail) : null;
    return new ApiError(detail ?? `Request failed (${status ?? 'no status'})`, status);
  }
  return new ApiError(error instanceof Error ? error.message : 'Unexpected error', null);
}

export function getErrorMessage(error: unknown): string {
  return toApiError(error).message;
}

http.interceptors.response.use(
  (response) => response,
  (error: unknown) => Promise.reject(toApiError(error)),
);

// ---- helpers ---------------------------------------------------------------------------------------
const get = <T>(url: string, config?: AxiosRequestConfig) => http.get<T>(url, config).then((r) => r.data);
const post = <T>(url: string, body?: unknown, config?: AxiosRequestConfig) =>
  http.post<T>(url, body, config).then((r) => r.data);
const put = <T>(url: string, body?: unknown) => http.put<T>(url, body).then((r) => r.data);
const del = <T>(url: string) => http.delete<T>(url).then((r) => r.data);

const seg = encodeURIComponent;

// ---- namespaces ------------------------------------------------------------------------------------
export const configApi = {
  get: () => get<ConfigResponse>('/config'),
  health: () => get<HealthResponse>('/health'),
};

export const ingestApi = {
  /** Multipart: `file` plus form fields channel, market, business_line, seller_name, seller_email, ... */
  upload: (form: FormData, onProgress?: (event: AxiosProgressEvent) => void) =>
    post<IngestResponse>('/ingest/upload', form, { onUploadProgress: onProgress }),
  fixtures: () => get<FixtureInfo[]>('/fixtures'),
  /** The real recording of a scripted call, for an <audio> element (present when `recording_seconds` is set). */
  fixtureAudioUrl: (fixtureId: string) => `${http.defaults.baseURL ?? ''}/fixtures/${seg(fixtureId)}/audio`,
  simulate: (body: SimulateRequest) => post<IngestResponse>('/ingest/simulate', body),
};

export interface ConversationListParams {
  status?: ConversationStatus[];
  limit?: number;
  offset?: number;
}

export const conversationsApi = {
  list: (params: ConversationListParams = {}) => get<ConversationListItem[]>('/conversations', { params }),
  get: (id: string) => get<ConversationDetail>(`/conversations/${seg(id)}`),
  /** Only call when ConversationDetail.transcript_available is true; 404 carries the policy explanation. */
  transcript: async (id: string): Promise<TranscriptResponse> => {
    const response = await http.get<CanonicalConversation>(`/conversations/${seg(id)}/transcript`);
    const header: unknown = response.headers['x-transcript-source'];
    const source: TranscriptSource | null = header === 'working' || header === 'vault' ? header : null;
    return { transcript: response.data, source };
  },
  approve: (id: string, body: ApproveRequest = {}) => post<ConversationDetail>(`/conversations/${seg(id)}/approve`, body),
  reject: (id: string, body: RejectRequest = {}) => post<ConversationDetail>(`/conversations/${seg(id)}/reject`, body),
  reprocess: (id: string, body: ReprocessRequest) => post<ConversationDetail>(`/conversations/${seg(id)}/reprocess`, body),
};

export const policiesApi = {
  get: () => get<PoliciesResponse>('/policies'),
  updateMarket: (policy: MarketPolicy) => put<PoliciesResponse>(`/policies/markets/${seg(policy.market)}`, policy),
  updateProcessing: (policy: ProcessingPolicy) => put<PoliciesResponse>('/policies/processing', policy),
  updateNotes: (policy: NotesPolicy) => put<PoliciesResponse>('/policies/notes', policy),
  retentionPreview: (market: string) => get<RetentionPreview>('/policies/retention-preview', { params: { market } }),
};

export const catalogueApi = {
  stats: () => get<CatalogueStats>('/catalogue/stats'),
  search: (q: string, top = 10) => get<ProductSearchHit[]>('/catalogue/search', { params: { q, top } }),
  aliases: () => get<LearnedAlias[]>('/aliases'),
  resetAliases: () => del<DeletedCount>('/aliases'),
  templates: () => get<SummaryTemplate[]>('/templates'),
  taxonomy: () => get<Taxonomy>('/taxonomy'),
};

export const crmApi = {
  records: (entity: CrmEntity, limit = 100) => get<CrmRow[]>('/crm/records', { params: { entity, limit } }),
  health: () => get<CrmHealth>('/crm/health'),
};

export const evalApi = {
  run: (body: EvalRunRequest) => post<EvalRun>('/eval/run', body),
  runs: () => get<EvalRun[]>('/eval/runs'),
  get: (runId: string) => get<EvalRun>(`/eval/runs/${seg(runId)}`),
};

export const metricsApi = {
  summary: () => get<MetricsSummary>('/metrics/summary'),
  pipeline: () => get<PipelineStats>('/metrics/pipeline'),
};

export const teamsApi = {
  status: () => get<TeamsStatus>('/sources/teams'),
  /** Look for new transcripts now instead of waiting for the next scheduled poll. */
  poll: () => post<TeamsPollResult>('/sources/teams/poll'),
  /** The transcripts the organisers still have from the last `days` days (nothing is ingested). */
  history: (days: number) => get<TeamsHistory>('/sources/teams/history', { params: { days } }),
  /** Run the pipeline on transcripts picked from `history`, without waiting for a live meeting. */
  importTranscripts: (items: TeamsImportItem[]) => post<TeamsImportResult>('/sources/teams/import', { items }),
};

export const vaultApi = {
  items: (conversationId?: string) =>
    get<VaultItem[]>('/vault/items', { params: conversationId ? { conversation_id: conversationId } : undefined }),
  sweep: () => post<SweepResponse>('/retention/sweep'),
};

/** The VoiceCRM phone line: the service answers and records, this browser can dial it and can answer as the seller. */
export const phoneApi = {
  status: () => get<PhoneStatus>('/sources/phone'),
  /** An ACS identity and calling token for this browser. 409 when the line is not configured, 502 on an Azure error. */
  token: (body: PhoneTokenRequest) => post<PhoneToken>('/phone/token', body),
  /** Registers this browser as the seller console; call it again within `ttl_seconds` to keep the registration alive. */
  registerSeller: (body: PhoneSellerRegistration) => post<PhoneSellerRegistered>('/phone/seller', body),
  unregisterSeller: (identity: string) => del<Pick<PhoneSellerRegistered, 'registered'>>(`/phone/seller?identity=${seg(identity)}`),
  /** For `pagehide`: axios cannot outlive the page, a keepalive fetch can. Best effort; the registration also expires on its own. */
  unregisterSellerOnUnload: (identity: string) => {
    if (USE_MOCK) return;
    void fetch(`${http.defaults.baseURL ?? ''}/phone/seller?identity=${seg(identity)}`, { method: 'DELETE', keepalive: true }).catch(() => undefined);
  },
};

/** The Live demo page: a load test run from the UI, and the in-app check of the live services. */
export const liveApi = {
  load: () => get<LoadStatus>('/live/load'),
  estimate: (body: LoadRequest) => post<LoadEstimate>('/live/load/estimate', body),
  start: (body: LoadRequest) => post<LoadStatus>('/live/load', body),
  stop: () => post<LoadStatus>('/live/load/stop'),
  /** Removes every call a load test created (inbox, vault, state store and the local CRM simulator). */
  clear: () => del<DeletedCount>('/live/load/calls'),
  preflight: (only?: PreflightProbe[]) => post<PreflightResponse>('/live/preflight', only ? { only } : {}),
};

/** Demo housekeeping on the Live demo page: what a reset would remove, and the reset itself. */
export const demoApi = {
  data: () => get<DemoData>('/demo/data'),
  /** Removes every call, CRM row the pipeline wrote, vault copy, learned alias, evaluation run and saved policy change. */
  reset: () => post<DemoCounts>('/demo/reset', { confirm: true }),
};
