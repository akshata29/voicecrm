// VoiceCRM demo - one-command Azure deployment (`azd up`) into ONE resource group.
//
//   Log Analytics + Application Insights          Storage (audio/transcript vault + lifecycle backstop)
//   Azure AI Services (Speech + model deployment)  Azure AI Search (catalogue index)
//   Cosmos DB serverless (optional state store)    Container Registry + Container Apps environment
//   ONE Container App (FastAPI API + built SPA) under a user-assigned managed identity
//
// Secure by default: no keys, passwords or credentials are created or output. Storage, AI Services, Search, Cosmos DB
// and Application Insights have key/local auth disabled (so the Application Insights connection string is only an
// identifier); the app identity is authorised with RBAC only. All data is synthetic. See docs/infrastructure.md.
targetScope = 'subscription'

// ---------------------------------------------------------------------------------------------------------------
// azd-provided
// ---------------------------------------------------------------------------------------------------------------
@minLength(1)
@maxLength(64)
@description('azd environment name (AZURE_ENV_NAME). Drives the resource group name, the unique resource-name suffix and the azd-env-name tag.')
param environmentName string

@minLength(1)
@description('Primary Azure region (AZURE_LOCATION). It must offer Speech fast transcription AND the chosen model deployment type - see docs/infrastructure.md.')
@metadata({
  azd: {
    type: 'location'
  }
})
param location string

@description('Optional resource group name (AZURE_RESOURCE_GROUP). Default: rg-<environmentName>.')
param resourceGroupName string = ''

@description('Set by azd (SERVICE_APP_RESOURCE_EXISTS): true once the Container App exists, so re-provisioning keeps the image azd deployed instead of resetting it to the placeholder.')
param appExists bool = false

// ---------------------------------------------------------------------------------------------------------------
// Extraction model deployment (hosted on the AI Services account)
// Availability varies by region, subscription and deployment type, and models retire: confirm in the Foundry model
// retirement schedule and region availability tables, and check quota, before changing these.
// gpt-5.4-mini (GA, retires 2027-09-21) lists GlobalStandard in nearly every region. gpt-4.1-mini and gpt-4o-mini
// are Deprecated: subscriptions that never deployed them cannot create them. GPT-5 family models are reasoning
// models; the app's Foundry adapter negotiates max_completion_tokens and drops unsupported sampling parameters.
// ---------------------------------------------------------------------------------------------------------------
@description('Model name for the extraction deployment.')
param modelName string = 'gpt-5.4-mini'

@description('Model version. Pinned (no auto-upgrade) so accuracy measurements stay comparable over time.')
param modelVersion string = '2026-03-17'

@allowed([ 'GlobalStandard', 'DataZoneStandard', 'Standard' ])
@description('Deployment type. GlobalStandard has the widest availability but may process prompts outside the account region; DataZoneStandard/Standard keep processing within the data zone/region where the model offers them.')
param modelSkuName string = 'GlobalStandard'

@minValue(1)
@description('Capacity in thousands of tokens per minute (1 = 1K TPM). Must fit the subscription quota for this model, type and region.')
param modelCapacity int = 30

@description('Deployment name the app calls (VOICECRM_FOUNDRY_DEPLOYMENT). Default: the model name.')
param modelDeploymentName string = ''

// ---------------------------------------------------------------------------------------------------------------
// Data services
// ---------------------------------------------------------------------------------------------------------------
@minValue(1)
@maxValue(36500)
@description('Storage lifecycle BACKSTOP in days after blob creation. Set to the longest market retention; the app enforces the real per-market retention itself.')
param blobRetentionBackstopDays int = 1825

@minValue(0)
@maxValue(365)
@description('Blob/container soft-delete days; 0 = off so application purges are immediate.')
param blobSoftDeleteDays int = 0

@allowed([ 'basic', 'standard' ])
@description('Azure AI Search tier.')
param searchSkuName string = 'basic'

@description('Deploy the optional Cosmos DB (serverless) state store. The app uses SQLite unless a Cosmos adapter is added.')
param deployCosmosDb bool = false

// ---------------------------------------------------------------------------------------------------------------
// App behaviour (the VOICECRM_* settings that are not derived from deployed resources)
// ---------------------------------------------------------------------------------------------------------------
@allowed([ 'local', 'azure_language' ])
@description('PII detection. local = in-app rules (default). azure_language = Azure AI Language on the same AI Services account (adds the Language Reader role).')
param piiMode string = 'local'

