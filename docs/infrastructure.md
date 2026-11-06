# VoiceCRM on Azure - infrastructure

One command deploys the demo into **one resource group**: `scripts\deploy_azure.bat` (a guarded wrapper around `azd up`).
The Bicep in `infra\` is **compiled and linted but has never been deployed**: no Azure resource was created while it was
written. Expect to tune region, model and quota on the first real run. All data is synthetic.

```
 browser ──HTTPS──► Container App "app"  (FastAPI API + built SPA, one container, user-assigned managed identity)
                        │  Entra ID only - no keys anywhere
                        ├─► Azure AI Services ─ Speech fast transcription + extraction model deployment
                        ├─► Azure AI Search ──── catalogue index (created by the app)
                        ├─► Storage (blob) ───── voicecrm-audio / voicecrm-transcripts (+ lifecycle backstop)
                        ├─► Cosmos DB serverless  optional, off by default
                        └─► Application Insights ► Log Analytics ◄─ Container Apps logs
 azd ──remote build──► Azure Container Registry ──AcrPull──► the Container App
```

## Deploy

Prerequisites
- `azd` >= 1.20 and the Azure CLI, both signed in: `az login` and `azd auth login`. Docker is not needed (the image is built remotely in ACR; azd falls back to local Docker only if ACR Tasks are not allowed in the subscription).
- Rights to create a resource group **and role assignments** at subscription scope: Owner, or Contributor plus User Access Administrator.
- Quota for the model in the chosen region (see [Region, model and quota](#region-model-and-quota)).

```
scripts\deploy_azure.bat /preview     what-if only, creates nothing
scripts\deploy_azure.bat              asks for an explicit go-ahead (type DEPLOY), then runs azd up
scripts\deploy_azure.bat /yes         skips the prompt: automation only, after the owner has approved the spend
azd down --purge                      teardown
```

`azd up` prompts once for environment name, subscription and region. It provisions `infra\main.bicep`, builds the
`Dockerfile` remotely, then swaps the image into the Container App. Until that last step the app runs a Microsoft
placeholder image, so a briefly unhealthy first revision is normal. `appExists` (`SERVICE_APP_RESOURCE_EXISTS`) stops a
later `azd provision` from resetting the deployed image.

Verify: `azd env get-value VOICECRM_APP_URL`, then `GET <url>/api/v1/health` and `GET <url>/api/v1/config` - transcription,
llm, search and vault should report `live`. In demo mode a service with incomplete settings silently falls back to its mock
adapter, so check `/config` rather than assuming. Fresh role assignments can take minutes to propagate (transient 403s).

## Resources, purpose and settings

| Resource (module) | Purpose | App settings |
|---|---|---|
| Resource group `rg-<env>` | one boundary; `azd down` removes everything | - |
| Managed identity `id-*` (`identity`) | the only identity the app uses: image pull and every data-plane call | `AZURE_CLIENT_ID`, `VOICECRM_AZURE_AUTH=entra` |
| Log Analytics `log-*` + Application Insights `appi-*` (`monitoring`) | Container Apps logs; app telemetry (Entra-only ingestion) | `APPLICATIONINSIGHTS_CONNECTION_STRING` |
| Storage `st*` (`storage`), containers `voicecrm-audio`, `voicecrm-transcripts`, lifecycle backstop | transcript/audio vault | `VOICECRM_VAULT_MODE=azure_blob`, `VOICECRM_BLOB_ACCOUNT_URL`, `VOICECRM_BLOB_CONTAINER_AUDIO`, `VOICECRM_BLOB_CONTAINER_TRANSCRIPTS` |
| AI Services `ais-*`, kind `AIServices` (`ai-services`) | Speech fast transcription | `VOICECRM_TRANSCRIPTION_MODE=azure_speech`, `VOICECRM_SPEECH_ENDPOINT` |
| Model deployment on that account | LLM extraction | `VOICECRM_LLM_MODE=hybrid`, `VOICECRM_FOUNDRY_ENDPOINT`, `VOICECRM_FOUNDRY_DEPLOYMENT` |
| AI Search `srch-*`, basic (`search`) | catalogue index | `VOICECRM_SEARCH_MODE=azure_search`, `VOICECRM_SEARCH_ENDPOINT`, `VOICECRM_SEARCH_INDEX=voicecrm-catalogue` |
| Cosmos DB `cosmos-*`, serverless (`cosmos`, **optional**) | state store | `VOICECRM_COSMOS_ENDPOINT/DATABASE/CONTAINER` - **not read by the app yet** (it uses SQLite) |
| Container Registry `cr*`, Basic (`registry`) | remote build target, image source | `AZURE_CONTAINER_REGISTRY_ENDPOINT` (for azd) |
| Container Apps environment `cae-*` + app `ca-*` (`container-apps-env`, `container-app`) | runs the single container | `VOICECRM_CORS_ORIGINS` (own URL), `VOICECRM_DEMO_MODE`, `VOICECRM_CRM_MODE`, `VOICECRM_DEFAULT_*` |

Decisions
- **PII stays local** (`VOICECRM_PII_MODE=local`). The AI Services account is multi-service, so Azure AI Language needs no extra resource: `azd env set VOICECRM_PII_MODE azure_language` then `azd provision` adds `VOICECRM_LANGUAGE_ENDPOINT` and the Language Reader role. Off by default to keep the demo predictable.
- **CRM is the local simulator** until `VOICECRM_DATAVERSE_URL` is set (then `VOICECRM_CRM_MODE=dataverse`). Use a non-production Dataverse environment only, never MSX. Register the identity (`VOICECRM_APP_IDENTITY_CLIENT_ID`) as an application user with a security role - Bicep cannot do that.
- **Endpoints**: Speech/Language use `https://<name>.cognitiveservices.azure.com` and the model uses `https://<name>.openai.azure.com` - both need the custom subdomain that Entra auth requires.
- **State is SQLite on the replica**: ephemeral, so the app is pinned to one always-on replica (`minReplicas = maxReplicas = 1`). A redeploy or restart resets conversations and learned aliases.
- **Retention**: the app enforces per-market retention. The storage lifecycle rule only deletes blobs `blobRetentionBackstopDays` (default 1825) after creation, on both containers. Soft delete is off by default so purges are immediate.

