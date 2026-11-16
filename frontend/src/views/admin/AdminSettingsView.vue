<template>
  <div class="p-8 max-w-4xl mx-auto w-full space-y-6">
    <div class="flex items-center justify-between">
      <div>
        <div class="flex items-center gap-2">
          <span class="text-xs font-mono px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30">ADMIN</span>
          <h1 class="text-2xl font-bold text-slate-100 tracking-tight">网关运行参数配置</h1>
        </div>
        <p class="text-xs text-slate-400 mt-1">全局热更新调度、熔断策略与代理网络参数（存入 SQLite system_settings）</p>
      </div>

      <button
        @click="saveSettings"
        :disabled="saving"
        class="px-4 py-2 bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-semibold text-xs rounded-lg shadow-lg shadow-emerald-500/20 disabled:opacity-50"
      >
        {{ saving ? '正在保存...' : '保存全局配置' }}
      </button>
    </div>

    <div v-if="successMsg" class="p-3 bg-emerald-500/10 border border-emerald-500/30 rounded-lg text-emerald-400 text-xs">
      {{ successMsg }}
    </div>

    <!-- 参数配置表单 -->
    <div class="glass-panel p-6 rounded-xl border border-slate-800 space-y-5 text-xs">
      <div class="grid grid-cols-1 md:grid-cols-2 gap-5">
        <div>
          <label class="block text-slate-300 mb-1 font-medium">单号调用后冷却时间 (秒)</label>
          <input v-model.number="settings.cooldown_seconds" type="number" step="0.5" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100" />
          <span class="text-[11px] text-slate-500 mt-1 block">防止单一账号调用频率过密触发风控</span>
        </div>

        <div>
          <label class="block text-slate-300 mb-1 font-medium">遭遇 429 惩罚保护期 (秒)</label>
          <input v-model.number="settings.rate_limit_cooldown_seconds" type="number" step="1" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100" />
          <span class="text-[11px] text-slate-500 mt-1 block">上游返回 429 时账号自动避险退避时长</span>
        </div>

        <div>
          <label class="block text-slate-300 mb-1 font-medium">单会话最大轮数 (Max Turns)</label>
          <input v-model.number="settings.max_session_turns" type="number" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100" />
          <span class="text-[11px] text-slate-500 mt-1 block">超出轮数后系统将平滑分叉会话保障长度</span>
        </div>

        <div>
          <label class="block text-slate-300 mb-1 font-medium">最低有效账号水位线 (Min Active)</label>
          <input v-model.number="settings.min_active_accounts" type="number" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100" />
          <span class="text-[11px] text-slate-500 mt-1 block">低于该水位线时系统会告警并触发自愈</span>
        </div>
      </div>

      <div class="pt-2 border-t border-slate-800/80">
        <label class="block text-slate-300 mb-1 font-medium">出口 HTTP/SOCKS 代理</label>
        <input v-model="settings.proxy" type="text" placeholder="http://127.0.0.1:7890" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100 font-mono" />
        <span class="text-[11px] text-slate-500 mt-1 block">网关上游出站请求代理地址，留空则使用本机直连</span>
      </div>

      <div class="pt-3 border-t border-slate-800/80 space-y-3">
        <label class="flex items-center gap-3 cursor-pointer">
          <input type="checkbox" v-model="settings.auto_replenish" class="rounded bg-slate-900 text-emerald-500" />
          <div>
            <div class="text-slate-200 font-medium">自动后台洗礼补位 (Auto Replenish)</div>
            <div class="text-slate-500 text-[11px]">当有效账号数低于阈值时，自动唤醒冷库账号或后台补位</div>
          </div>
        </label>

        <label class="flex items-center gap-3 cursor-pointer">
          <input type="checkbox" v-model="settings.anti_cascade_same_ip" class="rounded bg-slate-900 text-emerald-500" />
          <div>
            <div class="text-slate-200 font-medium">同 IP 连坐熔断保护 (Anti-Cascade)</div>
            <div class="text-slate-500 text-[11px]">遭遇官方大面积封号禁言时，自动阻断同出口连带雪崩</div>
          </div>
        </label>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { adminApi } from '../../api.js'

const settings = ref({})
const saving = ref(false)
const successMsg = ref('')

onMounted(async () => {
  try {
    const res = await adminApi.getSettings()
    settings.value = res || {}
  } catch (err) {
    console.error('获取系统设置失败', err)
  }
})

const saveSettings = async () => {
  saving.value = true
  successMsg.value = ''
  try {
    await adminApi.updateSettings(settings.value)
    successMsg.value = '系统配置已成功保存并立即热生效！'
  } catch (err) {
    alert(err.message || '保存设置失败')
  } finally {
    saving.value = false
  }
}
</script>