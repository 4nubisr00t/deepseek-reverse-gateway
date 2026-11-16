<template>
  <div class="min-h-screen flex bg-[#05080f] text-slate-100 antialiased font-sans">
    <!-- 全局悬浮通知容器 -->
    <div class="fixed top-4 right-4 z-[9999] flex flex-col gap-2 max-w-sm w-full pointer-events-none">
      <div
        v-for="t in toasts"
        :key="t.id"
        :class="t.type === 'error' ? 'border-red-500/40 bg-red-950/90 text-red-200' : (t.type === 'success' ? 'border-emerald-500/40 bg-emerald-950/90 text-emerald-200' : (t.type === 'warn' ? 'border-amber-500/40 bg-amber-950/90 text-amber-200' : 'border-slate-700 bg-slate-900/95 text-slate-200'))"
        class="pointer-events-auto px-4 py-3 rounded-xl border shadow-2xl backdrop-blur-md text-xs font-medium flex items-center gap-2.5 transition-all"
      >
        <AppIcon :name="t.type === 'error' ? 'alert' : (t.type === 'success' ? 'check' : 'layers')" :size="15" />
        <span>{{ t.message }}</span>
      </div>
    </div>

    <!-- 侧边导航栏 (在登录与注册页面或管理员页面隐藏) -->
    <Sidebar v-if="showCustomerSidebar" />

    <!-- 主展示区域 -->
    <main class="flex-1 flex flex-col h-screen overflow-y-auto w-full">
      <router-view />
    </main>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import Sidebar from './components/Sidebar.vue'
import { authState, toasts } from './api.js'

const route = useRoute()

const showCustomerSidebar = computed(() => {
  return authState.isAuthenticated() 
    && route.path !== '/login' 
    && route.path !== '/register' 
    && !route.path.startsWith('/admin')
})
</script>