## RBAC (all assigned by Bicep, all on the resource itself)

| Principal | Scope | Role | Why |
|---|---|---|---|
| app identity | Container Registry | AcrPull `7f951dda-4ed3-4680-a7ca-43fe172d538d` | pull the image |
| app identity | AI Services | Cognitive Services Speech User `f2dc8367-1007-4938-bd23-fe263f013447` | fast transcription |
| app identity | AI Services | Cognitive Services OpenAI User `5e0bd9bd-7b93-4f28-af87-19fc36ad61bd` | model calls |
| app identity | AI Services | Cognitive Services Language Reader `7628b7b8-a8b2-4cdc-b46f-e9b35248918e` (only `azure_language`) | PII `analyze-text` |
| app identity | Storage | Storage Blob Data Contributor `ba92f5b4-2d11-453d-a403-e96b0029c9fe` | write, read, delete vault blobs |
| app identity | AI Search | Search Service Contributor `7ca78c08-252a-4471-8644-bb5ff32d4ba0` | create/update the index definition |
| app identity | AI Search | Search Index Data Contributor `8ebe5a00-799e-43f5-93ac-243d3dce84a7` | upload documents, query |
| app identity | Application Insights | Monitoring Metrics Publisher `3913510d-42f4-4e42-8a64-420c390055eb` | Entra-only telemetry ingestion |
| app identity | Cosmos account (if enabled) | Cosmos DB Built-in Data Contributor `00000000-0000-0000-0000-000000000002` (Cosmos SQL role) | data plane |
| developer (optional) | Storage, AI Services, Search, Cosmos | the same data-plane roles as the app (no AcrPull, no Metrics Publisher) | run the app locally against these resources |

Speech User and OpenAI User are used instead of the broader Cognitive Services User, which also lists keys and covers every API.
The app's Foundry adapter requests tokens for scope `https://ai.azure.com/.default`. Nothing here has been exercised against live
Azure, so if a first live model call returns 401/403, check the token audience and role assignment before widening any role.
Role names and IDs were checked against the Azure built-in roles reference.

## Parameters

Set with `azd env set <VAR> <value>`, then `azd provision` (or `azd up`). Unset = the Bicep default. Array values are JSON, so in PowerShell
quote them: `azd env set VOICECRM_ALLOWED_IP_RANGES '["203.0.113.0/24"]'`.

