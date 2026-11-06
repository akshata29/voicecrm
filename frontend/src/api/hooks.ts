/** TanStack Query hooks: one query-key factory, polling rules from the API contract, cache invalidation on writes. */
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from '@tanstack/react-query';
import {
  catalogueApi,
  configApi,
  conversationsApi,
  crmApi,
  evalApi,
  ingestApi,
  liveApi,
  metricsApi,
  policiesApi,
  teamsApi,
  vaultApi,
  type ConversationListParams,
} from './client';
import type {
  ApproveRequest,
  ConversationDetail,
  CrmEntity,
  Engine,
  EvalRunRequest,
  LoadRequest,
  MarketPolicy,
  NotesPolicy,
  PoliciesResponse,
  PreflightProbe,
  ProcessingPolicy,
  RejectRequest,
  ReprocessRequest,
  SimulateRequest,
} from './types';
import { ENGINES } from './types';

export const qk = {
  config: ['config'] as const,
  conversations: ['conversations'] as const,
  conversationList: (params: ConversationListParams) => ['conversations', 'list', params] as const,
  conversation: (id: string) => ['conversation', id] as const,
  transcript: (id: string, revision = '') => ['transcript', id, revision] as const,
  policies: ['policies'] as const,
  retentionPreview: (market: string) => ['retention-preview', market] as const,
  templates: ['templates'] as const,
  taxonomy: ['taxonomy'] as const,
  fixtures: ['fixtures'] as const,
  catalogueStats: ['catalogue', 'stats'] as const,
  catalogueSearch: (q: string, top: number) => ['catalogue', 'search', q, top] as const,
  aliases: ['aliases'] as const,
  crmRecords: (entity: CrmEntity, limit: number) => ['crm', 'records', entity, limit] as const,
  crmAll: ['crm'] as const,
  crmHealth: ['crm', 'health'] as const,
  vault: ['vault'] as const,
  vaultItems: (conversationId?: string) => ['vault', 'items', conversationId ?? 'all'] as const,
  evalRuns: ['eval', 'runs'] as const,
  evalRun: (id: string) => ['eval', 'run', id] as const,
  metrics: ['metrics'] as const,
  pipelineStats: ['pipeline', 'stats'] as const,
  teams: ['sources', 'teams'] as const,
  load: ['live', 'load'] as const,
  loadEstimate: (body: LoadRequest | null) => ['live', 'load-estimate', body] as const,
};

/** Statuses during which the detail endpoint is polled (contract: every 1 s while received | processing). */
const LIVE_STATUSES = new Set(['received', 'processing', 'approved']);

/** Shared by the Inbox and the nav badge so both observe one cache entry. */
export const INBOX_PARAMS: ConversationListParams = { limit: 100 };

// ---- queries ---------------------------------------------------------------------------------------
export function useConfig() {
  return useQuery({ queryKey: qk.config, queryFn: configApi.get, staleTime: 30_000, refetchInterval: 30_000 });
}

/**
 * The deterministic "rules" engine always exists; "llm" and "hybrid" only exist when a model is live
 * (otherwise the backend answers 422 "engine is not available" or silently falls back to rules).
 */
export function useEngineOptions(): { engines: readonly Engine[]; modelLive: boolean } {
  const config = useConfig();
  const modelLive = config.data?.modes.llm?.live === true;
  return { engines: modelLive ? ENGINES : ['rules'], modelLive };
}

export function useConversationList(params: ConversationListParams = {}, pollMs: number | false = 2000) {
  return useQuery({
    queryKey: qk.conversationList(params),
    queryFn: () => conversationsApi.list(params),
    refetchInterval: pollMs,
    placeholderData: keepPreviousData,
  });
}

export function useConversation(id: string | undefined) {
  return useQuery({
    queryKey: qk.conversation(id ?? 'none'),
    queryFn: () => conversationsApi.get(id ?? ''),
    enabled: Boolean(id),
    refetchInterval: (query) => {
      const status = query.state.data?.record.status;
      return status && LIVE_STATUSES.has(status) ? 1000 : false;
    },
  });
}

/**
 * Only enabled when the caller has checked ConversationDetail.transcript_available.
 * `revision` changes whenever the record changes (approve, reprocess...), which swaps the query instead of refetching a copy that may be gone.
 */
