<template>
  <div class="p-8 max-w-7xl mx-auto w-full space-y-6">
    <!-- Header -->
    <div class="flex items-center justify-between">
      <div>
        <div class="flex items-center gap-2">
          <span class="text-xs font-mono px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30">ADMIN</span>
          <h1 class="text-2xl font-bold text-slate-100 tracking-tight">账号池状态与运维</h1>
        </div>
        <p class="text-xs text-slate-400 mt-1">监控上游 DeepSeek 账号状态，管理探活、会话保活与资产录入</p>
      </div>

      <div class="flex items-center gap-3">
        <button
          @click="checkAllAccounts"
          :disabled="checking"
          class="px-3.5 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-medium border border-slate-700 transition-all flex items-center gap-2 disabled:opacity-50"
        >
          <svg class="w-3.5 h-3.5 text-cyan-400" :class="{ 'animate-spin': checking }" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
            <polyline points="23 4 23 10 17 10" />
            <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
          </svg>
          一键全量探活
        </button>

        <button
          @click="showAddModal = true"
          class="px-3.5 py-2 rounded-lg bg-emerald-500 hover:bg-emerald-400 text-slate-950 text-xs font-semibold shadow-lg shadow-emerald-500/20 transition-all flex items-center gap-2"
        >
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
            <line x1="12" y1="5" x2="12" y2="19" />
            <line x1="5" y1="12" x2="19" y2="12" />
          </svg>
          录入新账号
        </button>
      </div>
    </div>

    <!-- 资产统计条 -->
    <div class="grid grid-cols-2 md:grid-cols-5 gap-4">
      <div class="glass-panel p-4 rounded-xl border border-slate-800">
        <span class="text-xs text-slate-400 block mb-1">总账号数</span>
        <span class="text-xl font-bold font-mono text-slate-100">{{ accounts.length }}</span>
      </div>
      <div class="glass-panel p-4 rounded-xl border border-slate-800">
        <span class="text-xs text-slate-400 block mb-1">活跃账号 (ACTIVE)</span>
        <span class="text-xl font-bold font-mono text-emerald-400">{{ activeCount }}</span>
      </div>
      <div class="glass-panel p-4 rounded-xl border border-slate-800">
        <span class="text-xs text-slate-400 block mb-1">有期禁言 (DATED)</span>
        <span class="text-xl font-bold font-mono text-yellow-400">{{ datedCount }}</span>
      </div>
      <div class="glass-panel p-4 rounded-xl border border-slate-800">
        <span class="text-xs text-slate-400 block mb-1">冷却/退避 (MUTED)</span>
        <span class="text-xl font-bold font-mono text-amber-400">{{ mutedCount }}</span>
      </div>
      <div class="glass-panel p-4 rounded-xl border border-slate-800">
        <span class="text-xs text-slate-400 block mb-1">失效待续杯 (INVALID)</span>
        <span class="text-xl font-bold font-mono text-red-400">{{ invalidCount }}</span>
      </div>
    </div>

    <!-- 账号列表表格 -->
    <div class="glass-panel rounded-xl border border-slate-800 overflow-hidden">
      <table class="w-full text-left text-xs">
        <thead class="bg-slate-900/80 border-b border-slate-800 text-slate-400 font-medium">
          <tr>
            <th class="py-3 px-4">账号标识 (Label)</th>
            <th class="py-3 px-4">状态</th>
            <th class="py-3 px-4">Cookie 有效性</th>
            <th class="py-3 px-4">最近调用</th>
            <th class="py-3 px-4 text-right">操作</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-800/60 text-slate-200">
          <tr v-for="acc in accounts" :key="acc.account_label" class="hover:bg-slate-800/30 transition-colors">
            <td class="py-3.5 px-4 font-mono text-slate-200">
              {{ acc.account_label }}
            </td>
            <td class="py-3.5 px-4">
              <span
                class="px-2 py-0.5 rounded text-[10px] font-mono"
                :class="getStatusBadgeClass(acc.status, acc)"
              >
                {{ (acc.is_dated_ban || acc.ban_kind === 'dated') ? 'DATED (有期)' : acc.status }}
              </span>
            </td>
            <td class="py-3.5 px-4">
              <span v-if="acc.is_valid" class="text-emerald-400 flex items-center gap-1 font-mono">
                <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span> 正常
              </span>
              <span v-else class="text-red-400 flex items-center gap-1 font-mono">
                <span class="w-1.5 h-1.5 rounded-full bg-red-400"></span> 失效
              </span>
            </td>
            <td class="py-3.5 px-4 font-mono text-slate-400 text-[11px]">
              {{ acc.last_used ? new Date(acc.last_used * 1000).toLocaleString('zh-CN') : '未调用' }}
            </td>
            <td class="py-3.5 px-4 text-right space-x-2">
              <button
                @click="checkSingle(acc.account_label)"
                class="text-[11px] text-cyan-400 hover:text-cyan-300"
              >
                探活
              </button>
              <button
                @click="relogin(acc.account_label)"
                class="text-[11px] text-amber-400 hover:text-amber-300"
                title="纯 HTTP 极速续杯"
              >
                ⚡ 续杯
              </button>
              <button
                @click="deleteAcc(acc.account_label)"
                class="text-[11px] text-red-400 hover:text-red-300"
              >
                删除
              </button>
            </td>
          </tr>
          <tr v-if="accounts.length === 0">
            <td colspan="5" class="py-12 text-center text-slate-500">
              账号池为空，请录入或启动批量注册。
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 录入账号模态框 -->
    <div v-if="showAddModal" class="fixed inset-0 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4 z-50">
      <div class="glass-panel w-full max-w-lg rounded-2xl p-6 border border-slate-700 shadow-2xl space-y-4">
        <h2 class="text-base font-bold text-slate-100">录入新账号到池</h2>

        <div class="flex gap-2 border-b border-slate-800 pb-2 text-xs">
          <button
            @click="addMode = 'token'"
            class="px-3 py-1 rounded"
            :class="addMode === 'token' ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40' : 'text-slate-400'"
          >
            Token 直填
          </button>
          <button
            @click="addMode = 'curl'"
            class="px-3 py-1 rounded"
            :class="addMode === 'curl' ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40' : 'text-slate-400'"
          >
            cURL 解析导入
          </button>
          <button
            @click="addMode = 'email_pwd'"
            class="px-3 py-1 rounded"
            :class="addMode === 'email_pwd' ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40' : 'text-slate-400'"
          >
            邮箱 + 密码
          </button>
        </div>

        <div v-if="addMode === 'token'" class="space-y-3 text-xs">
          <div>
            <label class="block text-slate-300 mb-1">账号标识 (Label/邮箱)</label>
            <input v-model="addForm.account_label" type="text" placeholder="user@domain.com" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100" />
          </div>
          <div>
            <label class="block text-slate-300 mb-1">Bearer Token</label>
            <input v-model="addForm.token" type="text" placeholder="eyJhbGciOi..." class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100" />
          </div>
        </div>

        <div v-else-if="addMode === 'curl'" class="space-y-3 text-xs">
          <div>
            <label class="block text-slate-300 mb-1">粘贴浏览器复制的 cURL (bash)</label>
            <textarea v-model="addForm.curl_text" rows="5" placeholder="curl 'https://chat.deepseek.com/api/v0/users/current' -H 'authorization: Bearer ...'" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100 font-mono"></textarea>
          </div>
        </div>

        <div v-else class="space-y-3 text-xs">
          <div>
            <label class="block text-slate-300 mb-1">邮箱</label>
            <input v-model="addForm.email" type="email" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100" />
          </div>
          <div>
            <label class="block text-slate-300 mb-1">密码</label>
            <input v-model="addForm.password" type="password" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100" />
          </div>
        </div>

        <div class="flex justify-end gap-3 pt-2">
          <button @click="showAddModal = false" class="px-4 py-2 text-xs text-slate-400 hover:text-slate-200">取消</button>
          <button @click="submitAddAccount" class="px-4 py-2 bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-semibold text-xs rounded">确认录入</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { adminApi } from '../../api.js'

