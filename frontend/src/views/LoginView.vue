<template>
  <div class="min-h-screen flex items-center justify-center p-4 w-full relative overflow-hidden">
    <div class="w-full max-w-md glass-panel rounded-2xl p-8 border border-slate-800/90 shadow-2xl relative z-10">
      <div class="flex flex-col items-center mb-7">
        <div class="w-12 h-12 rounded-xl bg-amber-500/10 border border-amber-500/30 flex items-center justify-center text-amber-400 mb-3 shadow-lg shadow-amber-500/10">
          <AppIcon name="shield" :size="26" />
        </div>
        <h1 class="text-2xl font-bold text-slate-100 tracking-tight font-sacred">DEEPSEEK REVERSE GATEWAY</h1>
        <p class="text-xs text-slate-400 mt-1">协议逆向与多协议 Agent 网关 · 运维管理后台</p>
      </div>

      <form @submit.prevent="handleLogin" class="space-y-4">
        <div v-if="errorMsg" class="p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400 text-xs flex items-center gap-2">
          <AppIcon name="alert" :size="14" />
          <span>{{ errorMsg }}</span>
        </div>

        <div>
          <label class="block text-xs font-medium text-slate-300 mb-1.5">
            管理员口令 (ADMIN PASSWORD)
          </label>
          <input
            v-model="form.password"
            type="password"
            required
            placeholder="••••••••"
            class="w-full px-3.5 py-2.5 rounded-lg bg-slate-900/90 border border-slate-700/80 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:border-amber-500 font-mono"
          />
        </div>

        <button
          type="submit"
          :disabled="loading"
          class="w-full py-2.5 rounded-lg bg-amber-500 hover:bg-amber-400 text-slate-950 font-semibold text-sm shadow-lg shadow-amber-500/20 transition-all disabled:opacity-50 flex items-center justify-center gap-2"
        >
          <AppIcon name="shield" :size="15" />
          <span>{{ loading ? '正在鉴权进入...' : '验证口令进入管理后台' }}</span>
        </button>
      </form>
    </div>
  </div>
</template>

<script>
import { ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { authState, apiRequest, notify } from '../api.js'

export default {
  name: 'LoginView',
  setup() {
    const router = useRouter()
    const form = reactive({ password: '' })
    const loading = ref(false)
    const errorMsg = ref('')

    const handleLogin = async () => {
      loading.value = true
      errorMsg.value = ''
      try {
        const res = await apiRequest('/api/auth/login', {
          method: 'POST',
          body: JSON.stringify({ email: 'admin', password: form.password })
        })
        authState.setAuth(res.token, res.user)
        notify('认证成功，欢迎进入网关管理后台', 'success')
        router.push('/admin/accounts')
      } catch (err) {
        errorMsg.value = err.message || '登录失败'
      } finally {
        loading.value = false
      }
    }
    return { form, loading, errorMsg, handleLogin }
  }
}
</script>