@description('Dataverse environment URL (non-production only, never MSX). Empty = the app uses the local CRM simulator.')
param dataverseUrl string = ''

@description('VOICECRM_DATAVERSE_FIELD_PROFILE: standard (no custom columns) or custom.')
param dataverseFieldProfile string = 'standard'

param defaultMarket string = 'US'
param defaultBusinessLine string = 'professional_buyer'

@description('VOICECRM_DEMO_MODE: a service with incomplete live settings falls back to its mock/local adapter instead of failing.')
param demoMode bool = true

// ---------------------------------------------------------------------------------------------------------------
// Container App sizing and exposure
// ---------------------------------------------------------------------------------------------------------------
@allowed([ '0.25', '0.5', '0.75', '1', '1.25', '1.5', '1.75', '2' ])
param appCpu string = '0.5'

@minValue(0)
@description('Keep at 1: state is SQLite on the replica, so scale-to-zero loses conversations.')
param minReplicas int = 1

@minValue(1)
@description('Keep at 1 while the state store is SQLite (one replica = one consistent database).')
param maxReplicas int = 1

@description('CIDR ranges allowed to reach the app. Empty = open to the internet; the MVP has no end-user sign-in, so restrict this before sharing the URL.')
param allowedIpRanges array = []

@description('Optional object ID of a developer/group that also receives the data-plane roles, to run the app locally against the deployed resources. Empty = nobody but the app identity.')
param developerPrincipalId string = ''

@allowed([ 'User', 'Group', 'ServicePrincipal' ])
param developerPrincipalType string = 'User'

// ---------------------------------------------------------------------------------------------------------------
// Naming and tags
// ---------------------------------------------------------------------------------------------------------------
var resourceToken = uniqueString(subscription().id, environmentName, location)

var tags = {
  'azd-env-name': environmentName
  project: 'voicecrm'
  workload: 'voicecrm-poc'
}

var names = {
  resourceGroup: !empty(resourceGroupName) ? resourceGroupName : 'rg-${environmentName}'
  identity: 'id-${resourceToken}'
  logAnalytics: 'log-${resourceToken}'
  applicationInsights: 'appi-${resourceToken}'
  registry: 'cr${resourceToken}'
  storage: 'st${resourceToken}'
  aiServices: 'ais-${resourceToken}'
  search: 'srch-${resourceToken}'
  cosmos: 'cosmos-${resourceToken}'
  containerAppsEnvironment: 'cae-${resourceToken}'
  app: 'ca-${resourceToken}'
}

var blobContainers = {
  audio: 'voicecrm-audio'
  transcripts: 'voicecrm-transcripts'
}

var searchIndexName = 'voicecrm-catalogue'
var deploymentName = !empty(modelDeploymentName) ? modelDeploymentName : modelName
var placeholderImage = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

resource rg 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: names.resourceGroup
  location: location
  tags: tags
}

// Keep the image azd deployed when the app already exists (azd sets SERVICE_APP_RESOURCE_EXISTS).
resource existingApp 'Microsoft.App/containerApps@2025-01-01' existing = if (appExists) {
  scope: rg
  name: names.app
}

// ---------------------------------------------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------------------------------------------
module identity 'modules/identity.bicep' = {
  scope: rg
  name: 'identity'
  params: {
    name: names.identity
    location: location
    tags: tags
  }
}

module monitoring 'modules/monitoring.bicep' = {
  scope: rg
  name: 'monitoring'
  params: {
    location: location
    tags: tags
    logAnalyticsName: names.logAnalytics
    applicationInsightsName: names.applicationInsights
    appPrincipalId: identity.outputs.principalId
  }
}

module registry 'modules/registry.bicep' = {
  scope: rg
  name: 'registry'
  params: {
    name: names.registry
    location: location
    tags: tags
    pullPrincipalId: identity.outputs.principalId
  }
}

module storage 'modules/storage.bicep' = {
  scope: rg
  name: 'storage'
  params: {
    name: names.storage
    location: location
    tags: tags
    containerNames: [
      blobContainers.audio
      blobContainers.transcripts
    ]
    retentionBackstopDays: blobRetentionBackstopDays
    softDeleteDays: blobSoftDeleteDays
    appPrincipalId: identity.outputs.principalId
    developerPrincipalId: developerPrincipalId
    developerPrincipalType: developerPrincipalType
  }
}

