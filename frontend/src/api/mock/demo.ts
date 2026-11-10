/** Demo housekeeping for the offline mock: what a reset removes, and the reset itself (an empty Inbox, like the backend). */
import type { DemoCounts, DemoData } from '../types';
import { defaultPolicies } from './seed';
import { db, setDb } from './state';

function counts(): DemoCounts {
  const state = db();
  return {
    conversations: state.convs.size,
    crm_phone_calls: state.crm.phonecall.length,
    crm_tasks: state.crm.task.length,
    crm_insights: state.crm.insight.length,
    vault_items: state.vault.length,
    learned_aliases: state.aliases.length,
    evaluation_runs: state.evalRuns.length,
    policy_changes: JSON.stringify(state.policies) !== JSON.stringify(defaultPolicies()),
  };
}

export function demoData(): DemoData {
  return { ...counts(), can_reset: true, reason: null, in_flight: 0, crm: 'local_sim' };
}

/** Reloading the page brings the sample data back; a reset leaves nothing, as it does against the real backend. */
export function resetDemo(): DemoCounts {
  const removed = counts();
  setDb({ convs: new Map(), aliases: [], policies: defaultPolicies(), vault: [], crm: { phonecall: [], task: [], insight: [] }, evalRuns: [] });
  return removed;
}
