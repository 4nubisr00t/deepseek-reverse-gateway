<template>
<aside class="w-64 border-r border-slate-800/90 bg-[#070b14] flex flex-col h-screen select-none shrink-0">
          <div class="h-16 flex items-center px-6 border-b border-slate-800/80 gap-3">
            <div class="w-8 h-8 rounded-lg bg-emerald-500/10 border border-emerald-500/30 flex items-center justify-center text-emerald-400">
              <AppIcon name="crest" :size="18" />
            </div>
            <div>
              <div class="font-bold text-sm tracking-wider text-slate-100 uppercase font-sacred">Anubis Gateway</div>
              <div class="text-[11px] text-emerald-400 font-mono tracking-tight">开发者工作台</div>
            </div>
          </div>

          <!-- 管理员专属独立传送卡片 -->
          <div v-if="authState.isAdmin()" class="p-3.5 mx-3 my-3 rounded-xl bg-gradient-to-br from-amber-500/15 via-amber-500/10 to-transparent border border-amber-500/35 shadow-lg">
            <div class="flex items-center justify-between mb-1">
              <span class="text-[11px] font-bold text-amber-300 font-sacred flex items-center gap-1.5">
                <AppIcon name="shield" :size="13" />
                <span>圣殿主控权限</span>
              </span>
              <span class="text-[9px] bg-amber-400 text-slate-950 font-bold px-1.5 py-0.2 rounded font-mono">MASTER</span>
            </div>
            <p class="text-[10px] text-slate-400 mb-2.5">进入全屏圣殿账号池与运维总台</p>
            <router-link to="/admin/accounts" class="w-full py-2 px-3 rounded-lg bg-amber-400 hover:bg-amber-300 text-slate-950 font-bold text-xs flex items-center justify-center gap-1.5 shadow transition-all">
              <AppIcon name="server" :size="13" />
              <span>进入管理总台 (/admin)</span>
            </router-link>
          </div>

          <div class="flex-1 overflow-y-auto px-3.5 py-3 space-y-6">
            <div>
              <div class="px-2.5 mb-2 text-[10px] font-semibold text-slate-500 uppercase tracking-wider font-mono">Developer Console</div>
              <nav class="space-y-1">
                <router-link to="/dashboard" class="flex items-center gap-2.5 px-3 py-2 text-xs font-medium rounded-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800/50 transition-all" active-class="!bg-emerald-500/10 !text-emerald-400 !border !border-emerald-500/25">
                  <AppIcon name="dashboard" :size="15" />
                  <span>控制台总览</span>
                </router-link>
                <router-link to="/keys" class="flex items-center gap-2.5 px-3 py-2 text-xs font-medium rounded-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800/50 transition-all" active-class="!bg-emerald-500/10 !text-emerald-400 !border !border-emerald-500/25">
                  <AppIcon name="key" :size="15" />
                  <span>API 密钥管理</span>
                </router-link>
                <router-link to="/usage" class="flex items-center gap-2.5 px-3 py-2 text-xs font-medium rounded-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800/50 transition-all" active-class="!bg-emerald-500/10 !text-emerald-400 !border !border-emerald-500/25">
                  <AppIcon name="chart" :size="15" />
                  <span>用量流水明细</span>
                </router-link>
                <router-link to="/quickstart" class="flex items-center gap-2.5 px-3 py-2 text-xs font-medium rounded-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800/50 transition-all" active-class="!bg-emerald-500/10 !text-emerald-400 !border !border-emerald-500/25">
                  <AppIcon name="code" :size="15" />
                  <span>快速接入指南</span>
                </router-link>
                <router-link to="/settings" class="flex items-center gap-2.5 px-3 py-2 text-xs font-medium rounded-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800/50 transition-all" active-class="!bg-emerald-500/10 !text-emerald-400 !border !border-emerald-500/25">
                  <AppIcon name="settings" :size="15" />
                  <span>账户安全设置</span>
                </router-link>
              </nav>
            </div>
          </div>

          <div class="p-4 border-t border-slate-800/80 bg-slate-950/40">
            <div class="flex items-center justify-between mb-3">
              <div class="truncate text-xs font-medium text-slate-200 font-mono">{{ authState.user?.email }}</div>
              <span class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                {{ authState.user?.tier || 'FREE' }}
              </span>
            </div>
            <button @click="logout" class="w-full py-1.5 text-xs text-slate-400 hover:text-red-400 hover:bg-red-500/10 rounded border border-slate-800 transition-all flex items-center justify-center gap-1.5">
              <AppIcon name="logout" :size="13" />
              <span>退出登录</span>
            </button>
          </div>
        </aside>
</template>

<script>
import { ref, reactive, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { authState, apiRequest, notify, toasts } from '../api.js'

export default {
  name: 'SidebarComponent',
  setup() {
    const router = useRouter()
    const route = useRoute()

    const logout = async () => {
          try { await apiRequest('/api/auth/logout', { method: 'POST' }); } catch (e) {}
          authState.clearAuth();
          router.push('/login');
        };
        return { authState, logout };
  }
}
</script>