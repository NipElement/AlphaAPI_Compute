import { createRouter, createWebHashHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

// One source of truth for the nav + routes. `group` drives the sider sections,
// `admin` gates ops modules (also enforced at the gateway — nav hiding is UX).
export interface NavItem {
  path: string
  name: string
  labelKey: string
  icon: string
  group: 'workbench' | 'operations'
  admin?: boolean
}

export const NAV: NavItem[] = [
  { path: 'overview', name: 'overview', labelKey: 'nav.overview', icon: 'icon-dashboard', group: 'workbench' },
  { path: 'dev', name: 'dev', labelKey: 'nav.devMachines', icon: 'icon-desktop', group: 'workbench' },
  { path: 'jobs', name: 'jobs', labelKey: 'nav.jobs', icon: 'icon-thunderbolt', group: 'workbench' },
  { path: 'services', name: 'services', labelKey: 'nav.services', icon: 'icon-cloud', group: 'workbench' },
  { path: 'volumes', name: 'volumes', labelKey: 'nav.volumes', icon: 'icon-storage', group: 'workbench' },
  { path: 'images', name: 'images', labelKey: 'nav.images', icon: 'icon-layers', group: 'workbench' },
  { path: 'quota', name: 'quota', labelKey: 'nav.quota', icon: 'icon-bar-chart', group: 'workbench' },
  { path: 'usage', name: 'usage', labelKey: 'nav.usage', icon: 'icon-file', group: 'workbench' },
  { path: 'fleet', name: 'fleet', labelKey: 'nav.fleet', icon: 'icon-computer', group: 'operations', admin: true },
  { path: 'monitoring', name: 'monitoring', labelKey: 'nav.monitoring', icon: 'icon-dashboard', group: 'operations', admin: true },
  { path: 'alerts', name: 'alerts', labelKey: 'nav.alerts', icon: 'icon-exclamation-circle', group: 'operations', admin: true },
  { path: 'audit', name: 'audit', labelKey: 'nav.audit', icon: 'icon-history', group: 'operations', admin: true },
  { path: 'users', name: 'users', labelKey: 'nav.users', icon: 'icon-user-group', group: 'operations', admin: true },
]

const REAL: Record<string, () => Promise<unknown>> = {
  overview: () => import('@/views/OverviewView.vue'),
  dev: () => import('@/views/DevMachinesView.vue'),
  jobs: () => import('@/views/JobsView.vue'),
  services: () => import('@/views/ServicesView.vue'),
  volumes: () => import('@/views/VolumesView.vue'),
  images: () => import('@/views/ImagesView.vue'),
  quota: () => import('@/views/QuotaView.vue'),
  usage: () => import('@/views/UsageView.vue'),
  fleet: () => import('@/views/FleetView.vue'),
  monitoring: () => import('@/views/MonitoringView.vue'),
  alerts: () => import('@/views/AlertsView.vue'),
  audit: () => import('@/views/AuditView.vue'),
  users: () => import('@/views/UsersView.vue'),
}

const routes: RouteRecordRaw[] = [
  { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue') },
  {
    path: '/',
    component: () => import('@/layout/AppLayout.vue'),
    meta: { requiresAuth: true },
    children: [
      { path: '', redirect: '/overview' },
      ...NAV.map((n) => ({
        path: n.path,
        name: n.name,
        component: REAL[n.name],
        meta: { requiresAuth: true, requiresAdmin: !!n.admin, labelKey: n.labelKey },
      })),
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/overview' },
]

export const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

router.beforeEach(async (to) => {
  const authStore = useAuthStore()
  if (!authStore.ready) await authStore.load()
  if (to.meta.requiresAuth && !authStore.me) return { name: 'login' }
  if (to.name === 'login' && authStore.me) return { path: '/overview' }
  if (to.meta.requiresAdmin && !authStore.isAdmin()) return { path: '/overview' }
  return true
})

// A mid-session 401 (from the api client) bounces to login without a full reload.
window.addEventListener('arise-unauthorized', () => {
  const authStore = useAuthStore()
  authStore.me = null
  if (router.currentRoute.value.name !== 'login') router.push({ name: 'login' })
})
