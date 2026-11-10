"""PolicyStore: saved changes persist across a restart, and a reset returns to the shipped defaults."""
from __future__ import annotations

from voicecrm.pipeline.policy_store import PolicyStore


def test_a_saved_change_survives_a_restart_and_a_reset_removes_it(tmp_path):
    path = tmp_path / "state" / "policies.override.yaml"
    store = PolicyStore.from_data_dir(None, path)
    defaults = store.bundle
    assert store.customised is False and not path.exists()

    store.update_processing(defaults.processing.model_copy(update={"auto_approve_enabled": True, "top_k_products": 5}))
    store.update_market(defaults.markets["US"].model_copy(update={"transcript_retention": "on", "transcript_retention_days": 90}))
    assert store.customised is True and path.exists()

    restarted = PolicyStore.from_data_dir(None, path)
    assert restarted.bundle.processing.auto_approve_enabled is True and restarted.customised is True

    assert restarted.reset() is True
    assert restarted.bundle == defaults and restarted.customised is False and not path.exists()
    assert PolicyStore.from_data_dir(None, path).bundle == defaults             # it stays reset after the next restart
    assert restarted.reset() is False                                           # nothing left to reset


def test_a_store_without_a_file_resets_in_memory():
    store = PolicyStore.from_data_dir(None, None)
    defaults = store.bundle
    store.update_notes(defaults.notes.model_copy(update={"keep_relationship_context": False}))
    assert store.customised is True
    assert store.reset() is True and store.bundle == defaults and store.customised is False
