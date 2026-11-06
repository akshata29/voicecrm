// User-assigned managed identity used by the Container App for every Azure call (image pull + data plane).
// One identity, no keys: RBAC assignments live next to each resource module.
param name string
param location string
param tags object = {}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: name
  location: location
  tags: tags
}

output id string = identity.id
output name string = identity.name
output principalId string = identity.properties.principalId
output clientId string = identity.properties.clientId
