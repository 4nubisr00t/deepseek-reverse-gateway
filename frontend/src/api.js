import { reactive } from 'vue'

const TOKEN_KEY = 'ds_customer_jwt'
const USER_KEY = 'ds_customer_user'

export const toasts = reactive([])
let toastSeq = 0

export function notify(message, type = 'info', duration = 3400) {
  const id = ++toastSeq
  toasts.push({ id, message, type })
  setTimeout(() => {
    const idx = toasts.findIndex(t => t.id === id)
    if (idx !== -1) toasts.splice(idx, 1)
  }, duration)
}

export const authState = reactive({
  token: localStorage.getItem(TOKEN_KEY) || '',
  user: null,
  isAuthenticated() {
    return !!this.token
  },
  isAdmin() {
    return this.user && this.user.role === 'admin'
  },
  setAuth(token, user) {
    this.token = token
    this.user = user
    localStorage.setItem(TOKEN_KEY, token)
    localStorage.setItem(USER_KEY, JSON.stringify(user))
  },
  clearAuth() {
    this.token = ''
    this.user = null
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
  }
})

try {
  const cachedUser = localStorage.getItem(USER_KEY)
  if (cachedUser) {
    authState.user = JSON.parse(cachedUser)
  }
} catch (e) {
  authState.clearAuth()
}

export async function apiRequest(endpoint, options = {}) {
  const headers = {
    'Content-Type': 'application/json',
    ...(options.headers || {})
  }

  if (authState.token) {
    headers['Authorization'] = `Bearer ${authState.token}`
  }

  const response = await fetch(endpoint, {
    ...options,
    headers,
    credentials: 'include'
  })

  if (response.status === 401 && !endpoint.includes('/auth/login') && !endpoint.includes('/admin/login')) {
    authState.clearAuth()
    if (window.location.pathname !== '/login' && window.location.pathname !== '/register') {
      window.location.href = '/login'
    }
    throw new Error('认证已过期，请重新登录')
  }

  const data = await response.json().catch(() => ({}))

  if (!response.ok) {
    const errorMsg = (data.detail && typeof data.detail === 'object')
      ? (data.detail.message || JSON.stringify(data.detail))
      : (data.detail || data.message || `请求失败 (${response.status})`)
    throw new Error(errorMsg)
  }

  return data
}
