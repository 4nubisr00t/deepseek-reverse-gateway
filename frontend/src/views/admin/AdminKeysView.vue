<template>
  <div class="p-8 max-w-7xl mx-auto w-full space-y-6">
    <div class="flex items-center justify-between">
      <div>
        <div class="flex items-center gap-2">
          <span class="text-xs font-mono px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30">ADMIN</span>
          <h1 class="text-2xl font-bold text-slate-100 tracking-tight">全局 Key 与独占绑定</h1>
        </div>
        <p class="text-xs text-slate-400 mt-1">全局管理所有用户签发的 API Key，并支持指定账号独占绑定</p>
      </div>
    </div>

    <!-- Key 表格 -->
    <div class="glass-panel rounded-xl border border-slate-800 overflow-hidden">
      <table class="w-full text-left text-xs">
        <thead class="bg-slate-900/80 border-b border-slate-800 text-slate-400 font-medium">
          <tr>
            <th class="py-3 px-4">Key ID / 前缀</th>
            <th class="py-3 px-4">归属者 (Owner)</th>
            <th class="py-3 px-4">备注名</th>
            <th class="py-3 px-4">每日配额 (次/Token)</th>
            <th class="py-3 px-4">独占绑定账号</th>
            <th class="py-3 px-4">状态</th>
            <th class="py-3 px-4 text-right">操作</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-800/60 text-slate-200">
          <tr v-for="key in allKeys" :key="key.id" class="hover:bg-slate-800/30 transition-colors">
            <td class="py-3.5 px-4 font-mono text-emerald-400">
              {{ key.prefix }}
            </td>
            <td class="py-3.5 px-4 font-medium text-slate-300">
              {{ key.owner || '公共' }}
            </td>
            <td class="py-3.5 px-4 text-slate-300">
              {{ key.name }}
            </td>
            <td class="py-3.5 px-4 font-mono text-slate-400">
              {{ key.quota_daily_requests || '不限' }} / {{ key.quota_daily_tokens ? (key.quota_daily_tokens / 1000) + 'k' : '不限' }}
            </td>
            <td class="py-3.5 px-4">
              <span v-if="key.account_labels && key.account_labels.length" class="text-cyan-400 font-mono text-[11px]">
                {{ key.account_labels.join(', ') }}
              </span>
              <span v-else class="text-slate-500 font-mono text-[11px]">公共池</span>
            </td>
            <td class="py-3.5 px-4">
              <span v-if="key.revoked" class="text-red-400 font-mono text-[10px]">已作废</span>
              <span v-else-if="key.enabled" class="text-emerald-400 font-mono text-[10px]">有效</span>
              <span v-else class="text-amber-400 font-mono text-[10px]">已停用</span>
            </td>
            <td class="py-3.5 px-4 text-right">
              <button
                v-if="!key.revoked"
                @click="openAssign(key)"
                class="px-2.5 py-1 text-[11px] bg-slate-800 hover:bg-slate-700 text-cyan-300 rounded border border-slate-700"
              >
                分配账号
              </button>
            </td>
          </tr>
          <tr v-if="allKeys.length === 0">
            <td colspan="7" class="py-12 text-center text-slate-500">
              暂无任何 Key 记录。
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 分配独占账号模态框 -->
    <div v-if="assignModalKey" class="fixed inset-0 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4 z-50">
      <div class="glass-panel w-full max-w-md rounded-2xl p-6 border border-slate-700 shadow-2xl space-y-4">
        <h2 class="text-base font-bold text-slate-100">为 Key 分配独占账号</h2>
        <p class="text-xs text-slate-400">
          目标 Key: <span class="text-emerald-400 font-mono">{{ assignModalKey.name }} ({{ assignModalKey.prefix }})</span>
        </p>

        <div>
          <label class="block text-xs font-medium text-slate-300 mb-1">
            独占账号 Label 列表（多个用英文逗号或换行隔开，留空则回归公共池）
          </label>
          <textarea
            v-model="assignLabelsText"
            rows="4"
            placeholder="acc1@mail.com, acc2@mail.com"
            class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-xs font-mono text-slate-100"
          ></textarea>
        </div>

        <div class="flex justify-end gap-3 pt-2">
          <button @click="assignModalKey = null" class="px-4 py-2 text-xs text-slate-400 hover:text-slate-200">取消</button>
          <button @click="submitAssign" class="px-4 py-2 bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-semibold text-xs rounded">保存分配</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { adminApi } from '../../api.js'

const allKeys = ref([])
const assignModalKey = ref(null)
const assignLabelsText = ref('')

const loadAllKeys = async () => {
  try {
    const res = await adminApi.getAllKeys()
    allKeys.value = res.keys || []
  } catch (err) {
    console.error('获取所有 Key 失败', err)
  }
}

onMounted(() => {
  loadAllKeys()
})

const openAssign = (key) => {
  assignModalKey.value = key
  assignLabelsText.value = (key.account_labels || []).join(', ')
}

const submitAssign = async () => {
  if (!assignModalKey.value) return
  const labels = assignLabelsText.value
    .split(/[\n,]/)
    .map(s => s.trim())
    .filter(Boolean)

  try {
    await adminApi.assignKeyAccounts(assignModalKey.value.id, labels)
    assignModalKey.value = null
    await loadAllKeys()
    alert('账号独占绑定成功')
  } catch (err) {
    alert(err.message || '分配账号失败')
  }
}
</script>