module aiServices 'modules/ai-services.bicep' = {
  scope: rg
  name: 'ai-services'
  params: {
    name: names.aiServices
    location: location
    tags: tags
    modelName: modelName
    modelVersion: modelVersion
    modelSkuName: modelSkuName
    modelCapacity: modelCapacity
    deploymentName: deploymentName
    grantLanguageAccess: piiMode == 'azure_language'
    appPrincipalId: identity.outputs.principalId
    developerPrincipalId: developerPrincipalId
    developerPrincipalType: developerPrincipalType
  }
}

module search 'modules/search.bicep' = {
  scope: rg
  name: 'search'
  params: {
    name: names.search
    location: location
    tags: tags
    skuName: searchSkuName
    appPrincipalId: identity.outputs.principalId
    developerPrincipalId: developerPrincipalId
    developerPrincipalType: developerPrincipalType
  }
}

module cosmos 'modules/cosmos.bicep' = if (deployCosmosDb) {
  scope: rg
  name: 'cosmos'
  params: {
    name: names.cosmos
    location: location
    tags: tags
    appPrincipalId: identity.outputs.principalId
    developerPrincipalId: developerPrincipalId
  }
}

module containerAppsEnvironment 'modules/container-apps-env.bicep' = {
  scope: rg
  name: 'container-apps-environment'
  params: {
    name: names.containerAppsEnvironment
    location: location
    tags: tags
    logAnalyticsName: monitoring.outputs.logAnalyticsName
  }
}

// ---------------------------------------------------------------------------------------------------------------
// Application settings = the VOICECRM_* variables from .env.example, wired from the deployed resources
// ---------------------------------------------------------------------------------------------------------------
var crmMode = empty(dataverseUrl) ? 'local' : 'dataverse'
var appUrl = 'https://${names.app}.${containerAppsEnvironment.outputs.defaultDomain}'

// Live adapters this deployment turns on (the rest of the mode switches stay with the app defaults).
var modes = {
  transcription: 'azure_speech'
  llm: 'hybrid'
  search: 'azure_search'
  vault: 'azure_blob'
  auth: 'entra'
}

var speechEndpoint = aiServices.outputs.endpoint
var foundryEndpoint = aiServices.outputs.openAiEndpoint
var foundryDeployment = aiServices.outputs.modelDeploymentName
var languageEndpoint = piiMode == 'azure_language' ? aiServices.outputs.endpoint : ''
var searchEndpoint = search.outputs.endpoint
var blobAccountUrl = storage.outputs.blobEndpoint
var cosmosEndpoint = deployCosmosDb ? cosmos!.outputs.endpoint : ''

var appEnv = concat(
  [
    { name: 'VOICECRM_DEMO_MODE', value: demoMode ? 'true' : 'false' }
    { name: 'VOICECRM_TRANSCRIPTION_MODE', value: modes.transcription }
    { name: 'VOICECRM_LLM_MODE', value: modes.llm }
    { name: 'VOICECRM_SEARCH_MODE', value: modes.search }
    { name: 'VOICECRM_PII_MODE', value: piiMode }
    { name: 'VOICECRM_CRM_MODE', value: crmMode }
    { name: 'VOICECRM_VAULT_MODE', value: modes.vault }
    { name: 'VOICECRM_AZURE_AUTH', value: modes.auth }
    { name: 'VOICECRM_SPEECH_ENDPOINT', value: speechEndpoint }
    { name: 'VOICECRM_FOUNDRY_ENDPOINT', value: foundryEndpoint }
    { name: 'VOICECRM_FOUNDRY_DEPLOYMENT', value: foundryDeployment }
    { name: 'VOICECRM_SEARCH_ENDPOINT', value: searchEndpoint }
    { name: 'VOICECRM_SEARCH_INDEX', value: searchIndexName }
    { name: 'VOICECRM_BLOB_ACCOUNT_URL', value: blobAccountUrl }
    { name: 'VOICECRM_BLOB_CONTAINER_AUDIO', value: blobContainers.audio }
    { name: 'VOICECRM_BLOB_CONTAINER_TRANSCRIPTS', value: blobContainers.transcripts }
    { name: 'VOICECRM_DATAVERSE_FIELD_PROFILE', value: dataverseFieldProfile }
    { name: 'VOICECRM_DEFAULT_MARKET', value: defaultMarket }
    { name: 'VOICECRM_DEFAULT_BUSINESS_LINE', value: defaultBusinessLine }
    // Same-origin SPA: CORS only needs to allow the app's own origin.
    { name: 'VOICECRM_CORS_ORIGINS', value: appUrl }
    // Selects the user-assigned identity for DefaultAzureCredential / ManagedIdentityCredential.
    { name: 'AZURE_CLIENT_ID', value: identity.outputs.clientId }
    { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: monitoring.outputs.applicationInsightsConnectionString }
  ],
  // Optional settings are only emitted when they have a value (empty equals the app default).
  empty(dataverseUrl) ? [] : [ { name: 'VOICECRM_DATAVERSE_URL', value: dataverseUrl } ],
  piiMode == 'azure_language' ? [ { name: 'VOICECRM_LANGUAGE_ENDPOINT', value: languageEndpoint } ] : [],
  deployCosmosDb ? [
    // Not read by the app yet (no Cosmos state adapter); emitted so one can be wired without re-deploying.
    { name: 'VOICECRM_COSMOS_ENDPOINT', value: cosmosEndpoint }
    { name: 'VOICECRM_COSMOS_DATABASE', value: cosmos!.outputs.databaseName }
    { name: 'VOICECRM_COSMOS_CONTAINER', value: cosmos!.outputs.containerName }
  ] : []
)

