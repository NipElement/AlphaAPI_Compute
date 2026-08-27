// The HTTP contract the gateway exposes, typed. Field names mirror the Python
// backends verbatim (tenant_portal.overview / console.build_fleet / auth).

export type Role = 'admin' | 'user'

export interface Me {
  name: string
  display: string
  role: Role
  tenant: string | null
}

export interface QuotaEntry {
  used: string
  hard: string
}

export interface WorkloadRow {
  name: string
  phase: string | null
  node?: string | null
  running?: number
  replicas?: number
  queue?: string
  ready?: number
  endpoint?: string
  ssh?: { service: string; port: number; user: string } | null
  class?: string
  size?: string
}

export interface Overview {
  namespace: string
  queue: string
  quota: Record<string, QuotaEntry>
  jobs: WorkloadRow[]
  devmachines: WorkloadRow[]
  services: WorkloadRow[]
  volumes: WorkloadRow[]
}

export interface Flavor {
  key: string
  vcpu: number | null
  memGi: number | null
  gpu: number
  desc: string
}

export interface Flavors {
  flavors: Flavor[]
  images: string[]
  hw: Record<string, string | number>
  priorities: Record<string, string[]>
  grid: { vcpuStep: number; memStepGi: number; storageStepGi: number }
  tenants: string[]
}

export interface GateEval {
  allowed: boolean
  reasons: string[]
}

export interface FleetNode {
  nodeId: string
  pair: string
  owner: string
  phase: string
  ready: boolean
  cordoned: boolean
  listed: boolean
  gpuUsed: number
  gpuTotal: number
  simVcpuUsed: number
  simVcpuTotal: number
  simMemUsed: number
  simMemTotal: number
  activeContracts: number
  hw: Record<string, string | number>
  gates: Record<string, GateEval>
}

export interface InfraNode {
  name: string
  role: string
  simVcpu?: number
  simMemGi?: number
  ready: boolean
  tainted: boolean
}

// From Alertmanager (/am/api/v2/alerts).
export interface AlertRow {
  labels: { severity?: string; alertname?: string; node?: string; [k: string]: string | undefined }
  startsAt?: string
}

// From /oapi/fleet .alerts (console.active_alerts) — a flatter shape.
export interface FleetAlert {
  name: string
  severity: string
  node?: string
}

export interface Fleet {
  nodes: FleetNode[]
  infraNodes: InfraNode[]
  alerts: FleetAlert[]
  events: EventRow[]
  adapter: { mode: string; productionEnabled: boolean }
  generatedAt: string
}

export interface Instance {
  name: string
  phase: string | null
  node: string | null
  started: string | null
  gpu: string
  vcpu: string
  memGi: string
}

export interface EventRow {
  at: string
  type: string
  reason: string
  message?: string
  object?: string
}

export interface PromResult {
  data: { result: Array<{ metric: Record<string, string>; value: [number, string] }> }
}
