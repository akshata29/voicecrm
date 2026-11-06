// OPTIONAL Azure Cosmos DB (NoSQL, serverless) as an application state store. Off by default - the app uses SQLite.
// Entra-only (local auth disabled): the data plane is authorised with Cosmos DB's own RBAC (sqlRoleAssignments), not keys.
// The database/container are generic placeholders until a Cosmos state adapter exists (partition key /pk).
param name string
param location string
param tags object = {}
param databaseName string = 'voicecrm'
param containerName string = 'state'

@description('Object ID of the app managed identity.')
param appPrincipalId string

@description('Optional object ID of a developer/group that also gets data access.')
param developerPrincipalId string = ''

// Cosmos DB Built-in Data Contributor (a Cosmos SQL role definition, not an Azure RBAC role)
var cosmosDataContributorRoleId = '00000000-0000-0000-0000-000000000002'

var assigneeIds = concat(
  [ appPrincipalId ],
  empty(developerPrincipalId) ? [] : [ developerPrincipalId ]
)

resource account 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: name
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    capabilities: [
      {
        name: 'EnableServerless'
      }
    ]
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    disableLocalAuth: true
    minimalTlsVersion: 'Tls12'
    publicNetworkAccess: 'Enabled'
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: account
  name: databaseName
  properties: {
    resource: {
      id: databaseName
    }
  }
}

resource container 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: database
  name: containerName
  properties: {
    resource: {
      id: containerName
      partitionKey: {
        paths: [ '/pk' ]
        kind: 'Hash'
        version: 2
      }
      // TTL enabled with no default expiry: the app can set per-item ttl for retention.
      defaultTtl: -1
    }
  }
}

resource dataContributor 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = [for assigneeId in assigneeIds: {
  parent: account
  name: guid(account.id, assigneeId, cosmosDataContributorRoleId)
  properties: {
    roleDefinitionId: '${account.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    principalId: assigneeId
    scope: account.id
  }
}]

output name string = account.name
output endpoint string = account.properties.documentEndpoint
output databaseName string = database.name
output containerName string = container.name
