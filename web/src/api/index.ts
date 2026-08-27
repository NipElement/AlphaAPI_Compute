import { api } from './client'
import type {
  Me, Overview, Flavors, Fleet, Instance, EventRow, PromResult, AlertRow,
} from './types'

// Namespace is always sent; the gateway re-pins it to the caller's tenant for
// non-admins, so the value here is authoritative only for admins choosing a
// tenant. Never omit it (an omitted ns was the cross-tenant bypass, audit C1).
const withNs = (path: string, ns: string) =>
  `${path}${path.includes('?') ? '&' : '?'}ns=${encodeURIComponent(ns)}`

export const auth = {
  me: () => api.get<Me>('/auth/me'),
  login: (username: string, password: string) =>
    api.post<Me>('/auth/login', { username, password }),
  logout: () => api.post<{ ok: boolean }>('/auth/logout'),
  listUsers: () => api.get<{ users: Me[] }>('/auth/users'),
  createUser: (body: { username: string; password: string; role: string; tenant?: string; display?: string }) =>
    api.post<Me>('/auth/users', body),
  deleteUser: (name: string) => api.del<{ deleted: string }>(`/auth/users/${encodeURIComponent(name)}`),
}

export const papi = {
  flavors: () => api.get<Flavors>('/papi/flavors'),
  overview: (ns: string) => api.get<Overview>(withNs('/papi/overview', ns)),
  instances: (ns: string, workload: string) =>
    api.get<{ instances: Instance[] }>(withNs(`/papi/instances?workload=${encodeURIComponent(workload)}`, ns)),
  logs: (ns: string, pod: string, tail = 300) =>
    api.get<{ pod: string; log: string }>(withNs(`/papi/logs?pod=${encodeURIComponent(pod)}&tail=${tail}`, ns)),
  events: (ns: string, name: string) =>
    api.get<{ events: EventRow[] }>(withNs(`/papi/events?name=${encodeURIComponent(name)}`, ns)),
  createDevmachine: (ns: string, body: unknown) => api.post(withNs('/papi/devmachines', ns), body),
  rotateDevmachineKey: (ns: string, name: string, sshPublicKey: string) =>
    api.put(withNs(`/papi/devmachines/${encodeURIComponent(name)}/ssh-key`, ns), { sshPublicKey }),
  createJob: (ns: string, body: unknown) => api.post(withNs('/papi/jobs', ns), body),
  createService: (ns: string, body: unknown) => api.post(withNs('/papi/services', ns), body),
  createVolume: (ns: string, body: unknown) => api.post(withNs('/papi/volumes', ns), body),
  remove: (ns: string, kind: string, name: string) =>
    api.del(withNs(`/papi/${kind}/${encodeURIComponent(name)}`, ns)),
}

export const oapi = {
  fleet: () => api.get<Fleet>('/oapi/fleet'),
  transition: (body: { nodeId: string; desiredOwner: string; approvedBy: string; reason?: string }) =>
    api.post<{ transitionId: string }>('/oapi/transition', body),
}

export const metrics = {
  query: (q: string) => api.get<PromResult>(`/prom/api/v1/query?query=${encodeURIComponent(q)}`),
  queryRange: (q: string, start: number, end: number, step: number) =>
    api.get<{ data: { result: Array<{ metric: Record<string, string>; values: [number, string][] }> } }>(
      `/prom/api/v1/query_range?query=${encodeURIComponent(q)}&start=${start}&end=${end}&step=${step}`),
  alerts: () => api.get<AlertRow[]>(`/am/api/v2/alerts`),
}
