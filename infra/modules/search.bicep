// Azure AI Search for the product catalogue index (VOICECRM_SEARCH_MODE=azure_search).
// API keys are disabled (Entra/RBAC only). The index itself is a data-plane object created by the app's search adapter,
// which is why the app identity gets Search Service Contributor (create/update index) plus Search Index Data Contributor.
param name string
param location string
param tags object = {}

@allowed([ 'basic', 'standard' ])
param skuName string = 'basic'

@description('Object ID of the app managed identity.')
param appPrincipalId string

@description('Optional object ID of a developer/group that also gets index access (for running the app locally against these resources).')
param developerPrincipalId string = ''

@allowed([ 'User', 'Group', 'ServicePrincipal' ])
param developerPrincipalType string = 'User'

// Search Service Contributor - manage the service and index definitions (no document access)
var searchServiceContributorRoleId = '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
// Search Index Data Contributor - read/write documents in indexes
var searchIndexDataContributorRoleId = '8ebe5a00-799e-43f5-93ac-243d3dce84a7'

var assignees = concat(
  [ { id: appPrincipalId, type: 'ServicePrincipal' } ],
  empty(developerPrincipalId) ? [] : [ { id: developerPrincipalId, type: developerPrincipalType } ]
)

resource search 'Microsoft.Search/searchServices@2025-05-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: skuName
  }
  properties: {
    replicaCount: 1
    partitionCount: 1
    publicNetworkAccess: 'enabled'
    disableLocalAuth: true
  }
}

resource serviceContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for assignee in assignees: {
  name: guid(search.id, assignee.id, searchServiceContributorRoleId)
  scope: search
  properties: {
    principalId: assignee.id
    principalType: assignee.type
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', searchServiceContributorRoleId)
  }
}]

resource indexDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for assignee in assignees: {
  name: guid(search.id, assignee.id, searchIndexDataContributorRoleId)
  scope: search
  properties: {
    principalId: assignee.id
    principalType: assignee.type
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', searchIndexDataContributorRoleId)
  }
}]

output name string = search.name
output endpoint string = search.properties.endpoint