const accounts = ref([])
const checking = ref(false)
const showAddModal = ref(false)
const addMode = ref('token')
const addForm = reactive({
  account_label: '',
  token: '',
  curl_text: '',
  email: '',
  password: ''
})

const loadAccounts = async () => {
  try {
    const res = await adminApi.getAccountsHealth()
    accounts.value = res.results || res.accounts || []
  } catch (err) {
    console.error('加载账号池异常', err)
  }
}

onMounted(() => {
  loadAccounts()
})

const activeCount = computed(() => accounts.value.filter(a => a.status === 'ACTIVE' && a.usable).length)
const datedCount = computed(() => accounts.value.filter(a => a.is_dated_ban || a.ban_kind === 'dated').length)
const mutedCount = computed(() => accounts.value.filter(a => a.status === 'MUTED' && !a.is_dated_ban && a.ban_kind !== 'dated').length)
const invalidCount = computed(() => accounts.value.filter(a => !a.is_valid || a.status === 'INVALID').length)

const getStatusBadgeClass = (status, acc) => {
  if (acc && (acc.is_dated_ban || acc.ban_kind === 'dated')) return 'bg-yellow-500/10 text-yellow-400 border border-yellow-500/20'
  if (status === 'ACTIVE') return 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
  if (status === 'MUTED') return 'bg-amber-500/10 text-amber-400 border border-amber-500/20'
  return 'bg-red-500/10 text-red-400 border border-red-500/20'
}

const checkAllAccounts = async () => {
  checking.value = true
  try {
    await adminApi.checkAccount()
    await loadAccounts()
  } catch (err) {
    alert(err.message || '探活请求失败')
  } finally {
    checking.value = false
  }
}

const checkSingle = async (label) => {
  try {
    await adminApi.checkAccount(label)
    await loadAccounts()
  } catch (err) {
    alert(err.message || '探活失败')
  }
}

const relogin = async (label) => {
  try {
    await adminApi.reloginAccount(label)
    await loadAccounts()
    alert('纯 HTTP 续杯请求已触发')
  } catch (err) {
    alert(err.message || 'HTTP 续杯失败')
  }
}

const deleteAcc = async (label) => {
  if (confirm(`确定彻底删除账号 ${label}？`)) {
    try {
      await adminApi.deleteAccount(label)
      await loadAccounts()
    } catch (err) {
      alert(err.message || '删除失败')
    }
  }
}

const submitAddAccount = async () => {
  try {
    await adminApi.addAccount({
      mode: addMode.value,
      ...addForm
    })
    showAddModal.value = false
    addForm.account_label = ''
    addForm.token = ''
    addForm.curl_text = ''
    addForm.email = ''
    addForm.password = ''
    await loadAccounts()
    alert('账号录入成功')
  } catch (err) {
    alert(err.message || '录入失败')
  }
}
</script>