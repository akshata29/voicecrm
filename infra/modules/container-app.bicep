// The single VoiceCRM Container App: FastAPI backend + built React SPA served from one container.
// Runs under the user-assigned managed identity (image pull from ACR + all Azure data-plane access); no secrets are defined.
param name string
param location string
param tags object = {}

@description('azd service name from azure.yaml. The azd-service-name tag is how `azd deploy` finds this app.')
param serviceName string = 'app'

param environmentId string
param identityId string
param registryLoginServer string

@description('Container image. A placeholder until `azd deploy` pushes the real image; main.bicep keeps the deployed image on re-provision.')
param image string

@description('Port the app listens on (matches the Dockerfile CMD) and the ingress target port.')
param targetPort int = 8000

@allowed([ '0.25', '0.5', '0.75', '1', '1.25', '1.5', '1.75', '2' ])
param cpu string = '0.5'

@description('Keep minReplicas at 1 (the SQLite state is per replica and lost when the replica goes away) and maxReplicas at 1 unless the app uses a shared state store.')
@minValue(0)
param minReplicas int = 1

@minValue(1)
param maxReplicas int = 1

param ingressExternal bool = true

@description('CIDR ranges allowed to reach the ingress. Empty = open to the internet (the app has no end-user sign-in).')
param allowedIpRanges array = []

@description('Environment variables ({ name, value }). No secrets: all Azure access is Entra ID via the managed identity.')
param env array

// Consumption profile requires memory = 2 x vCPU
var memoryByCpu = {
  '0.25': '0.5Gi'
  '0.5': '1Gi'
  '0.75': '1.5Gi'
  '1': '2Gi'
  '1.25': '2.5Gi'
  '1.5': '3Gi'
  '1.75': '3.5Gi'
  '2': '4Gi'
}

var healthPath = '/api/v1/health'

var ipRestrictions = [for (range, i) in allowedIpRanges: {
  name: 'allow-${i}'
  description: 'Allowed client range ${i}'
  ipAddressRange: range
  action: 'Allow'
}]

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: name
  location: location
  tags: union(tags, { 'azd-service-name': serviceName })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: ingressExternal
        targetPort: targetPort
        transport: 'auto'
        allowInsecure: false
        ipSecurityRestrictions: empty(allowedIpRanges) ? null : ipRestrictions
      }
      registries: [
        {
          server: registryLoginServer
          identity: identityId
        }
      ]
    }
    template: {
      // The app drains in-flight pipeline work on shutdown (FastAPI lifespan); give it more than the 30 s default.
      terminationGracePeriodSeconds: 60
      containers: [
        {
          name: 'app'
          image: image
          resources: {
            cpu: json(cpu)
            memory: memoryByCpu[cpu]
          }
          env: env
          probes: [
            {
              // Generous startup window: the app loads the synthetic catalogue before it serves.
              type: 'Startup'
              httpGet: {
                path: healthPath
                port: targetPort
              }
              initialDelaySeconds: 5
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 10
            }
            {
              type: 'Liveness'
              httpGet: {
                path: healthPath
                port: targetPort
              }
              periodSeconds: 30
              timeoutSeconds: 5
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: healthPath
                port: targetPort
              }
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '50'
              }
            }
          }
        ]
      }
    }
  }
}

output name string = app.name
output fqdn string = app.properties.configuration.ingress.fqdn
output uri string = 'https://${app.properties.configuration.ingress.fqdn}'