export function useTranscript(id: string, enabled: boolean, revision = '') {
  return useQuery({
    queryKey: qk.transcript(id, revision),
    queryFn: () => conversationsApi.transcript(id),
    enabled,
    retry: false,
  });
}

export function usePolicies() {
  return useQuery({ queryKey: qk.policies, queryFn: policiesApi.get });
}

export function useRetentionPreview(market: string) {
  return useQuery({ queryKey: qk.retentionPreview(market), queryFn: () => policiesApi.retentionPreview(market) });
}

export function useTemplates() {
  return useQuery({ queryKey: qk.templates, queryFn: catalogueApi.templates, staleTime: 5 * 60_000 });
}

export function useTaxonomy() {
  return useQuery({ queryKey: qk.taxonomy, queryFn: catalogueApi.taxonomy, staleTime: 5 * 60_000 });
}

export function useFixtures() {
  return useQuery({ queryKey: qk.fixtures, queryFn: ingestApi.fixtures, staleTime: 5 * 60_000 });
}

export function useCatalogueStats() {
  return useQuery({ queryKey: qk.catalogueStats, queryFn: catalogueApi.stats });
}

export function useCatalogueSearch(query: string, top = 10) {
  return useQuery({
    queryKey: qk.catalogueSearch(query, top),
    queryFn: () => catalogueApi.search(query, top),
    enabled: query.trim().length > 0,
    placeholderData: keepPreviousData,
  });
}

export function useAliases() {
  return useQuery({ queryKey: qk.aliases, queryFn: catalogueApi.aliases });
}

export function useCrmRecords(entity: CrmEntity, limit = 100) {
  return useQuery({
    queryKey: qk.crmRecords(entity, limit),
    queryFn: () => crmApi.records(entity, limit),
    refetchInterval: 5000,
  });
}

export function useCrmHealth() {
  return useQuery({ queryKey: qk.crmHealth, queryFn: crmApi.health, retry: false });
}

export function useVaultItems(conversationId?: string) {
  return useQuery({ queryKey: qk.vaultItems(conversationId), queryFn: () => vaultApi.items(conversationId) });
}

export function useEvalRuns() {
  return useQuery({ queryKey: qk.evalRuns, queryFn: evalApi.runs });
}

export function useEvalRun(runId: string | null) {
  return useQuery({
    queryKey: qk.evalRun(runId ?? 'none'),
    queryFn: () => evalApi.get(runId ?? ''),
    enabled: Boolean(runId),
  });
}

export function useMetricsSummary() {
  return useQuery({ queryKey: qk.metrics, queryFn: metricsApi.summary, refetchInterval: 5000 });
}

/** Live batch figures: once a second while calls are being processed, every 10 s when idle. */
export function usePipelineStats() {
  return useQuery({
    queryKey: qk.pipelineStats,
    queryFn: metricsApi.pipeline,
    refetchInterval: (query) => (query.state.data?.active ? 1000 : 10_000),
  });
}

export function useTeamsStatus() {
  return useQuery({ queryKey: qk.teams, queryFn: teamsApi.status, refetchInterval: 4000 });
}

export function usePollTeams() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => teamsApi.poll(),
    onSuccess: (result) => {
      client.setQueryData(qk.teams, result.status);
      if (result.ingested > 0) refreshAfterConversationChange(client);
    },
    onError: () => void client.invalidateQueries({ queryKey: qk.teams }),
  });
}

// ---- Live demo: load test and service check -------------------------------------------------------
export const LOAD_ACTIVE_STATES: ReadonlySet<string> = new Set(['preparing', 'running', 'stopping']);

/** Once a second while a load test is going, every 10 s otherwise (the Inbox banner reads `stored_calls` from it). */
export function useLoadStatus() {
  return useQuery({
    queryKey: qk.load,
    queryFn: liveApi.load,
    refetchInterval: (query) => (query.state.data && LOAD_ACTIVE_STATES.has(query.state.data.state) ? 1000 : 10_000),
  });
}

/** Tokens, list-price cost and rough duration of the run in `body`; `null` pauses it (nothing valid to size yet). */
export function useLoadEstimate(body: LoadRequest | null) {
  return useQuery({
    queryKey: qk.loadEstimate(body),
    queryFn: () => liveApi.estimate(body as LoadRequest),
    enabled: body !== null,
    placeholderData: keepPreviousData,
    retry: false,
    staleTime: 5000,
  });
}

