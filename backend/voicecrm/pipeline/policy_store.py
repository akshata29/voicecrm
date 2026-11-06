"""Runtime policy store: data/policies/policies.yaml plus a persisted override written by the UI (under .state/)."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import yaml

from ..data_loader import load_policies, parse_policies
from ..domain.policy import MarketPolicy, NotesPolicy, PolicyBundle, ProcessingPolicy

logger = logging.getLogger(__name__)


class PolicyStore:
    def __init__(self, defaults: PolicyBundle, override_path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._override_path = override_path
        self._bundle = defaults
        if override_path and override_path.exists():
            try:
                self._bundle = parse_policies(yaml.safe_load(override_path.read_text(encoding="utf-8")))
            except Exception:  # a corrupt override must never take the app down
                logger.warning("ignoring unreadable policy override at %s", override_path, exc_info=True)

    @classmethod
    def from_data_dir(cls, data_dir: Path | None, override_path: Path | None) -> PolicyStore:
        return cls(load_policies(data_dir), override_path)

    @property
    def bundle(self) -> PolicyBundle:
        with self._lock:
            return self._bundle

    def market(self, market: str) -> MarketPolicy:
        b = self.bundle
        return b.markets.get(market) or next(iter(b.markets.values()))

    def update_market(self, policy: MarketPolicy) -> PolicyBundle:
        with self._lock:
            markets = {**self._bundle.markets, policy.market: policy}
            self._bundle = self._bundle.model_copy(update={"markets": markets})
            self._persist()
            return self._bundle

    def update_processing(self, processing: ProcessingPolicy) -> PolicyBundle:
        with self._lock:
            self._bundle = self._bundle.model_copy(update={"processing": processing})
            self._persist()
            return self._bundle

    def update_notes(self, notes: NotesPolicy) -> PolicyBundle:
        with self._lock:
            self._bundle = self._bundle.model_copy(update={"notes": notes})
            self._persist()
            return self._bundle

    def _persist(self) -> None:
        if not self._override_path:
            return
        self._override_path.parent.mkdir(parents=True, exist_ok=True)
        b = self._bundle
        raw = {
            "markets": {k: {f: v for f, v in m.model_dump(mode="json").items() if f != "market"} for k, m in b.markets.items()},
            "notes": b.notes.model_dump(mode="json"),
            "processing": b.processing.model_dump(mode="json"),
        }
        # quote on/off so YAML 1.1 readers do not turn them into booleans
        for m in raw["markets"].values():
            m["transcript_retention"] = str(m["transcript_retention"])
        self._override_path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