| Bicep parameter | azd variable | Default | Notes |
|---|---|---|---|
| `environmentName` / `location` | `AZURE_ENV_NAME` / `AZURE_LOCATION` | prompted | region must offer Speech fast transcription and the model |
| `resourceGroupName` | `AZURE_RESOURCE_GROUP` | `rg-<env>` | |
| `modelName` / `modelVersion` | `VOICECRM_MODEL_NAME` / `_VERSION` | `gpt-5.4-mini` / `2026-03-17` | version pinned, no auto-upgrade |
| `modelSkuName` | `VOICECRM_MODEL_SKU` | `GlobalStandard` | or `DataZoneStandard`, `Standard` |
| `modelCapacity` | `VOICECRM_MODEL_CAPACITY` | `30` | thousands of tokens per minute |
| `modelDeploymentName` | `VOICECRM_MODEL_DEPLOYMENT_NAME` | model name | becomes `VOICECRM_FOUNDRY_DEPLOYMENT` |
| `blobRetentionBackstopDays` | `VOICECRM_BLOB_RETENTION_BACKSTOP_DAYS` | `1825` | set to the longest market retention |
| `blobSoftDeleteDays` | `VOICECRM_BLOB_SOFT_DELETE_DAYS` | `0` | 0 = off |
| `searchSkuName` | `VOICECRM_SEARCH_SKU` | `basic` | `basic` or `standard` |
| `deployCosmosDb` | `VOICECRM_DEPLOY_COSMOS` | `false` | |
| `piiMode` | `VOICECRM_PII_MODE` | `local` | `local` or `azure_language` |
| `dataverseUrl` | `VOICECRM_DATAVERSE_URL` | empty | empty = local CRM simulator |
| `dataverseFieldProfile` | `VOICECRM_DATAVERSE_FIELD_PROFILE` | `standard` | |
| `defaultMarket` / `defaultBusinessLine` | `VOICECRM_DEFAULT_MARKET` / `_BUSINESS_LINE` | `US` / `professional_buyer` | |
| `demoMode` | `VOICECRM_DEMO_MODE` | `true` | |
| `appCpu` | `VOICECRM_APP_CPU` | `0.5` | memory is set to 2 x vCPU |
| `minReplicas` / `maxReplicas` | `VOICECRM_APP_MIN_REPLICAS` / `_MAX_REPLICAS` | `1` / `1` | keep 1 while state is SQLite |
| `allowedIpRanges` | `VOICECRM_ALLOWED_IP_RANGES` | `[]` | JSON array of CIDRs, e.g. `["203.0.113.0/24"]` |
| `developerPrincipalId` / `developerPrincipalType` | `VOICECRM_DEVELOPER_PRINCIPAL_ID` / `_TYPE` | empty / `User` | object ID from `az ad signed-in-user show --query id -o tsv` |

Outputs are the `VOICECRM_*` settings derived from the deployed resources (endpoints, names and modes; no secrets). Together with the values you set
yourself with `azd env set`, `azd env get-values` is a ready-made local config:

```
azd env get-values | Select-String '^VOICECRM_' | ForEach-Object Line | Set-Content -Encoding ascii .env
```

`.env` is git-ignored. Sign in with `az login` and deploy with `developerPrincipalId` set so your account holds the data-plane roles.

## Cost drivers (no prices; use the Azure pricing calculator)

| Driver | What makes it grow |
|---|---|
| AI Search (basic) | billed per search unit while it exists, whether or not it is queried - usually the largest idle cost |
| Container App | vCPU-seconds, GiB-seconds and requests; an always-on replica can qualify for the lower idle rate while idle, scale-to-zero bills nothing. Per-subscription monthly free grants apply (180,000 vCPU-s, 360,000 GiB-s, 2 M requests) |
| Model deployment | tokens per call (pay-as-you-go for Global/DataZone Standard) |
| Speech fast transcription | audio hours transcribed |
| Language PII | text records, only when `azure_language` |
| Container Registry | Basic daily fee plus image storage; ACR Tasks bill remote-build compute |
| Log Analytics / Application Insights | GB ingested and retention; console logs are the main source |
| Storage | capacity and transactions; audio kept up to the backstop grows with volume |
| Cosmos serverless | request units and storage, only if enabled |

To stop spend between demos, `azd down --purge` and redeploy later.

## Region, model and quota

