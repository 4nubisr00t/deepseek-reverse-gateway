import { createRouter, createWebHistory } from 'vue-router'
import { authState } from '../api.js'

import LoginView from '../views/LoginView.vue'
import AdminMasterView from '../views/admin/AdminMasterView.vue'

const routes = [
  { path: '/login', name: 'login', component: LoginView },
  { path: '/', redirect: '/admin/accounts' },

  // 运维管理控制台 (要求 role === 'admin')
  { path: '/admin', redirect: '/admin/accounts' },
  { path: '/admin/dashboard', redirect: '/admin/accounts' },
  {
    path: '/admin/accounts',
    name: 'admin-accounts',
    component: AdminMasterView,
    meta: { requiresAuth: true, requiresAdmin: true }
  },
  {
    path: '/admin/usage',
    name: 'admin-usage',
    component: AdminMasterView,
    meta: { requiresAuth: true, requiresAdmin: true }
  },
  {
    path: '/admin/keys',
    name: 'admin-keys',
    component: AdminMasterView,
    meta: { requiresAuth: true, requiresAdmin: true }
  },
  {
    path: '/admin/ops',
    name: 'admin-ops',
    component: AdminMasterView,
    meta: { requiresAuth: true, requiresAdmin: true }
  },
  { path: '/admin/logs', redirect: '/admin/ops' },
  {
    path: '/admin/register',
    name: 'admin-register',
    component: AdminMasterView,
    meta: { requiresAuth: true, requiresAdmin: true }
  },
  { path: '/admin/registration', redirect: '/admin/register' },
  {
    path: '/admin/login',
    name: 'admin-login',
    component: AdminMasterView,
    meta: { requiresAuth: true, requiresAdmin: true }
  },
  {
    path: '/admin/add',
    name: 'admin-add',
    component: AdminMasterView,
    meta: { requiresAuth: true, requiresAdmin: true }
  },
  {
    path: '/admin/settings',
    name: 'admin-settings',
    component: AdminMasterView,
    meta: { requiresAuth: true, requiresAdmin: true }
  },
  { path: '/:pathMatch(.*)*', redirect: '/admin/accounts' }
]

const router = createRouter({
  history: createWebHistory('/'),
  routes
})

router.beforeEach((to, from, next) => {
  const isAuth = authState.isAuthenticated()
  const isAdmin = authState.isAdmin()

  if (to.meta.requiresAuth && (!isAuth || !isAdmin)) {
    next({ name: 'login' })
  } else if (to.name === 'login' && isAuth && isAdmin) {
    next({ name: 'admin-accounts' })
  } else {
    next()
  }
})

export default router
