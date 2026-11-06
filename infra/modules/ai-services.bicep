// Azure AI Services account (kind AIServices): Speech fast transcription + the extraction model deployment, one resource.
// Local (key) auth is disabled; the app identity uses Entra ID. A custom subdomain is mandatory for Entra auth and
// gives the endpoints https://<name>.cognitiveservices.azure.com (Speech, Language) and https://<name>.openai.azure.com (models).
param name string
param location string
param tags object = {}

// Model deployment. Availability varies by region, subscription and deployment type - verify quota first
// (docs/infrastructure.md). Deprecated model versions cannot be created by new subscriptions.
param modelName string
param modelVersion string

@allowed([ 'GlobalStandard', 'DataZoneStandard', 'Standard' ])
param modelSkuName string
param modelCapacity int
param deploymentName string

@description('Also grant Azure AI Language (PII) access; only needed when VOICECRM_PII_MODE=azure_language.')
param grantLanguageAccess bool = false

@description('Object ID of the app managed identity.')
param appPrincipalId string

@description('Optional object ID of a developer/group that also gets data-plane access (for running the app locally against these resources).')
param developerPrincipalId string = ''

@allowed([ 'User', 'Group', 'ServicePrincipal' ])
param developerPrincipalType string = 'User'

// Cognitive Services Speech User - speech recognition/synthesis APIs, no key listing, view-only custom projects
var speechUserRoleId = 'f2dc8367-1007-4938-bd23-fe263f013447'
// Cognitive Services OpenAI User - chat/completions/embeddings calls and read of deployments
var openAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
// Cognitive Services Language Reader - includes Language analyze-text (PII detection)
var languageReaderRoleId = '7628b7b8-a8b2-4cdc-b46f-e9b35248918e'

var assignees = concat(
  [ { id: appPrincipalId, type: 'ServicePrincipal' } ],
  empty(developerPrincipalId) ? [] : [ { id: developerPrincipalId, type: developerPrincipalType } ]
)

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: name
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: name
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
    }
  }
}

resource model 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: deploymentName
  sku: {
    name: modelSkuName
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
    // Pinned: extraction accuracy is measured over time, so the model must not change silently.
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource speechUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for assignee in assignees: {
  name: guid(account.id, assignee.id, speechUserRoleId)
  scope: account
  properties: {
    principalId: assignee.id
    principalType: assignee.type
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', speechUserRoleId)
  }
}]

resource openAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for assignee in assignees: {
  name: guid(account.id, assignee.id, openAiUserRoleId)
  scope: account
  properties: {
    principalId: assignee.id
    principalType: assignee.type
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRoleId)
  }
}]

resource languageReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for assignee in assignees: if (grantLanguageAccess) {
  name: guid(account.id, assignee.id, languageReaderRoleId)
  scope: account
  properties: {
    principalId: assignee.id
    principalType: assignee.type
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', languageReaderRoleId)
  }
}]

output name string = account.name
@description('Speech and Language endpoint (https://<name>.cognitiveservices.azure.com/).')
output endpoint string = account.properties.endpoint
@description('Azure OpenAI-compatible endpoint used for model deployments (https://<name>.openai.azure.com/).')
output openAiEndpoint string = replace(account.properties.endpoint, '.cognitiveservices.', '.openai.')
output modelDeploymentName string = model.name