- Speech fast transcription is region-limited. Per the Speech regions table (checked 2026-11-03) it is offered in, among others, `eastus`, `eastus2`, `westus`, `westus2`, `westus3`, `northeurope`, `westeurope`, `swedencentral`, `uksouth`, `francecentral`, `germanywestcentral`, `australiaeast`, `japaneast`. It is not offered in `southindia` or `spaincentral` even though those host AI Services.
- Default model `gpt-5.4-mini 2026-03-17` is GA, retires 2027-09-21, and lists Global Standard in nearly every region (also Data Zone Standard in many). `gpt-4.1-mini` and `gpt-4o-mini` are **Deprecated**: a subscription that never deployed them cannot create them. Check the Foundry model retirement schedule and region availability pages before changing the model, and `az cognitiveservices usage list --location <region> -o table` for quota. A quota error means lowering `VOICECRM_MODEL_CAPACITY` or picking another region; an access or availability error means the subscription cannot use that model, so try another (for example `gpt-5.4-nano` or `gpt-5-mini`).
- Global Standard may process prompts outside the account region. For strict residency choose `DataZoneStandard` or `Standard` where the model offers them, and settle the Azure region with the customer first (data residency is still an open question).

## Use existing resources instead (just set the env vars)

The app only reads the `VOICECRM_*` variables, so any compatible resource works.

- **Locally**: put the values in `.env` (see `.env.example`), keep `VOICECRM_AZURE_AUTH=entra`, `az login`, and grant your account the roles above on those resources.
- **Deployed app**: `az containerapp update -g <rg> -n <app> --set-env-vars VOICECRM_SEARCH_ENDPOINT=https://<existing>.search.windows.net ...`, and grant the app identity the roles above on the existing resources (`az role assignment create --assignee <VOICECRM_APP_IDENTITY_CLIENT_ID> --assignee-principal-type ServicePrincipal --role <role> --scope <resource id>`). The next `azd provision` re-applies the Bicep values, so make it permanent by removing the corresponding module from `infra\main.bicep`.
- Requirements: the Speech endpoint must be the custom-domain URL, Search and AI Services must allow Entra (RBAC) auth, and the vault storage account must allow Entra-authorised blob access.

## Teardown

`azd down --purge` (add `--force` to skip the prompt). `--purge` also purges the soft-deleted AI Services account and Log Analytics workspace,
which otherwise keep their names, the custom subdomain and model quota reserved. Without it, redeploying the same environment can
fail on a name conflict. `azd down` does not delete the local `.azure\` environment folder.

## Security checklist

Done by default
- [x] No keys, passwords or credentials in code, parameters or outputs; all Azure access is Entra ID through one managed identity with least-privilege roles. (The Application Insights connection string is only an identifier because key ingestion is disabled.)
- [x] Storage: shared-key and anonymous blob access off, TLS 1.2, private containers, HTTPS only.
- [x] AI Services, AI Search, Cosmos DB and Application Insights: local/key auth disabled. Container Registry: admin user and anonymous pull off.
- [x] Ingress is HTTPS only (HTTP is redirected); the container runs as a non-root user and `/app/data` is read-only to it.
- [x] Model version pinned; retention backstop on the vault; no secrets baked into the image (`.env`, state and SQLite files are excluded by `.dockerignore`).

Before sharing the URL or using real data
- [ ] **The app has no end-user sign-in.** Set `allowedIpRanges`, or put Entra authentication in front of the app.
- [ ] All endpoints are public (Entra-protected). For production add private endpoints, a VNet-integrated environment and `publicNetworkAccess: Disabled` (ACR private link needs Premium).
- [ ] Add diagnostic settings (audit logs) on AI Services, Search, Storage and Cosmos; none are configured.
- [ ] The Container Apps environment sends logs to Log Analytics with its shared key (held by the platform). In strict environments switch to the `azure-monitor` destination plus diagnostic settings and disable workspace local auth.
- [ ] App Insights accepts Entra-authenticated telemetry only; the app has no exporter yet (see below).
- [ ] Decide model deployment type and region for data residency (Global Standard vs Data Zone/Standard).
- [ ] Review soft delete (`blobSoftDeleteDays`) against the retention policy.
- [ ] Pin base images by digest and scan the image.

## Known limits

- The app has no OpenTelemetry/Application Insights exporter yet. When added it must use `DefaultAzureCredential` (`AZURE_CLIENT_ID` is set) because key ingestion is disabled.
- No Cosmos state adapter exists; `VOICECRM_COSMOS_*` are emitted for when one does (database `voicecrm`, container `state`, partition key `/pk`).
- The AI Search index is a data-plane object: the app's search adapter (`CatalogueSearchPort.index()`) creates it, Bicep does not. The identity holds the roles to do so.
- `data\audio\` (generated, git-ignored) is excluded from the image by `.dockerignore`.
- Every resource name carries a token derived from subscription, environment name and region, so changing the region under the same environment name creates a second set of resources.
