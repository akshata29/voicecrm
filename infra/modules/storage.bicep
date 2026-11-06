// Storage account for the audio/transcript vault (VOICECRM_VAULT_MODE=azure_blob).
// Hardened: TLS 1.2, no anonymous blob access, shared-key (account key / SAS) access disabled => Entra ID + RBAC only.
// A lifecycle rule acts as a BACKSTOP at the maximum market retention; the application enforces per-market retention itself.
param name string
param location string
param tags object = {}

@description('Private blob containers to create.')
param containerNames array

@description('Lifecycle backstop: delete blobs (and any snapshots/versions) this many days after creation. Set it to the LONGEST market retention. The app enforces the real per-market retention; this only catches blobs it missed.')
@minValue(1)
@maxValue(36500)
param retentionBackstopDays int = 1825

@description('Blob and container soft-delete retention in days; 0 disables soft delete so application purges are immediate.')
@minValue(0)
@maxValue(365)
param softDeleteDays int = 0

@description('Object ID of the app managed identity.')
param appPrincipalId string

@description('Optional object ID of a developer/group that also gets blob data access (for running the app locally against these resources).')
param developerPrincipalId string = ''

@allowed([ 'User', 'Group', 'ServicePrincipal' ])
param developerPrincipalType string = 'User'

// Storage Blob Data Contributor (read, write, delete blobs - the vault deletes expired items)
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

var assignees = concat(
  [ { id: appPrincipalId, type: 'ServicePrincipal' } ],
  empty(developerPrincipalId) ? [] : [ { id: developerPrincipalId, type: developerPrincipalType } ]
)

var lifecyclePrefixes = [for containerName in containerNames: '${containerName}/']

resource storage 'Microsoft.Storage/storageAccounts@2025-01-01' = {
  name: name
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    allowCrossTenantReplication: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2025-01-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: softDeleteDays > 0 ? { enabled: true, days: max(softDeleteDays, 1) } : { enabled: false }
    containerDeleteRetentionPolicy: softDeleteDays > 0 ? { enabled: true, days: max(softDeleteDays, 1) } : { enabled: false }
  }
}

resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' = [for containerName in containerNames: {
  parent: blobService
  name: containerName
  properties: {
    publicAccess: 'None'
  }
}]

resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2025-01-01' = {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'voicecrm-retention-backstop'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: [ 'blockBlob' ]
              prefixMatch: lifecyclePrefixes
            }
            actions: {
              baseBlob: {
                delete: {
                  daysAfterCreationGreaterThan: retentionBackstopDays
                }
              }
              snapshot: {
                delete: {
                  daysAfterCreationGreaterThan: retentionBackstopDays
                }
              }
              version: {
                delete: {
                  daysAfterCreationGreaterThan: retentionBackstopDays
                }
              }
            }
          }
        }
      ]
    }
  }
  dependsOn: [
    containers
  ]
}

resource blobDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for assignee in assignees: {
  name: guid(storage.id, assignee.id, storageBlobDataContributorRoleId)
  scope: storage
  properties: {
    principalId: assignee.id
    principalType: assignee.type
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
  }
}]

output name string = storage.name
output blobEndpoint string = storage.properties.primaryEndpoints.blob
