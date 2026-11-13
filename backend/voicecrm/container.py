"""Composition root: builds the pipeline from settings, with a mock/local fallback for every live service.

`build_container` is the only place that knows which adapter implements which port. `Container.isolated()` returns a
second, throw-away container (in-memory state store and CRM, temp vault, its own resolver with no learned aliases) that
shares the stateless adapters - the evaluation harness uses it so scoring a golden set never touches real state.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .adapters.crm.local_sim import LocalCrm
from .adapters.pii.local import LocalPiiDetector
from .adapters.transcription.mock import MockTranscriber
from .adapters.vault.local_fs import LocalVault
from .config import ResolvedMode, Settings, get_settings
from .data_loader import (
    load_catalogue, load_crm_seed, load_pricing, load_scripted_calls, load_taxonomy, load_templates,
)
from .domain.catalogue import Catalogue
from .domain.crm import CrmSeed
from .domain.fixtures import ScriptedCall
from .domain.metering import PricingTable
from .domain.taxonomy import Taxonomy
from .domain.templates import SummaryTemplate
from .engines.catalogue_index import CatalogueIndex, LocalCatalogueSearch
from .engines.cost import PricingCostEngine
from .engines.extractor import build_extractors
from .engines.notes_policy import RulesNotesPolicyEngine
from .engines.product_resolver import RulesProductResolver
from .engines.retention import PolicyRetentionEngine
from .engines.summarizer import RulesSummaryEngine
from .engines.tasks import RulesTaskEngine
from .engines.templates import MarkdownNoteRenderer, select_template
from .engines.topics import RulesTopicEngine
from .engines.validator import RulesValidator
from .pipeline.models import PipelineDeps
from .pipeline.policy_store import PolicyStore
from .pipeline.service import PipelineService
from .ports.llm import LlmPort
from .ports.search import CatalogueSearchPort
from .store.sqlite_store import SqliteStateStore

if TYPE_CHECKING:
    from .pipeline.load import LoadRunner

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DataBundle:
    """Everything loaded from data/ - read-only, so it is cached per data directory."""

    catalogue: Catalogue
    taxonomy: Taxonomy
    templates: dict[str, SummaryTemplate]
    pricing: PricingTable
    crm_seed: CrmSeed
    scripted_calls: dict[str, ScriptedCall]
    index: CatalogueIndex


@lru_cache(maxsize=4)
def _load_bundle(data_dir: str) -> DataBundle:
    path = Path(data_dir)
    catalogue = load_catalogue(path)
    return DataBundle(
        catalogue=catalogue, taxonomy=load_taxonomy(path), templates=load_templates(path), pricing=load_pricing(path),
        crm_seed=load_crm_seed(path), scripted_calls={c.fixture_id: c for c in load_scripted_calls(path)},
        index=CatalogueIndex(catalogue))


@dataclass
class Container:
    settings: Settings
    modes: dict[str, ResolvedMode]
    deps: PipelineDeps
    service: PipelineService
    policy_store: PolicyStore
    scripted_calls: dict[str, ScriptedCall]
    search: CatalogueSearchPort
    index: CatalogueIndex
    llm: LlmPort | None = None
    teams: Any | None = None                          # TeamsIngestor when VOICECRM_TEAMS_MODE is poll | webhook
    phone: Any | None = None                          # PhoneLine when VOICECRM_PHONE_MODE is acs
    _ingress: Any | None = None                       # the phone ingress listener (webhooks only), started with the phone line
    _data: DataBundle | None = None
    _temp_dir: Path | None = None
    _crm_owned: bool = True
    _closers: list[Any] = field(default_factory=list)
    _load: Any | None = None

    @property
    def credential(self) -> Any | None:
        """The shared Entra credential, or None when the adapters use keys."""
        return self._closers[0] if self._closers else None

    @property
    def load(self) -> LoadRunner:
        """The Live demo load-test runner, created on first use."""
        if self._load is None:
            from .pipeline.load import LoadRunner

            self._load = LoadRunner(self)
        return self._load

    # ------------------------------------------------------------------ lifecycle
    async def startup(self) -> None:
        """One-time async initialisation (e.g. make sure a live search index exists)."""
        try:
            if self.modes["search"].live:
                await self.search.index(self.deps.catalogue)
        except Exception:  # the app still works on the local index; surface it in the log, not as a crash
            logger.warning("catalogue search index could not be initialised", exc_info=True)
        if self.teams is not None:
            self.teams.start()
        if self.phone is not None:
            await self.phone.start()
            if self.phone.tel is not None and self.settings.phone_ingress_port > 0:
                from .api.phone_ingress import IngressServer, create_ingress_app

                self._ingress = IngressServer(create_ingress_app(self), host=self.settings.phone_ingress_host,
                                              port=self.settings.phone_ingress_port)
                self.phone.ingress_listening = await self._ingress.start()

    async def shutdown(self) -> None:
        if self._load is not None:
            await self._load.aclose()
        if self.teams is not None:
            await self.teams.stop()
        if self._ingress is not None:
            await self._ingress.stop()
        if self.phone is not None:
            await self.phone.aclose()
        await self.service.drain()
        for adapter in (self.deps.transcription, self.deps.pii, self.deps.crm, self.deps.vault, self.search, self.llm):
            closer = getattr(adapter, "aclose", None)
            if closer is not None:
                try:
                    await closer()
                except Exception:
                    logger.debug("adapter %s failed to close cleanly", type(adapter).__name__, exc_info=True)
        for shared in self._closers:                      # the shared Entra credential, once every adapter is done with it
            close = getattr(shared, "close", None)
            if close is not None:
                try:
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
                except Exception:
                    logger.debug("shared credential failed to close cleanly", exc_info=True)
        self.close()

    def close(self) -> None:
        store_close = getattr(self.deps.store, "close", None)
        if store_close:
            store_close()
        if self._crm_owned:
            crm_close = getattr(self.deps.crm, "close", None)
            if crm_close:
                crm_close()
        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------ isolation (evaluation, tests)
    def isolated(self) -> Container:
        data = self._data or _load_bundle(str(self.settings.data_dir))
        temp = Path(tempfile.mkdtemp(prefix="voicecrm-iso-"))
        policies = PolicyStore(self.policy_store.bundle.model_copy(deep=True), None)
        store = SqliteStateStore(":memory:")
        crm = LocalCrm(":memory:", data.crm_seed)
        vault = LocalVault(temp / "vault")
        deps = _assemble(
            settings=self.settings, data=data, policies=policies, store=store, crm=crm, vault=vault,
            transcription=self.deps.transcription, pii=self.deps.pii, llm=self.llm)
        deps.default_engine = self.deps.default_engine if self.deps.default_engine in deps.extractors else "rules"
        service = PipelineService(deps)
        return Container(
            settings=self.settings, modes=self.modes, deps=deps, service=service, policy_store=policies,
            scripted_calls=self.scripted_calls, search=LocalCatalogueSearch(cindex=data.index), index=data.index, llm=self.llm,
            _data=data, _temp_dir=temp, _crm_owned=True)


def _assemble(*, settings: Settings, data: DataBundle, policies: PolicyStore, store: Any, crm: Any, vault: Any, transcription: Any,
              pii: Any, llm: LlmPort | None, default_engine: str | None = None) -> PipelineDeps:
    resolver = RulesProductResolver(data.catalogue, index=data.index)
    extractors = build_extractors(resolver=resolver, topics=RulesTopicEngine(data.taxonomy), tasks=RulesTaskEngine(),
                                  summarizer=RulesSummaryEngine(), taxonomy=data.taxonomy, llm=llm)
    configured = default_engine or policies.bundle.processing.engine
    return PipelineDeps(
        settings=settings, store=store, transcription=transcription, pii=pii, vault=vault, crm=crm, resolver=resolver,
        notes_engine=RulesNotesPolicyEngine(), extractors=extractors,
        validator=RulesValidator(data.catalogue, data.taxonomy), renderer=MarkdownNoteRenderer(),
        retention_engine=PolicyRetentionEngine(), cost_engine=PricingCostEngine(), catalogue=data.catalogue,
        taxonomy=data.taxonomy, templates=data.templates, pricing=data.pricing, policy_provider=lambda: policies.bundle,
        select_template=select_template, default_engine=configured if configured in extractors else "rules",
        max_concurrency=max(1, settings.max_concurrency))


def build_container(settings: Settings | None = None) -> Container:
    settings = settings or get_settings()
    modes = settings.resolved_modes()
    data = _load_bundle(str(settings.data_dir))
    policies = PolicyStore.from_data_dir(settings.data_dir, settings.state_dir / "policies.override.yaml")
    calls = list(data.scripted_calls.values())

    # one Entra credential (optionally pinned to the resources' tenant) shared by every live adapter; none with keys
    credential: Any | None = None
    if settings.azure_auth == "entra" and any(m.live for m in modes.values()):
        from .adapters.credentials import build_credential

        credential = build_credential(settings.azure_tenant_id or None)

    # transcription
    if modes["transcription"].effective == "azure_speech":
        from .adapters.transcription.azure_speech import AzureSpeechTranscriber

        transcription: Any = AzureSpeechTranscriber(endpoint=settings.speech_endpoint, key=settings.speech_key or None,
                                                    auth=settings.azure_auth, credential=credential)
    else:
        transcription = MockTranscriber(calls, data_dir=settings.data_dir)

    # model
    llm: LlmPort | None = None
    llm_mode = modes["llm"].effective
    if llm_mode in ("foundry", "hybrid"):
        from .adapters.llm.foundry import FoundryLlm

        llm = FoundryLlm(endpoint=settings.foundry_endpoint, deployment=settings.foundry_deployment,
                         key=settings.foundry_key or None, auth=settings.azure_auth, credential=credential)

    # PII
    if modes["pii"].effective == "azure_language":
        from .adapters.pii.azure_language import AzureLanguagePiiDetector

        pii: Any = AzureLanguagePiiDetector(endpoint=settings.language_endpoint, key=settings.language_key or None,
                                            auth=settings.azure_auth, credential=credential,
                                            concurrency=max(1, settings.pii_concurrency),
                                            pack_chars=max(0, settings.pii_pack_chars))
    else:
        pii = LocalPiiDetector()

    # search
    if modes["search"].effective == "azure_search":
        from .adapters.search.azure_search import AzureSearchCatalogue

        search: CatalogueSearchPort = AzureSearchCatalogue(endpoint=settings.search_endpoint, index_name=settings.search_index,
                                                          key=settings.search_key or None, auth=settings.azure_auth,
                                                          credential=credential)
    else:
        search = LocalCatalogueSearch(cindex=data.index)

    # CRM (Dataverse is Entra-only: it always needs a credential, whatever VOICECRM_AZURE_AUTH says)
    if modes["crm"].effective == "dataverse":
        from .adapters.crm.dataverse import DataverseCrm

        if credential is None:
            from .adapters.credentials import build_credential

            credential = build_credential(settings.azure_tenant_id or None)
        crm: Any = DataverseCrm(settings.dataverse_url, credential=credential, field_profile=settings.dataverse_field_profile,
                                templates=data.templates, topic_ids=[t.topic_id for t in data.taxonomy.topics])
    else:
        crm = LocalCrm(settings.crm_db_path, data.crm_seed)

    # vault (Blob is Entra-only as well)
    if modes["vault"].effective == "azure_blob":
        from .adapters.vault.azure_blob import BlobVault

        if credential is None:
            from .adapters.credentials import build_credential

            credential = build_credential(settings.azure_tenant_id or None)
        vault: Any = BlobVault.from_account_url(settings.blob_account_url, audio_container=settings.blob_container_audio,
                                                transcript_container=settings.blob_container_transcripts,
                                                credential=credential)
    else:
        vault = LocalVault(settings.vault_dir)

    store = SqliteStateStore(settings.db_path)
    default_engine = {"foundry": "llm", "hybrid": "hybrid"}.get(llm_mode)
    deps = _assemble(settings=settings, data=data, policies=policies, store=store, crm=crm, vault=vault,
                     transcription=transcription, pii=pii, llm=llm, default_engine=default_engine)
    service = PipelineService(deps)
    teams = None
    if settings.teams_mode != "off":
        from .pipeline.teams_live import TeamsIngestor

        teams = TeamsIngestor.from_settings(service, settings)
    phone = _build_phone(settings, service, teams, credential) if settings.phone_mode != "off" else None
    return Container(settings=settings, modes=modes, deps=deps, service=service, policy_store=policies,
                     scripted_calls=data.scripted_calls, search=search, index=data.index, llm=llm, _data=data,
                     teams=teams, phone=phone, _closers=[credential] if credential is not None else [])


def _build_phone(settings: Settings, service: PipelineService, teams: Any | None, credential: Any | None) -> Any:
    """The phone line: ACS call control, the Teams directory for the seller's name, and the shared Entra credential."""
    from .pipeline.phone_live import DirectoryUser, PhoneLine

    missing = settings.phone_missing()
    state_file = settings.state_dir / "phone_state.json"
    if missing:
        problem = "missing settings: " + ", ".join("VOICECRM_" + name.upper() for name in missing)
        return PhoneLine(service, None, settings=settings, state_file=state_file, problem=problem)
    from .adapters.telephony.acs import AcsTelephony

    owns = False
    if not settings.phone_acs_connection_string and credential is None:
        from .adapters.credentials import build_credential

        credential, owns = build_credential(settings.azure_tenant_id or None), True
    telephony = AcsTelephony(endpoint=settings.phone_acs_endpoint or None,
                             connection_string=settings.phone_acs_connection_string or None,
                             credential=None if settings.phone_acs_connection_string else credential, owns_credential=owns)

    async def directory(ident: str) -> DirectoryUser | None:
        if teams is None:
            return None
        user_id, name, email = await teams.lookup_user(ident)
        return DirectoryUser(user_id, name, email)

    return PhoneLine(service, telephony, settings=settings, state_file=state_file, directory=directory)
