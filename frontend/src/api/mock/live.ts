/**
 * Live demo endpoints for the offline mock. There is no pipeline to load-test in the browser, so the runner reports idle,
 * runs are sized from the call count, a start is refused with the way to the real API, and the service check says every
 * service is a mock.
 */
import type { LoadEstimate, LoadRequest, LoadStatus, PreflightCheck, PreflightProbe, PreflightResponse } from '../types';
import { pipelineStats } from './admin';
import { fixtureInfos } from './scripts';
import { HttpError } from './state';

const MAX_CALLS = 2000;
const NEEDS_BACKEND =
  'The offline demo (npm run dev:mock) has no pipeline to load-test. Start the backend with scripts\\run_backend.bat and open the app from it.';

export function loadStatus(): LoadStatus {
  return {
    state: 'idle', run_id: null, request: null, started_at: null, finished_at: null, planned: 0, sent: 0, finished: 0, failed: 0,
    approved: 0, in_flight: 0, queued: 0, parallel: 0, window: 0, estimate: null, report: null, error: null, stored_calls: 0,
    max_calls: MAX_CALLS, pipeline: pipelineStats(),
  };
}

export function loadEstimate(body: LoadRequest): LoadEstimate {
  const count = Math.max(1, Math.floor(body.count ?? 1));
  if (count > MAX_CALLS) throw new HttpError(422, `a load test is limited to ${MAX_CALLS.toLocaleString('en-US')} calls (VOICECRM_LOAD_MAX_CALLS)`);
  const parallel = body.parallel ?? 16;
  const perSecond = Math.min(parallel / 0.05, 20);
  return {
    calls: count, via: body.via ?? 'transcript', engine: 'rules', parallel, pool: fixtureInfos().length, speech_hours: 0, pii_records: 0,
    tokens_in: 0, tokens_out: 0, cost_usd: 0, lines: [], billed: [], seconds: Math.round(count / perSecond), calls_per_minute: Math.round(perSecond * 60),
    basis: 'time: about 0 s per call with 16 in parallel, capped near 20 calls/s on one core', warnings: [], illustrative: true,
  };
}

export function startLoad(): never {
  throw new HttpError(409, NEEDS_BACKEND);
}

export function preflight(only?: PreflightProbe[]): PreflightResponse {
  const teams: PreflightCheck = {
    name: 'teams: Graph access', status: 'skip', detail: 'Teams live ingestion is off (VOICECRM_TEAMS_MODE=off)', seconds: null,
    hint: 'Set VOICECRM_TEAMS_MODE=poll and the VOICECRM_TEAMS_* settings; see docs/live-demo.md, scenario B.',
  };
  const phone: PreflightCheck = {
    name: 'phone: line', status: 'warn', detail: 'Simulated line: the offline demo places calls in this browser, so no Azure Communication Services resource is used', seconds: null,
    hint: 'Run against the real backend with the phone line configured to place a real call.',
  };
  const single = only?.length === 1 ? only[0] : undefined;
  const checks: PreflightCheck[] =
    single === 'teams'
      ? [teams]
      : single === 'phone'
        ? [phone]
        : [
            { name: 'config: modes', status: 'warn', detail: 'live: none | mock/local: transcription, llm, search, pii, crm, vault', hint: 'Every service is a mock in the offline demo.', seconds: null },
            { name: 'crm: health', status: 'pass', detail: 'local CRM simulator (SQLite) - no Dataverse configured', hint: '', seconds: 0.01 },
          ];
  return {
    checks,
    readiness: [
      { scenario: 'A  audio -> Speech -> Language -> model', verdict: 'NOT READY', why: 'speech: transcription is mock/skipped' },
      { scenario: 'B  Teams call -> VoiceCRM', verdict: 'NOT CONFIGURED', why: 'set VOICECRM_TEAMS_* (see docs/live-demo.md, scenario B)' },
      { scenario: 'C  bulk run (hundreds of calls)', verdict: 'NOT READY', why: 'language: PII detection is mock/skipped' },
      { scenario: 'D  phone call -> VoiceCRM line', verdict: 'NOT READY', why: 'phone: the offline demo simulates the line in the browser' },
    ],
    seconds: 0.1,
  };
}