export function useStartLoad() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: LoadRequest) => liveApi.start(body),
    onSuccess: (status) => {
      client.setQueryData(qk.load, status);
      refreshAfterConversationChange(client);       // starting a run replaces the previous run's calls
    },
  });
}

export function useStopLoad() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: liveApi.stop,
    onSuccess: (status) => client.setQueryData(qk.load, status),
  });
}

export function useClearLoad() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: liveApi.clear,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: qk.load });
      refreshAfterConversationChange(client);
    },
  });
}

/** Probes the live services this API is running on; each caller keeps its own result (all services vs Teams only). */
export function usePreflight() {
  return useMutation({ mutationFn: (only?: PreflightProbe[]) => liveApi.preflight(only) });
}

// ---- mutations -------------------------------------------------------------------------------------
function refreshAfterConversationChange(client: QueryClient) {
  void client.invalidateQueries({ queryKey: qk.conversations });
  void client.invalidateQueries({ queryKey: qk.metrics });
  void client.invalidateQueries({ queryKey: qk.crmAll });
  void client.invalidateQueries({ queryKey: qk.vault });
  void client.invalidateQueries({ queryKey: qk.aliases });
  void client.invalidateQueries({ queryKey: qk.catalogueStats });
}

function storeDetail(client: QueryClient, id: string, detail: ConversationDetail) {
  client.setQueryData(qk.conversation(id), detail);
  refreshAfterConversationChange(client);
}

export function useApprove(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ApproveRequest) => conversationsApi.approve(id, body),
    onSuccess: (detail) => storeDetail(client, id, detail),
  });
}

/** Inbox one-click approve: the same call, but the row owns its own pending state. */
export function useApproveAny() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ApproveRequest }) => conversationsApi.approve(id, body),
    onSuccess: (detail, { id }) => storeDetail(client, id, detail),
  });
}

export function useReject(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: RejectRequest) => conversationsApi.reject(id, body),
    onSuccess: (detail) => storeDetail(client, id, detail),
  });
}

export function useReprocess(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ReprocessRequest) => conversationsApi.reprocess(id, body),
    onSuccess: (detail) => storeDetail(client, id, detail),
  });
}

export function useSimulate() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: SimulateRequest) => ingestApi.simulate(body),
    onSuccess: () => refreshAfterConversationChange(client),
  });
}

export function useUpload() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ form, onProgress }: { form: FormData; onProgress?: (percent: number) => void }) =>
      ingestApi.upload(form, (event) => {
        if (onProgress && event.total) onProgress(Math.round((event.loaded / event.total) * 100));
      }),
    onSuccess: () => refreshAfterConversationChange(client),
  });
}

function afterPolicyChange(client: QueryClient, policies: PoliciesResponse) {
  client.setQueryData(qk.policies, policies);
  void client.invalidateQueries({ queryKey: qk.config });
  void client.invalidateQueries({ queryKey: ['retention-preview'] });
  // Transcript availability and retention decisions follow the market policy.
  void client.invalidateQueries({ queryKey: ['conversation'] });
  void client.invalidateQueries({ queryKey: qk.vault });
}

export function useUpdateMarketPolicy() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (policy: MarketPolicy) => policiesApi.updateMarket(policy),
    onSuccess: (policies) => afterPolicyChange(client, policies),
  });
}

export function useUpdateProcessingPolicy() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (policy: ProcessingPolicy) => policiesApi.updateProcessing(policy),
    onSuccess: (policies) => afterPolicyChange(client, policies),
  });
}

export function useUpdateNotesPolicy() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (policy: NotesPolicy) => policiesApi.updateNotes(policy),
    onSuccess: (policies) => afterPolicyChange(client, policies),
  });
}

export function useSweep() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: vaultApi.sweep,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: qk.vault });
      void client.invalidateQueries({ queryKey: ['conversation'] });
    },
  });
}

export function useResetAliases() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: catalogueApi.resetAliases,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: qk.aliases });
      void client.invalidateQueries({ queryKey: qk.catalogueStats });
    },
  });
}

export function useRunEval() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: EvalRunRequest) => evalApi.run(body),
    onSuccess: (run) => {
      client.setQueryData(qk.evalRun(run.run_id), run);
      void client.invalidateQueries({ queryKey: qk.evalRuns });
      void client.invalidateQueries({ queryKey: qk.aliases });
    },
  });
}