module app 'modules/container-app.bicep' = {
  scope: rg
  name: 'app'
  params: {
    name: names.app
    location: location
    tags: tags
    serviceName: 'app'
    environmentId: containerAppsEnvironment.outputs.id
    identityId: identity.outputs.id
    registryLoginServer: registry.outputs.loginServer
    image: appExists ? existingApp!.properties.template.containers[0].image : placeholderImage
    cpu: appCpu
    minReplicas: minReplicas
    maxReplicas: maxReplicas
    allowedIpRanges: allowedIpRanges
    env: appEnv
  }
}

// ---------------------------------------------------------------------------------------------------------------
// Outputs (azd writes these to the environment: `azd env get-values`). Nothing secret is output.
// The VOICECRM_* outputs are the settings derived from the deployed resources. Settings you choose yourself (PII mode,
// Dataverse URL, ...) are azd inputs and are already in the environment, so `azd env get-values` is a complete local
// config. Input names are deliberately not re-output: that would pin stale defaults in the azd environment.
// ---------------------------------------------------------------------------------------------------------------
output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = registry.outputs.loginServer
output AZURE_CONTAINER_REGISTRY_NAME string = registry.outputs.name
output AZURE_CONTAINER_ENVIRONMENT_NAME string = containerAppsEnvironment.outputs.name

output VOICECRM_APP_URL string = appUrl
@description('Client ID of the app managed identity: register it as the application user in Dataverse when VOICECRM_CRM_MODE=dataverse.')
output VOICECRM_APP_IDENTITY_CLIENT_ID string = identity.outputs.clientId

output VOICECRM_TRANSCRIPTION_MODE string = modes.transcription
output VOICECRM_LLM_MODE string = modes.llm
output VOICECRM_SEARCH_MODE string = modes.search
output VOICECRM_CRM_MODE string = crmMode
output VOICECRM_VAULT_MODE string = modes.vault
output VOICECRM_AZURE_AUTH string = modes.auth
output VOICECRM_SPEECH_ENDPOINT string = speechEndpoint
output VOICECRM_FOUNDRY_ENDPOINT string = foundryEndpoint
output VOICECRM_FOUNDRY_DEPLOYMENT string = foundryDeployment
output VOICECRM_LANGUAGE_ENDPOINT string = languageEndpoint
output VOICECRM_SEARCH_ENDPOINT string = searchEndpoint
output VOICECRM_SEARCH_INDEX string = searchIndexName
output VOICECRM_BLOB_ACCOUNT_URL string = blobAccountUrl
output VOICECRM_BLOB_CONTAINER_AUDIO string = blobContainers.audio
output VOICECRM_BLOB_CONTAINER_TRANSCRIPTS string = blobContainers.transcripts
output VOICECRM_COSMOS_ENDPOINT string = cosmosEndpoint
