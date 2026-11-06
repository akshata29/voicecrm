"""Runtime configuration (env prefix VOICECRM_, optional .env at the repo root).

Every live Azure service has a mock/local fallback. In demo mode a service whose live settings are missing falls
back to its mock/local adapter, and `resolved_modes()` reports exactly what is live so the UI can show it.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class ResolvedMode(BaseModel):
    service: str
    requested: str
    effective: str
    live: bool
    reason: str | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VOICECRM_", env_file=str(REPO_ROOT / ".env"), extra="ignore")

    demo_mode: bool = True
    data_dir: Path = REPO_ROOT / "data"
    state_dir: Path = REPO_ROOT / ".state"

    # adapter modes
    transcription_mode: Literal["mock", "azure_speech"] = "mock"
    llm_mode: Literal["rules", "foundry", "hybrid"] = "rules"
    search_mode: Literal["local", "azure_search"] = "local"
    pii_mode: Literal["local", "azure_language"] = "local"
    crm_mode: Literal["local", "dataverse"] = "local"
    vault_mode: Literal["local", "azure_blob"] = "local"

    azure_auth: Literal["entra", "key"] = "entra"
    azure_tenant_id: str = ""                      # Entra only: the tenant that owns the Azure resources (see docs/live-demo.md)

    # throughput: calls processed at once, and requests the PII client keeps in flight across all calls
    max_concurrency: int = 8
    pii_concurrency: int = 8
    # Azure AI Language PII: pack transcript segments into documents of up to this many characters (0 = one document per
    # segment). Packing bills the text actually sent and cuts requests about fivefold; see adapters/pii/packing.py.
    pii_pack_chars: int = 4000

    # largest load test the Live demo page will start (each call is billed on live services; see pipeline/load.py)
    load_max_calls: int = 2000
    # Microsoft Teams live ingestion through Graph (app-only; see docs/live-demo.md, scenario B)
    teams_mode: Literal["off", "poll", "webhook"] = "off"
    teams_tenant_id: str = ""
    teams_client_id: str = ""
    teams_client_secret: str = ""
    teams_organizers: str = ""                     # comma-separated UPNs / object ids of the sellers who host the meetings
    teams_poll_seconds: int = 20
    teams_lookback_minutes: int = 480              # how far back each poll looks (a transcript appears after its meeting ends)
    teams_client_state: str = ""                   # secret Graph echoes in every webhook notification
    teams_public_base_url: str = ""                # https URL Graph can reach (e.g. a dev tunnel), used by `voicecrm teams subscribe`

    speech_endpoint: str = ""
    speech_key: str = ""
    foundry_endpoint: str = ""
    foundry_deployment: str = ""
    foundry_key: str = ""
    language_endpoint: str = ""
    language_key: str = ""
    search_endpoint: str = ""
    search_index: str = "voicecrm-catalogue"
    search_key: str = ""
    blob_account_url: str = ""
    blob_container_audio: str = "voicecrm-audio"
    blob_container_transcripts: str = "voicecrm-transcripts"
    dataverse_url: str = ""
    dataverse_field_profile: str = "standard"      # "standard" = no custom columns; "custom" = use data/crm/field_profile.yaml

    default_market: str = "US"
    default_business_line: str = "professional_buyer"
    cors_origins: str = "http://localhost:5173"
    spa_dir: str = ""                              # built frontend (defaults to <repo>/frontend/dist when it exists)
    zoom_webhook_secret: str = ""                  # Zoom webhook secret token (URL validation + signature check)

    # ---- derived ------------------------------------------------------------------------------------
    @property
    def recordings_dir(self) -> Path:
        """Real speech recordings of the scripted calls (made by tools/gen_call_audio.py; git-ignored)."""
        return self.data_dir / "audio" / "live"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "voicecrm.sqlite"

    @property
    def crm_db_path(self) -> Path:
        return self.state_dir / "crm_sim.sqlite"

    @property
    def vault_dir(self) -> Path:
        return self.state_dir / "vault"

    def _missing(self, service: str) -> list[str]:
        key = self.azure_auth == "key"
        need: dict[str, list[tuple[str, str]]] = {
            "transcription": [("speech_endpoint", self.speech_endpoint)] + ([("speech_key", self.speech_key)] if key else []),
            "llm": [("foundry_endpoint", self.foundry_endpoint), ("foundry_deployment", self.foundry_deployment)]
            + ([("foundry_key", self.foundry_key)] if key else []),
            "search": [("search_endpoint", self.search_endpoint)] + ([("search_key", self.search_key)] if key else []),
            "pii": [("language_endpoint", self.language_endpoint)] + ([("language_key", self.language_key)] if key else []),
            "crm": [("dataverse_url", self.dataverse_url)],
            "vault": [("blob_account_url", self.blob_account_url)],
        }
        return [name for name, value in need[service] if not value]

    def resolved_modes(self) -> dict[str, ResolvedMode]:
        """Applies demo-mode fallbacks. Raises ValueError when demo_mode is off and a requested live service is incomplete."""
        spec = {
            "transcription": (self.transcription_mode, "mock"),
            "llm": (self.llm_mode, "rules"),
            "search": (self.search_mode, "local"),
            "pii": (self.pii_mode, "local"),
            "crm": (self.crm_mode, "local"),
            "vault": (self.vault_mode, "local"),
        }
        out: dict[str, ResolvedMode] = {}
        for service, (requested, fallback) in spec.items():
            if requested == fallback:
                out[service] = ResolvedMode(service=service, requested=requested, effective=requested, live=False)
                continue
            missing = self._missing(service)
            if not missing:
                out[service] = ResolvedMode(service=service, requested=requested, effective=requested, live=True)
            elif self.demo_mode:
                out[service] = ResolvedMode(
                    service=service, requested=requested, effective=fallback, live=False,
                    reason=f"missing settings: {', '.join('VOICECRM_' + m.upper() for m in missing)}",
                )
            else:
                raise ValueError(f"{service}={requested} requires: {', '.join('VOICECRM_' + m.upper() for m in missing)}")
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
