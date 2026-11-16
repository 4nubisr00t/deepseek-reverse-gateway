<template>
<div class="sanctuary-console p-3 sm:p-5 md:p-6 max-w-[1640px] mx-auto w-full space-y-4 sm:space-y-6 text-slate-100 overflow-x-hidden min-w-0">
          
          <!-- 1. 顶部圣殿主控 Header -->
          <div class="dash-header flex flex-col lg:flex-row lg:items-center justify-between gap-4 p-5 rounded-2xl border border-amber-500/30 bg-slate-900/85 backdrop-blur-md relative overflow-hidden shadow-2xl">
            <div class="absolute top-0 left-0 right-0 h-[2px] bg-gradient-to-r from-amber-500/10 via-amber-400 to-emerald-400/30"></div>
            <div class="flex items-center gap-4">
              <div class="w-12 h-12 rounded-xl bg-amber-500/10 border border-amber-500/35 flex items-center justify-center text-amber-400 shrink-0 shadow-lg shadow-amber-500/10">
                <AppIcon name="crest" :size="26" />
              </div>
              <div>
                <h1 class="text-lg md:text-xl font-bold font-sacred tracking-wider text-transparent bg-clip-text bg-gradient-to-r from-amber-200 via-amber-400 to-amber-200 flex items-center gap-2.5 flex-wrap">
                  <span>SANCTUARY · DEEPSEEK GATEWAY</span>
                  <span class="text-[10px] font-mono font-bold px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/40">V3.0 UNIFIED ARCHITECTURE</span>
                </h1>
                <div class="text-xs text-slate-400 font-mono mt-1.5 flex items-center gap-2.5 flex-wrap">
                  <span class="inline-flex items-center gap-1.5 text-emerald-400 font-medium">
                    <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>
                    <span>活跃就绪: {{ stats.valid }}</span>
                  </span>
                  <span class="text-slate-700">|</span>
                  <span class="text-cyan-400">零IP冷库: {{ coldAccounts.length }}</span>
                  <span class="text-slate-700">|</span>
                  <span class="text-yellow-400">有期禁言: {{ stats.dated }}</span>
                  <span class="text-slate-700">|</span>
                  <span class="text-red-400">隔离垃圾: {{ stats.quarantine }}</span>
                  <span class="text-slate-700">|</span>
                  <span class="text-amber-300">今日请求: {{ (stats.today_requests || 0).toLocaleString() }} 次</span>
                  <span class="text-slate-700">|</span>
                  <span class="text-purple-300">{{ stats.db_engine }} ({{ stats.db_mode }}) [{{ stats.db_size }}]</span>
                </div>
              </div>
            </div>
            <div class="flex items-center gap-2.5 shrink-0 flex-wrap">
              <button @click="checkAllAccounts" :disabled="checkingAll" class="px-3.5 py-2 rounded-lg bg-amber-500/15 hover:bg-amber-500/25 text-amber-300 text-xs font-semibold border border-amber-500/40 transition-all flex items-center gap-1.5 shadow-lg shadow-amber-500/10 disabled:opacity-50">
                <AppIcon name="eye" :size="14" :class="{'animate-spin': checkingAll}" />
                <span>{{ checkingAll ? '全池探活中...' : '全池无痕探活' }}</span>
              </button>
              <button @click="openInboxModal" class="relative px-3.5 py-2 rounded-lg bg-slate-800/90 hover:bg-slate-700 text-slate-200 text-xs font-medium border border-slate-700 transition-all flex items-center gap-1.5 shadow-sm">
                <AppIcon name="mail" :size="14" class="text-amber-400" />
                <span>风控收件箱</span>
                <span v-if="unreadAlertsCount > 0" class="ml-1 px-1.5 py-0.2 rounded-full text-[10px] font-bold font-mono bg-red-500 text-white animate-pulse">
                  {{ unreadAlertsCount > 99 ? '99+' : unreadAlertsCount }}
                </span>
              </button>
              <button @click="refreshAll" class="px-3 py-2 rounded-lg bg-slate-800/90 hover:bg-slate-700 text-slate-200 text-xs font-medium border border-slate-700 transition-all flex items-center gap-1.5">
                <AppIcon name="refresh" :size="14" />
                <span>刷新态势</span>
              </button>
              <router-link to="/dashboard" class="px-3 py-2 rounded-lg bg-slate-800/90 hover:bg-slate-700 text-slate-300 hover:text-emerald-400 text-xs border border-slate-700 transition-all flex items-center gap-1.5">
                <AppIcon name="dashboard" :size="14" />
                <span>客户工作台</span>
              </router-link>
              <button @click="doLogout" class="px-3 py-2 rounded-lg bg-red-500/10 hover:bg-red-500/20 text-red-400 border border-red-500/30 text-xs transition-all flex items-center gap-1.5">
                <AppIcon name="logout" :size="14" />
                <span>退出</span>
              </button>
            </div>
          </div>

          <!-- 2. 圣殿 6 维实时指标矩阵 (Stat Cards) -->
          <div class="grid grid-cols-2 lg:grid-cols-6 gap-4">
            <div class="glass-panel glass-card-hover p-4 rounded-xl border border-emerald-500/30 bg-emerald-500/5">
              <div class="text-xs text-slate-400 mb-1.5 flex items-center justify-between font-medium">
                <span>激活活跃池 (Active)</span>
                <AppIcon name="shieldCheck" :size="15" class="text-emerald-400" />
              </div>
              <div class="text-2xl font-bold font-mono text-emerald-400">{{ stats.valid }}</div>
              <div class="text-[11px] text-slate-500 mt-1 font-mono">前线就绪业务承载</div>
            </div>

            <div class="glass-panel glass-card-hover p-4 rounded-xl border border-cyan-500/30 bg-cyan-500/5">
              <div class="text-xs text-slate-400 mb-1.5 flex items-center justify-between font-medium">
                <span>零IP冷库 (Cold)</span>
                <AppIcon name="snowflake" :size="15" class="text-cyan-400" />
              </div>
              <div class="text-2xl font-bold font-mono text-cyan-300">{{ coldAccounts.length }}</div>
              <div class="text-[11px] text-slate-500 mt-1 font-mono flex items-center justify-between">
                <span>免Token静默储备</span>
                <button v-if="coldAccounts.length > 0" @click="openWakeModal" class="text-cyan-300 hover:underline font-semibold">一键唤醒</button>
              </div>
            </div>

            <div class="glass-panel glass-card-hover p-4 rounded-xl border border-yellow-500/30 bg-yellow-500/5">
              <div class="text-xs text-slate-400 mb-1.5 flex items-center justify-between font-medium">
                <span>有期禁言 (Dated)</span>
                <AppIcon name="clock" :size="15" class="text-yellow-400" />
              </div>
              <div class="text-2xl font-bold font-mono text-yellow-400">{{ stats.dated }}</div>
              <div class="text-[11px] text-slate-500 mt-1 font-mono">到期自动探活归还原主</div>
            </div>

            <div class="glass-panel glass-card-hover p-4 rounded-xl border border-red-500/30 bg-red-500/5">
              <div class="text-xs text-slate-400 mb-1.5 flex items-center justify-between font-medium">
                <span>隔离垃圾池 (Quarantine)</span>
                <AppIcon name="shieldAlert" :size="15" class="text-red-400" />
              </div>
              <div class="text-2xl font-bold font-mono text-red-400">{{ stats.quarantine }}</div>
              <div class="text-[11px] text-slate-500 mt-1 font-mono">封禁/失效已剥离归属</div>
            </div>

            <div class="glass-panel glass-card-hover p-4 rounded-xl border border-amber-500/30 bg-amber-500/5">
              <div class="text-xs text-slate-400 mb-1.5 flex items-center justify-between font-medium">
                <span>全网总账号 (Total)</span>
                <AppIcon name="server" :size="15" class="text-amber-400" />
              </div>
              <div class="text-2xl font-bold font-mono text-amber-300">{{ stats.accounts }}</div>
              <div class="text-[11px] text-slate-500 mt-1 font-mono">今日调度 {{ (stats.today_requests || 0).toLocaleString() }} 次</div>
            </div>

            <div class="glass-panel glass-card-hover p-4 rounded-xl border border-purple-500/30 bg-purple-500/5">
              <div class="text-xs text-slate-400 mb-1.5 flex items-center justify-between font-medium">
                <span>数据引擎 ({{ stats.db_engine }})</span>
                <AppIcon name="database" :size="15" class="text-purple-400" />
              </div>
              <div class="text-xl font-bold font-mono text-purple-300 mt-0.5">{{ stats.db_mode }} 模式</div>
              <div class="text-[11px] text-slate-400 mt-1 font-mono">{{ stats.db_size }} · 单轨活跃</div>
            </div>
          </div>

          <!-- 3. 7 大主控子路由导航栏 (纯 SVG 图标) -->
          <div class="flex items-center gap-1.5 border-b border-amber-500/30 bg-slate-900/85 p-1.5 rounded-t-xl overflow-x-auto select-none border-t border-x shadow-md">
            <button v-for="t in tabs" :key="t.id" @click="switchTab(t.id)" :class="activeTab === t.id ? 'bg-amber-500/20 text-amber-300 border-amber-500/50 shadow-md font-semibold' : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/40 border-transparent'" class="px-4 py-2.5 rounded-lg text-xs font-medium border transition-all flex items-center gap-2 whitespace-nowrap">
              <AppIcon :name="t.icon" :size="15" />
              <span>{{ t.label }}</span>
              <span v-if="t.id === 'accounts' && stats.quarantine > 0" class="w-2 h-2 rounded-full bg-red-400 animate-pulse"></span>
              <span v-if="t.id === 'register' && (isRegisterRunning || wakeStatus.running)" class="w-2 h-2 rounded-full bg-amber-400 animate-pulse"></span>
            </button>
          </div>

          <!-- 4. 主控面板容器 (Tab Panels) -->
          <div class="glass-panel rounded-b-xl border border-amber-500/20 border-t-0 p-3 sm:p-5 md:p-6 -mt-6 bg-[#080d1a]/95 shadow-2xl min-h-[540px] overflow-hidden">
            
            <!-- ═════════ Tab 1: 圣殿号池 (/admin/accounts) ═════════ -->
            <div v-if="activeTab === 'accounts'" class="space-y-4">
              <!-- 冷库平缓唤醒实时进度横幅 (完整移植老面板 wake_status 轮询特性) -->
              <div v-if="wakeStatus.running || (wakeStatus.total > 0 && wakeStatus.current > 0)" class="p-4 rounded-xl bg-cyan-500/10 border border-cyan-500/35 text-xs space-y-2">
                <div class="flex items-center justify-between">
                  <div class="font-bold text-cyan-300 flex items-center gap-2">
                    <AppIcon name="snowflake" :size="15" :class="{'animate-spin': wakeStatus.running}" />
                    <span>{{ wakeStatus.running ? '冷库极速唤醒进行中 (纯 HTTP 并发无感刷新)' : '最近一次冷库唤醒任务已完成' }}</span>
                  </div>
                  <span class="font-mono text-cyan-300 font-bold">
                    进度: {{ wakeStatus.current }} / {{ wakeStatus.total }} (成功 {{ wakeStatus.success_count || 0 }} · 失败 {{ wakeStatus.fail_count || 0 }})
                  </span>
                </div>
                <div class="w-full h-2 rounded-full bg-slate-900 overflow-hidden">
                  <div class="h-full bg-gradient-to-r from-cyan-500 to-emerald-400 transition-all duration-500" :style="{ width: (wakeStatus.total ? Math.round((wakeStatus.current / wakeStatus.total) * 100) : 0) + '%' }"></div>
                </div>
                <div v-if="wakeStatus.last_account" class="text-[11px] font-mono text-slate-400">
                  当前处理账号: <span class="text-slate-200">{{ wakeStatus.last_account }}</span>
                </div>
              </div>


              <!-- ★ 免码重登健康态横幅：明确区分【过热(IP频控)】/【需重新登记设备】/【封号】 -->
              <div v-if="reloginHealth && reloginHealth.ip && reloginHealth.ip.restricted"
                   class="p-4 rounded-xl bg-amber-500/10 border border-amber-500/40 text-xs space-y-1.5">
                <div class="font-bold text-amber-300 flex items-center gap-2">
                  <span>🔥</span>
                  <span>上游频控限制（40029）— 账号凭据本身有效，可切换代理出口后重试</span>
                </div>
                <div class="text-slate-300 leading-relaxed">
                  冷却剩余
                  <span class="font-mono text-amber-300 font-bold">{{ formatRemainingSeconds(reloginHealth.ip.remaining_s) }}</span>。
                  若触发上游频控限制，请切换代理出口或稍后再发起纯 HTTP 续杯，<b>无需删号</b>。
                  当前无感刷新由纯 HTTP iOS 原生协议驱动，彻底免疫数美 b11 设备风控，不再受本地浏览器节流锁限制。
                </div>
              </div>

              <!-- 工具栏与统计 -->
              <div class="flex flex-col lg:flex-row lg:items-center justify-between gap-4">
                <h2 class="text-base font-bold text-amber-300 flex items-center gap-2 font-sacred tracking-wide">
                  <AppIcon name="server" :size="18" />
                  <span>圣殿受戒账号矩阵 ({{ accounts.length }})</span>
                </h2>
                <div class="flex items-center gap-2 flex-wrap">
                  <button v-if="coldAccounts.length > 0" @click="openWakeModal" class="px-3 py-1.5 rounded-lg bg-cyan-500/20 hover:bg-cyan-500/30 text-cyan-300 border border-cyan-500/40 text-xs font-bold flex items-center gap-1.5 shadow-md shadow-cyan-500/10">
                    <AppIcon name="snowflake" :size="13" />
                    <span>⚡ 一键唤醒冷库 ({{ coldAccounts.length }})</span>
                  </button>

                  <button v-if="quarantineAccounts.length > 0" @click="clearQuarantine" class="px-3 py-1.5 rounded-lg bg-red-500/15 hover:bg-red-500/25 text-red-300 border border-red-500/40 text-xs font-semibold flex items-center gap-1.5">
                    <AppIcon name="trash" :size="13" />
                    <span>一键清空垃圾库 ({{ quarantineAccounts.length }})</span>
                  </button>
                  <button @click="reloginAllAccounts" class="px-3 py-1.5 rounded-lg bg-blue-500/15 hover:bg-blue-500/25 text-blue-300 border border-blue-500/40 text-xs font-medium flex items-center gap-1.5">
                    <AppIcon name="refresh" :size="13" />
                    <span>批量极速续杯</span>
                  </button>
                  <button @click="checkAllAccounts" :disabled="checkingAll" class="px-3 py-1.5 rounded-lg bg-amber-500 hover:bg-amber-400 text-slate-950 text-xs font-semibold flex items-center gap-1.5 shadow disabled:opacity-50">
                    <AppIcon name="bolt" :size="13" />
                    <span>一键全池探活</span>
                  </button>
                  <button @click="loadAccounts" class="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 text-xs flex items-center gap-1.5">
                    <AppIcon name="refresh" :size="13" />
                    <span>刷新矩阵</span>
                  </button>
                </div>
              </div>

              <!-- 4 类池子快速筛选 + 搜索框 -->
              <div class="flex flex-col md:flex-row md:items-center justify-between gap-3">
                <div class="flex items-center gap-2 flex-wrap">
                  <button @click="accFilter = 'all'" :class="accFilter === 'all' ? 'bg-amber-500/20 text-amber-300 border-amber-500/40 font-semibold' : 'bg-slate-900 border-slate-800 text-slate-400 hover:text-slate-200'" class="px-3 py-1.5 rounded-lg text-xs font-medium border transition-all">
                    全部矩阵 ({{ accounts.length }})
                  </button>
                  <button @click="accFilter = 'active'" :class="accFilter === 'active' ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40 font-semibold' : 'bg-slate-900 border-slate-800 text-slate-400 hover:text-slate-200'" class="px-3 py-1.5 rounded-lg text-xs font-medium border transition-all flex items-center gap-1.5">
                    <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
                    <span>激活活跃池 ({{ activeAccounts.length }})</span>
                  </button>
                  <button @click="accFilter = 'cold'" :class="accFilter === 'cold' ? 'bg-cyan-500/20 text-cyan-300 border-cyan-500/40 font-semibold' : 'bg-slate-900 border-slate-800 text-slate-400 hover:text-slate-200'" class="px-3 py-1.5 rounded-lg text-xs font-medium border transition-all flex items-center gap-1.5">
                    <span class="w-2 h-2 rounded-full bg-cyan-400"></span>
                    <span>🧊 零IP冷库 ({{ coldAccounts.length }})</span>
                  </button>

                  <button @click="accFilter = 'quarantine'" :class="accFilter === 'quarantine' ? 'bg-red-500/20 text-red-300 border-red-500/40 font-semibold' : 'bg-slate-900 border-slate-800 text-slate-400 hover:text-slate-200'" class="px-3 py-1.5 rounded-lg text-xs font-medium border transition-all flex items-center gap-1.5">
                    <span class="w-2 h-2 rounded-full bg-red-400"></span>
                    <span>封号垃圾池 ({{ quarantineAccounts.length }})</span>
                  </button>
                </div>
                <div class="relative max-w-xs w-full">
                  <input v-model="accSearch" type="text" placeholder="搜索账号 / 邮箱 / 归属人..." class="w-full px-3 py-1.5 rounded-lg bg-slate-900/90 border border-slate-700 text-xs text-slate-200 placeholder-slate-500 font-mono focus:border-amber-500 focus:outline-none" />
                </div>
              </div>

              <!-- 账号池高密度大表格 -->
              <div class="rounded-xl border border-slate-800 overflow-x-auto bg-slate-950/50">
                <table class="w-full text-left text-xs font-mono">
                  <thead class="bg-slate-900/80 border-b border-slate-800 text-slate-400 font-sans">
                    <tr>
                      <th class="py-3 px-4">账号标识 (Credential)</th>
                      <th class="py-3 px-4">归属 (Owner)</th>
                      <th class="py-3 px-4">号池分级状态</th>
                      <th class="py-3 px-4">受信任态 / 续杯倒计时</th>
                      <th class="py-3 px-4">解禁时间</th>
                      <th class="py-3 px-4">运行附言</th>
                      <th class="py-3 px-4 text-right">圣所操作</th>
                    </tr>
                  </thead>
                  <tbody class="divide-y divide-slate-800/60 text-slate-200">
                    <tr v-for="r in filteredAccounts" :key="r.account || r.account_label" class="hover:bg-slate-800/30 transition-colors">
                      <td class="py-3 px-4 font-semibold text-slate-100 select-all font-mono">
                        <div class="flex items-center gap-1.5 flex-wrap">
                          <span>{{ r.account || r.account_label }}</span>
                          <span v-if="r.device_trusted && r.expire_at_fmt" class="px-1.5 py-0.5 rounded text-[10px] font-mono bg-emerald-500/15 text-emerald-300 border border-emerald-500/30">🛡️ 设备受信任 (180天)</span>
                          <span v-else class="px-1.5 py-0.5 rounded text-[10px] font-mono bg-slate-700/50 text-slate-400 border border-slate-600/30">⚠️ 未受信任设备</span>
                          <span v-if="r.is_disposable" class="px-1.5 py-0.5 rounded text-[10px] font-mono bg-amber-500/15 text-amber-300 border border-amber-500/30">⚡ 一次性耗材</span>
                          <span v-else-if="r.is_renewable" class="px-1.5 py-0.5 rounded text-[10px] font-mono bg-cyan-500/15 text-cyan-300 border border-cyan-500/30">🔄 可无限续杯</span>
                        </div>
                        <div v-if="r.token" class="flex items-center gap-1.5 mt-1 text-[10px] text-purple-300 font-mono">
                          <span class="bg-purple-500/10 px-1.5 py-0.5 rounded border border-purple-500/20 truncate max-w-[150px]" :title="r.token">
                            Token: {{ r.token.substring(0, 8) }}...{{ r.token.substring(r.token.length - 6) }}
                          </span>
                          <button @click.stop="copyText(r.token, 'DeepSeek Token')" class="text-emerald-400 hover:text-emerald-300 underline text-[10px] font-sans font-medium">复制Token</button>
                        </div>
                      </td>
                      <td class="py-3 px-4">
                        <span v-if="r.owner" class="owner-chip">{{ r.owner }}</span>
                        <span v-else class="px-2 py-0.5 rounded text-[11px] bg-blue-500/10 text-blue-400 border border-blue-500/20 font-sans">公共池</span>
                      </td>
                      <td class="py-3 px-4">
                        <span :class="getStatusBadgeClass(r)" class="px-2 py-0.5 rounded text-[10px] border font-sans font-medium inline-flex items-center gap-1.5 w-fit">
                          <span class="w-1.5 h-1.5 rounded-full bg-current"></span>
                          <span>{{ getStatusText(r) }}</span>
                        </span>
                        <span v-if="r.relogin_kind && r.relogin_kind !== 'OK'"
                              :class="reloginBadgeClass(r)"
                              class="ml-1.5 px-2 py-0.5 rounded text-[10px] border font-sans font-medium inline-flex items-center gap-1 w-fit"
                              :title="(r.relogin_desc || '') + (r.relogin_error ? '\n' + r.relogin_error : '')">
                          {{ r.relogin_label }}
                        </span>
                      </td>
                      <td class="py-3 px-4 text-slate-300 text-[11px] font-mono">
                        <div v-if="r.is_renewable && r.renew_countdown_fmt" class="space-y-0.5">
                          <div class="text-cyan-400 flex items-center gap-1">
                            <span>⏳</span>
                            <span>倒计时: {{ r.renew_countdown_fmt }}</span>
                          </div>
                          <div v-if="r.expire_at_fmt" class="text-[10px] text-slate-500" :title="'受信任设备有效期截止: ' + r.expire_at_fmt">
                            设备到期: {{ r.expire_at_fmt.split(' ')[0] }}
                          </div>
                        </div>
                        <div v-else-if="r.is_disposable" class="text-amber-400/80 text-[10px]">
                          已锁止自动续杯 (一次性)
                        </div>
                        <div v-else class="text-slate-500 text-[10px]">
                          不可自动刷新
                        </div>
                      </td>
                      <td class="py-3 px-4 text-slate-400 text-[11px]">{{ r.mute_until || '-' }}</td>
                      <td class="py-3 px-4 text-slate-400 text-[11px] max-w-xs truncate" :title="r.error || (r.warmup && r.warmup.message) || ''">
                        {{ (r.warmup && r.warmup.message && r.status === 'WARMING') ? r.warmup.message : (r.error || (r.status === 'INACTIVE_COLD' ? '零 IP 接触纯净冷库储备' : '正常守候中')) }}
                      </td>
                      <td class="py-3 px-4 text-right space-x-1.5 font-sans whitespace-nowrap">
                        <template v-if="r.status === 'INACTIVE_COLD'">
                          <button @click="activateAccount(r.account || r.account_label)" class="px-2.5 py-1 rounded bg-emerald-500/20 text-emerald-300 hover:bg-emerald-500/30 border border-emerald-500/40 text-[11px]">
                            ⚡ 唤醒入池
                          </button>
                          <button @click="deleteAccount(r.account || r.account_label)" class="px-2.5 py-1 rounded bg-red-500/10 text-red-400 hover:bg-red-500/20 border border-red-500/20 text-[11px]">
                            移除
                          </button>
                        </template>

                        <template v-else-if="(r.status && r.status.includes('QUARANTINE')) || r.status === 'MUTED' || r.muted">
                          <button @click="reloginAccount(r.account || r.account_label)" class="px-2.5 py-1 rounded bg-blue-500/20 text-blue-300 hover:bg-blue-500/30 border border-blue-500/40 text-[11px]">
                            ⚡ HTTP 刷新
                          </button>
                          <button @click="rehabAccount(r.account || r.account_label)" class="px-2.5 py-1 rounded bg-cyan-500/20 text-cyan-300 hover:bg-cyan-500/30 border border-cyan-500/40 text-[11px]">
                            🧊 解隔回冷库
                          </button>
                          <button @click="deleteAccount(r.account || r.account_label)" class="px-2.5 py-1 rounded bg-red-500/20 text-red-400 hover:bg-red-500/40 text-[11px]">
                            彻底销毁
                          </button>
                        </template>
                        <template v-else>
                          <button @click="probeAccount(r.account || r.account_label)" class="px-2 py-0.5 rounded text-cyan-400 hover:text-cyan-300 hover:bg-cyan-500/10 text-[11px]">
                            探活
                          </button>
                          <button v-if="r.has_trusted_mfa" @click="toggleDisposable(r)" :class="r.is_disposable ? 'text-amber-400 hover:text-amber-300 hover:bg-amber-500/10' : 'text-slate-400 hover:text-slate-300 hover:bg-slate-700/30'" class="px-2 py-0.5 rounded text-[11px]" :title="r.is_disposable ? '取消一次性标记，恢复自动续杯' : '标记为一次性账号，禁止后台自动续杯'">
                            {{ r.is_disposable ? '取消一次性' : '设为一次性' }}
                          </button>
                          <button v-if="r.is_renewable" @click="reloginAccount(r.account || r.account_label)" class="px-2 py-0.5 rounded text-amber-400 hover:text-amber-300 hover:bg-amber-500/10 text-[11px]" title="通过纯 HTTP iOS 原生协议极速续杯">
                            ⚡ 极速续杯
                          </button>
                          <button @click="warmupAccount(r.account || r.account_label)" class="px-2 py-0.5 rounded text-yellow-400 hover:text-yellow-300 hover:bg-yellow-500/10 text-[11px]">
                            养号
                          </button>
                          <button v-if="r.has_trusted_mfa" @click="demoteAccount(r.account || r.account_label)" class="px-2 py-0.5 rounded text-cyan-300 hover:text-cyan-200 hover:bg-cyan-500/10 text-[11px]" title="注销当前Token并退入零IP冷库储备">
                            🧊 退入冷库
                          </button>
                          <button @click="deleteAccount(r.account || r.account_label)" class="px-2 py-0.5 rounded text-red-400 hover:text-red-300 hover:bg-red-500/10 text-[11px]">
                            移除
                          </button>
                        </template>
                      </td>
                    </tr>
                    <tr v-if="filteredAccounts.length === 0">
                      <td colspan="6" class="py-12 text-center text-slate-500 font-sans">
                        当前筛选矩阵暂无账号数据。
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div class="text-[11px] text-slate-500 flex justify-between items-center font-mono">
                <span>* 探活使用 DeepSeek 官方 /users/current 协议零风控无痕检测，不向号池注入多余对话。</span>
                <span>最后刷新: {{ lastUpdated }}</span>
              </div>
            </div>

            <!-- ═════════ Tab 2: 祭坛调用统计 (/admin/usage) ═════════ -->
            <div v-if="activeTab === 'usage'" class="space-y-6">
              <div class="flex items-center justify-between">
                <h2 class="text-base font-bold text-amber-300 flex items-center gap-2 font-sacred">
                  <AppIcon name="chart" :size="18" />
                  <span>祭坛调用监控与会话树遥测统计</span>
                </h2>
                <button @click="loadUsage" class="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs border border-slate-700 flex items-center gap-1.5">
                  <AppIcon name="refresh" :size="13" />
                  <span>刷新记录</span>
                </button>
              </div>

              <!-- 5 大关键指标卡 -->
              <div class="grid grid-cols-2 md:grid-cols-5 gap-4">
                <div class="p-4 rounded-xl bg-slate-900/60 border border-slate-800">
                  <div class="text-xs text-slate-400 mb-1">累计请求数</div>
                  <div class="text-2xl font-bold font-mono text-amber-300">{{ (usageSummary.total_requests || 0).toLocaleString() }}</div>
                </div>
                <div class="p-4 rounded-xl bg-slate-900/60 border border-slate-800">
                  <div class="text-xs text-slate-400 mb-1">调用成功率</div>
                  <div class="text-2xl font-bold font-mono text-emerald-400">{{ usageSummary.success_rate ?? 100 }}%</div>
                </div>
                <div class="p-4 rounded-xl bg-slate-900/60 border border-slate-800">
                  <div class="text-xs text-slate-400 mb-1">上下文总吞吐</div>
                  <div class="text-2xl font-bold font-mono text-cyan-400">{{ (usageSummary.total_tokens || 0).toLocaleString() }}</div>
                </div>
                <div class="p-4 rounded-xl bg-slate-900/60 border border-slate-800">
                  <div class="text-xs text-slate-400 mb-1">缓存命中率</div>
                  <div class="text-2xl font-bold font-mono text-emerald-400">{{ usageSummary.cache_hit_rate || 0 }}%</div>
                </div>
                <div class="p-4 rounded-xl bg-slate-900/60 border border-slate-800">
                  <div class="text-xs text-slate-400 mb-1">平均推理延迟</div>
                  <div class="text-2xl font-bold font-mono text-amber-400">{{ usageSummary.avg_duration_ms || 0 }} ms</div>
                </div>
              </div>

              <!-- 会话树分叉与被动防风控遥测条 -->
              <div class="p-3.5 rounded-xl bg-slate-900/80 border border-emerald-500/25 flex items-center justify-between flex-wrap gap-3 text-xs font-mono">
                <div class="flex items-center gap-2 text-emerald-400 font-sans font-semibold">
                  <AppIcon name="branch" :size="15" />
                  <span>多分支会话树 (DAG) 与粘性调度遥测</span>
                </div>
                <div class="flex items-center gap-5 flex-wrap text-slate-300">
                  <span>会话复用命中: <b class="text-emerald-400">{{ usageSummary.counters?.['session.reused'] || 0 }}</b></span>
                  <span>树状分叉回溯 (LCA): <b class="text-cyan-400">{{ usageSummary.counters?.['session.branched'] || 0 }}</b></span>
                  <span>上下文压缩换会话: <b class="text-amber-300">{{ usageSummary.counters?.['session.prefix_rebuild'] || 0 }}</b></span>
                  <span>工具流式输出: <b class="text-purple-300">{{ usageSummary.counters?.['tool.emitted'] || 0 }}</b></span>
                </div>
              </div>

              <!-- 按圣钥用量与配额表 -->
              <div class="space-y-3">
                <div class="flex items-center justify-between">
                  <h3 class="text-sm font-semibold text-amber-300 flex items-center gap-2 font-sacred">
                    <AppIcon name="key" :size="15" />
                    <span>活跃圣钥用量与配额明细 ({{ byKeyList.length }})</span>
                  </h3>
                  <button v-if="revokedByKeyList.length > 0" @click="pruneRevokedUsage" class="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 text-[11px] flex items-center gap-1">
                    <AppIcon name="trash" :size="12" />
                    <span>清理已作废圣钥历史 ({{ revokedByKeyList.length }})</span>
                  </button>
                </div>
                <div class="rounded-xl border border-slate-800 overflow-x-auto bg-slate-950/50">
                  <table class="w-full text-left text-xs font-mono">
                    <thead class="bg-slate-900/80 border-b border-slate-800 text-slate-400 font-sans">
                      <tr>
                        <th class="py-3 px-4">归属人 / 圣钥</th>
                        <th class="py-3 px-4">今日请求</th>
                        <th class="py-3 px-4">今日 Tokens</th>
                        <th class="py-3 px-4">今日错误</th>
                        <th class="py-3 px-4">配额拦截</th>
                        <th class="py-3 px-4">每日配额</th>
                        <th class="py-3 px-4">配额使用率</th>
                        <th class="py-3 px-4">并发上限</th>
                        <th class="py-3 px-4">累计用量</th>
                        <th class="py-3 px-4 text-right">操作</th>
                      </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-800/60 text-slate-200">
                      <tr v-for="r in byKeyList" :key="r.id" class="hover:bg-slate-800/30">
                        <td class="py-3 px-4 font-sans">
                          <span v-if="r.owner" class="owner-chip mr-1.5">{{ r.owner }}</span>
                          <span class="font-semibold text-slate-100">{{ r.name }}</span>
                          <span v-if="r.enabled === false" class="ml-1.5 px-1.5 py-0.5 rounded text-[10px] bg-amber-500/10 text-amber-400 border border-amber-500/20">已暂停</span>
                        </td>
                        <td class="py-3 px-4 font-bold text-amber-300">{{ (r.today?.requests || 0).toLocaleString() }}</td>
                        <td class="py-3 px-4 text-slate-300">
                          {{ (r.today?.total_tokens || 0).toLocaleString() }}
                          <span v-if="r.today?.cached_tokens" class="text-emerald-400 text-[10px] ml-1">缓存 {{ r.today?.cache_hit_rate }}%</span>
                        </td>
                        <td class="py-3 px-4" :class="r.today?.errors ? 'text-red-400 font-bold' : 'text-slate-500'">{{ r.today?.errors || 0 }}</td>
                        <td class="py-3 px-4" :class="r.today?.rejected_by_quota ? 'text-amber-400 font-bold' : 'text-slate-500'">{{ r.today?.rejected_by_quota || 0 }}</td>
                        <td class="py-3 px-4 text-slate-300">
                          {{ (r.quota?.daily_requests || r.quota?.daily_tokens) ? (r.quota?.daily_requests ? r.quota.daily_requests + '次' : '不限') + ' / ' + (r.quota?.daily_tokens ? (r.quota.daily_tokens).toLocaleString() + ' tok' : '不限') : '不限' }}
                        </td>
                        <td class="py-3 px-4">
                          <div v-if="r.quota?.used_percent_requests !== null || r.quota?.used_percent_tokens !== null" class="flex items-center gap-2">
                            <div class="w-16 h-1.5 rounded-full bg-slate-800 overflow-hidden">
                              <div :style="{ width: Math.max(r.quota?.used_percent_requests || 0, r.quota?.used_percent_tokens || 0) + '%' }" :class="(Math.max(r.quota?.used_percent_requests || 0, r.quota?.used_percent_tokens || 0) >= 90) ? 'bg-red-400' : 'bg-emerald-400'" class="h-full"></div>
                            </div>
                            <span class="text-[11px]">{{ Math.max(r.quota?.used_percent_requests || 0, r.quota?.used_percent_tokens || 0) }}%</span>
                          </div>
                          <span v-else class="text-slate-500">—</span>
                        </td>
                        <td class="py-3 px-4 text-slate-400 font-sans">
                          {{ (r.account_labels && r.account_labels.length) ? (r.max_concurrent_streams + ' 路') : '公共池' }}
                        </td>
                        <td class="py-3 px-4 text-slate-400 text-[11px]">
                          {{ (r.lifetime?.requests || 0).toLocaleString() }}次 / {{ (r.lifetime?.total_tokens || 0).toLocaleString() }}tok
                        </td>
                        <td class="py-3 px-4 text-right space-x-1.5 font-sans whitespace-nowrap">
                          <button @click="openQuotaModal(r)" class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 hover:bg-emerald-500/30 border border-emerald-500/30 text-[11px]">
                            配额
                          </button>
                          <button @click="resetKeyUsage(r.id, r.owner || r.name)" class="px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 text-[11px]">
                            清零今日
                          </button>
                        </td>
                      </tr>
                      <tr v-if="byKeyList.length === 0">
                        <td colspan="10" class="py-8 text-center text-slate-500 font-sans">暂无圣钥用量记录。</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>

              <!-- 最近调用流水记录表 -->
              <div class="space-y-3">
                <h3 class="text-sm font-semibold text-amber-300 flex items-center gap-2 font-sacred">
                  <AppIcon name="activity" :size="15" />
                  <span>最近调用流水记录 (最新 {{ recentRecords.length }} 条)</span>
                </h3>
                <div class="rounded-xl border border-slate-800 overflow-x-auto bg-slate-950/50">
                  <table class="w-full text-left text-xs font-mono">
                    <thead class="bg-slate-900/80 border-b border-slate-800 text-slate-400 font-sans">
                      <tr>
                        <th class="py-3 px-4">时间</th>
                        <th class="py-3 px-4">圣钥</th>
                        <th class="py-3 px-4">协议端点</th>
                        <th class="py-3 px-4">模型</th>
                        <th class="py-3 px-4">状态码</th>
                        <th class="py-3 px-4">耗时</th>
                        <th class="py-3 px-4">Prompt</th>
                        <th class="py-3 px-4">Completion</th>
                        <th class="py-3 px-4">总 Tokens</th>
                        <th class="py-3 px-4">承载账号</th>
                      </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-800/60 text-slate-200">
                      <tr v-for="(rec, idx) in recentRecords" :key="idx" class="hover:bg-slate-800/30">
                        <td class="py-2.5 px-4 text-slate-400 text-[11px]">{{ rec.time_str }}</td>
                        <td class="py-2.5 px-4 text-amber-300 select-all">{{ rec.key || '-' }}</td>
                        <td class="py-2.5 px-4 text-slate-300">{{ rec.endpoint }}</td>
                        <td class="py-2.5 px-4 text-slate-300">{{ rec.model }}</td>
                        <td class="py-2.5 px-4">
                          <span :class="(rec.status_code >= 200 && rec.status_code < 400) ? 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20' : 'text-red-400 bg-red-500/10 border-red-500/20'" class="px-1.5 py-0.5 rounded text-[10px] border">
                            {{ rec.status_code }} {{ (rec.status_code >= 200 && rec.status_code < 400) ? 'OK' : 'ERR' }}
                          </span>
                        </td>
                        <td class="py-2.5 px-4 text-slate-300">{{ rec.duration_ms }} ms</td>
                        <td class="py-2.5 px-4 text-slate-400">{{ rec.prompt_tokens }}</td>
                        <td class="py-2.5 px-4 text-slate-400">{{ rec.completion_tokens }}</td>
                        <td class="py-2.5 px-4 font-bold text-amber-300">{{ rec.total_tokens }}</td>
                        <td class="py-2.5 px-4 text-slate-400 select-all text-[11px]">{{ rec.account }}</td>
                      </tr>
                      <tr v-if="recentRecords.length === 0">
                        <td colspan="10" class="py-8 text-center text-slate-500 font-sans">暂无最近调用流水。</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>
            </div>

            <!-- ═════════ Tab 3: 圣钥权限 (/admin/keys) ═════════ -->
            <div v-if="activeTab === 'apiKeys'" class="space-y-4">
              <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
                <h2 class="text-base font-bold text-amber-300 flex items-center gap-2 font-sacred">
                  <AppIcon name="key" :size="18" />
                  <span>圣钥权限凭据与独占归属管理 (API Keys)</span>
                </h2>
                <div class="flex items-center gap-2">
                  <button @click="openSensitiveModal" class="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 text-xs flex items-center gap-1.5">
                    <AppIcon name="shield" :size="13" />
                    <span>敏感词风控规则库</span>
                  </button>
                  <button @click="openCreateCardKeyModal" class="px-3.5 py-1.5 rounded-lg bg-gradient-to-r from-amber-500 to-amber-600 hover:from-amber-400 hover:to-amber-500 text-slate-950 font-bold text-xs flex items-center gap-1.5 shadow-lg shadow-amber-500/20">
                    <AppIcon name="receipt" :size="13" />
                    <span>➕ 铸造新卡密 (选号/绑号)</span>
                  </button>
                  <button @click="cleanExpiredCards" class="px-3 py-1.5 rounded-lg bg-amber-500/15 hover:bg-amber-500/25 text-amber-300 border border-amber-500/40 text-xs font-semibold flex items-center gap-1.5">
                    <AppIcon name="refresh" :size="13" />
                    <span>♻️ 回收清理到期卡密</span>
                  </button>
                  <button @click="openCreateKeyModal" class="px-3.5 py-1.5 rounded-lg bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-semibold text-xs flex items-center gap-1.5 shadow">
                    <AppIcon name="plus" :size="13" />
                    <span>铸造新圣钥</span>
                  </button>
                  <button @click="purgeAllRevokedKeys" class="px-3.5 py-1.5 rounded-lg bg-red-500/15 hover:bg-red-500/25 text-red-300 border border-red-500/40 text-xs font-semibold flex items-center gap-1.5 shadow">
                    <AppIcon name="trash" :size="13" />
                    <span>清理作废 Key</span>
                  </button>
                </div>
              </div>

              <!-- 号池归属态势横幅 -->
              <div v-if="ownershipSummary" class="p-4 rounded-xl bg-slate-900/80 border border-amber-500/20 text-xs space-y-1">
                <div class="font-bold text-amber-300 flex items-center gap-2">
                  <AppIcon name="layers" :size="14" />
                  <span>号池归属与隔离态势总览</span>
                </div>
                <div class="text-slate-300 flex items-center gap-3 flex-wrap font-mono pt-1">
                  <span>账号总数: <b class="text-slate-100">{{ ownershipSummary.total }}</b></span>
                  <span>•</span>
                  <span>已分配独占: <b class="text-emerald-400">{{ ownershipSummary.assigned }}</b></span>
                  <span>•</span>
                  <span>公共池待命: <b class="text-amber-400">{{ ownershipSummary.in_pool }}</b></span>
                  <span>•</span>
                  <span>有效圣钥: <b class="text-cyan-400">{{ ownershipSummary.keys }}</b></span>
                  <span>•</span>
                  <span>理论并发容量: <b class="text-purple-300">{{ ownershipSummary.concurrency_capacity }} 路</b></span>
                </div>
                <div class="text-slate-400 text-[11px] pt-1">
                  隔离契约：<b>有主之号只对所属圣钥开放</b>，其他 Key 绝不串用；当专属账号临时禁言并解封康复时，软挂起租约将<b>自动归还原主 Key</b>。
                </div>
              </div>

              <!-- 核心分流控制子 Tab -->
              <div class="flex items-center gap-2 border-b border-slate-800 pb-2.5">
                <button
                  @click="keySubTab = 'cardKeys'"
                  :class="keySubTab === 'cardKeys' ? 'bg-amber-500/20 text-amber-300 border-amber-500/40 font-bold' : 'text-slate-400 hover:text-slate-200 border-transparent hover:bg-slate-800/40'"
                  class="px-4 py-2 rounded-lg text-xs border transition-all flex items-center gap-2"
                >
                  <AppIcon name="receipt" :size="14" />
                  <span>💳 卡密批发专区 (免账号 / 天卡周卡 / 到期自动释放)</span>
                  <span class="px-2 py-0.5 rounded-full bg-amber-500/20 text-amber-300 text-[10px] font-mono font-bold">{{ cardKeysList.length }}</span>
                </button>

                <button
                  @click="keySubTab = 'favorKeys'"
                  :class="keySubTab === 'favorKeys' ? 'bg-cyan-500/20 text-cyan-300 border-cyan-500/40 font-bold' : 'text-slate-400 hover:text-slate-200 border-transparent hover:bg-slate-800/40'"
                  class="px-4 py-2 rounded-lg text-xs border transition-all flex items-center gap-2"
                >
                  <AppIcon name="gift" :size="14" />
                  <span>🎁 人情用户专区 (送朋友 / 稳定测试 / 支持移入卡池库)</span>
                  <span class="px-2 py-0.5 rounded-full bg-cyan-500/20 text-cyan-300 text-[10px] font-mono font-bold">{{ favorKeysList.length }}</span>
                </button>

                <button
                  @click="keySubTab = 'userPools'"
                  :class="keySubTab === 'userPools' ? 'bg-purple-500/20 text-purple-300 border-purple-500/40 font-bold' : 'text-slate-400 hover:text-slate-200 border-transparent hover:bg-slate-800/40'"
                  class="px-4 py-2 rounded-lg text-xs border transition-all flex items-center gap-2"
                >
                  <AppIcon name="user" :size="14" />
                  <span>👤 注册会员专区 (C端自注册 / 套餐保供)</span>
                  <span class="px-2 py-0.5 rounded-full bg-purple-500/20 text-purple-300 text-[10px] font-mono font-bold">{{ userPoolsList.length }}</span>
                </button>
              </div>

                            <!-- 0. 卡密批发专区子视图 -->
              <div v-if="keySubTab === 'cardKeys'" class="rounded-xl border border-slate-800 overflow-x-auto bg-slate-950/50">
                <table class="w-full text-left text-xs font-mono">
                  <thead class="bg-slate-900/80 border-b border-slate-800 text-slate-400 font-sans">
                    <tr>
                      <th class="py-3 px-4">卡密名称 / 备注</th>
                      <th class="py-3 px-4">规格</th>
                      <th class="py-3 px-4">Key 密文</th>
                      <th class="py-3 px-4">名下独占账号 (选号结果)</th>
                      <th class="py-3 px-4">有效期与到期时间</th>
                      <th class="py-3 px-4">今日用量</th>
                      <th class="py-3 px-4">状态</th>
                      <th class="py-3 px-4 text-right">操作</th>
                    </tr>
                  </thead>
                  <tbody class="divide-y divide-slate-800/60 text-slate-200">
                    <tr v-for="k in cardKeysList" :key="k.id" class="hover:bg-slate-800/30">
                      <td class="py-3 px-4 font-sans font-bold text-amber-300">{{ k.name }}</td>
                      <td class="py-3 px-4 font-sans">
                        <span class="px-2 py-0.5 rounded text-[11px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30">
                          {{ k.card_category === 'daily' ? '天卡' : (k.card_category === 'weekly' ? '周卡' : (k.card_category === 'monthly' ? '月卡' : (k.card_category === 'forever' ? '永久' : '卡密'))) }}
                        </span>
                      </td>
                      <td class="py-3 px-4 text-cyan-300 font-mono select-all">{{ k.prefix }}</td>
                      <td class="py-3 px-4 font-sans">
                        <div v-if="k.account_labels && k.account_labels.length" class="space-y-1">
                          <span class="px-2 py-0.5 rounded text-[10px] bg-purple-500/20 text-purple-300 border border-purple-500/30 font-bold">
                            独占 {{ k.account_labels.length }} 个号
                          </span>
                          <div class="text-[11px] text-slate-300 truncate max-w-xs font-mono" :title="k.account_labels.join(', ')">
                            {{ k.account_labels.join(', ') }}
                          </div>
                        </div>
                        <span v-else class="px-2 py-0.5 rounded text-[10px] bg-blue-500/15 text-blue-300 border border-blue-500/30">
                          高速公共池 (未选号)
                        </span>
                      </td>
                      <td class="py-3 px-4 font-sans text-xs">
                        <div v-if="k.expires_at" :class="isKeyExpired(k.expires_at) ? 'text-red-400 font-bold' : 'text-emerald-300'" class="font-medium">
                          {{ formatCardExpiry(k.expires_at) }}
                        </div>
                        <div v-else class="text-slate-500">永久无限制</div>
                      </td>
                      <td class="py-3 px-4 text-slate-300">
                        <div v-if="keyUsageMap[k.id]">
                          {{ keyUsageMap[k.id].today?.requests || 0 }} 次 · {{ (keyUsageMap[k.id].today?.total_tokens || 0).toLocaleString() }} tok
                        </div>
                        <span v-else class="text-slate-500">—</span>
                      </td>
                      <td class="py-3 px-4 font-sans">
                        <span v-if="k.revoked" class="px-2 py-0.5 rounded text-[10px] bg-red-500/10 text-red-400 border border-red-500/30">已作废</span>
                        <span v-else-if="isKeyExpired(k.expires_at)" class="px-2 py-0.5 rounded text-[10px] bg-red-500/20 text-red-300 border border-red-500/40">已到期</span>
                        <span v-else-if="k.enabled === false" class="px-2 py-0.5 rounded text-[10px] bg-amber-500/10 text-amber-400 border border-amber-500/30">已暂停</span>
                        <span v-else class="px-2 py-0.5 rounded text-[10px] bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">有效赋权</span>
                      </td>
                      <td class="py-3 px-4 text-right space-x-1.5 font-sans whitespace-nowrap">
                        <button @click="openAssignModal(k)" class="px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 hover:bg-amber-500/30 border border-amber-500/30 text-[11px]" title="为该卡密选择或更换独占账号（选号/补号）">
                          🎯 选号/补号
                        </button>
                        <button v-if="k.account_labels && k.account_labels.length" @click="moveFavorKeyToPool(k)" class="px-2 py-0.5 rounded bg-cyan-500/20 text-cyan-300 hover:bg-cyan-500/30 border border-cyan-500/30 text-[11px]" title="将此卡密绑定的独占账号解绑并释放回公共卡池">
                          ♻️ 移入卡池库
                        </button>
                        <button @click="openQuotaModal(k)" class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 hover:bg-emerald-500/30 border border-emerald-500/30 text-[11px]">
                          配额
                        </button>
                        <button @click="toggleKeyEnabled(k)" class="px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 text-[11px]">
                          {{ k.enabled === false ? '启用' : '暂停' }}
                        </button>
                        <button @click="revokeKey(k)" class="px-2 py-0.5 rounded bg-red-500/10 text-red-400 hover:bg-red-500/20 border border-red-500/20 text-[11px]">
                          作废
                        </button>
                        <button @click="deleteKeyForever(k)" class="px-2 py-0.5 rounded bg-red-500/20 text-red-400 hover:bg-red-500/30 border border-red-500/35 text-[11px]">
                          删除
                        </button>
                      </td>
                    </tr>
                    <tr v-if="cardKeysList.length === 0">
                      <td colspan="8" class="py-8 text-center text-slate-500 font-sans">暂无卡密数据，可点击上方「➕ 铸造新卡密」快速选号生成。</td>
                    </tr>
                  </tbody>
                </table>
              </div>

<!-- 1. 用户个人池子视图 -->
              <div v-if="keySubTab === 'userPools'" class="rounded-xl border border-slate-800 overflow-x-auto bg-slate-950/50">
                <table class="w-full text-left text-xs font-mono">
                  <thead class="bg-slate-900/80 border-b border-slate-800 text-slate-400 font-sans">
                    <tr>
                      <th class="py-3 px-4">用户标识 / 邮箱</th>
                      <th class="py-3 px-4">商业特权等级</th>
                      <th class="py-3 px-4">名下共享 Key 列表</th>
                      <th class="py-3 px-4">个人专属号池 (Labels)</th>
                      <th class="py-3 px-4">保供状态</th>
                      <th class="py-3 px-4 text-right">操作</th>
                    </tr>
                  </thead>
                  <tbody class="divide-y divide-slate-800/60 text-slate-200">
                    <tr v-for="u in userPoolsList" :key="u.id" class="hover:bg-slate-800/30">
                      <td class="py-3 px-4 font-sans">
                        <div class="font-bold text-slate-100">{{ u.email }}</div>
                        <div v-if="u.username" class="text-[11px] text-amber-400 font-mono">@{{ u.username }}</div>
                        <div class="text-[10px] text-slate-500 font-mono">{{ u.id }}</div>
                      </td>
                      <td class="py-3 px-4 font-sans">
                        <span v-if="u.is_vip" class="px-2 py-0.5 rounded text-[11px] bg-amber-500/20 text-amber-300 border border-amber-500/40 font-bold inline-flex items-center gap-1">
                          <AppIcon name="crown" :size="11" />
                          <span>VIP 订阅用户</span>
                        </span>
                        <span v-else class="px-2 py-0.5 rounded text-[11px] bg-slate-800 text-slate-400 border border-slate-700">普通用户</span>
                        <div v-if="u.subscription" class="text-[10px] text-slate-400 mt-0.5 font-mono">
                          {{ u.subscription.plan_id }} · 到期 {{ new Date(u.subscription.period_end * 1000).toLocaleDateString() }}
                        </div>
                      </td>
                      <td class="py-3 px-4 max-w-xs font-sans">
                        <div v-if="u.keys && u.keys.length" class="space-y-1">
                          <div v-for="k in u.keys" :key="k.id" class="flex items-center gap-1.5 text-[11px]">
                            <span class="text-amber-300 font-mono">{{ k.prefix }}</span>
                            <span class="text-slate-300">{{ k.name }}</span>
                            <span class="text-[10px] px-1 py-0.2 rounded bg-slate-800 text-slate-400">共享此池</span>
                          </div>
                        </div>
                        <span v-else class="text-slate-500 text-[11px]">暂无 Key</span>
                      </td>
                      <td class="py-3 px-4 max-w-sm">
                        <div v-if="u.personal_pool && u.personal_pool.length" class="flex flex-wrap gap-1">
                          <span v-for="lbl in u.personal_pool" :key="lbl" class="acct-chip" :title="lbl">
                            {{ lbl.split('@')[0] }}
                          </span>
                        </div>
                        <span v-else class="px-2 py-0.5 rounded text-[11px] bg-blue-500/10 text-blue-400 border border-blue-500/20 font-sans">未分配专属号 (走公共池)</span>
                      </td>
                      <td class="py-3 px-4 font-sans">
                        <span v-if="u.auto_replenish" class="text-emerald-400 inline-flex items-center gap-1 text-[11px] font-bold">
                          <AppIcon name="bolt" :size="12" />
                          <span>自动保供中</span>
                        </span>
                        <span v-else class="text-slate-500 text-[11px]">未开启保供</span>
                        <div v-if="u.pool_health?.total" class="text-[10px] text-slate-400 font-mono">
                          健康: {{ u.pool_health.healthy }}/{{ u.pool_health.total }}
                        </div>
                      </td>
                      <td class="py-3 px-4 text-right space-x-1.5 font-sans whitespace-nowrap">
                        <button @click="openManageSubModal(u)" class="px-2.5 py-1 rounded bg-purple-500/20 text-purple-300 hover:bg-purple-500/30 border border-purple-500/30 text-[11px] font-semibold">
                          👑 操纵套餐与有效期
                        </button>
                        <button @click="autoAllocateUser(u)" class="px-2.5 py-1 rounded bg-cyan-500/20 text-cyan-300 hover:bg-cyan-500/30 border border-cyan-500/30 text-[11px] font-medium" title="智能优选分配专属保供号">
                          ⚡ 智能选号
                        </button>
                        <button @click="openEditUserPool(u)" class="px-2.5 py-1 rounded bg-amber-500/20 text-amber-300 hover:bg-amber-500/30 border border-amber-500/30 text-[11px] font-medium">
                          管理个人池
                        </button>
                        <button @click="triggerUserReplenish(u)" class="px-2.5 py-1 rounded bg-emerald-500/20 text-emerald-300 hover:bg-emerald-500/30 border border-emerald-500/30 text-[11px] font-medium">
                          ⚡ 热补位
                        </button>
                      </td>
                    </tr>
                    <tr v-if="userPoolsList.length === 0">
                      <td colspan="6" class="py-8 text-center text-slate-500 font-sans">暂无注册用户个人池数据。</td>
                    </tr>
                  </tbody>
                </table>
              </div>

              <!-- 2. 孤单人情 Key 列表大表 -->
              <div v-if="keySubTab === 'favorKeys'" class="rounded-xl border border-slate-800 overflow-x-auto bg-slate-950/50">
                <table class="w-full text-left text-xs font-mono">
                  <thead class="bg-slate-900/80 border-b border-slate-800 text-slate-400 font-sans">
                    <tr>
                      <th class="py-3 px-4">人情对象 (Owner)</th>
                      <th class="py-3 px-4">圣钥名称</th>
                      <th class="py-3 px-4">Key 前缀密文</th>
                      <th class="py-3 px-4">名下独占账号</th>
                      <th class="py-3 px-4">公共池兜底</th>
                      <th class="py-3 px-4">今日用量</th>
                      <th class="py-3 px-4">风控策略</th>
                      <th class="py-3 px-4">受权状态</th>
                      <th class="py-3 px-4">最近行使</th>
                      <th class="py-3 px-4 text-right">操作</th>
                    </tr>
                  </thead>
                  <tbody class="divide-y divide-slate-800/60 text-slate-200">
                    <tr v-for="k in favorKeysList" :key="k.id" class="hover:bg-slate-800/30">
                      <td class="py-3 px-4 font-sans">
                        <span v-if="k.owner" class="owner-chip">{{ k.owner }}</span>
                        <span v-else class="text-slate-500">未指定</span>
                      </td>
                      <td class="py-3 px-4 font-semibold text-slate-100 font-sans">{{ k.name }}</td>
                      <td class="py-3 px-4 text-amber-300 font-mono select-all">{{ k.prefix }}</td>
                      <td class="py-3 px-4 max-w-xs">
                        <template v-if="k.account_labels && k.account_labels.length">
                          <span v-for="lbl in k.account_labels" :key="lbl" class="acct-chip" :title="lbl">
                            {{ lbl.split('@')[0] }}
                          </span>
                        </template>
                        <span v-else class="px-2 py-0.5 rounded text-[11px] bg-blue-500/10 text-blue-400 border border-blue-500/20 font-sans">公共池</span>
                      </td>
                      <td class="py-3 px-4 font-sans">
                        <span v-if="k.account_labels && k.account_labels.length" @click="toggleKeyFallback(k)" :class="k.pool_fallback ? 'text-cyan-300 bg-cyan-500/10 border-cyan-500/30' : 'text-slate-400 bg-slate-800 border-slate-700'" class="px-2 py-0.5 rounded text-[10px] border cursor-pointer select-none" title="点击切换自有号满载时是否允许借用公共池">
                          {{ k.pool_fallback ? '可借公共池' : '严格独占' }}
                        </span>
                        <span v-else class="text-slate-500 text-[11px]">默认公共池</span>
                      </td>
                      <td class="py-3 px-4 text-slate-300">
                        <div v-if="keyUsageMap[k.id]">
                          {{ keyUsageMap[k.id].today?.requests || 0 }} 次 · {{ (keyUsageMap[k.id].today?.total_tokens || 0).toLocaleString() }} tok
                        </div>
                        <span v-else class="text-slate-500">—</span>
                      </td>
                      <td class="py-3 px-4 font-sans">
                        <span @click="toggleKeySensitive(k)" :class="(k.sensitive_filter !== false) ? 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30' : 'text-amber-400 bg-amber-500/10 border-amber-500/30'" class="px-2 py-0.5 rounded text-[10px] border cursor-pointer select-none inline-flex items-center gap-1">
                          <AppIcon :name="(k.sensitive_filter !== false) ? 'shieldCheck' : 'bolt'" :size="11" />
                          <span>{{ (k.sensitive_filter !== false) ? '严格拦截' : '豁免放行' }}</span>
                        </span>
                      </td>
                      <td class="py-3 px-4 font-sans">
                        <span v-if="k.revoked" class="px-2 py-0.5 rounded text-[10px] bg-red-500/10 text-red-400 border border-red-500/30">已作废</span>
                        <span v-else-if="k.enabled === false" class="px-2 py-0.5 rounded text-[10px] bg-amber-500/10 text-amber-400 border border-amber-500/30">已暂停</span>
                        <span v-else class="px-2 py-0.5 rounded text-[10px] bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">有效赋权</span>
                      </td>
                      <td class="py-3 px-4 text-slate-400 text-[11px]">
                        {{ k.last_used_at ? new Date(k.last_used_at * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '从未行使' }}
                      </td>
                      <td class="py-3 px-4 text-right space-x-1.5 font-sans whitespace-nowrap">
                        <button v-if="k.account_labels && k.account_labels.length" @click="moveFavorKeyToPool(k)" class="px-2 py-0.5 rounded bg-cyan-500/20 text-cyan-300 hover:bg-cyan-500/30 border border-cyan-500/30 text-[11px]" title="将此人情 Key 绑定的独占账号解绑并移入公共卡池">
                          ♻️ 移入卡池库
                        </button>
                        <button @click="openAssignModal(k)" class="px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 hover:bg-amber-500/30 border border-amber-500/30 text-[11px]">
                          分配账号
                        </button>
                        <button @click="openQuotaModal(k)" class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 hover:bg-emerald-500/30 border border-emerald-500/30 text-[11px]">
                          配额
                        </button>
                        <button @click="editOwnerPrompt(k)" class="px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 text-[11px]">
                          归属人
                        </button>
                        <button @click="toggleKeyEnabled(k)" class="px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 text-[11px]">
                          {{ k.enabled === false ? '启用' : '暂停' }}
                        </button>
                        <button @click="revokeKey(k)" class="px-2 py-0.5 rounded bg-red-500/10 text-red-400 hover:bg-red-500/20 border border-red-500/20 text-[11px]">
                          作废
                        </button>
                        <button @click="deleteKeyForever(k)" class="px-2 py-0.5 rounded bg-red-500/20 text-red-400 hover:bg-red-500/30 border border-red-500/35 text-[11px]" title="彻底物理删除此 Key">
                          彻底删除
                        </button>
                      </td>
                    </tr>
                    <tr v-if="activeKeys.length === 0">
                      <td colspan="10" class="py-8 text-center text-slate-500 font-sans">暂无圣钥数据。</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>

    <!-- 个人池账号分配与自动保供模态框 (全面采用 ACTIVE 账号勾选列表，彻底干掉 textarea) -->
    <div v-if="userPoolModal.show" class="fixed inset-0 bg-black/75 backdrop-blur-sm flex items-center justify-center p-4 z-50">
      <div class="glass-panel w-full max-w-2xl rounded-2xl p-6 border border-amber-500/30 shadow-2xl space-y-4 max-h-[90vh] flex flex-col">
        <div class="flex items-center justify-between border-b border-slate-800 pb-3">
          <div class="flex items-center gap-2 font-sacred text-amber-300 font-bold text-base">
            <AppIcon name="user" :size="18" />
            <span>配置用户个人专属号池 (选号 / 补号)</span>
          </div>
          <button @click="userPoolModal.show = false" class="text-slate-500 hover:text-slate-300">
            <AppIcon name="close" :size="16" />
          </button>
        </div>

        <div class="text-xs text-slate-300 space-y-1 bg-slate-900/80 p-3 rounded-xl border border-slate-800">
          <div class="flex items-center justify-between">
            <div>目标用户: <b class="text-amber-200">{{ userPoolModal.user?.email }}</b> <span v-if="userPoolModal.user?.username" class="text-slate-400 font-mono">(@{{ userPoolModal.user?.username }})</span></div>
            <div>商业等级: <span :class="userPoolModal.user?.is_vip ? 'text-amber-300 font-bold' : 'text-slate-400'">{{ userPoolModal.user?.is_vip ? '👑 VIP 订阅用户' : '普通用户' }}</span></div>
          </div>
          <div class="text-[11px] text-slate-400">⚡ 规则说明：勾选的账号将作为该用户的<b>专属个人池</b>；名下所有 API Key 自动共享该池。留空未选则走公共池。</div>
        </div>

        <!-- 搜索与快捷选号栏 -->
        <div class="space-y-2">
          <div class="flex items-center justify-between gap-3">
            <div class="relative flex-1">
              <input
                v-model="userPoolModal.searchQuery"
                type="text"
                placeholder="🔍 快速搜索邮箱或 Label..."
                class="w-full px-3 py-1.5 rounded-lg bg-slate-950 border border-slate-700 text-xs text-slate-100 placeholder-slate-500 focus:outline-none focus:border-amber-500 font-mono"
              />
            </div>
            <div class="flex items-center gap-2 text-xs">
              <button
                type="button"
                @click="quickSelectUserPool(1)"
                class="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-amber-300 border border-slate-700 text-[11px] font-semibold"
                title="自动挑选 1 个空闲可用 ACTIVE 账号"
              >
                +选1个
              </button>
              <button
                type="button"
                @click="quickSelectUserPool(2)"
                class="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-amber-300 border border-slate-700 text-[11px] font-semibold"
                title="自动挑选 2 个空闲可用 ACTIVE 账号"
              >
                +选2个
              </button>
              <button
                type="button"
                @click="userPoolModal.selectedLabels = []"
                class="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-400 border border-slate-700 text-[11px]"
              >
                清空选择
              </button>
            </div>
          </div>

          <div class="flex items-center justify-between text-[11px] px-1 text-slate-400">
            <label class="flex items-center gap-1.5 cursor-pointer select-none">
              <input type="checkbox" v-model="userPoolModal.onlyActive" class="rounded bg-slate-900 border-slate-700 text-amber-500 focus:ring-0 text-xs" />
              <span>只看有效 (ACTIVE) 号</span>
            </label>
            <div>
              已勾选: <b class="text-amber-400 font-mono text-xs">{{ (userPoolModal.selectedLabels || []).length }}</b> 个专属号
            </div>
          </div>
        </div>

        <!-- 账号勾选列表 (滚动区域) -->
        <div class="overflow-y-auto flex-1 pr-1 space-y-1.5 min-h-[200px] max-h-[340px] font-mono text-xs">
          <div
            v-for="acc in filteredUserPoolAccounts"
            :key="acc.account_label"
            @click="toggleUserPoolAccount(acc.account_label)"
            :class="[
              userPoolModal.selectedLabels.includes(acc.account_label)
                ? 'bg-amber-500/10 border-amber-500/40 text-amber-200'
                : 'bg-slate-900/50 border-slate-800/80 text-slate-300 hover:bg-slate-800/50',
              'p-2.5 rounded-xl border flex items-center justify-between cursor-pointer transition select-none'
            ]"
          >
            <div class="flex items-center gap-2.5 truncate">
              <input
                type="checkbox"
                :checked="userPoolModal.selectedLabels.includes(acc.account_label)"
                @click.stop
                @change="toggleUserPoolAccount(acc.account_label)"
                class="rounded bg-slate-950 border-slate-700 text-amber-500 focus:ring-0 cursor-pointer"
              />
              <span class="font-bold truncate" :title="acc.account_label">{{ acc.account_label }}</span>
            </div>
            <div class="flex items-center gap-2 shrink-0">
              <span v-if="acc.status === 'ACTIVE'" class="px-2 py-0.5 rounded text-[10px] bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">
                ACTIVE 就绪
              </span>
              <span v-else-if="acc.status === 'WARMING'" class="px-2 py-0.5 rounded text-[10px] bg-cyan-500/15 text-cyan-300 border border-cyan-500/30">
                预热中
              </span>
              <span v-else-if="acc.status === 'INACTIVE_COLD'" class="px-2 py-0.5 rounded text-[10px] bg-blue-500/15 text-blue-300 border border-blue-500/30">
                冷库
              </span>
              <span v-else class="px-2 py-0.5 rounded text-[10px] bg-red-500/15 text-red-400 border border-red-500/30">
                {{ acc.status || '异常' }}
              </span>
              <span v-if="acc.takenByOther" class="text-[10px] text-slate-500 max-w-[120px] truncate" :title="acc.owner">
                被占用
              </span>
            </div>
          </div>

          <div v-if="!filteredUserPoolAccounts || filteredUserPoolAccounts.length === 0" class="py-8 text-center text-slate-500 text-xs">
            没有符合条件的账号
          </div>
        </div>

        <div class="pt-2 border-t border-slate-800 flex items-center justify-between gap-4">
          <label class="flex items-center gap-2 cursor-pointer select-none">
            <input type="checkbox" id="userPoolAutoReplenish" v-model="userPoolModal.autoReplenish" class="rounded bg-slate-900 border-slate-700 text-amber-500 focus:ring-0">
            <span class="text-xs text-slate-300">
              开启封号自动保供 (掉号自动热替换)
            </span>
          </label>
          <div class="flex items-center gap-2 shrink-0">
            <button @click="userPoolModal.show = false" class="px-4 py-2 text-xs text-slate-400 hover:text-slate-200">取消</button>
            <button @click="saveUserPoolModal" class="px-5 py-2 bg-gradient-to-r from-amber-500 to-amber-600 hover:from-amber-400 hover:to-amber-500 text-slate-950 font-bold text-xs rounded-lg shadow-lg shadow-amber-500/20">保存并全量同步</button>
          </div>
        </div>
      </div>
    </div>

            <!-- ═════════ Tab 4: 运维监控与日志 (/admin/ops) ═════════ -->
            <div v-if="activeTab === 'opsLogs'" class="space-y-6">
              <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
                <div>
                  <h2 class="text-base font-bold text-amber-300 flex items-center gap-2 font-sacred">
                    <AppIcon name="terminal" :size="18" />
                    <span>圣殿运维枢纽 · 实时日志与溯源诊断</span>
                  </h2>
                  <p class="text-xs text-slate-400 mt-0.5">账号风控与禁言自动溯源 · 多出口住宅代理测速与公网 IP 嗅探 · Systemd 服务全功能日志流</p>
                </div>
                <div class="flex items-center gap-2">
                  <button @click="loadOpsLogs" class="px-3.5 py-1.5 rounded-lg bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-semibold text-xs flex items-center gap-1.5 shadow">
                    <AppIcon name="refresh" :size="13" />
                    <span>立即拉取最新日志</span>
                  </button>
                  <button @click="downloadServerOpsLogs" class="px-3 py-1.5 rounded-lg bg-amber-500/15 hover:bg-amber-500/25 text-amber-300 border border-amber-500/40 text-xs flex items-center gap-1.5 font-medium">
                    <AppIcon name="download" :size="13" />
                    <span>服务端全量导出 (.log)</span>
                  </button>
                  <button @click="exportOpsLogs" class="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 text-xs flex items-center gap-1.5">
                    <AppIcon name="save" :size="13" />
                    <span>保存当前视图</span>
                  </button>
                </div>
              </div>

              <!-- 诊断双网格 -->
              <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
                <!-- 账号风控与禁言诊断 -->
                <div class="glass-panel p-4 rounded-xl border border-red-500/30 bg-red-500/5 space-y-3">
                  <div class="flex items-center justify-between">
                    <div class="font-bold text-xs text-red-400 flex items-center gap-1.5">
                      <AppIcon name="shieldAlert" :size="15" />
                      <span>账号封禁/受限溯源诊断 (Risk Forensics)</span>
                    </div>
                    <button @click="loadForensics" class="text-[11px] text-slate-400 hover:text-slate-200 flex items-center gap-1">
                      <AppIcon name="refresh" :size="12" />
                      <span>刷新溯源</span>
                    </button>
                  </div>
                  <div class="space-y-2 text-xs">
                    <div v-if="forensics.muted_accounts && forensics.muted_accounts.length > 0" class="space-y-1.5 max-h-40 overflow-y-auto font-mono">
                      <div v-for="m in forensics.muted_accounts" :key="m.account" class="p-2.5 bg-slate-900/80 rounded border border-red-500/20 flex items-center justify-between gap-2">
                        <div>
                          <span class="font-bold text-red-300">{{ m.account }}</span>
                          <span class="text-slate-400 text-[11px] block">{{ m.status }} · 归属: {{ m.owner || '公共池' }} · 熔断 {{ m.mute_count || 1 }} 次</span>
                        </div>
                        <div class="text-right shrink-0">
                          <span class="text-amber-400 text-[11px] block">{{ m.mute_until }}</span>
                          <span class="text-[10px] text-slate-400">{{ formatRemainingSeconds(m.remaining_seconds, m.is_permanent) }}</span>
                        </div>
                      </div>
                    </div>
                    <div v-else class="text-emerald-400/90 bg-emerald-500/10 border border-emerald-500/20 rounded-lg py-4 px-3 text-center flex items-center justify-center gap-2">
                      <AppIcon name="shieldCheck" :size="16" />
                      <span>圣殿号池当前零封号风控，全池运行健康</span>
                    </div>

                    <div v-if="forensics.recent_events && forensics.recent_events.length > 0" class="pt-2 border-t border-slate-800/80">
                      <div class="text-[11px] text-slate-400 mb-1 font-semibold">近期关键风控与容灾事件溯源:</div>
                      <div class="bg-black/40 p-2 rounded max-h-24 overflow-y-auto font-mono text-[10px] text-amber-200/90 space-y-0.5">
                        <div v-for="(ev, idx) in forensics.recent_events" :key="idx">{{ ev }}</div>
                      </div>
                    </div>
                  </div>
                </div>

                <!-- 出口代理节点探针 -->
                <div class="glass-panel p-4 rounded-xl border border-amber-500/30 bg-amber-500/5 space-y-3">
                  <div class="flex items-center justify-between">
                    <div class="font-bold text-xs text-amber-400 flex items-center gap-1.5">
                      <AppIcon name="globe" :size="15" />
                      <span>多出口代理节点探针 (Egress Health)</span>
                    </div>
                    <button @click="fetchProxies" :disabled="testingProxies" class="text-[11px] text-amber-300 hover:text-amber-200 disabled:opacity-50 flex items-center gap-1">
                      <AppIcon name="activity" :size="12" />
                      <span>{{ testingProxies ? '测速探查中...' : '一键测速与探查' }}</span>
                    </button>
                  </div>
                  <div class="space-y-2 text-xs">
                    <div v-if="proxiesList.length > 0" class="space-y-1.5 max-h-48 overflow-y-auto font-mono">
                      <div v-for="(p, idx) in proxiesList" :key="idx" class="p-2.5 bg-slate-900/80 rounded border border-slate-800 flex items-center justify-between">
                        <div>
                          <span class="font-bold text-slate-200">{{ p.proxy }}</span>
                          <span class="text-slate-400 text-[11px] block">公网出网 IP: <b class="text-cyan-400">{{ p.egress_ip || p.ip || '未知' }}</b></span>
                        </div>
                        <div class="text-right shrink-0">
                          <span :class="(p.online || p.ok) ? 'text-emerald-400' : 'text-red-400'" class="font-bold">
                            {{ (p.online || p.ok) ? ((p.latency_ms ?? '-') + ' ms') : '连接异常' }}
                          </span>
                          <span class="block text-[10px] text-slate-500">HTTP {{ p.status_code || '-' }}</span>
                        </div>
                      </div>
                    </div>
                    <div v-else class="text-slate-500 text-center py-6">
                      点击右上角「一键测速与探查」，实时嗅探多出口代理连通性与公网 IP。
                    </div>
                  </div>
                </div>
              </div>

              <!-- 终端日志控制台 (完整移植老版日期筛选、日志级别、快捷 Pill 过滤与服务端下载) -->
              <div class="glass-panel rounded-xl border border-slate-800 overflow-hidden space-y-2">
                <div class="bg-slate-900/95 px-4 py-3 border-b border-slate-800 space-y-2.5 text-xs">
                  <div class="flex flex-wrap items-center justify-between gap-3">
                    <div class="flex items-center gap-3 flex-wrap">
                      <div class="flex items-center gap-1.5">
                        <span class="text-slate-400 font-semibold">服务单元:</span>
                        <select v-model="opsService" @change="loadOpsLogs" class="bg-slate-950 border border-slate-700 rounded px-2 py-1 text-slate-200 font-mono">
                          <option value="ds-gateway">ds-gateway.service (核心网关)</option>
                          <option value="clash">clash.service (Mihomo 代理内核)</option>
                        </select>
                      </div>

                      <div class="flex items-center gap-1.5">
                        <span class="text-slate-400 font-semibold">指定日期:</span>
                        <input type="date" v-model="opsDate" @change="loadOpsLogs" class="bg-slate-950 border border-slate-700 rounded px-2 py-1 text-slate-200 font-mono text-[11px]" />
                        <div class="inline-flex gap-1">
                          <button type="button" @click="setOpsQuickDate('today')" class="px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 text-[11px]">今天</button>
                          <button type="button" @click="setOpsQuickDate('yesterday')" class="px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 text-[11px]">昨天</button>
                          <button type="button" @click="setOpsQuickDate('realtime')" class="px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-amber-300 text-[11px]">实时</button>
                        </div>
                      </div>

                      <div class="flex items-center gap-1.5">
                        <span class="text-slate-400 font-semibold">级别:</span>
                        <select v-model="opsLevel" @change="loadOpsLogs" class="bg-slate-950 border border-slate-700 rounded px-2 py-1 text-slate-200 font-mono">
                          <option value="">全部级别</option>
                          <option value="error">ERROR 仅报错</option>
                          <option value="warn">WARN 警告以上</option>
                          <option value="info">INFO 信息以上</option>
                        </select>
                      </div>

                      <div class="flex items-center gap-1.5">
                        <span class="text-slate-400 font-semibold">行数:</span>
                        <select v-model.number="opsLines" @change="loadOpsLogs" class="bg-slate-950 border border-slate-700 rounded px-2 py-1 text-slate-200 font-mono">
                          <option :value="100">100 行</option>
                          <option :value="300">300 行</option>
                          <option :value="500">500 行</option>
                          <option :value="1000">1000 行</option>
                          <option :value="2000">2000 行</option>
                        </select>
                      </div>

                      <label class="flex items-center gap-1.5 cursor-pointer text-slate-400 hover:text-slate-200 select-none">
                        <input type="checkbox" v-model="opsAutoRefresh" class="rounded bg-slate-950" />
                        <span>自动轮询 (5s)</span>
                      </label>
                    </div>
                    <button @click="clearOpsLogs" class="text-slate-400 hover:text-slate-200">清屏</button>
                  </div>

                  <!-- 快捷过滤 Pills + 搜索框 -->
                  <div class="flex flex-wrap items-center justify-between gap-2 pt-2 border-t border-slate-800/80">
                    <div class="flex items-center gap-1.5 flex-wrap">
                      <button v-for="pill in opsFilterPills" :key="pill.val" @click="opsFilter = pill.val; loadOpsLogs()" :class="opsFilter === pill.val ? 'bg-amber-500/20 text-amber-300 border-amber-500/40 font-semibold' : 'bg-slate-950 text-slate-400 border-slate-800 hover:text-slate-200'" class="px-2.5 py-1 rounded-full text-[11px] border transition-all">
                        {{ pill.label }}
                      </button>
                    </div>
                    <div class="flex items-center gap-1.5">
                      <input v-model="opsFilter" @keyup.enter="loadOpsLogs" type="text" placeholder="输入任意关键词过滤 (回车)..." class="bg-slate-950 border border-slate-700 rounded px-2.5 py-1 text-slate-200 font-mono text-[11px] w-52" />
                      <button @click="loadOpsLogs" class="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-200 text-[11px]">过滤</button>
                    </div>
                  </div>
                </div>

                <div ref="opsTerminalBox" class="sacred-terminal h-96 overflow-y-auto space-y-0.5">
                  <div v-for="(line, idx) in opsLogs" :key="idx" :class="getLogLineClass(line)" class="term-line flex gap-3">
                    <span class="text-slate-600 select-none w-8 text-right shrink-0">{{ idx + 1 }}</span>
                    <span class="flex-1">{{ line }}</span>
                  </div>
                  <div v-if="opsLogs.length === 0" class="text-slate-600 py-12 text-center">暂无匹配的日志流输出，请点击上方按钮拉取或调整过滤条件。</div>
                </div>
              </div>
            </div>

            <!-- ═════════ Tab 5: AnyMessage 真机接码与冷库工坊 (/admin/register) ═════════ -->
            <div v-if="activeTab === 'register'" class="space-y-5 max-w-4xl">
              <div class="flex items-center justify-between">
                <div class="flex items-center gap-3">
                  <div class="w-9 h-9 rounded-xl bg-amber-500/15 border border-amber-500/35 flex items-center justify-center text-amber-300">
                    <AppIcon name="bolt" :size="18" />
                  </div>
                  <div>
                    <h2 class="text-base font-bold text-amber-300 font-sacred">一键单号快速注册 (P-256 受信任设备铸造)</h2>
                    <p class="text-[11px] text-slate-400">保留纯 HTTP 协议绕过与 ECDSA P-256 签章闭环：按一次仅注册并自检入池 1 个完整账号</p>
                  </div>
                </div>
                <button @click="loadRegisterData" class="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs border border-slate-700">
                  刷新任务历史
                </button>
              </div>

              <!-- 步骤 1: 填写邮箱与打码农场 -->
              <div class="glass-panel p-5 rounded-2xl border border-slate-800 space-y-4">
                <div class="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
                  <div>
                    <label class="block text-slate-400 mb-1">接收验证码邮箱</label>
                    <input v-model="quickReg.email" type="email" placeholder="your_email@example.com" class="w-full px-3 py-2 rounded-lg bg-slate-950 border border-slate-700 text-slate-100 font-mono" />
                  </div>
                  <div>
                    <label class="block text-slate-400 mb-1">账号密码（留空自动强随机生成）</label>
                    <input v-model="quickReg.password" type="text" placeholder="可选" class="w-full px-3 py-2 rounded-lg bg-slate-950 border border-slate-700 text-slate-100 font-mono" />
                  </div>
                  <div>
                    <label class="block text-slate-400 mb-1">海外出口代理（选填）</label>
                    <input v-model="quickReg.proxy" type="text" placeholder="http://127.0.0.1:7890" class="w-full px-3 py-2 rounded-lg bg-slate-950 border border-slate-700 text-slate-100 font-mono" />
                  </div>
                  <div>
                    <label class="block text-slate-400 mb-1">解耦打码农场地址 (blacklight-vlm-solver)</label>
                    <input v-model="quickReg.farm_url" type="text" placeholder="http://127.0.0.1:9527/farm/solve" class="w-full px-3 py-2 rounded-lg bg-slate-950 border border-slate-700 text-slate-100 font-mono" />
                  </div>
                </div>

                <div class="flex flex-wrap gap-2">
                  <button @click="runQuickPreflight" :disabled="quickReg.busy" class="px-4 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-semibold border border-slate-700 disabled:opacity-50">
                    1. PoW 出口合规预检
                  </button>
                  <button @click="startQuickRegister" :disabled="quickReg.busy || !quickReg.email" class="px-4 py-2 rounded-lg bg-amber-500 hover:bg-amber-400 text-slate-950 text-xs font-bold disabled:opacity-50">
                    {{ quickReg.busy ? '执行中...' : '2. 启动单号快速注册 (发送第 1 封注册码)' }}
                  </button>
                </div>

                <!-- 步骤 2: 注册 OTP -->
                <div v-if="quickReg.job && quickReg.job.stage === 'awaiting_register_otp'" class="p-4 rounded-xl bg-emerald-500/10 border border-emerald-500/30 space-y-3 text-xs">
                  <div class="text-emerald-300 font-semibold">第 1 封注册验证码已发送至 {{ quickReg.job.email }}</div>
                  <div class="flex items-center gap-2">
                    <input v-model="quickReg.register_otp" type="text" placeholder="输入 6 位注册验证码" class="flex-1 px-3 py-2 rounded-lg bg-slate-950 border border-slate-700 text-slate-100 font-mono" />
                    <button @click="verifyQuickRegisterOtp" :disabled="quickReg.busy || !quickReg.register_otp" class="px-4 py-2 rounded-lg bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold disabled:opacity-50">
                      提交注册并触发二次确权发码
                    </button>
                  </div>
                </div>

                <!-- 步骤 3: 二次确权 MFA OTP -->
                <div v-if="quickReg.job && quickReg.job.stage === 'awaiting_mfa_otp'" class="p-4 rounded-xl bg-cyan-500/10 border border-cyan-500/30 space-y-3 text-xs">
                  <div class="text-cyan-300 font-semibold">二次确权邮件已发出，请输入第 2 封邮件验证码以铸造 ECDSA P-256 密钥对</div>
                  <div class="flex items-center gap-2">
                    <input v-model="quickReg.mfa_otp" type="text" placeholder="输入 6 位 MFA 验证码" class="flex-1 px-3 py-2 rounded-lg bg-slate-950 border border-slate-700 text-slate-100 font-mono" />
                    <button @click="completeQuickRegister" :disabled="quickReg.busy || !quickReg.mfa_otp" class="px-4 py-2 rounded-lg bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-bold disabled:opacity-50">
                      铸造 30 天签证并自检入池
                    </button>
                  </div>
                </div>

                <div v-if="quickReg.job && quickReg.job.stage === 'completed'" class="p-4 rounded-xl bg-emerald-500/15 border border-emerald-500/40 text-xs text-emerald-200 space-y-1 font-mono">
                  <div class="font-bold">✅ 单号快速注册并入池完成</div>
                  <div>Email: {{ quickReg.job.account_summary?.email }}</div>
                  <div>CredentialId: {{ quickReg.job.account_summary?.credential_id }}</div>
                  <div>DeviceId: {{ quickReg.job.account_summary?.device_id }}</div>
                </div>

                <div v-if="quickReg.job && quickReg.job.logs && quickReg.job.logs.length" class="p-3 rounded-xl bg-slate-950 border border-slate-800 text-[11px] font-mono text-slate-300 max-h-48 overflow-y-auto space-y-1">
                  <div v-for="(l, idx) in quickReg.job.logs" :key="idx">{{ l }}</div>
                </div>
              </div>
            </div>

            <div v-if="activeTab === 'login'" class="max-w-2xl space-y-6">
              <div>
                <h2 class="text-base font-bold text-amber-300 flex items-center gap-2 font-sacred">
                  <AppIcon name="upload" :size="18" />
                  <span>纳新入池 (手动录入受戒账号)</span>
                </h2>
                <p class="text-xs text-slate-400 mt-1">支持三种方式直接将 DeepSeek 账号或凭据导入至圣殿号池，系统自动静默探活并初始化会话。</p>
              </div>

              <!-- 4 种方式切换按钮 -->
              <div class="flex items-center gap-2 flex-wrap">
                <button @click="addMode = 'jsonl'" :class="addMode === 'jsonl' ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40 font-semibold' : 'bg-slate-900 border-slate-800 text-slate-400'" class="px-3.5 py-2 rounded-lg text-xs font-medium border transition-all flex items-center gap-1.5">
                  <AppIcon name="database" :size="14" />
                  <span>📦 方式一：5件套 JSONL 批量导入 (推荐 · 支持冷库/活跃池)</span>
                </button>
                <button @click="addMode = 'email'" :class="addMode === 'email' ? 'bg-amber-500/20 text-amber-300 border-amber-500/40 font-semibold' : 'bg-slate-900 border-slate-800 text-slate-400'" class="px-3.5 py-2 rounded-lg text-xs font-medium border transition-all flex items-center gap-1.5">
                  <AppIcon name="mail" :size="14" />
                  <span>方式二：邮箱密码登录</span>
                </button>
                <button @click="addMode = 'token'" :class="addMode === 'token' ? 'bg-amber-500/20 text-amber-300 border-amber-500/40 font-semibold' : 'bg-slate-900 border-slate-800 text-slate-400'" class="px-3.5 py-2 rounded-lg text-xs font-medium border transition-all flex items-center gap-1.5">
                  <AppIcon name="shield" :size="14" />
                  <span>方式三：直接录入 Token</span>
                </button>
                <button @click="addMode = 'curl'" :class="addMode === 'curl' ? 'bg-amber-500/20 text-amber-300 border-amber-500/40 font-semibold' : 'bg-slate-900 border-slate-800 text-slate-400'" class="px-3.5 py-2 rounded-lg text-xs font-medium border transition-all flex items-center gap-1.5">
                  <AppIcon name="code" :size="14" />
                  <span>方式四：cURL 解析</span>
                </button>
              </div>

              <!-- 提示条：区分 DeepSeek 账号 Token 与 AnyMessage 接码 Token -->
              <div class="p-3 rounded-xl bg-purple-500/10 border border-purple-500/30 text-[11px] text-purple-200 flex items-start gap-2.5">
                <AppIcon name="alert" :size="16" class="shrink-0 mt-0.5 text-purple-400" />
                <div class="space-y-1">
                  <div><b>注意区分 Token 类型：</b>本面板用于录入 <b>DeepSeek 官方账号的 Bearer 会话 Token</b>（入池用于业务分发）。</div>
                  <div>若需要配置或更换接码平台的 <b>AnyMessage 充值令牌</b>，请前往 <button @click="switchTab('register')" class="text-amber-300 underline font-semibold hover:text-amber-200">【接码产号与冷库工坊】</button> 或 <button @click="switchTab('settings')" class="text-amber-300 underline font-semibold hover:text-amber-200">【圣殿设置】</button>。</div>
                </div>
              </div>

              <!-- 模式表单 -->
              <form @submit.prevent="submitAddAccount" class="glass-panel p-6 rounded-xl border border-amber-500/30 space-y-4 text-xs">
                <template v-if="addMode === 'jsonl'">
                  <div class="space-y-3">
                    <div>
                      <label class="block text-slate-300 mb-1.5 font-semibold">选择入库目标池 (Target Pool)</label>
                      <div class="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
                        <button type="button" @click="addForm.targetStatus = 'INACTIVE_COLD'" :class="addForm.targetStatus === 'INACTIVE_COLD' ? 'bg-cyan-500/20 text-cyan-300 border-cyan-500/50 font-bold' : 'bg-slate-900 border-slate-800 text-slate-400'" class="p-3 rounded-xl border text-left transition-all">
                          <div class="text-xs flex items-center gap-1.5"><span>🧊 存入零IP冷库 (INACTIVE_COLD)</span></div>
                          <div class="text-[10px] text-slate-400 mt-1 font-normal">零 Token 静默储备，需要时通过纯 HTTP 协议 + 真实数美 B 指纹毫秒级极速续杯上线</div>
                        </button>
                        <button type="button" @click="addForm.targetStatus = 'ACTIVE'" :class="addForm.targetStatus === 'ACTIVE' ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/50 font-bold' : 'bg-slate-900 border-slate-800 text-slate-400'" class="p-3 rounded-xl border text-left transition-all">
                          <div class="text-xs flex items-center gap-1.5"><span>🟢 直接投入活跃池 (ACTIVE)</span></div>
                          <div class="text-[10px] text-slate-400 mt-1 font-normal">自动探活现有 Token；若无 Token 或已过期则自动走纯 HTTP 指纹引擎无感续杯入池</div>
                        </button>
                      </div>
                    </div>
                    <div>
                      <label class="block text-slate-300 mb-1">粘贴 5 件套 JSONL / JSON 数组数据（每行一个含 credential_id & mfa_private_key 的 JSON）</label>
                      <textarea v-model="addForm.jsonlText" rows="7" placeholder='{"email": "xxx@outlook.com", "password": "...", "credential_id": "...", "mfa_private_key": "...", "mfa_public_key": "...", "mfa_key_version": "...", "device_id": "..."}' class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100 font-mono text-[11px]"></textarea>
                    </div>
                  </div>
                </template>

                <template v-if="addMode === 'email'">
                  <div>
                    <label class="block text-slate-300 mb-1">DeepSeek 注册邮箱</label>
                    <input v-model="addForm.email" type="email" placeholder="your_email@example.com" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100 font-mono" />
                  </div>
                  <div>
                    <label class="block text-slate-300 mb-1">DeepSeek 登录密码</label>
                    <input v-model="addForm.password" type="password" placeholder="••••••••" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100" />
                  </div>
                  <div>
                    <label class="block text-slate-300 mb-1">账号别名 / 标签 (可选)</label>
                    <input v-model="addForm.emailLabel" type="text" placeholder="留空则自动以邮箱为标识" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100 font-mono" />
                  </div>
                </template>

                <template v-if="addMode === 'token'">
                  <div>
                    <label class="block text-slate-300 mb-1">User Token (Bearer 之后的密文)</label>
                    <textarea v-model="addForm.token" rows="4" placeholder="粘贴抓包获取的 user token 密文..." class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100 font-mono"></textarea>
                  </div>
                  <div>
                    <label class="block text-slate-300 mb-1">账号标识 / 备注名</label>
                    <input v-model="addForm.tokenLabel" type="text" placeholder="例如：user-vip-01" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100 font-mono" />
                  </div>
                </template>

                <template v-if="addMode === 'curl'">
                  <div>
                    <label class="block text-slate-300 mb-1">浏览器 Copy as cURL 抓包文本</label>
                    <textarea v-model="addForm.curlText" rows="6" placeholder="粘贴从浏览器 DevTools 复制的完整 curl 'https://chat.deepseek.com/...' 字符串" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100 font-mono"></textarea>
                  </div>
                  <div>
                    <label class="block text-slate-300 mb-1">账号标识 / 备注名 (可选)</label>
                    <input v-model="addForm.curlLabel" type="text" placeholder="例如：chrome-session-01" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100 font-mono" />
                  </div>
                </template>

                <button type="submit" :disabled="submittingAdd" class="w-full py-2.5 rounded-lg bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-semibold text-xs shadow-lg shadow-emerald-500/20 disabled:opacity-50">
                  {{ submittingAdd ? '正在校验并纳新入池...' : '确认导入并静默探活入库' }}
                </button>
              </form>
            </div>

            <!-- ═════════ Tab 7: 圣殿枢纽设置 (/admin/settings) ═════════ -->
            <div v-if="activeTab === 'settings'" class="max-w-4xl space-y-6">
              <div class="flex items-center justify-between">
                <h2 class="text-base font-bold text-amber-300 flex items-center gap-2 font-sacred">
                  <AppIcon name="settings" :size="18" />
                  <span>圣殿中继核心参数与防御配置</span>
                </h2>
                <button @click="saveGatewaySettings" class="px-4 py-2 bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-semibold text-xs rounded-lg shadow flex items-center gap-1.5">
                  <AppIcon name="save" :size="14" />
                  <span>保存中继设置</span>
                </button>
              </div>

              <div v-if="settingsSaved" class="p-3 rounded-lg bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 text-xs">
                系统设置已即时落入 SQLite 并热更新生效。
              </div>

              <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                <!-- 出口网络设置 -->
                <div class="glass-panel p-5 rounded-xl border border-amber-500/30 space-y-4 text-xs">
                  <h3 class="text-sm font-semibold text-amber-300 flex items-center gap-2 font-sacred">
                    <AppIcon name="globe" :size="15" />
                    <span>出口网络与透传中继</span>
                  </h3>
                  <div>
                    <label class="block text-slate-300 mb-1">出口代理 (PROXY)</label>
                    <input v-model="gatewaySettings.proxy" type="text" placeholder="http://127.0.0.1:7890" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100 font-mono" />
                    <small class="text-slate-500 text-[11px] block mt-1">留空直连，支持 http/socks5。配置变更即刻对所有新会话生效。</small>
                  </div>
                  <div>
                    <label class="block text-slate-300 mb-1">透传模式 (PASSTHROUGH)</label>
                    <select v-model="gatewaySettings.passthrough" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100">
                      <option :value="false">关闭 (严格经过反风控拟人状态机与多分支会话树)</option>
                      <option :value="true">开启 (直通透传)</option>
                    </select>
                  </div>
                </div>

                <!-- 管理员密码设置 -->
                <div class="glass-panel p-5 rounded-xl border border-amber-500/30 space-y-4 text-xs">
                  <h3 class="text-sm font-semibold text-amber-300 flex items-center gap-2 font-sacred">
                    <AppIcon name="lock" :size="15" />
                    <span>管理员圣契密匙 (ADMIN PASSWORD)</span>
                  </h3>
                  <div>
                    <label class="block text-slate-300 mb-1">重设管理员密码</label>
                    <input v-model="adminPwd.newPwd" type="password" placeholder="请输入新密码（至少 4 位）" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100" />
                  </div>
                  <div>
                    <label class="block text-slate-300 mb-1">确认新密码</label>
                    <input v-model="adminPwd.confirmPwd" type="password" placeholder="再次确认新密码" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100" />
                  </div>
                  <button @click="changeAdminPwd" class="w-full py-2 bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 border border-amber-500/40 rounded font-semibold text-xs">
                    更新圣契密码
                  </button>
                </div>

                <!-- AnyMessage 接码令牌配置卡片 -->
                <div class="glass-panel p-5 rounded-xl border border-purple-500/30 space-y-4 text-xs md:col-span-2">
                  <div class="flex items-center justify-between flex-wrap gap-2">
                    <h3 class="text-sm font-semibold text-purple-300 flex items-center gap-2 font-sacred">
                      <AppIcon name="key" :size="15" />
                      <span>AnyMessage 接码凭证配置 (ANYMESSAGE TOKEN)</span>
                    </h3>
                    <div class="flex items-center gap-2">
                      <span class="text-xs font-mono font-bold text-amber-300 bg-amber-500/10 px-2 py-0.5 rounded border border-amber-500/30">
                        💰 余额: ${{ Number(anymessageActiveBalance || 0).toFixed(4) }} (≈{{ anymessageEstAccounts }}个号)
                      </span>
                      <button @click="loadAnyMessageTokens" class="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs flex items-center gap-1">
                        <AppIcon name="refresh" :size="12" />
                        <span>查余额</span>
                      </button>
                    </div>
                  </div>
                  <div>
                    <label class="block text-slate-300 mb-1">当前生效 Token</label>
                    <div class="flex items-center gap-2 bg-slate-900 border border-slate-700 rounded px-3 py-2">
                      <span class="font-mono text-purple-300 flex-1 truncate select-all">{{ anymessageActiveToken || '未设置' }}</span>
                      <button v-if="anymessageActiveToken" @click="copyText(anymessageActiveToken, 'AnyMessage Token')" class="text-emerald-400 hover:text-emerald-300 text-[11px] font-semibold">
                        复制
                      </button>
                    </div>
                  </div>
                  <div>
                    <div class="flex items-center justify-between mb-1">
                      <label class="block text-slate-300">AnyMessage 令牌列表 (每行一个 · 持久化落入 SQLite <code>system_settings</code>)</label>
                      <span class="text-[10px] text-slate-400">支持 <code>#</code> 注释行</span>
                    </div>
                    <textarea v-model="anymessageRawText" rows="3" placeholder="在此输入 AnyMessage Token，每行一个" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100 font-mono text-xs focus:border-purple-500 focus:outline-none"></textarea>
                  </div>
                  <div class="flex justify-end">
                    <button @click="saveAnyMessageTokens" :disabled="savingTokens" class="px-4 py-2 bg-purple-600 hover:bg-purple-500 text-white font-semibold text-xs rounded-lg shadow flex items-center gap-1.5 disabled:opacity-50 transition-all">
                      <AppIcon name="save" :size="14" />
                      <span>{{ savingTokens ? '保存中...' : '保存 AnyMessage 令牌' }}</span>
                    </button>
                  </div>
                </div>
              </div>
            </div>

            <!-- ═════════ Tab 8: 财务订单与付费档位控制 (/admin/users) ═════════ -->
          </div>

          <!-- ═════════ 5. 全套弹窗模态框 ═════════ -->

          <!-- 1. 独占账号分配 / 选号 / 补号模态框 (assignModal) -->
          <div v-if="assignModal.show" class="fixed inset-0 bg-black/75 backdrop-blur-sm flex items-center justify-center p-4 z-50">
            <div class="glass-panel w-full max-w-2xl rounded-2xl p-6 border border-amber-500/40 shadow-2xl space-y-4 max-h-[88vh] flex flex-col">
              <div class="flex items-center justify-between pb-2 border-b border-slate-800">
                <div class="flex items-center gap-2">
                  <h3 class="text-sm font-bold text-amber-300 font-sacred">
                    🎯 独占账号选号 / 补号 · {{ assignModal.key?.name }}
                  </h3>
                  <span v-if="assignModal.key?.owner" class="text-xs text-slate-400 font-mono">({{ assignModal.key?.owner }})</span>
                  <span class="px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30 font-sans">
                    {{ assignModal.key?.key_type === 'card' || assignModal.key?.is_card ? '卡密' : '人情圣钥' }}
                  </span>
                </div>
                <button @click="assignModal.show = false" class="text-slate-400 hover:text-slate-200"><AppIcon name="close" :size="16" /></button>
              </div>

              <div class="p-3 rounded-xl bg-amber-500/10 border border-amber-500/20 text-xs text-slate-300 leading-relaxed">
                为 <b>{{ assignModal.key?.name }}</b> 挑选绑定的独占账号（选号）。<br>
                💡 <b>已勾选 = 该卡独占</b>；取消勾选 = 退回公共池。<span class="text-amber-200">若官方掉号/封号，取消死号并勾上新号即可秒级完成补号！</span>
              </div>

              <!-- 搜索与快捷选号栏 -->
              <div class="space-y-2">
                <div class="flex items-center justify-between gap-3">
                  <div class="relative flex-1">
                    <input
                      v-model="assignModal.searchQuery"
                      type="text"
                      placeholder="🔍 快速搜索邮箱或 Label..."
                      class="w-full px-3 py-1.5 rounded-lg bg-slate-950 border border-slate-700 text-xs text-slate-100 placeholder-slate-500 focus:outline-none focus:border-amber-500 font-mono"
                    />
                  </div>
                  <div class="flex items-center gap-2 text-xs">
                    <button
                      type="button"
                      @click="quickSelectAssign(1)"
                      class="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-amber-300 border border-slate-700 text-[11px] font-semibold"
                      title="自动挑选 1 个空闲 ACTIVE 账号"
                    >
                      +选1个
                    </button>
                    <button
                      type="button"
                      @click="quickSelectAssign(2)"
                      class="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-amber-300 border border-slate-700 text-[11px] font-semibold"
                      title="自动挑选 2 个空闲 ACTIVE 账号"
                    >
                      +选2个
                    </button>
                    <button
                      type="button"
                      @click="assignModal.selectedLabels = []"
                      class="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-400 border border-slate-700 text-[11px]"
                      title="清空后所有账号退回公共池"
                    >
                      清空选择 (走公共池)
                    </button>
                  </div>
                </div>

                <div class="flex items-center justify-between text-[11px] px-1 text-slate-400">
                  <label class="flex items-center gap-1.5 cursor-pointer select-none">
                    <input type="checkbox" v-model="assignModal.onlyActive" class="rounded bg-slate-900 border-slate-700 text-amber-500 focus:ring-0 text-xs" />
                    <span>只看有效 (ACTIVE) 号</span>
                  </label>
                  <div>
                    当前已勾选: <b class="text-amber-400 font-mono text-xs">{{ (assignModal.selectedLabels || []).length }}</b> 个独占账号
                  </div>
                </div>
              </div>

              <!-- 账号列表滚动区域 -->
              <div class="space-y-1.5 overflow-y-auto flex-1 pr-1 font-mono text-xs max-h-[360px]">
                <div
                  v-for="acc in filteredAssignAccounts"
                  :key="acc.account_label"
                  @click="toggleAssignAccount(acc)"
                  :class="[
                    acc.takenByOther
                      ? 'opacity-40 cursor-not-allowed bg-slate-900/30 border-slate-800/40 text-slate-500'
                      : assignModal.selectedLabels.includes(acc.account_label)
                        ? 'bg-amber-500/10 border-amber-500/40 text-amber-200 cursor-pointer'
                        : 'bg-slate-900/50 border-slate-800/80 text-slate-300 hover:bg-slate-800/50 cursor-pointer',
                    'p-2.5 rounded-xl border flex items-center justify-between transition select-none'
                  ]"
                >
                  <div class="flex items-center gap-2.5 truncate">
                    <input
                      type="checkbox"
                      :value="acc.account_label"
                      :checked="assignModal.selectedLabels.includes(acc.account_label)"
                      :disabled="acc.takenByOther"
                      @click.stop
                      @change="toggleAssignAccount(acc)"
                      class="rounded bg-slate-950 border-slate-700 text-amber-500 focus:ring-0 cursor-pointer"
                    />
                    <span :class="acc.takenByOther ? 'text-slate-500' : 'text-slate-200 font-bold'" class="truncate" :title="acc.account_label">
                      {{ acc.account_label }}
                    </span>
                  </div>
                  <div class="flex items-center gap-2 shrink-0 font-sans">
                    <span v-if="acc.status === 'ACTIVE'" class="px-2 py-0.5 rounded text-[10px] bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">
                      ACTIVE 就绪
                    </span>
                    <span v-else-if="acc.status === 'WARMING'" class="px-2 py-0.5 rounded text-[10px] bg-cyan-500/15 text-cyan-300 border border-cyan-500/30">
                      预热中
                    </span>
                    <span v-else-if="acc.status === 'INACTIVE_COLD'" class="px-2 py-0.5 rounded text-[10px] bg-blue-500/15 text-blue-300 border border-blue-500/30">
                      冷库
                    </span>
                    <span v-else class="px-2 py-0.5 rounded text-[10px] bg-red-500/15 text-red-400 border border-red-500/30">
                      {{ acc.status || '异常' }}
                    </span>
                    <span v-if="acc.takenByOther" class="text-[10px] text-red-400/80">
                      已被「{{ acc.owner || '其他Key' }}」独占
                    </span>
                    <span v-else-if="assignModal.selectedLabels.includes(acc.account_label)" class="text-[10px] text-amber-300 font-bold">
                      ✓ 已独占
                    </span>
                    <span v-else class="text-[10px] text-slate-500">
                      空闲可用
                    </span>
                  </div>
                </div>

                <div v-if="!filteredAssignAccounts || filteredAssignAccounts.length === 0" class="py-8 text-center text-slate-500 text-xs">
                  没有符合条件的账号
                </div>
              </div>

              <div class="flex justify-end gap-2 pt-3 border-t border-slate-800">
                <button @click="assignModal.show = false" class="px-3.5 py-1.5 text-xs text-slate-400 hover:text-slate-200">取消</button>
                <button @click="saveAssignModal" class="px-5 py-2 bg-gradient-to-r from-emerald-500 to-emerald-600 hover:from-emerald-400 hover:to-emerald-500 text-slate-950 font-bold text-xs rounded-lg shadow-lg shadow-emerald-500/20">保存选号 / 补号生效</button>
              </div>
            </div>
          </div>

          <!-- 2. 配额模态框 (quotaModal) -->
          <div v-if="quotaModal.show" class="fixed inset-0 bg-black/75 backdrop-blur-sm flex items-center justify-center p-4 z-50">
            <div class="glass-panel w-full max-w-md rounded-2xl p-6 border border-amber-500/40 shadow-2xl space-y-4">
              <div class="flex items-center justify-between pb-2 border-b border-slate-800">
                <h3 class="text-sm font-bold text-amber-300 font-sacred">
                  每日配额设置 · {{ quotaModal.key?.owner || quotaModal.key?.name }}
                </h3>
                <button @click="quotaModal.show = false" class="text-slate-400 hover:text-slate-200"><AppIcon name="close" :size="16" /></button>
              </div>
              <div class="p-3 rounded-lg bg-amber-500/10 border border-amber-500/20 text-[11px] text-slate-300">
                填 <b>0 = 不限</b>。计数在本地次日 00:00 自动重置清零。
              </div>
              <div class="space-y-3 text-xs">
                <div>
                  <label class="block text-slate-300 mb-1">每日请求上限 (次，0 = 不限)</label>
                  <input v-model.number="quotaModal.reqLimit" type="number" min="0" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100 font-mono" />
                </div>
                <div>
                  <label class="block text-slate-300 mb-1">每日 Token 上限 (0 = 不限)</label>
                  <input v-model.number="quotaModal.tokLimit" type="number" min="0" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100 font-mono" />
                </div>
              </div>
              <div class="flex justify-end gap-2 pt-3 border-t border-slate-800">
                <button @click="quotaModal.show = false" class="px-3.5 py-1.5 text-xs text-slate-400">取消</button>
                <button @click="saveQuotaModal" class="px-4 py-1.5 bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-semibold text-xs rounded">保存配额</button>
              </div>
            </div>
          </div>

          <!-- 3. 敏感词风控库模态框 (sensitiveModal) -->
          <div v-if="sensitiveModal.show" class="fixed inset-0 bg-black/75 backdrop-blur-sm flex items-center justify-center p-4 z-50">
            <div class="glass-panel w-full max-w-xl rounded-2xl p-6 border border-amber-500/40 shadow-2xl space-y-4 max-h-[85vh] flex flex-col">
              <div class="flex items-center justify-between pb-2 border-b border-slate-800">
                <h3 class="text-sm font-bold text-amber-300 font-sacred">敏感词风控与防封拦截策略</h3>
                <button @click="sensitiveModal.show = false" class="text-slate-400 hover:text-slate-200"><AppIcon name="close" :size="16" /></button>
              </div>
              <div class="space-y-3 overflow-y-auto flex-1 pr-2 text-xs">
                <p class="text-slate-300 text-[11px] leading-relaxed">
                  针对 DeepSeek 官方已知风控触发点（biz_code: 5 封号 72 小时 / 永久）：<br>
                  • 默认对普通 API Key 开启前置敏感词拦截，避免高危请求直接打到上游引发封号；<br>
                  • 针对特定研发测试人员，可在「圣钥列表」点击 <b>严格拦截</b> 切换为 <b>豁免放行</b>。
                </p>

                <div class="font-bold text-amber-300 pt-1">内置防封拦截规则类别：</div>
                <div class="p-3 rounded-lg bg-black/50 border border-slate-800 space-y-2 text-[11px] font-mono">
                  <div class="text-red-400 font-semibold">高危载荷特征 ({{ (sensitiveModal.builtIn?.cyber_exploit || []).length }} 项):</div>
                  <div class="text-slate-300 pl-2 max-h-16 overflow-y-auto">
                    {{ (sensitiveModal.builtIn?.cyber_exploit || []).join(' · ') }}
                  </div>
                  <div class="text-amber-400 font-semibold pt-1">提示词注入特征 ({{ (sensitiveModal.builtIn?.jailbreak || []).length }} 项):</div>
                  <div class="text-slate-300 pl-2 max-h-16 overflow-y-auto">
                    {{ (sensitiveModal.builtIn?.jailbreak || []).join(' · ') }}
                  </div>
                </div>

                <div class="font-bold text-amber-300 pt-1">自定义额外拦截词（每行一个，不区分大小写）：</div>
                <textarea v-model="sensitiveModal.customWords" rows="5" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100 font-mono text-xs"></textarea>
              </div>
              <div class="flex justify-end gap-2 pt-3 border-t border-slate-800">
                <button @click="sensitiveModal.show = false" class="px-3.5 py-1.5 text-xs text-slate-400">取消</button>
                <button @click="saveSensitiveModal" class="px-4 py-1.5 bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-semibold text-xs rounded">保存词库</button>
              </div>
            </div>
          </div>

          <!-- 4. 铸造新圣钥明文展示模态框 (createKeyModal) -->
          <div v-if="createKeyModal.show" class="fixed inset-0 bg-black/75 backdrop-blur-sm flex items-center justify-center p-4 z-50">
            <div class="glass-panel w-full max-w-md rounded-2xl p-6 border border-amber-500/40 shadow-2xl space-y-4">
              <div class="flex items-center justify-between pb-2 border-b border-slate-800">
                <h3 class="text-sm font-bold text-amber-300 font-sacred">铸造新圣钥 (Create API Key)</h3>
                <button @click="createKeyModal.show = false" class="text-slate-400 hover:text-slate-200"><AppIcon name="close" :size="16" /></button>
              </div>

              <div v-if="createKeyModal.generatedKey" class="space-y-4 text-xs">
                <div class="p-3 bg-emerald-500/10 border border-emerald-500/30 rounded-lg text-emerald-400">
                  圣钥铸造成功。该明文仅在创建时展示一次，请妥善复制并保存：
                </div>
                <div class="p-3 bg-slate-950 rounded-lg border border-slate-700 font-mono text-xs text-amber-300 break-all select-all flex items-center justify-between gap-2">
                  <span>{{ createKeyModal.generatedKey }}</span>
                  <button @click="copyGeneratedKey" class="px-2.5 py-1 text-[11px] bg-amber-500/20 text-amber-300 rounded shrink-0">
                    {{ createKeyModal.copied ? '已复制' : '复制' }}
                  </button>
                </div>
                <button @click="createKeyModal.show = false; loadKeys();" class="w-full py-2 bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-semibold rounded text-xs">
                  完成并关闭
                </button>
              </div>

              <div v-else class="space-y-3 text-xs">
                <div>
                  <label class="block text-slate-300 mb-1">新圣钥标识备注名</label>
                  <input v-model="createKeyModal.name" type="text" placeholder="如：vscode-codex / claude-code" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100" />
                </div>
                <div>
                  <label class="block text-slate-300 mb-1">归属人 (谁用这把钥；留空 = 未指定)</label>
                  <input v-model="createKeyModal.owner" type="text" placeholder="如：合伙人A / 生产应用" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100" />
                </div>
                <div class="flex justify-end gap-2 pt-3 border-t border-slate-800">
                  <button @click="createKeyModal.show = false" class="px-3.5 py-1.5 text-xs text-slate-400">取消</button>
                  <button @click="submitCreateKey" class="px-4 py-1.5 bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-semibold text-xs rounded">立即铸造</button>
                </div>
              </div>
            </div>
          </div>

          <!-- 4.5 温库一键秒级投产模态框 (promoteModal) -->
          <div v-if="promoteModal.show" class="fixed inset-0 bg-black/75 backdrop-blur-sm flex items-center justify-center p-4 z-50">
            <div class="glass-panel w-full max-w-md rounded-2xl p-6 border border-amber-500/40 shadow-2xl space-y-4">
              <div class="flex items-center justify-between pb-2 border-b border-slate-800">
                <h3 class="font-bold text-amber-300 text-sm flex items-center gap-2">
                  <AppIcon name="bolt" :size="16" />
                  <span>待命温库一键瞬时投产</span>
                </h3>
                <button @click="promoteModal.show = false" class="text-slate-400 hover:text-slate-200"><AppIcon name="close" :size="16" /></button>
              </div>
              <div class="p-3 bg-amber-500/10 border border-amber-500/30 rounded-lg text-xs text-slate-300 space-y-1">
                <div>当前温库就绪储备: <b class="text-amber-400">{{ standbyAccounts.length }}</b> 个 (均已取得永久 Token)</div>
                <div class="text-[11px] text-slate-400">⚡ 温库账号已完成全部认证，投产过程 0 毫秒生效，无风控，直接参与前线负载均衡！</div>
              </div>
              <div>
                <label class="block text-slate-300 text-xs mb-1 font-medium">本次投产账号数量 (个)</label>
                <input v-model.number="promoteModal.count" type="number" min="1" :max="standbyAccounts.length" class="w-full px-3 py-2 rounded-lg bg-slate-900 border border-slate-700 text-slate-100 font-mono text-sm" />
              </div>
              <div class="flex justify-end gap-2 pt-3 border-t border-slate-800">
                <button @click="promoteModal.show = false" class="px-3.5 py-1.5 text-xs text-slate-400">取消</button>
                <button @click="submitPromoteModal" :disabled="promoteModal.loading" class="px-4 py-1.5 bg-amber-500 hover:bg-amber-400 text-slate-950 font-bold text-xs rounded-lg shadow flex items-center gap-1.5 disabled:opacity-50">
                  <AppIcon v-if="promoteModal.loading" name="refresh" :size="14" class="animate-spin" />
                  <span>{{ promoteModal.loading ? '正在投产...' : '立即秒级投产入池' }}</span>
                </button>
              </div>
            </div>
          </div>

          <!-- 5. 唤醒冷库账号模态框 (wakeModal) -->
          <div v-if="wakeModal.show" class="fixed inset-0 bg-black/75 backdrop-blur-sm flex items-center justify-center p-4 z-50">
            <div class="glass-panel w-full max-w-md rounded-2xl p-6 border border-cyan-500/40 shadow-2xl space-y-4">
              <div class="flex items-center justify-between pb-2 border-b border-slate-800">
                <h3 class="text-sm font-bold text-cyan-300 font-sacred flex items-center gap-2">
                  <AppIcon name="snowflake" :size="15" />
                  <span>冷库平缓批量唤醒</span>
                </h3>
                <button @click="wakeModal.show = false" class="text-slate-400 hover:text-slate-200"><AppIcon name="close" :size="16" /></button>
              </div>
              <div class="p-3 bg-cyan-500/10 border border-cyan-500/30 rounded-lg text-xs text-slate-300 space-y-1">
                <div>当前冷库储备待命账号: <b class="text-cyan-400">{{ wakeModal.maxCold }}</b> 个</div>
                <div class="text-[11px] text-slate-400">纯 HTTP 无感刷新，支持批量温和唤醒冷库账号入池（默认请求间隔 2 秒）。</div>
              </div>
              <div class="text-xs">
                <label class="block text-slate-300 mb-1">计划唤醒账号数量</label>
                <input v-model.number="wakeModal.count" type="number" min="1" :max="wakeModal.maxCold" class="w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 text-slate-100 font-mono" />
              </div>
              <div class="flex justify-end gap-2 pt-3 border-t border-slate-800">
                <button @click="wakeModal.show = false" class="px-3.5 py-1.5 text-xs text-slate-400">取消</button>
                <button @click="submitWakeModal" class="px-4 py-1.5 bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-semibold text-xs rounded">确认平缓唤醒</button>
              </div>
            </div>
          </div>

          <!-- 6. 风控与封号收件箱模态框 (inboxModal) -->
          <div v-if="inboxModal.show" class="fixed inset-0 bg-black/80 backdrop-blur-md flex items-center justify-center p-4 z-50">
            <div class="glass-panel w-full max-w-3xl rounded-2xl p-6 border border-amber-500/40 shadow-2xl space-y-4 max-h-[88vh] flex flex-col">
              <div class="flex items-center justify-between pb-3 border-b border-slate-800 shrink-0">
                <div class="flex items-center gap-3">
                  <div class="w-10 h-10 rounded-xl bg-amber-500/15 border border-amber-500/35 flex items-center justify-center text-amber-400 shadow-lg shadow-amber-500/10">
                    <AppIcon name="mail" :size="20" />
                  </div>
                  <div>
                    <h3 class="text-base font-bold text-slate-100 font-sacred flex items-center gap-2.5">
                      <span>圣殿风控与封号收件箱</span>
                      <span v-if="unreadAlertsCount > 0" class="text-[10px] font-mono px-2 py-0.5 rounded-full bg-red-500/20 text-red-400 border border-red-500/40">
                        {{ unreadAlertsCount }} 条未读
                      </span>
                    </h3>
                    <p class="text-[11px] text-slate-400 font-mono mt-0.5">全量持久化追踪官方禁言、失效、隔离及风控轨迹（支持状态追踪与历史清理）</p>
                  </div>
                </div>
                <button @click="inboxModal.show = false" class="text-slate-400 hover:text-slate-200 p-1.5 rounded-lg hover:bg-slate-800 transition-colors">
                  <AppIcon name="close" :size="18" />
                </button>
              </div>

              <!-- 顶部工具栏与筛选 -->
              <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2.5 py-2 border-b border-slate-800/80 text-xs shrink-0">
                <div class="flex items-center gap-1.5">
                  <button @click="inboxFilter = 'all'" :class="inboxFilter === 'all' ? 'bg-amber-500/20 text-amber-300 border-amber-500/40 font-semibold' : 'text-slate-400 hover:text-slate-200 border-transparent'" class="px-3 py-1 rounded-lg text-xs border transition-all">
                    全部 ({{ inboxAlerts.length }})
                  </button>
                  <button @click="inboxFilter = 'unread'" :class="inboxFilter === 'unread' ? 'bg-red-500/20 text-red-300 border-red-500/40 font-semibold' : 'text-slate-400 hover:text-slate-200 border-transparent'" class="px-3 py-1 rounded-lg text-xs border transition-all flex items-center gap-1">
                    <span>未读</span>
                    <span v-if="unreadAlertsCount > 0" class="px-1.5 py-0.2 rounded-full bg-red-500 text-white text-[10px]">{{ unreadAlertsCount }}</span>
                  </button>
                  <button @click="inboxFilter = 'read'" :class="inboxFilter === 'read' ? 'bg-slate-800 text-slate-200 border-slate-700 font-semibold' : 'text-slate-400 hover:text-slate-200 border-transparent'" class="px-3 py-1 rounded-lg text-xs border transition-all">
                    已读 ({{ Math.max(0, inboxAlerts.length - unreadAlertsCount) }})
                  </button>
                </div>
                <div class="flex items-center gap-2">
                  <button v-if="unreadAlertsCount > 0" @click="markAllAlertsRead" class="px-3 py-1.5 rounded-lg bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-300 text-xs font-semibold border border-emerald-500/40 transition-all flex items-center gap-1.5">
                    <AppIcon name="check" :size="13" />
                    <span>一键全部已读</span>
                  </button>
                  <button v-if="inboxAlerts.some(a => a.is_read)" @click="clearReadAlerts" class="px-3 py-1.5 rounded-lg bg-red-500/10 hover:bg-red-500/20 text-red-400 border border-red-500/30 text-xs transition-all flex items-center gap-1.5">
                    <AppIcon name="trash" :size="13" />
                    <span>清空已读</span>
                  </button>
                </div>
              </div>

              <!-- 消息列表滚动区域 -->
              <div class="overflow-y-auto space-y-2.5 pr-1 text-xs font-mono flex-1">
                <div v-if="filteredInboxAlerts.length === 0" class="text-center py-16 text-slate-500">
                  <AppIcon name="mail" :size="36" class="mx-auto mb-2 opacity-25" />
                  <div>收件箱空空如也，暂无此类记录</div>
                </div>
                <div v-for="a in filteredInboxAlerts" :key="a.id"
                     :class="a.is_read ? 'bg-slate-900/60 border-slate-800/80 text-slate-400' : 'bg-red-500/5 border-red-500/35 text-slate-200 shadow-md'"
                     class="p-3.5 rounded-xl border transition-all relative">
                  <div class="flex items-start justify-between gap-3 mb-2">
                    <div class="flex items-center gap-2 flex-wrap">
                      <span v-if="!a.is_read" class="w-2 h-2 rounded-full bg-red-500 shrink-0"></span>
                      <span v-else class="w-2 h-2 rounded-full bg-slate-600 shrink-0"></span>
                      <span class="font-bold text-slate-100">{{ a.account }}</span>
                      <span :class="getAlertBadgeClass(a.alert_type)" class="px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase">
                        {{ a.alert_type }}
                      </span>
                      <span v-if="a.caller_owner" class="text-slate-400 text-[11px]">
                        归属者: {{ a.caller_owner }} {{ a.caller_key ? '(' + a.caller_key + ')' : '' }}
                      </span>
                    </div>
                    <div class="flex items-center gap-2.5 shrink-0">
                      <span class="text-[11px] text-slate-400">{{ a.time_fmt || '' }}</span>
                      <button v-if="!a.is_read" @click="markAlertRead(a.id)" title="标为已读" class="px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-emerald-400 text-[11px] border border-slate-700 flex items-center gap-1 transition-colors">
                        <AppIcon name="check" :size="11" />
                        <span>标为已读</span>
                      </button>
                    </div>
                  </div>
                  <div class="text-[11.5px] leading-relaxed text-slate-300 break-words pl-3 border-l-2" :class="a.is_read ? 'border-slate-700' : 'border-red-500/60'">
                    {{ a.message }}
                  </div>
                  <div v-if="a.wake_at" class="mt-2 text-[11px] text-amber-400 flex items-center gap-1.5 pl-3">
                    <AppIcon name="clock" :size="12" />
                    <span>预计自动探活/解封时间: {{ a.wake_at }}</span>
                  </div>
                </div>
              </div>

              <!-- 底部操作与关闭 -->
              <div class="flex justify-between items-center pt-3 border-t border-slate-800 shrink-0 text-xs">
                <span class="text-slate-500 text-[11px] font-mono">共 {{ inboxAlerts.length }} 条记录 · 未读 {{ unreadAlertsCount }} 条</span>
                <button @click="inboxModal.show = false" class="px-4 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium border border-slate-700 transition-colors">
                  关闭收件箱
                </button>
              </div>
            </div>
          </div>

      
      <!-- 操纵用户套餐档位与到期时间 弹窗 -->
      <div v-if="manageSubModal.show" class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-md">
        <div class="w-full max-w-xl bg-slate-900 border border-amber-500/40 rounded-2xl p-6 shadow-2xl space-y-5 max-h-[90vh] overflow-y-auto">
          <div class="flex items-center justify-between border-b border-slate-800 pb-3">
            <div class="flex items-center gap-2 text-amber-300 font-bold font-sacred text-base">
              <AppIcon name="crown" :size="20" />
              <span>操纵用户套餐档位与到期时间</span>
            </div>
            <button @click="manageSubModal.show = false" class="text-slate-400 hover:text-slate-200 text-lg">&times;</button>
          </div>

          <div v-if="manageSubModal.user" class="space-y-4 text-xs font-sans">
            <!-- 目标用户标识 -->
            <div class="p-3 rounded-xl bg-slate-950 border border-slate-800 flex items-center justify-between">
              <div>
                <span class="text-slate-400 block text-[11px]">目标用户标识 / 邮箱</span>
                <span class="text-amber-300 font-bold text-sm font-mono">{{ manageSubModal.user.email }}</span>
                <span v-if="manageSubModal.user.username" class="text-slate-400 text-xs ml-2">({{ manageSubModal.user.username }})</span>
              </div>
              <span class="px-2 py-0.5 rounded text-[10px] font-mono font-bold" :class="tierClass(manageSubModal.form.tier)">
                {{ tierName(manageSubModal.form.tier) }}
              </span>
            </div>

            <!-- 1. 选择套餐档位 -->
            <div>
              <label class="block text-slate-300 font-medium mb-1.5">选择商业套餐档位 (Tier)</label>
              <div class="grid grid-cols-2 sm:grid-cols-3 gap-2">
                <button
                  type="button"
                  v-for="t in [
                    { id: 'free', name: '免费版', desc: '1.28亿额度' },
                    { id: 'standard', name: '标准版', desc: '30元/20亿Token' },
                    { id: 'pro', name: '专业版', desc: '60元/50亿Token' },
                    { id: 'ultra', name: '无限尊享版', desc: '无限制调用' },
                    { id: 'legacy', name: '创世终身特权', desc: '永久无限额' },
                  ]"
                  :key="t.id"
                  @click="manageSubModal.form.tier = t.id"
                  class="p-2.5 rounded-xl border text-left transition-all"
                  :class="manageSubModal.form.tier === t.id ? 'bg-amber-500/20 border-amber-500 text-amber-200 shadow' : 'bg-slate-950/80 border-slate-800 text-slate-400 hover:border-slate-700'"
                >
                  <div class="font-bold text-xs">{{ t.name }}</div>
                  <div class="text-[10px] text-slate-500 mt-0.5">{{ t.desc }}</div>
                </button>
              </div>
            </div>

            <!-- 2. 到期时间控制 -->
            <div v-if="manageSubModal.form.tier !== 'free'" class="space-y-2">
              <div class="flex items-center justify-between">
                <label class="text-slate-300 font-medium">订阅到期时间 (到期自动过期回落)</label>
                <span v-if="manageSubModal.user.subscription?.period_end_fmt" class="text-slate-400 text-[11px]">
                  当前到期: <strong class="text-emerald-400">{{ manageSubModal.user.subscription.period_end_fmt }}</strong>
                </span>
              </div>

              <!-- 快捷延期按钮 -->
              <div class="flex flex-wrap gap-1.5">
                <button
                  type="button"
                  v-for="opt in [
                    { label: '+7天', days: 7 },
                    { label: '+15天 (半月)', days: 15 },
                    { label: '+30天 (1个月)', days: 30 },
                    { label: '+90天 (1季度)', days: 90 },
                    { label: '+1年', days: 365 },
                    { label: '永久 (2036年)', days: 'permanent' },
                  ]"
                  :key="opt.label"
                  @click="quickSetExpiry(opt.days)"
                  class="px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-[11px] border border-slate-700"
                >
                  {{ opt.label }}
                </button>
              </div>

              <!-- 精确时间选择 -->
              <input
                v-model="manageSubModal.form.datetime"
                type="datetime-local"
                class="w-full px-3 py-2 rounded-xl bg-slate-950 border border-slate-700 text-slate-100 font-mono text-xs focus:outline-none focus:border-amber-500"
              />
            </div>

            <!-- 3. 专属保供号池自动分配开关 -->
            <div v-if="manageSubModal.form.tier !== 'free'" class="p-3 rounded-xl bg-slate-950 border border-slate-800 space-y-2">
              <label class="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" v-model="manageSubModal.form.auto_allocate_account" class="rounded bg-slate-900 border-slate-700 text-amber-500" />
                <span class="text-slate-200 font-medium text-xs">自动智能优选分配专属保供号 (付钱用户绝不委屈)</span>
              </label>
              <p class="text-[11px] text-slate-500 pl-6">
                若该用户名下专属号不足，系统将自动从活跃号或零 IP 冷库中极速唤醒全新账号并绑定至其个人专属池。
              </p>
              <div v-if="manageSubModal.form.auto_allocate_account" class="pl-6 flex items-center gap-2">
                <span class="text-slate-400 text-xs">期望专属号数量:</span>
                <input
                  v-model.number="manageSubModal.form.account_count"
                  type="number"
                  min="1"
                  max="10"
                  class="w-16 px-2 py-1 rounded bg-slate-900 border border-slate-700 text-slate-200 font-mono text-xs"
                />
                <span class="text-slate-500 text-[10px]">(标准版建议 1 个，专业版建议 2 个)</span>
              </div>
            </div>

            <!-- 4. 额外赠送额度 (可选) -->
            <div>
              <label class="block text-slate-300 font-medium mb-1">额外赠送 Token 额度 (选填)</label>
              <input
                v-model.number="manageSubModal.form.bonus_tokens"
                type="number"
                placeholder="例如 1000000000 (10亿)"
                class="w-full px-3 py-2 rounded-xl bg-slate-950 border border-slate-700 text-slate-100 font-mono text-xs"
              />
            </div>

            <!-- 底部保存 -->
            <div class="flex justify-end gap-3 pt-3 border-t border-slate-800">
              <button
                type="button"
                @click="manageSubModal.show = false"
                class="px-4 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs"
              >
                取消
              </button>
              <button
                type="button"
                @click="submitManageSubModal"
                :disabled="manageSubModal.loading"
                class="px-5 py-2 rounded-xl bg-gradient-to-r from-amber-500 to-amber-600 hover:from-amber-400 hover:to-amber-500 text-slate-950 font-bold text-xs shadow-lg shadow-amber-500/20 disabled:opacity-50"
              >
                {{ manageSubModal.loading ? '正在保存更新...' : '确认并立即生效' }}
              </button>
            </div>
          </div>
        </div>
      </div>


      <!-- 铸造与批发零售卡密 (单张选号 / 批量批发) 弹窗 -->
      <div v-if="cardKeyModal.show" class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-md">
        <div class="w-full max-w-3xl bg-slate-900 border border-amber-500/40 rounded-2xl p-6 shadow-2xl space-y-4 max-h-[92vh] overflow-y-auto">
          <!-- 弹窗 Header -->
          <div class="flex items-center justify-between border-b border-slate-800 pb-3">
            <div class="flex items-center gap-2 text-amber-300 font-bold font-sacred text-base">
              <AppIcon name="receipt" :size="20" />
              <span>💳 铸造零售卡密 (即插即用 / 选号独占 / 到期自动回收)</span>
            </div>
            <button @click="cardKeyModal.show = false" class="text-slate-400 hover:text-slate-200 text-lg">&times;</button>
          </div>

          <!-- Tab 切换: 单张铸造 (选号/绑号) vs 批量备用 -->
          <div class="flex items-center gap-2 border-b border-slate-800/80 pb-2">
            <button
              type="button"
              @click="cardKeyModal.tab = 'single'"
              :class="cardKeyModal.tab === 'single' ? 'bg-amber-500/20 text-amber-300 border-amber-500/50 font-bold' : 'text-slate-400 hover:text-slate-200 border-transparent hover:bg-slate-800/50'"
              class="px-3.5 py-1.5 rounded-lg text-xs border transition-all flex items-center gap-1.5"
            >
              <span>🎯 铸造单张卡密 (支持选号/绑号)</span>
            </button>
            <button
              type="button"
              @click="cardKeyModal.tab = 'batch'"
              :class="cardKeyModal.tab === 'batch' ? 'bg-amber-500/20 text-amber-300 border-amber-500/50 font-bold' : 'text-slate-400 hover:text-slate-200 border-transparent hover:bg-slate-800/50'"
              class="px-3.5 py-1.5 rounded-lg text-xs border transition-all flex items-center gap-1.5"
            >
              <span>📦 批量快速铸造 (备用批次)</span>
            </button>
          </div>

          <!-- ══════ Tab 1: 单张铸造卡密 (选号/绑号) ══════ -->
          <div v-if="cardKeyModal.tab === 'single'">
            <!-- 未生成状态: 表单录入与选号 -->
            <div v-if="!cardKeyModal.generatedResult" class="space-y-4 text-xs">
              <div class="grid grid-cols-1 sm:grid-cols-2 gap-3.5">
                <div>
                  <label class="block text-slate-300 mb-1 font-medium">卡密备注名 / 标识 <span class="text-amber-400">*</span></label>
                  <input v-model="cardKeyModal.name" type="text" placeholder="例如: 周卡-买家小李 / 淘宝订单123" class="w-full px-3 py-2 rounded-xl bg-slate-950 border border-slate-700 text-slate-100 font-sans" />
                </div>

                <div>
                  <label class="block text-slate-300 mb-1 font-medium">买家说明 / 归属人 (可选)</label>
                  <input v-model="cardKeyModal.owner" type="text" placeholder="例如: 淘宝客户 / 微信好友" class="w-full px-3 py-2 rounded-xl bg-slate-950 border border-slate-700 text-slate-100 font-sans" />
                </div>

                <div>
                  <label class="block text-slate-300 mb-1 font-medium">时长规格类型</label>
                  <select v-model="cardKeyModal.category" class="w-full px-3 py-2 rounded-xl bg-slate-950 border border-slate-700 text-slate-100 font-sans">
                    <option value="daily">天卡 (1 天)</option>
                    <option value="weekly">周卡 (7 天)</option>
                    <option value="monthly">月卡 (30 天)</option>
                    <option value="quarterly">季卡 (90 天)</option>
                    <option value="forever">永久卡 (不限时)</option>
                    <option value="custom">自定义天数</option>
                  </select>
                </div>

                <div v-if="cardKeyModal.category === 'custom'">
                  <label class="block text-slate-300 mb-1 font-medium">自定义天数 (1~365 天)</label>
                  <input v-model.number="cardKeyModal.days" type="number" min="1" max="365" class="w-full px-3 py-2 rounded-xl bg-slate-950 border border-slate-700 text-slate-100" />
                </div>
              </div>

              <!-- 选号核心区域 (拒绝 textarea，直接可勾选卡片列表) -->
              <div class="p-3.5 rounded-xl bg-slate-950/70 border border-slate-800 space-y-3">
                <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                  <div>
                    <div class="font-bold text-amber-300 flex items-center gap-1.5">
                      <span>🎯 选号与绑定独占账号</span>
                      <span class="px-2 py-0.5 rounded-full text-[10px] bg-purple-500/20 text-purple-300 font-mono font-bold">
                        已选 {{ cardKeyModal.selectedLabels.length }} 个号
                      </span>
                    </div>
                    <div class="text-[11px] text-slate-400 mt-0.5">
                      不勾选号时将走高速公共卡池；如需提供独立独占保障，建议勾选 1~2 个号。后续可随时在列表操作栏点击【🎯 选号/补号】调优。
                    </div>
                  </div>

                  <!-- 快捷选号操作 -->
                  <div class="flex items-center gap-1.5 flex-wrap">
                    <button type="button" @click="quickSelectCardKey(1)" class="px-2.5 py-1 rounded bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 border border-amber-500/30 text-[11px] font-medium">
                      +选1个空闲号
                    </button>
                    <button type="button" @click="quickSelectCardKey(2)" class="px-2.5 py-1 rounded bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 border border-amber-500/30 text-[11px] font-medium">
                      +选2个空闲号
                    </button>
                    <button v-if="cardKeyModal.selectedLabels.length" type="button" @click="cardKeyModal.selectedLabels = []" class="px-2 py-1 rounded bg-red-500/10 hover:bg-red-500/20 text-red-300 border border-red-500/20 text-[11px]">
                      清空
                    </button>
                  </div>
                </div>

                <!-- 账号过滤栏 -->
                <div class="flex items-center gap-2">
                  <div class="relative flex-1">
                    <input
                      v-model="cardKeyModal.searchQuery"
                      type="text"
                      placeholder="搜索账号邮箱或标识..."
                      class="w-full pl-8 pr-3 py-1.5 rounded-lg bg-slate-900 border border-slate-700 text-slate-100 text-xs placeholder-slate-500"
                    />
                    <span class="absolute left-2.5 top-2 text-slate-500"><AppIcon name="search" :size="12" /></span>
                  </div>
                  <label class="flex items-center gap-1.5 text-xs text-slate-300 cursor-pointer select-none">
                    <input type="checkbox" v-model="cardKeyModal.onlyActive" class="rounded border-slate-700 bg-slate-900 text-amber-500 focus:ring-0" />
                    <span>仅看空闲 ACTIVE</span>
                  </label>
                </div>

                <!-- 可勾选账号网格列表 -->
                <div class="max-h-52 overflow-y-auto pr-1 grid grid-cols-1 sm:grid-cols-2 gap-2 border border-slate-800/80 rounded-lg p-2 bg-slate-900/40">
                  <div
                    v-for="acc in filteredCardKeyAccounts"
                    :key="acc.account_label"
                    @click="toggleCardKeyAccount(acc)"
                    :class="[
                      cardKeyModal.selectedLabels.includes(acc.account_label)
                        ? 'border-amber-500/80 bg-amber-500/10 shadow-sm shadow-amber-500/10'
                        : (acc.takenByOther ? 'border-slate-800 bg-slate-900/30 opacity-60' : 'border-slate-800 bg-slate-900/60 hover:border-slate-700 hover:bg-slate-900')
                    ]"
                    class="p-2 rounded-lg border flex items-center justify-between gap-2 cursor-pointer transition-all select-none text-xs"
                  >
                    <div class="flex items-center gap-2 truncate min-w-0">
                      <input
                        type="checkbox"
                        :checked="cardKeyModal.selectedLabels.includes(acc.account_label)"
                        class="rounded border-slate-700 bg-slate-800 text-amber-500 focus:ring-0 shrink-0 pointer-events-none"
                      />
                      <span class="font-mono text-slate-200 truncate" :title="acc.account_label">{{ acc.account_label }}</span>
                    </div>

                    <div class="flex items-center gap-1 shrink-0">
                      <span v-if="acc.takenByOther" class="px-1.5 py-0.5 rounded text-[10px] bg-purple-500/20 text-purple-300 border border-purple-500/30" :title="'已有独占: ' + (acc.owner || '其他Key')">
                        已独占
                      </span>
                      <span v-else-if="acc.status === 'ACTIVE'" class="px-1.5 py-0.5 rounded text-[10px] bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 font-bold">
                        ACTIVE
                      </span>
                      <span v-else class="px-1.5 py-0.5 rounded text-[10px] bg-slate-700 text-slate-400">
                        {{ acc.status || 'COLD' }}
                      </span>
                    </div>
                  </div>

                  <div v-if="!filteredCardKeyAccounts || filteredCardKeyAccounts.length === 0" class="col-span-full py-6 text-center text-slate-500">
                    未找到匹配的可用账号
                  </div>
                </div>
              </div>

              <!-- 提交按钮 -->
              <div class="flex justify-end gap-3 pt-2 border-t border-slate-800">
                <button type="button" @click="cardKeyModal.show = false" class="px-4 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300">取消</button>
                <button
                  type="button"
                  @click="submitCreateCardKey"
                  :disabled="cardKeyModal.loading"
                  class="px-5 py-2 rounded-xl bg-gradient-to-r from-amber-500 to-amber-600 hover:from-amber-400 hover:to-amber-500 disabled:opacity-50 text-slate-950 font-bold shadow-lg shadow-amber-500/20 flex items-center gap-2"
                >
                  <AppIcon name="receipt" :size="14" />
                  <span>{{ cardKeyModal.loading ? '正在铸造卡密...' : '立即铸造新卡密 (选号并绑定)' }}</span>
                </button>
              </div>
            </div>

            <!-- 生成成功状态: 密文与买家交付卡 -->
            <div v-else class="space-y-4 text-xs">
              <div class="p-3 bg-emerald-500/10 border border-emerald-500/30 rounded-xl text-emerald-400 flex items-center justify-between">
                <span>🎉 卡密铸造成功！已绑定独占保障，已设好到期自动释放策略。</span>
                <button type="button" @click="cardKeyModal.generatedResult = null" class="text-xs text-amber-400 underline font-bold">➕ 继续铸造新卡</button>
              </div>

              <div class="space-y-3">
                <!-- 单独 Key 密文 -->
                <div>
                  <div class="flex items-center justify-between mb-1">
                    <span class="text-slate-300 font-medium">卡密 API Key (明文仅展示一次):</span>
                    <button type="button" @click="copyGeneratedCardKey" class="px-2.5 py-1 rounded bg-amber-500/20 text-amber-300 hover:bg-amber-500/30 border border-amber-500/30 text-[11px] font-medium flex items-center gap-1">
                      <AppIcon name="copy" :size="12" />
                      <span>{{ cardKeyModal.keyCopied ? '已复制 Key' : '一键复制 Key' }}</span>
                    </button>
                  </div>
                  <div class="p-2.5 bg-slate-950 rounded-xl border border-slate-800 font-mono text-cyan-300 text-xs select-all break-all">
                    {{ cardKeyModal.generatedResult.key }}
                  </div>
                </div>

                <!-- 买家交付卡文本 -->
                <div>
                  <div class="flex items-center justify-between mb-1">
                    <span class="text-slate-300 font-medium">📋 买家发货交付文本 (格式化一键发送买家):</span>
                    <button type="button" @click="copyCardDeliveryText" class="px-3 py-1 rounded-lg bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold shadow flex items-center gap-1.5">
                      <AppIcon name="copy" :size="12" />
                      <span>{{ cardKeyModal.deliveryCopied ? '已复制交付文本' : '一键复制交付信息' }}</span>
                    </button>
                  </div>
                  <textarea
                    readonly
                    :value="cardKeyModal.deliveryText"
                    rows="8"
                    class="w-full p-3 rounded-xl bg-slate-950 border border-slate-800 text-amber-300 font-mono text-[11px] select-all leading-relaxed"
                  ></textarea>
                </div>
              </div>

              <div class="flex justify-end gap-3 pt-2 border-t border-slate-800">
                <button type="button" @click="cardKeyModal.generatedResult = null" class="px-4 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300">
                  继续铸造下一张
                </button>
                <button type="button" @click="cardKeyModal.show = false" class="px-5 py-2 rounded-xl bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold">
                  完成并关闭
                </button>
              </div>
            </div>
          </div>

          <!-- ══════ Tab 2: 批量快速铸造 (备用批次) ══════ -->
          <div v-else-if="cardKeyModal.tab === 'batch'">
            <div v-if="!batchCardResult" class="space-y-4 text-xs">
              <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <label class="block text-slate-300 mb-1 font-medium">卡密规格类型</label>
                  <select v-model="batchCardForm.category" class="w-full px-3 py-2 rounded-xl bg-slate-950 border border-slate-700 text-slate-100 font-sans">
                    <option value="daily">天卡 (1 天)</option>
                    <option value="weekly">周卡 (7 天)</option>
                    <option value="monthly">月卡 (30 天)</option>
                    <option value="custom">自定义天数</option>
                  </select>
                </div>

                <div v-if="batchCardForm.category === 'custom'">
                  <label class="block text-slate-300 mb-1 font-medium">自定义天数 (1~365 天)</label>
                  <input v-model.number="batchCardForm.days" type="number" min="1" max="365" class="w-full px-3 py-2 rounded-xl bg-slate-950 border border-slate-700 text-slate-100" />
                </div>

                <div>
                  <label class="block text-slate-300 mb-1 font-medium">生成卡密数量 (1~50 张)</label>
                  <input v-model.number="batchCardForm.count" type="number" min="1" max="50" class="w-full px-3 py-2 rounded-xl bg-slate-950 border border-slate-700 text-slate-100" />
                </div>

                <div>
                  <label class="block text-slate-300 mb-1 font-medium">批次名称 / 备注说明</label>
                  <input v-model="batchCardForm.batch_remark" type="text" placeholder="例如: 淘宝批次A" class="w-full px-3 py-2 rounded-xl bg-slate-950 border border-slate-700 text-slate-100 font-sans" />
                </div>

                <div>
                  <label class="block text-slate-300 mb-1 font-medium">号池调度模式</label>
                  <select v-model="batchCardForm.mode" class="w-full px-3 py-2 rounded-xl bg-slate-950 border border-slate-700 text-slate-100 font-sans">
                    <option value="shared">高速公共卡池 (推荐 / 负载均衡)</option>
                    <option value="dedicated">专属独占号 (每卡分配 1 个独立账号)</option>
                  </select>
                </div>
              </div>

              <div class="flex justify-end gap-3 pt-3 border-t border-slate-800">
                <button type="button" @click="cardKeyModal.show = false" class="px-4 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300">取消</button>
                <button type="button" @click="submitBatchCards" :disabled="batchCardLoading" class="px-5 py-2 rounded-xl bg-amber-500 hover:bg-amber-400 disabled:opacity-50 text-slate-950 font-bold shadow-lg shadow-amber-500/20">
                  {{ batchCardLoading ? '正在批量铸造...' : '立即一键批量生成卡密' }}
                </button>
              </div>
            </div>

            <!-- 批量结果与一键复制 -->
            <div v-else class="space-y-4 text-xs">
              <div class="p-3 bg-emerald-500/10 border border-emerald-500/30 rounded-xl text-emerald-400 flex items-center justify-between">
                <span>🎉 成功铸造 {{ batchCardResult.count }} 张卡密！已设好到期自动释放策略。</span>
                <button type="button" @click="batchCardResult = null" class="text-xs text-amber-400 underline">继续批量生成</button>
              </div>

              <div>
                <div class="flex items-center justify-between mb-1.5">
                  <span class="text-slate-300 font-medium">卡密导出文本 (可直接一行一条发给客户):</span>
                  <button type="button" @click="copyBatchCards" class="px-3 py-1 rounded-lg bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold shadow flex items-center gap-1.5">
                    <AppIcon name="copy" :size="12" />
                    <span>一键复制全部卡密</span>
                  </button>
                </div>
                <textarea readonly :value="batchCardResult.bundle_text" rows="8" class="w-full p-3 rounded-xl bg-slate-950 border border-slate-800 text-amber-300 font-mono text-[11px] select-all leading-relaxed"></textarea>
              </div>

              <div class="flex justify-end">
                <button type="button" @click="cardKeyModal.show = false" class="px-5 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200">关闭窗口</button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
</template>

<script>
import { ref, reactive, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { authState, apiRequest, notify, toasts } from '../../api.js'

export default {
  name: 'AdminMasterView',
  setup() {
    const router = useRouter()
    const route = useRoute()


        const checkingAll = ref(false);
        const lastUpdated = ref(new Date().toLocaleTimeString('zh-CN'));

        // 7 大圣咏 Tab (纯 SVG Icon 映射)
        const tabs = [
          { id: 'accounts', label: '圣殿号池', icon: 'server' },
          { id: 'usage',    label: '祭坛调用统计', icon: 'chart' },
          { id: 'apiKeys',  label: '圣钥权限 (API Keys)', icon: 'key' },
          { id: 'opsLogs',  label: '运维监控与日志', icon: 'terminal' },
          { id: 'register', label: '一键单号快注', icon: 'bolt' },
          { id: 'login',    label: '纳新入池 (添加账号)', icon: 'upload' },
          { id: 'settings', label: '圣殿枢纽设置', icon: 'settings' }
        ];

        const activeTab = computed(() => {
          const p = route.path;
          if (p.includes('/admin/usage')) return 'usage';
          if (p.includes('/admin/keys')) return 'apiKeys';
          if (p.includes('/admin/ops') || p.includes('/admin/logs')) return 'opsLogs';
          if (p.includes('/admin/register')) return 'register';
          if (p.includes('/admin/login') || p.includes('/admin/add')) return 'login';
          if (p.includes('/admin/settings')) return 'settings';
          return 'accounts';
        });

        const switchTab = (tId) => {
          const tabRouteMap = {
            accounts: '/admin/accounts',
            usage: '/admin/usage',
            apiKeys: '/admin/keys',
            opsLogs: '/admin/ops',
            register: '/admin/register',
            login: '/admin/add',
            settings: '/admin/settings'
          };
          router.push(tabRouteMap[tId] || '/admin/accounts');
        };

        const stats = reactive({
          valid: 0,
          standby: 0,
          cold: 0,
          dated: 0,
          quarantine: 0,
          accounts: 0,
          db_engine: 'SQLite',
          db_mode: 'WAL',
          db_size: 'WAL',
          today_requests: 0
        });

        // ── Tab 1: 圣殿号池 & 冷库唤醒进度跟踪 ──
        const accounts = ref([]);
        // ★ 纯 HTTP 续杯健康态：区分【已续杯】【HTTP续杯就绪】【一次性账号】
        const reloginHealth = ref({ ip: {}, accounts: {}, legend: {} });
        const reloginBadgeClass = (r) => {
          const lv = (r && r.relogin_level) || 'idle';
          if (lv === 'success') return 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40';
          if (lv === 'info') return 'bg-cyan-500/15 text-cyan-300 border-cyan-500/40';
          if (lv === 'warn') return 'bg-amber-500/15 text-amber-300 border-amber-500/40';
          if (lv === 'danger') return 'bg-orange-500/15 text-orange-300 border-orange-500/40';
          if (lv === 'banned') return 'bg-red-500/20 text-red-300 border-red-500/40';
          return 'bg-slate-700/40 text-slate-300 border-slate-600/40';
        };
        const alerts = ref([]);
        const accFilter = ref('all');
        const accSearch = ref('');
        const wakeStatus = reactive({ running: false, total: 0, current: 0, success_count: 0, fail_count: 0, last_account: '' });
        let wakePollTimer = null;

        const pollWakeStatus = async () => {
          try {
            const ws = await apiRequest('/api/accounts/wake_status');
            Object.assign(wakeStatus, ws || {});
            if (ws && ws.running) {
              if (!wakePollTimer) {
                wakePollTimer = setInterval(async () => {
                  const nextWs = await apiRequest('/api/accounts/wake_status').catch(() => ({}));
                  Object.assign(wakeStatus, nextWs);
                  await loadStats();
                  if (activeTab.value === 'accounts') await loadAccounts();
                  if (!nextWs.running) {
                    clearInterval(wakePollTimer);
                    wakePollTimer = null;
                    notify('冷库批量唤醒任务已完成', 'success');
                  }
                }, 3000);
              }
            }
          } catch (e) {}
        };

        const standbyAccounts = computed(() => accounts.value.filter(a => a.status === 'STANDBY'));
        const coldAccounts = computed(() => accounts.value.filter(a => a.status === 'INACTIVE_COLD'));
        const activeAccounts = computed(() => accounts.value.filter(a => a.status === 'ACTIVE' || a.status === 'WARMING' || a.status === 'BUSY' || a.status === 'COOLDOWN'));
        const quarantineAccounts = computed(() => accounts.value.filter(a => (a.status && a.status.includes('QUARANTINE')) || a.status === 'MUTED' || a.muted));

        const filteredAccounts = computed(() => {
          let list = accounts.value;
          if (accFilter.value === 'active') list = activeAccounts.value;
          else if (accFilter.value === 'cold') list = coldAccounts.value;
          else if (accFilter.value === 'quarantine') list = quarantineAccounts.value;

          const q = accSearch.value.trim().toLowerCase();
          if (!q) return list;
          return list.filter(a => (a.account || a.account_label || '').toLowerCase().includes(q) || (a.owner || '').toLowerCase().includes(q));
        });

        const getStatusBadgeClass = (r) => {
          if (r.status === 'INACTIVE_COLD') return 'text-cyan-400 bg-cyan-500/10 border-cyan-500/30';
          if (r.is_dated_ban || r.ban_kind === 'dated') return 'text-yellow-400 bg-yellow-500/10 border-yellow-500/30';
          if ((r.status && r.status.includes('QUARANTINE')) || r.status === 'MUTED' || r.muted) return 'text-red-400 bg-red-500/10 border-red-500/30';
          if (r.status === 'WARMING') return 'text-yellow-400 bg-yellow-500/10 border-yellow-500/30';
          if (r.status === 'BUSY' || r.is_busy) return 'text-blue-400 bg-blue-500/10 border-blue-500/30';
          if (r.status === 'COOLDOWN') return 'text-amber-400 bg-amber-500/10 border-amber-500/30';
          if (!r.is_valid) return 'text-slate-400 bg-slate-500/10 border-slate-500/30';
          return 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30';
        };

        const getStatusText = (r) => {
          if (r.status === 'INACTIVE_COLD') return '零IP冷库';
          if (r.is_dated_ban || r.ban_kind === 'dated') return '有期禁言';
          if ((r.status && r.status.includes('QUARANTINE')) || r.status === 'MUTED' || r.muted) return '垃圾库隔离';
          if (r.status === 'WARMING') return '拟真养号';
          if (r.status === 'BUSY' || r.is_busy) return '推理承载中';
          if (r.status === 'COOLDOWN') return '冷却保护';
          if (!r.is_valid) return '凭证失效';
          return 'ACTIVE 就绪';
        };

        const loadStats = async () => {
          try {
            const [h, u, dbInfo] = await Promise.all([
              apiRequest('/health').catch(() => ({})),
              apiRequest('/api/usage').catch(() => ({})),
              apiRequest('/api/system/db_status').catch(() => ({}))
            ]);
            stats.valid = h.valid || 0;
            stats.standby = h.standby || 0;
            stats.cold = h.cold || 0;
            stats.dated = h.dated || 0;
            stats.quarantine = h.quarantine || 0;
            stats.accounts = h.accounts || 0;
            stats.today_requests = u.total_requests || 0;
            if (dbInfo && dbInfo.engine) {
              stats.db_engine = dbInfo.engine;
              stats.db_mode = dbInfo.mode || 'WAL';
              stats.db_size = dbInfo.size_formatted || '';
            }
          } catch (e) { console.error('loadStats error:', e); }
        };

        const loadAccounts = async () => {
          try {
            const res = await apiRequest('/api/accounts/health');
            accounts.value = res.results || res.accounts || [];
            reloginHealth.value = res.relogin_health || { ip: {}, accounts: {}, legend: {} };
            alerts.value = res.alerts || [];
            if (typeof res.unread_alerts_count === 'number') {
              unreadAlertsCount.value = res.unread_alerts_count;
            }
            lastUpdated.value = new Date().toLocaleTimeString('zh-CN');
          } catch (e) { console.error('loadAccounts error:', e); }
        };

        const checkAllAccounts = async () => {
          if (!confirm('确定对圣殿号池内的全部活跃账号进行无痕静默探活？')) return;
          checkingAll.value = true;
          try {
            const res = await apiRequest('/api/accounts/check', { method: 'POST', body: JSON.stringify({}) });
            const results = res.results || [];
            const activeCount = results.filter(r => r.status === 'ACTIVE' && r.is_valid && !r.is_muted).length;
            const coldCount = results.filter(r => r.status === 'INACTIVE_COLD').length;
            const mutedCount = results.filter(r => r.is_muted || (r.status && r.status.includes('MUTED'))).length;
            notify(`全池探活完毕：活跃 ${activeCount} 个，冷库 ${coldCount} 个，隔离 ${mutedCount} 个`, 'success', 4500);
            await loadStats();
            await loadAccounts();
          } catch (e) { notify('探活失败: ' + e.message, 'error'); }
          finally { checkingAll.value = false; }
        };

        const probeAccount = async (acc) => {
          try {
            const res = await apiRequest('/api/accounts/check', { method: 'POST', body: JSON.stringify({ account: acc }) });
            const info = (res.results && res.results[0]) ? res.results[0] : {};
            notify(`${acc} 探活结果: ${info.msg || (info.is_valid ? '正常可用' : '失效')}`, info.is_valid ? 'success' : 'warn');
            await loadStats();
            await loadAccounts();
          } catch (e) { notify('探活失败: ' + e.message, 'error'); }
        };

        const reloginAccount = async (acc) => {
          notify(`正在为账号 ${acc} 执行纯 HTTP 极速续杯...`, 'info');
          try {
            const res = await apiRequest('/api/accounts/relogin', { method: 'POST', body: JSON.stringify({ account: acc }) });
            const r = (res.results || [])[0];
            if (r && r.ok) notify(`账号 ${acc} 纯 HTTP 续杯成功，已恢复活跃`, 'success');
            else notify(res.message || (r && r.msg) || 'HTTP 续杯已执行', 'info');
            await loadStats();
            await loadAccounts();
          } catch (e) { notify('续杯失败: ' + e.message, 'error'); }
        };

        const reloginAllAccounts = async () => {
          if (!confirm('确定对所有失效/隔离账号批量执行纯 HTTP 极速续杯刷新 Token？')) return;
          notify('正在执行批量纯 HTTP 续杯，请稍候...', 'info');
          try {
            const res = await apiRequest('/api/accounts/relogin', { method: 'POST', body: JSON.stringify({}) });
            const list = res.results || [];
            const okCnt = list.filter(x => x.ok).length;
            notify(`批量续杯完毕：成功 ${okCnt} 个 / 共 ${list.length} 个`, okCnt > 0 ? 'success' : 'warn');
            await loadStats();
            await loadAccounts();
          } catch (e) { notify('批量续杯失败: ' + e.message, 'error'); }
        };

        const warmupAccount = async (acc) => {
          if (!confirm(`确定为账号【${acc}】启动拟真养号流程？`)) return;
          try {
            const res = await apiRequest('/api/accounts/warmup', { method: 'POST', body: JSON.stringify({ account: acc }) });
            notify(res.message || '已启动拟真养号任务', 'success');
            await loadAccounts();
          } catch (e) { notify('养号启动失败: ' + e.message, 'error'); }
        };

        const activateAccount = async (acc) => {
          if (!confirm(`确定唤醒并激活冷库账号「${acc}」？`)) return;
          try {
            const res = await apiRequest('/api/accounts/activate', { method: 'POST', body: JSON.stringify({ account: acc }) });
            notify(res.message || '激活成功', 'success');
            await loadStats();
            await loadAccounts();
          } catch (e) { notify('激活失败: ' + e.message, 'error'); }
        };

        const toggleDisposable = async (row) => {
          const acc = row.account || row.account_label;
          const targetDisposable = !row.is_disposable;
          const targetTag = targetDisposable ? 'disposable' : 'active';
          const msg = targetDisposable
            ? `确定将账号「${acc}」标记为【一次性账号】？\n\n标记后该账号将严禁后台自动滑动续杯，仅在当前 Token 寿命内消耗。`
            : `确定恢复账号「${acc}」为【可无限续杯账号】？\n\n恢复后系统将在后台自动为其执行 180 天滑动续签。`;
          if (!confirm(msg)) return;
          try {
            await apiRequest(`/api/admin/accounts/${encodeURIComponent(acc)}/pool-tag`, {
              method: 'PUT',
              body: JSON.stringify({ pool_tag: targetTag, is_disposable: targetDisposable })
            });
            notify(targetDisposable ? `账号 ${acc} 已标为一次性，自动续杯已撤回` : `账号 ${acc} 自动续杯机制已恢复`, 'success');
            await loadAccounts();
          } catch (e) {
            notify('操作失败: ' + e.message, 'error');
          }
        };

        const demoteAccount = async (acc) => {
          if (!confirm(`确定将账号「${acc}」退入零IP冷库？\n\nToken 将被立即注销清空，账号转为静默冷储备，随时可用 5 件套免密极速唤醒。`)) return;
          try {
            const res = await apiRequest('/api/accounts/demote_cold', { method: 'POST', body: JSON.stringify({ account: acc }) });
            notify(res.message || '已退入零IP冷库', 'info');
            await loadStats();
            await loadAccounts();
          } catch (e) { notify('操作失败: ' + e.message, 'error'); }
        };

        const rehabAccount = async (acc) => {
          if (!confirm(`确定将隔离账号「${acc}」解除隔离并退入零IP冷库备用？`)) return;
          try {
            const res = await apiRequest('/api/accounts/quarantine_rehab', { method: 'POST', body: JSON.stringify({ account: acc }) });
            notify(res.message || '已解除隔离回冷库', 'info');
            await loadStats();
            await loadAccounts();
          } catch (e) { notify('操作失败: ' + e.message, 'error'); }
        };

        const clearQuarantine = async () => {
          if (!confirm('确定一键彻底清空所有风控隔离/垃圾账号吗？该操作不可撤销。')) return;
          try {
            const res = await apiRequest('/api/accounts/quarantine_clear', { method: 'POST' });
            notify(res.message || '垃圾库已清空', 'success');
            await loadStats();
            await loadAccounts();
          } catch (e) { notify('清空失败: ' + e.message, 'error'); }
        };

        const deleteAccount = async (acc) => {
          if (!confirm(`确定永久将账号【${acc}】剔除出圣殿号池？`)) return;
          try {
            await apiRequest('/api/accounts/delete', { method: 'POST', body: JSON.stringify({ account: acc }) });
            notify(`账号 ${acc} 已移除`, 'info');
            await loadStats();
            await loadAccounts();
          } catch (e) { notify('删除失败: ' + e.message, 'error'); }
        };

        // ── Tab 2: 祭坛调用统计 ──
        const usageSummary = ref({});
        const byKeyList = ref([]);
        const revokedByKeyList = ref([]);
        const recentRecords = computed(() => usageSummary.value.recent_records || []);

        const loadUsage = async () => {
          try {
            const [u, kb] = await Promise.all([
              apiRequest('/api/usage').catch(() => ({})),
              apiRequest('/api/usage/by-key').catch(() => ({}))
            ]);
            usageSummary.value = u || {};
            byKeyList.value = (kb && kb.keys) ? kb.keys : [];
            revokedByKeyList.value = (kb && kb.revoked_keys) ? kb.revoked_keys : [];
          } catch (e) { console.error('loadUsage error:', e); }
        };

        const resetKeyUsage = async (keyId, who) => {
          if (!confirm(`确定清零「${who}」的今日计数？累计用量与历史不受影响。`)) return;
          try {
            await apiRequest('/api/usage/reset', { method: 'POST', body: JSON.stringify({ key_id: keyId }) });
            notify('今日计数已清零', 'success');
            await loadUsage();
          } catch (e) { notify('清零失败: ' + e.message, 'error'); }
        };

        const pruneRevokedUsage = async () => {
          if (!confirm('确定彻底清理已作废圣钥的历史用量记录？')) return;
          try {
            const res = await apiRequest('/api/usage/prune', { method: 'POST' });
            notify(`已清理 ${res.removed || 0} 条作废圣钥历史记录`, 'success');
            await loadUsage();
          } catch (e) { notify('清理失败: ' + e.message, 'error'); }
        };

        // ── Tab 3: 圣钥权限 ──
        const keysList = ref([]);
        const ownershipSummary = ref(null);
        const keyUsageMap = reactive({});
        const userPoolsList = ref([]);
        const keySubTab = ref('cardKeys');

        const activeKeys = computed(() => keysList.value.filter(k => !k.revoked));
        const cardKeysList = computed(() => activeKeys.value.filter(k => k.key_type === 'card' || k.is_card));
        const favorKeysList = computed(() => activeKeys.value.filter(k => !k.user_id && k.key_type !== 'card' && !k.is_card));
        const userKeysList = computed(() => activeKeys.value.filter(k => !!k.user_id));

        const userPoolModal = reactive({
          show: false,
          user: null,
          selectedLabels: [],
          autoReplenish: true,
          searchQuery: '',
          onlyActive: true,
          allAccounts: [],
        });

        const openEditUserPool = async (u) => {
          userPoolModal.user = u;
          userPoolModal.selectedLabels = [...(u.personal_pool || [])];
          userPoolModal.autoReplenish = u.auto_replenish !== false;
          userPoolModal.searchQuery = '';
          userPoolModal.onlyActive = true;
          userPoolModal.show = true;
          try {
            const od = await apiRequest('/api/accounts/ownership');
            userPoolModal.allAccounts = (od.accounts || []).map(a => ({
              account_label: a.account_label,
              status: a.status || (a.is_valid ? 'ACTIVE' : 'MUTED'),
              is_valid: a.is_valid,
              owner: a.owner,
              takenByOther: !!(a.owner_key_id && (!u.keys || !u.keys.some(k => k.id === a.owner_key_id)))
            }));
          } catch (err) {
            userPoolModal.allAccounts = (accounts.value || []).map(a => ({
              account_label: a.account_label,
              status: a.status || (a.is_valid ? 'ACTIVE' : 'MUTED'),
              is_valid: a.is_valid,
              owner: '',
              takenByOther: false
            }));
          }
        };

        const toggleUserPoolAccount = (label) => {
          const idx = userPoolModal.selectedLabels.indexOf(label);
          if (idx > -1) {
            userPoolModal.selectedLabels.splice(idx, 1);
          } else {
            userPoolModal.selectedLabels.push(label);
          }
        };

        const quickSelectUserPool = (count) => {
          const currentSet = new Set(userPoolModal.selectedLabels);
          let added = 0;
          for (const acc of userPoolModal.allAccounts) {
            if (added >= count) break;
            const isAct = acc.status === 'ACTIVE' || (acc.is_valid && acc.status !== 'BANNED' && acc.status !== 'INACTIVE_COLD');
            if (isAct && !acc.takenByOther && !currentSet.has(acc.account_label)) {
              userPoolModal.selectedLabels.push(acc.account_label);
              currentSet.add(acc.account_label);
              added++;
            }
          }
        };

        const filteredUserPoolAccounts = computed(() => {
          let list = userPoolModal.allAccounts || [];
          if (userPoolModal.onlyActive) {
            list = list.filter(a => a.status === 'ACTIVE' || userPoolModal.selectedLabels.includes(a.account_label));
          }
          if (userPoolModal.searchQuery && userPoolModal.searchQuery.trim()) {
            const q = userPoolModal.searchQuery.trim().toLowerCase();
            list = list.filter(a => a.account_label.toLowerCase().includes(q));
          }
          return list;
        });

        const saveUserPoolModal = async () => {
          if (!userPoolModal.user) return;
          try {
            await apiRequest(`/api/admin/user-pools/${userPoolModal.user.id}`, {
              method: 'PUT',
              body: JSON.stringify({
                account_labels: userPoolModal.selectedLabels,
                auto_replenish: userPoolModal.autoReplenish,
              }),
            });
            notify('用户专属个人池已更新并即刻全局生效', 'success');
            userPoolModal.show = false;
            await loadKeys();
          } catch (err) {
            notify(err.message || '更新个人池失败', 'error');
          }
        };

        const triggerUserReplenish = async (u) => {
          try {
            const res = await apiRequest(`/api/admin/user-pools/${u.id}/replenish`, { method: 'POST' });
            notify(`已为用户「${u.email}」成功热替换补位账号: ${res.replenished_account}`, 'success');
            await loadKeys();
          } catch (err) {
            notify(err.message || '一键补位失败', 'error');
          }
        };

        const loadKeys = async () => {
          try {
            const [kd, od, ub, up] = await Promise.all([
              apiRequest('/api/keys').catch(() => []),
              apiRequest('/api/accounts/ownership').catch(() => null),
              apiRequest('/api/usage/by-key').catch(() => null),
              apiRequest('/api/admin/user-pools').catch(() => ({ user_pools: [] })),
            ]);
            keysList.value = Array.isArray(kd) ? kd : (Array.isArray(kd.keys) ? kd.keys : []);
            userPoolsList.value = up.user_pools || [];
            if (od && od.summary) ownershipSummary.value = od.summary;
            if (ub && ub.keys) {
              ub.keys.forEach(x => { keyUsageMap[x.id] = x; });
            }
          } catch (e) { console.error('loadKeys error:', e); }
        };

        const toggleKeyFallback = async (k) => {
          const newVal = !k.pool_fallback;
          try {
            await apiRequest(`/api/keys/${k.id}`, { method: 'PUT', body: JSON.stringify({ pool_fallback: newVal }) });
            k.pool_fallback = newVal;
            notify(newVal ? '已开启公共池兜底' : '已设为严格独占模式', 'info');
          } catch (e) { notify('更新失败: ' + e.message, 'error'); }
        };

        const toggleKeySensitive = async (k) => {
          const newVal = !(k.sensitive_filter !== false);
          try {
            await apiRequest(`/api/keys/${k.id}`, { method: 'PUT', body: JSON.stringify({ sensitive_filter: newVal }) });
            k.sensitive_filter = newVal;
            notify(newVal ? '已开启敏感词严格拦截' : '已开启敏感词豁免放行', 'info');
          } catch (e) { notify('更新敏感词策略失败: ' + e.message, 'error'); }
        };

        const toggleKeyEnabled = async (k) => {
          const newVal = !(k.enabled !== false);
          try {
            await apiRequest(`/api/keys/${k.id}`, { method: 'PUT', body: JSON.stringify({ enabled: newVal }) });
            k.enabled = newVal;
            notify(newVal ? '圣钥已启用' : '圣钥已暂停', 'info');
          } catch (e) { notify('操作失败: ' + e.message, 'error'); }
        };

        const revokeKey = async (k) => {
          if (!confirm(`确定废除圣钥「${k.name}」？撤销后下游所有使用此 Key 的客户端都将被即刻切断。`)) return;
          try {
            await apiRequest(`/api/keys/${k.id}`, { method: 'DELETE' });
            notify('圣钥已废除', 'warn');
            await loadKeys();
          } catch (e) { notify('撤销失败: ' + e.message, 'error'); }
        };

        const deleteKeyForever = async (k) => {
          if (!confirm(`确定彻底物理删除圣钥「${k.name || k.prefix}」？删除后数据库记录与凭据将完全消除。`)) return;
          try {
            await apiRequest(`/api/keys/${k.id}?purge=true`, { method: 'DELETE' });
            notify('圣钥已彻底物理删除', 'success');
            await loadKeys();
          } catch (e) { notify('删除失败: ' + e.message, 'error'); }
        };

        const purgeAllRevokedKeys = async () => {
          if (!confirm('确定彻底物理清除所有已作废、停用的历史垃圾 Key 记录吗？')) return;
          try {
            const res = await apiRequest('/api/keys/purge-revoked', { method: 'DELETE' });
            notify(res.message || `已成功清理 ${res.count || 0} 条作废 Key`, 'success');
            await loadKeys();
          } catch (e) { notify('清理作废 Key 失败: ' + e.message, 'error'); }
        };

        // ── Tab: 用户与付费档位控制 ──
        const userTierSubtab = ref('orders');
        const ordersList = ref([]);
        const orderCounts = ref({ pending: 0, paid: 0, active: 0, total: 0 });
        const orderStatusFilter = ref('');
        const loadingOrders = ref(false);
        const adminPayConfig = reactive({ alipay_qr: '', wechat_qr: '', alipay_account: '', wechat_account: '', notice: '', contact: '', enabled: true });
        const savingPayConfig = ref(false);

        const orderStatusText = (s) => ({
          pending: '待确认',
          paid: '已开通',
          cancelled: '已取消',
          refunded: '已退款'
        }[s] || s);

        const orderStatusClass = (s) => ({
          pending: 'bg-amber-500/20 text-amber-300 border border-amber-500/40',
          paid: 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40',
          cancelled: 'bg-slate-800 text-slate-400 border border-slate-700',
          refunded: 'bg-red-500/20 text-red-300 border border-red-500/40'
        }[s] || 'bg-slate-800 text-slate-400');

        const loadAdminOrders = async () => {
          loadingOrders.value = true;
          try {
            const q = orderStatusFilter.value ? `?status=${orderStatusFilter.value}` : '';
            const res = await apiRequest(`/api/admin/subscriptions/orders${q}`);
            ordersList.value = res.orders || [];
            if (res.counts) {
              orderCounts.value = { ...orderCounts.value, ...res.counts };
            }
          } catch (e) {
            notify('加载订单列表失败: ' + e.message, 'error');
          } finally {
            loadingOrders.value = false;
          }
        };

        const adminConfirmOrder = async (order) => {
          if (!confirm(`确认已收到用户 ${order.user_email || order.user_id} 的 ¥${order.amount_yuan} 款项？\n\n确认后将立即为其激活「${order.plan_name}」订阅并同步配额。`)) return;
          try {
            await apiRequest(`/api/admin/subscriptions/orders/${order.id}/confirm`, {
              method: 'POST',
              body: JSON.stringify({ admin_note: '管理员手动核对确认' })
            });
            notify('已确认收款，用户订阅已自动开通！', 'success');
            await loadAdminOrders();
            await loadUsersWithTiers();
          } catch (e) {
            notify('确认收款失败: ' + e.message, 'error');
          }
        };

        const adminCancelOrder = async (order) => {
          if (!confirm(`确认取消订单 ${order.id} 吗？`)) return;
          try {
            await apiRequest(`/api/admin/subscriptions/orders/${order.id}/status`, {
              method: 'POST',
              body: JSON.stringify({ status: 'cancelled', admin_note: '管理员取消' })
            });
            notify('订单已取消', 'info');
            await loadAdminOrders();
          } catch (e) {
            notify('取消订单失败: ' + e.message, 'error');
          }
        };

        const loadAdminPayConfig = async () => {
          try {
            const res = await apiRequest('/api/admin/subscriptions/payment-config');
            if (res.config) {
              Object.assign(adminPayConfig, res.config);
            }
          } catch (e) {
            notify('加载收款配置失败: ' + e.message, 'error');
          }
        };

        const saveAdminPayConfig = async () => {
          savingPayConfig.value = true;
          try {
            await apiRequest('/api/admin/subscriptions/payment-config', {
              method: 'PUT',
              body: JSON.stringify({
                alipay_qr: adminPayConfig.alipay_qr || '',
                wechat_qr: adminPayConfig.wechat_qr || '',
                alipay_account: adminPayConfig.alipay_account || '',
                wechat_account: adminPayConfig.wechat_account || '',
                notice: adminPayConfig.notice || '',
                contact: adminPayConfig.contact || '',
                enabled: !!adminPayConfig.enabled
              })
            });
            notify('收款与支付设置已成功保存！', 'success');
          } catch (e) {
            notify('保存收款设置失败: ' + e.message, 'error');
          } finally {
            savingPayConfig.value = false;
          }
        };

        const handleQrUpload = (event, key) => {
          const file = event.target.files && event.target.files[0];
          if (!file) return;
          if (file.size > 2 * 1024 * 1024) {
            notify('图片文件不能超过 2MB', 'error');
            return;
          }
          const reader = new FileReader();
          reader.onload = () => {
            adminPayConfig[key] = reader.result;
            notify('图片已加载，请记得点击下方的「保存收款配置」', 'info');
          };
          reader.onerror = () => notify('图片读取失败', 'error');
          reader.readAsDataURL(file);
        };

        const refreshUserTiersData = async () => {
          await Promise.all([loadAdminOrders(), loadUsersWithTiers(), loadAdminPayConfig()]);
        };

        const usersList = ref([]);
        const loadingUsers = ref(false);

        const loadUsersWithTiers = async () => {
          loadingUsers.value = true;
          try {
            const res = await apiRequest('/api/admin/users');
            usersList.value = res.users || [];
          } catch (e) {
            notify('加载用户列表失败: ' + e.message, 'error');
          } finally {
            loadingUsers.value = false;
          }
        };

        const changeUserTier = async (u, tier) => {
          const names = { free: '免费版', pro: '标准版', ultra: '无限版', legacy: '创世终身特权' };
          if (!confirm(`确定将用户 ${u.email} 调整为「${names[tier] || tier}」吗？`)) return;
          try {
            const res = await apiRequest(`/api/admin/users/${u.id}/tier`, {
              method: 'POST',
              body: JSON.stringify({ tier })
            });
            notify(res.message || '档位已成功调整', 'success');
            u.tier = tier;
            await loadUsersWithTiers();
          } catch (e) {
            notify('调整档位失败: ' + e.message, 'error');
          }
        };

        const toggleUserRole = async (u) => {
          const newRole = u.role === 'admin' ? 'user' : 'admin';
          const act = newRole === 'admin' ? '设为管理员' : '降为普通用户';
          if (!confirm(`确定要将用户 ${u.email} ${act} 吗？`)) return;
          try {
            const res = await apiRequest(`/api/admin/users/${u.id}/tier`, {
              method: 'POST',
              body: JSON.stringify({ tier: u.tier || 'free', role: newRole })
            });
            notify(`用户 ${u.email} 已${act}`, 'success');
            u.role = newRole;
            await loadUsersWithTiers();
          } catch (e) {
            notify('调整角色失败: ' + e.message, 'error');
          }
        };

        const grantLegacyAllUsers = async () => {
          if (!confirm('确认一键为全量存量用户授予「创世老用户」终身特权（终身无限额）吗？')) return;
          try {
            const res = await apiRequest('/api/admin/users/grant-legacy-all', { method: 'POST' });
            notify(res.message || `已为 ${res.count || 0} 位用户开通创世特权！`, 'success');
            await loadUsersWithTiers();
          } catch (e) {
            notify('一键授权失败: ' + e.message, 'error');
          }
        };

        const tierClass = (t) => {
          switch (t) {
            case 'legacy': return 'bg-amber-500/20 text-amber-300 border border-amber-500/30';
            case 'ultra': return 'bg-purple-500/20 text-purple-300 border border-purple-500/30';
            case 'pro': return 'bg-blue-500/20 text-blue-300 border border-blue-500/30';
            default: return 'bg-slate-800 text-slate-400 border border-slate-700';
          }
        };

        const tierName = (t) => {
          switch (t) {
            case 'legacy': return '创世特权';
            case 'ultra': return '无限版';
            case 'pro': return '标准版';
            default: return '免费版';
          }
        };

        const editOwnerPrompt = async (k) => {
          const owner = prompt('请输入该圣钥的归属人（留空 = 未指定）:', k.owner || '');
          if (owner === null) return;
          try {
            await apiRequest(`/api/keys/${k.id}`, { method: 'PUT', body: JSON.stringify({ owner: owner.trim() }) });
            notify('归属人已更新', 'success');
            await loadKeys();
          } catch (e) { notify('修改归属人失败: ' + e.message, 'error'); }
        };

        // ── Tab 4: 运维监控与日志 ──
        const forensics = ref({});
        const proxiesList = ref([]);
        const testingProxies = ref(false);
        const opsService = ref('ds-gateway');
        const opsDate = ref('');
        const opsLevel = ref('');
        const opsLines = ref(300);
        const opsFilter = ref('');
        const opsLogs = ref([]);
        const opsAutoRefresh = ref(false);
        const opsTerminalBox = ref(null);
        let opsTimer = null;

        const opsFilterPills = [
          { label: '全部日志', val: '' },
          { label: '封号/禁言 (biz:5)', val: 'biz:5,MUTED' },
          { label: '容灾换号 (Failover)', val: 'Failover,换号' },
          { label: '连坐熔断 (CircuitBreaker)', val: 'CircuitBreaker,熔断' },
          { label: '会话树分叉 (SessionTree)', val: 'SessionTree,命中会话树分叉' },
          { label: '报错/限流 (502/429)', val: '502,429,auth_expired' },
          { label: '代理出网 (Proxy)', val: 'Proxy,代理' }
        ];

        const formatRemainingSeconds = (sec, isPerm = false) => {
          if (isPerm) return '永久停用/需换号';
          if (!sec || sec <= 0) return '即将解禁/探活中';
          if (sec < 60) return `${sec} 秒`;
          const m = Math.floor(sec / 60);
          if (m < 60) return `${m} 分 ${sec % 60} 秒`;
          const h = Math.floor(m / 60);
          if (h < 24) return `${h} 小时 ${m % 60} 分`;
          return `${Math.floor(h / 24)} 天 ${h % 24} 小时`;
        };

        const setOpsQuickDate = (mode) => {
          const d = new Date();
          if (mode === 'today') opsDate.value = d.toISOString().slice(0, 10);
          else if (mode === 'yesterday') opsDate.value = new Date(d.getTime() - 86400000).toISOString().slice(0, 10);
          else opsDate.value = '';
          loadOpsLogs();
        };

        const loadForensics = async () => {
          try {
            forensics.value = await apiRequest('/api/system/forensics');
          } catch (e) { console.error(e); }
        };

        const fetchProxies = async () => {
          testingProxies.value = true;
          try {
            const res = await apiRequest('/api/system/proxies/status');
            proxiesList.value = res.proxies || [];
          } catch (e) { notify('代理测速失败: ' + e.message, 'error'); }
          finally { testingProxies.value = false; }
        };

        const loadOpsLogs = async () => {
          try {
            let url = `/api/system/logs?service=${encodeURIComponent(opsService.value)}&lines=${opsLines.value}`;
            if (opsDate.value) url += `&date=${encodeURIComponent(opsDate.value)}`;
            if (opsLevel.value) url += `&level=${encodeURIComponent(opsLevel.value)}`;
            if (opsFilter.value) url += `&filter=${encodeURIComponent(opsFilter.value)}`;
            const res = await apiRequest(url);
            opsLogs.value = res.lines || [];
            nextTick(() => {
              if (opsTerminalBox.value) opsTerminalBox.value.scrollTop = opsTerminalBox.value.scrollHeight;
            });
          } catch (e) { console.error(e); }
        };

        const clearOpsLogs = () => { opsLogs.value = []; };

        const downloadServerOpsLogs = () => {
          const url = `/api/system/logs/export?service=${encodeURIComponent(opsService.value)}&date=${encodeURIComponent(opsDate.value || '')}&filter=${encodeURIComponent(opsFilter.value || '')}&level=${encodeURIComponent(opsLevel.value || '')}`;
          const a = document.createElement('a');
          a.href = url;
          a.download = `ds-gateway-${opsService.value}-${opsDate.value || 'realtime'}.log`;
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
        };

        const exportOpsLogs = () => {
          const blob = new Blob([opsLogs.value.join('\n')], { type: 'text/plain;charset=utf-8' });
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url;
          a.download = `${opsService.value}-${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.log`;
          a.click();
          URL.revokeObjectURL(url);
        };

        const getLogLineClass = (line) => {
          if (!line) return '';
          if (line.includes('CircuitBreaker') || line.includes('同 IP 熔断') || line.includes('连坐')) return 'term-cascade';
          if (line.includes('成功') || line.includes('SUCCESS') || line.includes(' 200 ') || line.includes('OK') || line.includes('HEALTHY') || line.includes('命中会话树分叉')) return 'term-success';
          if (line.includes('失败') || line.includes('ERROR') || line.includes(' 500 ') || line.includes('429') || line.includes('biz:5') || line.includes('MUTED')) return 'term-error';
          if (line.includes('WARN') || line.includes('警告') || line.includes('等待') || line.includes('冷却') || line.includes('Failover')) return 'term-warn';
          if (line.includes('冷库') || line.includes('COLD') || line.includes('POP3')) return 'term-cold';
          return '';
        };

        // ── Tab 5: 自动洗礼注册 ──
        const coldStats = ref({});
        const regConfig = reactive({
          domain: 'outlook.com',
          anymessage_tokens: '',
          register_proxy: 'http://127.0.0.1:7892',
          has_vlm_key: false,
          vlm_api_key_masked: '',
          vlm_api_key: '',
          vlm_base_url: '',
          vlm_model: 'deepseek-v4.1-flash'
        });
        const regCount = ref(3);
        const regMode = ref('cold');
        const wakeCount = ref(3);
        const isRegisterRunning = ref(false);
        const stoppingRegister = ref(false);
        const regLogs = ref([]);
        const regTerminalBox = ref(null);
        let regTimer = null;
        const checkingTokens = ref(false);
        const autoScrollRegLogs = ref(true);
        const getLogLevelColor = (line) => {
          if (!line) return 'text-slate-300';
          if (line.includes('熔断') || line.includes('MAINLAND') || line.includes('失败') || line.includes('异常') || line.includes('[-]')) return 'text-rose-400 font-bold';
          if (line.includes('成功') || line.includes('SUCCESS') || line.includes('ACTIVE') || line.includes('STANDBY') || line.includes('[+]') || line.includes('🎉')) return 'text-emerald-400 font-semibold';
          if (line.includes('WARN') || line.includes('警告') || line.includes('探活') || line.includes('自愈') || line.includes('[*]')) return 'text-amber-400';
          if (line.includes('AnyMessage') || line.includes('余额') || line.includes('Token') || line.includes('订单') || line.includes('战报')) return 'text-purple-300';
          return 'text-slate-300';
        };

        // ── AnyMessage 纯净真机接码搞号工作台状态 ──
        const regWorkMode = ref('auto');
        const manualLoading = ref(false);
        const anymessageTokens = ref([]);
        const anymessageActiveToken = ref('');
        const anymessageActiveBalance = ref(0.0);
        const anymessageEstAccounts = ref(0);
        const anymessageRawText = ref('');
        const showTokenConfig = ref(false);
        const showFullToken = ref(false);
        const savingTokens = ref(false);

        const manualCandidate = reactive({
          email: '',
          password: '',
          order_id: '',
          token: '',
          domain: 'outlook.com',
          available_domains: ['outlook.com', 'hotmail.com'],
          signup_url: 'https://chat.deepseek.com/sign_up'
        });
        const showPassword = ref(true);
        const emailCopied = ref(false);
        const passwordCopied = ref(false);
        const codeCopied = ref(false);

        const manualCode = reactive({
          found: false,
          code: '',
          subject: '',
          date: '',
          checking: false
        });
        let manualCodePollTimer = null;
        const manualSubmitting = ref(false);
        const manualSessionAccounts = ref([]);
        const manualTokenInput = ref('');
        const viaScriptCopied = ref(false);

        const parsedTokenPreview = computed(() => {
          const raw = (manualTokenInput.value || '').trim();
          if (!raw) return '';
          if (raw.startsWith('{') && raw.includes('value')) {
            try {
              const obj = JSON.parse(raw);
              if (obj && obj.value) return String(obj.value).trim();
            } catch (e) {}
          }
          if (raw.startsWith('Bearer ')) return raw.slice(7).trim();
          return raw.replace(/^["']|["']$/g, '').trim();
        });

        const copyViaScript = async () => {
          const code = `// ==UserScript==
// @name         DeepSeek Token 悬浮提取器 (Via / 油猴)
// @namespace    https://chat.deepseek.com/
// @version      1.0
// @description  在 DeepSeek 官方页面一键提取 userToken 并复制
// @match        https://chat.deepseek.com/*
// @grant        GM_setClipboard
// ==/UserScript==

(function() {
    'use strict';
    function createButton() {
        if (document.getElementById('ds-token-btn')) return;
        const btn = document.createElement('button');
        btn.id = 'ds-token-btn';
        btn.innerHTML = '⚡ 提取 Token';
        btn.style.cssText = 'position:fixed;bottom:80px;right:16px;z-index:999999;padding:10px 16px;background:linear-gradient(135deg,#10b981,#059669);color:#fff;border:none;border-radius:24px;font-size:14px;font-weight:bold;box-shadow:0 4px 14px rgba(0,0,0,0.3);cursor:pointer;';
        btn.onclick = () => {
            const raw = localStorage.getItem('userToken');
            if (!raw) {
                alert('未检测到 Token，请先登录/注册 DeepSeek！');
                return;
            }
            let token = raw;
            try {
                const parsed = JSON.parse(raw);
                if (parsed && parsed.value) token = parsed.value;
            } catch(e) {}
            if (typeof GM_setClipboard === 'function') {
                GM_setClipboard(token);
            } else if (navigator.clipboard) {
                navigator.clipboard.writeText(token);
            } else {
                const ta = document.createElement('textarea');
                ta.value = token;
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
            }
            btn.innerHTML = '✅ Token 已复制';
            setTimeout(() => { btn.innerHTML = '⚡ 提取 Token'; }, 2000);
        };
        document.body.appendChild(btn);
    }
    window.addEventListener('load', () => setTimeout(createButton, 1000));
    setInterval(createButton, 2000);
})();`;
          await copyText(code, 'code');
          viaScriptCopied.value = true;
          notify('Via 脚本代码已复制到剪贴板，可直接在 Via 浏览器导入！', 'success');
          setTimeout(() => { viaScriptCopied.value = false; }, 2500);
        };

        const loadAnyMessageTokens = async () => {
          try {
            const res = await apiRequest('/api/register/anymessage/tokens');
            if (res.ok) {
              anymessageTokens.value = res.tokens || [];
              anymessageActiveToken.value = res.active_token || (res.tokens[0] ? res.tokens[0].token : '');
              anymessageActiveBalance.value = res.active_balance || 0.0;
              anymessageEstAccounts.value = Math.floor(anymessageActiveBalance.value / 0.0017);
              anymessageRawText.value = res.raw_text || '';
              if (res.domain) manualCandidate.domain = res.domain;
              if (res.available_domains) manualCandidate.available_domains = res.available_domains;
            }
          } catch (e) {
            console.error('加载 AnyMessage 状态失败', e);
          }
        };

        const switchActiveToken = async (newTok) => {
          anymessageActiveToken.value = newTok;
          const match = anymessageTokens.value.find(t => t.token === newTok);
          if (match) {
            anymessageActiveBalance.value = match.balance || 0.0;
            anymessageEstAccounts.value = match.est_accounts || 0;
          }
          await apiRequest('/api/register/anymessage/tokens', {
            method: 'POST',
            body: JSON.stringify({ active_token: newTok })
          }).catch(() => ({}));
          notify('已切换当前 AnyMessage 令牌', 'info', 1500);
          await loadManualNext(manualCandidate.domain);
        };

        const saveAnyMessageTokens = async () => {
          const raw = (anymessageRawText.value || '').trim();
          if (!raw) {
            notify('请先在输入框粘贴至少一个 AnyMessage 令牌', 'warn');
            return;
          }
          savingTokens.value = true;
          try {
            const res = await apiRequest('/api/register/anymessage/tokens', {
              method: 'POST',
              body: JSON.stringify({
                tokens_text: raw,
                domain: manualCandidate.domain || 'outlook.com'
              })
            });
            notify(res.message || 'AnyMessage 令牌已保存至 SQLite 并生效', 'success');
            showTokenConfig.value = false;
            await loadAnyMessageTokens();
            await loadManualNext(manualCandidate.domain);
          } catch (e) {
            notify('保存令牌失败: ' + (e.message || e), 'error');
          } finally {
            savingTokens.value = false;
          }
        };

        const loadManualNext = async (domain) => {
          manualLoading.value = true;
          manualCode.found = false;
          manualCode.code = '';
          try {
            const res = await apiRequest('/api/register/anymessage/order', {
              method: 'POST',
              body: JSON.stringify({
                token: anymessageActiveToken.value,
                domain: domain || manualCandidate.domain || 'outlook.com'
              })
            });
            if (res.ok) {
              manualCandidate.email = res.email;
              manualCandidate.order_id = res.order_id;
              manualCandidate.password = res.password;
              manualCandidate.token = res.token;
              manualCandidate.domain = res.domain;
              if (res.cold_count !== undefined) coldStats.value.cold_count = res.cold_count;
            }
          } catch (e) {
            notify('申领 AnyMessage 邮箱失败: ' + (e.message || e), 'error');
          } finally {
            manualLoading.value = false;
          }
        };

        const cancelAndReload = async () => {
          if (manualCandidate.order_id && manualCandidate.token) {
            try {
              await apiRequest('/api/register/anymessage/cancel', {
                method: 'POST',
                body: JSON.stringify({
                  token: manualCandidate.token,
                  order_id: manualCandidate.order_id
                })
              });
              notify('已退订当前邮箱，正在重新申领...', 'info', 1500);
            } catch (e) {}
          }
          await loadManualNext(manualCandidate.domain);
        };

        const fetchManualCode = async (showToast = false) => {
          if (!manualCandidate.order_id || !manualCandidate.token) return;
          manualCode.checking = true;
          try {
            const res = await apiRequest(`/api/register/anymessage/code?token=${encodeURIComponent(manualCandidate.token)}&order_id=${encodeURIComponent(manualCandidate.order_id)}`);
            if (res.ok && res.found) {
              manualCode.found = true;
              manualCode.code = res.code;
              manualCode.subject = 'AnyMessage 微软邮箱验证码';
              manualCode.date = new Date().toLocaleTimeString();
              if (showToast) notify(`🎉 成功捕获验证码: ${res.code}`, 'success');
              if (navigator.vibrate) navigator.vibrate([100, 50, 100]);
            } else {
              if (showToast) notify(res.message || '尚未收到验证码邮件，请在官方页面点击发送后稍候', 'info');
            }
          } catch (e) {
            if (showToast) notify('检索验证码失败: ' + (e.message || e), 'error');
          } finally {
            manualCode.checking = false;
          }
        };

        const copyText = async (text, type) => {
          if (!text) return;
          try {
            if (navigator.clipboard && navigator.clipboard.writeText) {
              await navigator.clipboard.writeText(text);
            } else {
              const ta = document.createElement('textarea');
              ta.value = text;
              ta.style.position = 'fixed';
              ta.style.opacity = '0';
              document.body.appendChild(ta);
              ta.select();
              document.execCommand('copy');
              document.body.removeChild(ta);
            }
            if (navigator.vibrate) navigator.vibrate(40);
            if (type === 'email') {
              emailCopied.value = true;
              setTimeout(() => { emailCopied.value = false; }, 2000);
              notify('已复制微软账号到剪贴板', 'success', 1500);
            } else if (type === 'password') {
              passwordCopied.value = true;
              setTimeout(() => { passwordCopied.value = false; }, 2000);
              notify('已复制随机密码到剪贴板', 'success', 1500);
            } else if (type === 'code') {
              codeCopied.value = true;
              setTimeout(() => { codeCopied.value = false; }, 2000);
              notify('已复制 6 位验证码到剪贴板', 'success', 1500);
            }
          } catch (e) {
            notify('复制失败，请长按手动复制', 'error');
          }
        };

        const manualTargetStatus = ref('INACTIVE_COLD');
        const promoteModal = reactive({ show: false, count: 1, loading: false });

        const openPromoteModal = () => {
          promoteModal.count = Math.min(1, Math.max(1, standbyAccounts.value.length));
          promoteModal.show = true;
        };

        const submitPromoteModal = async () => {
          if (promoteModal.count < 1) return;
          promoteModal.loading = true;
          try {
            const res = await apiRequest('/api/accounts/promote_standby', {
              method: 'POST',
              body: JSON.stringify({ count: promoteModal.count })
            });
            notify(res.message || `成功投产 ${res.promoted_count} 个温库账号！`, 'success');
            promoteModal.show = false;
            await loadStats();
            await loadAccounts();
          } catch (e) {
            notify('投产失败: ' + (e.message || e), 'error');
          } finally {
            promoteModal.loading = false;
          }
        };

        const promoteSingleAccount = async (acc) => {
          try {
            const res = await apiRequest('/api/accounts/promote_standby', {
              method: 'POST',
              body: JSON.stringify({ count: 1 })
            });
            notify('账号已成功投产为活跃号', 'success');
            await loadStats();
            await loadAccounts();
          } catch (e) { notify('投产失败: ' + e.message, 'error'); }
        };

        const demoteSingleAccount = async (acc) => {
          if (!confirm(`确定将活跃账号「${acc}」退回待命温库 (STANDBY)？退回后该账号将停止处理业务流量。`)) return;
          try {
            await apiRequest('/api/accounts/demote_to_standby', {
              method: 'POST',
              body: JSON.stringify({ account: acc })
            });
            notify(`账号 ${acc} 已退回待命温库`, 'info');
            await loadStats();
            await loadAccounts();
          } catch (e) { notify('退回失败: ' + e.message, 'error'); }
        };

        const submitManual = async () => {
          if (!manualCandidate.email || !manualCandidate.password) {
            notify('待注册账号或密码为空，请先申领邮箱', 'error');
            return;
          }
          const cleanTok = parsedTokenPreview.value;
          if (!cleanTok || cleanTok.length < 20) {
            notify('请在步骤 4 填入由 Via 脚本或网页抓取的有效 DeepSeek 官方 Token (长度需 >= 20)', 'error');
            return;
          }
          manualSubmitting.value = true;
          try {
            const res = await apiRequest('/api/register/anymessage/submit', {
              method: 'POST',
              body: JSON.stringify({
                email: manualCandidate.email,
                password: manualCandidate.password,
                order_id: manualCandidate.order_id,
                token: cleanTok,
                anymessage_token: manualCandidate.token || anymessageActiveToken.value,
                auto_exchange_token: false,
                status: manualTargetStatus.value || 'STANDBY',
                pool_tag: 'public',
                notes: '真机手动接码+Token录入温库'
              })
            });
            if (res.ok) {
              if (navigator.vibrate) navigator.vibrate([100, 50, 100]);
              notify(res.message || `账号 ${manualCandidate.email} 已成功存入待命温库！`, 'success', 4000);
              manualSessionAccounts.value.unshift({
                email: manualCandidate.email,
                password: manualCandidate.password,
                status: res.status || manualTargetStatus.value,
                has_token: true,
                time: new Date().toLocaleTimeString()
              });
              manualTokenInput.value = '';
              if (res.standby_count !== undefined) stats.standby = res.standby_count;
              if (res.cold_count !== undefined) coldStats.value.cold_count = res.cold_count;
              loadAnyMessageTokens();
              await loadStats();
              await loadManualNext(manualCandidate.domain);
            }
          } catch (e) {
            notify('入库失败: ' + (e.message || e), 'error');
          } finally {
            manualSubmitting.value = false;
          }
        };

        const copyAllProduced = async () => {
          if (manualSessionAccounts.value.length === 0) return;
          const text = manualSessionAccounts.value.map(a => `${a.email}----${a.password}`).join('\n');
          await copyText(text, 'email');
          notify(`已复制全部 ${manualSessionAccounts.value.length} 个已产账号密码`, 'success');
        };

        const startManualCodePoll = () => {
          if (manualCodePollTimer) clearInterval(manualCodePollTimer);
          manualCodePollTimer = setInterval(async () => {
            if (activeTab.value === 'register' && regWorkMode.value === 'manual' && !manualCode.found && manualCandidate.order_id) {
              await fetchManualCode(false);
            }
          }, 2500);
        };
        const tokenCheckResult = ref(null);

        const quickReg = reactive({
          email: '',
          password: '',
          proxy: '',
          farm_url: 'http://127.0.0.1:9527/farm/solve',
          register_otp: '',
          mfa_otp: '',
          busy: false,
          job: null
        });
        const runQuickPreflight = async () => {
          quickReg.busy = true;
          try {
            const res = await apiRequest('/api/register/preflight', { method: 'POST', body: JSON.stringify({ proxy: quickReg.proxy }) });
            notify(res.message || (res.ok ? '出口合规可用' : '出口受限'), res.ok ? 'success' : 'error');
          } catch (e) { notify('预检失败: ' + e.message, 'error'); }
          finally { quickReg.busy = false; }
        };
        const startQuickRegister = async () => {
          quickReg.busy = true;
          try {
            const res = await apiRequest('/api/register/quick/start', {
              method: 'POST',
              body: JSON.stringify({ email: quickReg.email, password: quickReg.password, proxy: quickReg.proxy, farm_url: quickReg.farm_url })
            });
            quickReg.job = res.job;
            notify('注册验证码已发送，请查收邮箱', 'success');
          } catch (e) { notify('启动失败: ' + e.message, 'error'); }
          finally { quickReg.busy = false; }
        };
        const verifyQuickRegisterOtp = async () => {
          if (!quickReg.job) return;
          quickReg.busy = true;
          try {
            const res = await apiRequest('/api/register/quick/verify_register', {
              method: 'POST',
              body: JSON.stringify({ job_id: quickReg.job.job_id, register_otp: quickReg.register_otp })
            });
            quickReg.job = res.job;
            notify('已提交注册并触发二次确权邮件', 'success');
          } catch (e) { notify('提交失败: ' + e.message, 'error'); }
          finally { quickReg.busy = false; }
        };
        const completeQuickRegister = async () => {
          if (!quickReg.job) return;
          quickReg.busy = true;
          try {
            const res = await apiRequest('/api/register/quick/complete', {
              method: 'POST',
              body: JSON.stringify({ job_id: quickReg.job.job_id, mfa_otp: quickReg.mfa_otp })
            });
            quickReg.job = res.job;
            notify('账号五件套铸造并入池成功', 'success');
            if (typeof loadAccounts === 'function') await loadAccounts();
          } catch (e) { notify('完成失败: ' + e.message, 'error'); }
          finally { quickReg.busy = false; }
        };
        const loadRegisterData = async () => {
          try {
            const res = await apiRequest('/api/register/quick/jobs').catch(() => ({ jobs: [] }));
            if (res && res.jobs && res.jobs.length && !quickReg.job) {
              quickReg.job = res.jobs[0];
            }
          } catch (e) { console.error(e); }
        };
        const checkTokens = async () => {
          const raw = (regConfig.anymessage_tokens || '').trim();
          if (!raw) {
            notify('请先在输入框粘贴至少一个 AnyMessage 令牌', 'warn');
            return;
          }
          checkingTokens.value = true;
          tokenCheckResult.value = null;
          try {
            const res = await apiRequest('/api/register/tokens/check', {
              method: 'POST',
              body: JSON.stringify({ tokens_text: raw })
            });
            tokenCheckResult.value = res;
          } catch (e) {
            notify('查验令牌失败: ' + e.message, 'error');
          } finally {
            checkingTokens.value = false;
          }
        };

        const saveRegConfig = async () => {
          try {
            const payload = {
              domain: regConfig.domain,
              anymessage_tokens: regConfig.anymessage_tokens,
              register_proxy: regConfig.register_proxy,
              vlm_base_url: regConfig.vlm_base_url,
              vlm_model: regConfig.vlm_model
            };
            if ((regConfig.vlm_api_key || '').trim()) {
              payload.vlm_api_key = regConfig.vlm_api_key.trim();
            }
            await apiRequest('/api/register/config', {
              method: 'PUT',
              body: JSON.stringify(payload)
            });
            regConfig.vlm_api_key = '';
            await loadRegisterData();
            notify('产号配置、VLM 设置与代理参数已保存至 SQLite', 'success');
          } catch (e) { notify('保存失败: ' + e.message, 'error'); }
        };

        const startRegister = async () => {
          const isCold = regMode.value === 'cold';
          const modeDesc = isCold ? '零IP冷库模式 (纯净保存在 SQLite 冷库)' : '直接激活入池模式 (立即登录换 Token)';
          const tokens = (regConfig.anymessage_tokens || anymessageActiveToken.value || '').trim();
          if (!tokens) {
            notify('请先输入至少一个 AnyMessage 令牌', 'warn');
            return;
          }
          const proxyDesc = regConfig.register_proxy || 'http://127.0.0.1:7892';
          if (!confirm(`确定在后台启动 ${regCount.value} 个账号的批量自动化生产？\n\n海外代理: ${proxyDesc}\n通道: Pure HTTP + VLM Farm (5件套 + 真实数美指纹)\n首选域名: @${regConfig.domain}\n入库模式: 【${modeDesc}】`)) return;

          try {
            const res = await apiRequest('/api/register/run', {
              method: 'POST',
              body: JSON.stringify({
                channel: 'anymessage',
                tokens_text: tokens,
                count: regCount.value,
                cold_reserve: isCold,
                domain: regConfig.domain,
                register_proxy: (regConfig.register_proxy || 'http://127.0.0.1:7892').trim(),
                auto_warmup: false
              })
            });
            notify(res.message || '批量注册任务已启动', 'success');
            isRegisterRunning.value = true;
            pollRegister();
          } catch (e) { notify('启动失败: ' + e.message, 'error'); }
        };

        const stopRegister = async () => {
          if (!confirm('确定紧急停止正在运行的批量注册流水线？\n\n系统将向任务发送硬终止信号并立刻停止后续申请。')) return;
          stoppingRegister.value = true;
          try {
            const res = await apiRequest('/api/register/stop', { method: 'POST' });
            notify(res.message || '已成功发送紧急停止指令', 'warn');
            await loadRegisterData();
          } catch (e) {
            notify('紧急停止失败: ' + e.message, 'error');
          } finally {
            stoppingRegister.value = false;
          }
        };

        const startWakeCold = async () => {
          if (!confirm(`确定在后台极速唤醒 ${wakeCount.value} 个冷库待命账号？将通过纯 HTTP + 真实指纹协议毫秒级换签入池。`)) return;
          try {
            const res = await apiRequest('/api/accounts/wake_cold_batch', {
              method: 'POST',
              body: JSON.stringify({ count: wakeCount.value, delay: 3.0 })
            });
            notify(res.message || '唤醒任务已启动', 'success');
            isRegisterRunning.value = true;
            pollRegister();
            pollWakeStatus();
          } catch (e) { notify('启动唤醒失败: ' + e.message, 'error'); }
        };

        const pollRegister = () => {
          if (regTimer) clearInterval(regTimer);
          regTimer = setInterval(async () => {
            await loadRegisterData();
            if (!isRegisterRunning.value) {
              clearInterval(regTimer);
              regTimer = null;
              await loadStats();
            }
          }, 2500);
        };

        const clearRegLogs = () => { regLogs.value = []; };

        // ── Tab 6: 纳新入池 ──
        const addMode = ref('jsonl');
        const addForm = reactive({
          jsonlText: '',
          targetStatus: 'INACTIVE_COLD',
          email: '',
          password: '',
          emailLabel: '',
          token: '',
          tokenLabel: '',
          curlText: '',
          curlLabel: ''
        });
        const submittingAdd = ref(false);

        const submitAddAccount = async () => {
          submittingAdd.value = true;
          try {
            if (addMode.value === 'jsonl') {
              if (!addForm.jsonlText || !addForm.jsonlText.trim()) throw new Error('请粘贴 5 件套 JSONL 或 JSON 数据');
              const res = await apiRequest('/api/accounts/batch_import', {
                method: 'POST',
                body: JSON.stringify({
                  accounts_text: addForm.jsonlText.trim(),
                  target_status: addForm.targetStatus || 'INACTIVE_COLD',
                  check_liveness: true,
                  deduplicate: true,
                  overwrite_existing: true
                })
              });
              notify(res.message || '批量导入完成', 'success', 4500);
              addForm.jsonlText = '';
              await loadStats();
              await loadAccounts();
              switchTab('accounts');
              return;
            }

            let payload = {};
            if (addMode.value === 'email') {
              if (!addForm.email || !addForm.password) throw new Error('请完整输入邮箱和密码');
              payload = { mode: 'email_pwd', email: addForm.email.trim(), password: addForm.password, account_label: addForm.emailLabel.trim() || undefined };
            } else if (addMode.value === 'token') {
              if (!addForm.token) throw new Error('请录入 Token');
              payload = { mode: 'token', token: addForm.token.trim(), account_label: addForm.tokenLabel.trim() || undefined };
            } else if (addMode.value === 'curl') {
              if (!addForm.curlText) throw new Error('请粘贴 cURL 抓包文本');
              payload = { mode: 'curl', curl_text: addForm.curlText.trim(), account_label: addForm.curlLabel.trim() || undefined };
            }

            const res = await apiRequest('/api/accounts/add', { method: 'POST', body: JSON.stringify(payload) });
            notify(res.message || '账号纳新成功并已就绪', 'success');
            addForm.email = ''; addForm.password = ''; addForm.token = ''; addForm.curlText = '';
            await loadStats();
            await loadAccounts();
            switchTab('accounts');
          } catch (e) { notify('纳新失败: ' + e.message, 'error'); }
          finally { submittingAdd.value = false; }
        };

        // ── Tab 7: 圣殿枢纽设置 ──
        const gatewaySettings = reactive({ proxy: '', passthrough: false });
        const adminPwd = reactive({ newPwd: '', confirmPwd: '' });
        const settingsSaved = ref(false);

        const loadGatewaySettings = async () => {
          try {
            const s = await apiRequest('/api/settings');
            gatewaySettings.proxy = s.proxy || '';
            gatewaySettings.passthrough = !!s.passthrough;
          } catch (e) { console.error(e); }
        };

        const saveGatewaySettings = async () => {
          try {
            await apiRequest('/api/settings', {
              method: 'PUT',
              body: JSON.stringify({
                proxy: gatewaySettings.proxy.trim() || null,
                passthrough: gatewaySettings.passthrough
              })
            });
            settingsSaved.value = true;
            notify('中继设置已保存并热更新', 'success');
            setTimeout(() => { settingsSaved.value = false; }, 3000);
          } catch (e) { notify('保存失败: ' + e.message, 'error'); }
        };

        const changeAdminPwd = async () => {
          if (!adminPwd.newPwd || adminPwd.newPwd.length < 4) {
            notify('密码长度不能少于 4 位', 'warn');
            return;
          }
          if (adminPwd.newPwd !== adminPwd.confirmPwd) {
            notify('两次输入的新密码不一致', 'warn');
            return;
          }
          try {
            await apiRequest('/api/settings/password', { method: 'PUT', body: JSON.stringify({ password: adminPwd.newPwd }) });
            notify('管理员密匙已修改成功', 'success');
            adminPwd.newPwd = ''; adminPwd.confirmPwd = '';
          } catch (e) { notify('修改失败: ' + e.message, 'error'); }
        };

        // ── 全套模态框状态与逻辑 ──
        const assignModal = reactive({
          show: false,
          key: null,
          allAccounts: [],
          selectedLabels: [],
          searchQuery: '',
          onlyActive: false,
        });
        const quotaModal = reactive({ show: false, key: null, reqLimit: 0, tokLimit: 0 });
        const sensitiveModal = reactive({ show: false, customWords: '', builtIn: {} });
        const createKeyModal = reactive({ show: false, name: '', owner: '', generatedKey: '', copied: false });
        const wakeModal = reactive({ show: false, maxCold: 0, count: 5 });
        const inboxModal = reactive({ show: false });
        const inboxAlerts = ref([]);
        const inboxFilter = ref('all');
        const unreadAlertsCount = ref(0);

        const openAssignModal = async (key) => {
          try {
            const od = await apiRequest('/api/accounts/ownership');
            const accs = od.accounts || [];
            assignModal.key = key;
            assignModal.searchQuery = '';
            assignModal.onlyActive = false;
            assignModal.allAccounts = accs.map(a => ({
              account_label: a.account_label,
              status: a.status,
              owner: a.owner,
              takenByOther: !!(a.owner_key_id && a.owner_key_id !== key.id)
            }));
            assignModal.selectedLabels = [...(key.account_labels || [])];
            assignModal.show = true;
          } catch (e) { notify('读取归属失败: ' + e.message, 'error'); }
        };

        const toggleAssignAccount = (acc) => {
          if (acc.takenByOther) return;
          const idx = assignModal.selectedLabels.indexOf(acc.account_label);
          if (idx > -1) {
            assignModal.selectedLabels.splice(idx, 1);
          } else {
            assignModal.selectedLabels.push(acc.account_label);
          }
        };

        const quickSelectAssign = (count) => {
          const currentSet = new Set(assignModal.selectedLabels);
          let added = 0;
          for (const acc of assignModal.allAccounts) {
            if (added >= count) break;
            const isAct = acc.status === 'ACTIVE' || (acc.status !== 'BANNED' && acc.status !== 'INACTIVE_COLD');
            if (!acc.takenByOther && isAct && !currentSet.has(acc.account_label)) {
              assignModal.selectedLabels.push(acc.account_label);
              currentSet.add(acc.account_label);
              added++;
            }
          }
        };

        const filteredAssignAccounts = computed(() => {
          let list = assignModal.allAccounts || [];
          if (assignModal.onlyActive) {
            list = list.filter(a => a.status === 'ACTIVE' || assignModal.selectedLabels.includes(a.account_label));
          }
          if (assignModal.searchQuery && assignModal.searchQuery.trim()) {
            const q = assignModal.searchQuery.trim().toLowerCase();
            list = list.filter(a => a.account_label.toLowerCase().includes(q));
          }
          return list;
        });

        const saveAssignModal = async () => {
          try {
            await apiRequest(`/api/keys/${assignModal.key.id}/accounts`, {
              method: 'PUT',
              body: JSON.stringify({ account_labels: assignModal.selectedLabels })
            });
            assignModal.show = false;
            notify('专属账号分配已保存', 'success');
            await loadKeys();
          } catch (e) { notify('分配保存失败: ' + e.message, 'error'); }
        };

        const openQuotaModal = (key) => {
          quotaModal.key = key;
          quotaModal.reqLimit = key.quota?.daily_requests || 0;
          quotaModal.tokLimit = key.quota?.daily_tokens || 0;
          quotaModal.show = true;
        };

        const saveQuotaModal = async () => {
          try {
            await apiRequest(`/api/keys/${quotaModal.key.id}`, {
              method: 'PUT',
              body: JSON.stringify({
                quota_daily_requests: quotaModal.reqLimit,
                quota_daily_tokens: quotaModal.tokLimit
              })
            });
            quotaModal.show = false;
            notify('配额设置已保存', 'success');
            if (activeTab.value === 'usage') await loadUsage();
            if (activeTab.value === 'apiKeys') await loadKeys();
          } catch (e) { notify('保存配额失败: ' + e.message, 'error'); }
        };

        const openSensitiveModal = async () => {
          try {
            const res = await apiRequest('/api/system/sensitive_words');
            sensitiveModal.customWords = (res.custom_words || []).join('\n');
            sensitiveModal.builtIn = res.built_in_rules || {};
            sensitiveModal.show = true;
          } catch (e) { notify('读取敏感词配置失败: ' + e.message, 'error'); }
        };

        const saveSensitiveModal = async () => {
          const words = sensitiveModal.customWords.split('\n').map(w => w.trim()).filter(w => w.length > 0);
          try {
            const res = await apiRequest('/api/system/sensitive_words', {
              method: 'POST',
              body: JSON.stringify({ words })
            });
            notify(`自定义敏感词库已更新，当前共 ${res.count} 个关键词`, 'success');
            sensitiveModal.show = false;
          } catch (e) { notify('保存失败: ' + e.message, 'error'); }
        };

        const openCreateKeyModal = () => {
          createKeyModal.name = '';
          createKeyModal.owner = '';
          createKeyModal.generatedKey = '';
          createKeyModal.copied = false;
          createKeyModal.show = true;
        };

        const submitCreateKey = async () => {
          try {
            const res = await apiRequest('/api/keys', {
              method: 'POST',
              body: JSON.stringify({
                name: createKeyModal.name.trim() || 'default-sanctuary-key',
                owner: createKeyModal.owner.trim() || ''
              })
            });
            createKeyModal.generatedKey = res.key;
            notify('新圣钥已铸造成功', 'success');
            await loadKeys();
          } catch (e) { notify('铸造失败: ' + e.message, 'error'); }
        };

        const copyGeneratedKey = () => {
          if (createKeyModal.generatedKey) {
            navigator.clipboard.writeText(createKeyModal.generatedKey);
            createKeyModal.copied = true;
            notify('圣钥明文已复制', 'success');
            setTimeout(() => { createKeyModal.copied = false; }, 2000);
          }
        };

        const openWakeModal = () => {
          wakeModal.maxCold = coldAccounts.value.length;
          wakeModal.count = Math.min(5, wakeModal.maxCold);
          wakeModal.show = true;
        };

        const submitWakeModal = async () => {
          try {
            const res = await apiRequest('/api/accounts/wake_cold_batch', {
              method: 'POST',
              body: JSON.stringify({ count: wakeModal.count, delay: 3.0 })
            });
            notify(res.message || '冷库平缓唤醒任务已启动', 'success');
            wakeModal.show = false;
            await loadStats();
            await loadAccounts();
            await pollWakeStatus();
          } catch (e) { notify('启动失败: ' + e.message, 'error'); }
        };

        const getAlertBadgeClass = (t) => {
          const type = String(t || '').toUpperCase();
          if (type.includes('DATED')) return 'bg-yellow-500/20 text-yellow-300 border border-yellow-500/40';
          if (type.includes('PERMANENT') || type.includes('BANNED') || type.includes('INVALID')) return 'bg-red-500/25 text-red-300 border border-red-500/50';
          if (type.includes('QUARANTINE')) return 'bg-orange-500/20 text-orange-300 border border-orange-500/40';
          return 'bg-amber-500/20 text-amber-300 border border-amber-500/40';
        };

        const loadInboxAlerts = async () => {
          try {
            const res = await apiRequest('/api/system/alerts?limit=100');
            if (res && res.ok) {
              inboxAlerts.value = res.alerts || [];
              unreadAlertsCount.value = res.unread_count || 0;
            }
          } catch (e) {
            console.error('loadInboxAlerts error:', e);
          }
        };

        const openInboxModal = async () => {
          inboxModal.show = true;
          await loadInboxAlerts();
        };

        const markAlertRead = async (alertId) => {
          try {
            const res = await apiRequest('/api/system/alerts/read', {
              method: 'POST',
              body: JSON.stringify({ id: alertId })
            });
            if (res && res.ok) {
              const target = inboxAlerts.value.find(a => a.id === alertId);
              if (target) target.is_read = true;
              unreadAlertsCount.value = res.unread_count || 0;
              notify('已标记为已读', 'success', 1500);
            }
          } catch (e) {
            notify('标记失败: ' + (e.message || e), 'error');
          }
        };

        const markAllAlertsRead = async () => {
          try {
            const res = await apiRequest('/api/system/alerts/read', {
              method: 'POST',
              body: JSON.stringify({ all: true })
            });
            if (res && res.ok) {
              inboxAlerts.value.forEach(a => { a.is_read = true; });
              unreadAlertsCount.value = 0;
              notify('已全部标记为已读', 'success', 2000);
            }
          } catch (e) {
            notify('操作失败: ' + (e.message || e), 'error');
          }
        };

        const clearReadAlerts = async () => {
          try {
            const res = await apiRequest('/api/system/alerts/clear', {
              method: 'DELETE'
            });
            if (res && res.ok) {
              inboxAlerts.value = inboxAlerts.value.filter(a => !a.is_read);
              unreadAlertsCount.value = res.unread_count || 0;
              notify(`已清理 ${res.cleared_count || 0} 条已读通知`, 'success', 2000);
            }
          } catch (e) {
            notify('清理失败: ' + (e.message || e), 'error');
          }
        };

        const filteredInboxAlerts = computed(() => {
          if (inboxFilter.value === 'unread') return inboxAlerts.value.filter(a => !a.is_read);
          if (inboxFilter.value === 'read') return inboxAlerts.value.filter(a => a.is_read);
          return inboxAlerts.value;
        });

        // 初始拉取收件箱状态
        loadInboxAlerts();

        const doLogout = async () => {
          if (!confirm('确定退出当前管理控制总台？')) return;
          try { await apiRequest('/api/auth/logout', { method: 'POST' }); } catch (e) {}
          authState.clearAuth();
          router.push('/login');
        };

        const refreshAll = async () => {
          await loadStats();
          await loadInboxAlerts();
          const tId = activeTab.value;
          if (tId === 'accounts') { await loadAccounts(); await pollWakeStatus(); }
          else if (tId === 'usage') await loadUsage();
          else if (tId === 'apiKeys') await loadKeys();
          else if (tId === 'opsLogs') { await loadForensics(); await loadOpsLogs(); }
          else if (tId === 'register') await loadRegisterData();
          else if (tId === 'settings') await loadGatewaySettings();
          notify('面板态势已刷新', 'info', 1800);
        };

        watch(activeTab, async (newTab) => {
          await loadStats();
          if (newTab === 'accounts') { await loadAccounts(); await pollWakeStatus(); }
          else if (newTab === 'usage') await loadUsage();
          else if (newTab === 'apiKeys') await loadKeys();
          else if (newTab === 'opsLogs') { await loadForensics(); await loadOpsLogs(); }
          else if (newTab === 'register') await loadRegisterData();
          else if (newTab === 'settings') { await loadGatewaySettings(); await loadAnyMessageTokens(); }
        }, { immediate: true });

        watch(opsAutoRefresh, (val) => {
          if (val) {
            opsTimer = setInterval(() => { if (activeTab.value === 'opsLogs') loadOpsLogs(); }, 5000);
          } else {
            if (opsTimer) clearInterval(opsTimer);
          }
        });

        onUnmounted(() => {
          if (opsTimer) clearInterval(opsTimer);
          if (regTimer) clearInterval(regTimer);
          if (wakePollTimer) clearInterval(wakePollTimer);
          if (manualCodePollTimer) clearInterval(manualCodePollTimer);
        });

        
    const keyCategoryFilter = ref('all')
    const favorKeysCount = computed(() => (keys.value || []).filter(k => !k.user_id).length)
    const userKeysCount = computed(() => (keys.value || []).filter(k => !!k.user_id).length)

    const cardKeyModal = reactive({
      show: false,
      tab: 'single',
      loading: false,
      name: '',
      owner: '',
      category: 'weekly',
      days: 7,
      searchQuery: '',
      onlyActive: true,
      allAccounts: [],
      selectedLabels: [],
      generatedResult: null,
      deliveryText: '',
      keyCopied: false,
      deliveryCopied: false
    })

    const openCreateCardKeyModal = async () => {
      const now = new Date()
      cardKeyModal.name = `卡密-${now.getMonth() + 1}月${now.getDate()}日`
      cardKeyModal.owner = ''
      cardKeyModal.category = 'weekly'
      cardKeyModal.days = 7
      cardKeyModal.searchQuery = ''
      cardKeyModal.onlyActive = true
      cardKeyModal.selectedLabels = []
      cardKeyModal.generatedResult = null
      cardKeyModal.deliveryText = ''
      cardKeyModal.keyCopied = false
      cardKeyModal.deliveryCopied = false
      cardKeyModal.tab = 'single'
      cardKeyModal.show = true

      try {
        const od = await apiRequest('/api/accounts/ownership')
        const accs = od.accounts || []
        cardKeyModal.allAccounts = accs.map(a => ({
          account_label: a.account_label,
          status: a.status,
          owner: a.owner,
          takenByOther: !!a.owner_key_id
        }))
      } catch (err) {
        notify('读取号池状态失败: ' + err.message, 'error')
      }
    }

    const filteredCardKeyAccounts = computed(() => {
      let list = cardKeyModal.allAccounts || []
      if (cardKeyModal.onlyActive) {
        list = list.filter(a => (a.status === 'ACTIVE' && !a.takenByOther) || cardKeyModal.selectedLabels.includes(a.account_label))
      }
      if (cardKeyModal.searchQuery && cardKeyModal.searchQuery.trim()) {
        const q = cardKeyModal.searchQuery.trim().toLowerCase()
        list = list.filter(a => a.account_label.toLowerCase().includes(q))
      }
      return list
    })

    const toggleCardKeyAccount = (acc) => {
      const idx = cardKeyModal.selectedLabels.indexOf(acc.account_label)
      if (idx > -1) {
        cardKeyModal.selectedLabels.splice(idx, 1)
      } else {
        cardKeyModal.selectedLabels.push(acc.account_label)
      }
    }

    const quickSelectCardKey = (count) => {
      const currentSet = new Set(cardKeyModal.selectedLabels)
      let added = 0
      for (const acc of cardKeyModal.allAccounts) {
        if (added >= count) break
        const isFreeActive = acc.status === 'ACTIVE' && !acc.takenByOther
        if (isFreeActive && !currentSet.has(acc.account_label)) {
          cardKeyModal.selectedLabels.push(acc.account_label)
          currentSet.add(acc.account_label)
          added++
        }
      }
      if (added === 0) {
        notify('没有更多空闲 ACTIVE 账号可选', 'warning')
      } else {
        notify(`已快捷勾选 ${added} 个空闲号`, 'success')
      }
    }

    const submitCreateCardKey = async () => {
      if (!cardKeyModal.name.trim()) {
        notify('请输入卡密备注名', 'warning')
        return
      }
      let days = null
      if (cardKeyModal.category === 'daily') days = 1
      else if (cardKeyModal.category === 'weekly') days = 7
      else if (cardKeyModal.category === 'monthly') days = 30
      else if (cardKeyModal.category === 'quarterly') days = 90
      else if (cardKeyModal.category === 'custom') days = Number(cardKeyModal.days) || 1

      cardKeyModal.loading = true
      try {
        const payload = {
          name: cardKeyModal.name.trim(),
          owner: cardKeyModal.owner.trim() || '零售卡密',
          account_labels: cardKeyModal.selectedLabels,
          key_type: 'card',
          card_category: cardKeyModal.category,
          days: days
        }
        const res = await apiRequest('/api/keys', {
          method: 'POST',
          body: JSON.stringify(payload)
        })

        const categoryTextMap = {
          daily: '天卡 (24 小时)',
          weekly: '周卡 (7 天)',
          monthly: '月卡 (30 天)',
          quarterly: '季卡 (90 天)',
          forever: '永久无限制',
          custom: `${days} 天卡`
        }
        const specName = categoryTextMap[cardKeyModal.category] || '专属卡密'
        const expiryDesc = res.expires_at ? formatCardExpiry(res.expires_at) : '永久有效'
        const originUrl = window.location.origin
        const dedicatedCount = (cardKeyModal.selectedLabels && cardKeyModal.selectedLabels.length) || 0
        const dedicatedInfo = dedicatedCount > 0 ? `专属独占通道（已绑定 ${dedicatedCount} 台独立号源）` : `高速公共卡池（智能高并发负载均衡）`

        const delivery = [
          `【DeepSeek 专属 API 卡密交付信息】`,
          `────────────────────────────────────`,
          `🔑 专属 API Key：${res.key}`,
          `🌐 接口 Base URL：${originUrl}/v1`,
          `🤖 调度通道模式：${dedicatedInfo}`,
          `⏱️ 有效期限状态：${specName}（${expiryDesc}）`,
          `🎯 支持模型名称：deepseek-chat, deepseek-reasoner`,
          `────────────────────────────────────`,
          `💡 极速配置指引：`,
          `1. 在 NextChat / Chatbox / Cherry Studio / Claude Code 中配置；`,
          `2. 接口类型选择 OpenAI 兼容或 DeepSeek 官方兼容格式；`,
          `3. API 地址填：${originUrl}/v1 ，密钥填上面的 Key 即开即用！`
        ].join('\n')

        cardKeyModal.deliveryText = delivery
        cardKeyModal.generatedResult = res
        notify('卡密铸造成功！', 'success')
        await loadKeys()
      } catch (err) {
        notify('铸造失败: ' + (err.message || err), 'error')
      } finally {
        cardKeyModal.loading = false
      }
    }

    const copyGeneratedCardKey = () => {
      if (!cardKeyModal.generatedResult || !cardKeyModal.generatedResult.key) return
      navigator.clipboard.writeText(cardKeyModal.generatedResult.key)
      cardKeyModal.keyCopied = true
      notify('卡密 API Key 已复制！', 'success')
      setTimeout(() => { cardKeyModal.keyCopied = false }, 2000)
    }

    const copyCardDeliveryText = () => {
      if (!cardKeyModal.deliveryText) return
      navigator.clipboard.writeText(cardKeyModal.deliveryText)
      cardKeyModal.deliveryCopied = true
      notify('买家交付文本已复制到剪贴板！可以直接发送给客户', 'success')
      setTimeout(() => { cardKeyModal.deliveryCopied = false }, 2000)
    }

    const showBatchCardModal = computed({
      get: () => cardKeyModal.show && cardKeyModal.tab === 'batch',
      set: (v) => { cardKeyModal.show = v; if (v) cardKeyModal.tab = 'batch'; }
    })
    const batchCardLoading = ref(false)
    const batchCardResult = ref(null)
    const batchCardForm = reactive({
      category: 'weekly',
      days: 7,
      count: 5,
      batch_remark: '零售卡密',
      mode: 'shared'
    })

    const submitBatchCards = async () => {
      batchCardLoading.value = true
      try {
        const res = await apiRequest('/api/admin/cards/batch-create', {
          method: 'POST',
          body: JSON.stringify(batchCardForm)
        })
        batchCardResult.value = res
        notify(`成功制造 ${res.count} 张卡密！`, 'success')
        refreshAll()
      } catch (err) {
        notify(err.message || '批量制造失败', 'error')
      } finally {
        batchCardLoading.value = false
      }
    }

    const copyBatchCards = () => {
      if (!batchCardResult.value || !batchCardResult.value.bundle_text) return
      navigator.clipboard.writeText(batchCardResult.value.bundle_text)
      notify('全部卡密已复制到剪贴板！可以直接发送给客户', 'success')
    }

    const cleanExpiredCards = async () => {
      if (!confirm('确定要立即回收并清理所有已过期的卡密吗？已过期卡密占用的号源将被立刻释放回公共池。')) return
      try {
        const res = await apiRequest('/api/admin/cards/clean-expired', { method: 'POST' })
        notify(`已回收清理 ${res.recycled_count} 张到期卡密！`, 'success')
        refreshAll()
      } catch (err) {
        notify(err.message || '清理失败', 'error')
      }
    }

    const moveFavorKeyToPool = async (k) => {
      if (!confirm(`确定将「${k.owner || k.name}」名下占用的 ${k.account_labels?.length || 0} 个独占账号移入公共卡池库吗？移入后该 Key 将转为走公共高速池调度。`)) return
      try {
        const res = await apiRequest(`/api/admin/keys/${k.id}/move-to-pool`, { method: 'POST' })
        notify(`成功将 ${res.released_accounts?.length || 0} 个账号移入公共卡池！`, 'success')
        refreshAll()
      } catch (err) {
        notify(err.message || '移入失败', 'error')
      }
    }

    const isKeyExpired = (expires_at) => {
      if (!expires_at) return false
      return Date.now() / 1000 > Number(expires_at)
    }

    const formatCardExpiry = (expires_at) => {
      if (!expires_at) return '永久有效'
      const now = Date.now() / 1000
      const exp = Number(expires_at)
      if (now > exp) {
        return '已过期 (' + new Date(exp * 1000).toLocaleDateString() + ')'
      }
      const diffSec = exp - now
      const days = Math.floor(diffSec / 86400)
      const hours = Math.floor((diffSec % 86400) / 3600)
      if (days > 0) {
        return `剩余 ${days}天${hours}小时 (${new Date(exp * 1000).toLocaleDateString()})`
      }
      return `剩余 ${hours}小时 (${new Date(exp * 1000).toLocaleTimeString()})`
    }

    const manageSubModal = reactive({
      show: false,
      loading: false,
      user: null,
      form: {
        tier: 'standard',
        datetime: '',
        add_days: 0,
        auto_allocate_account: true,
        account_count: 1,
        bonus_tokens: null,
      },
    })

    const openManageSubModal = (u) => {
      manageSubModal.user = u
      manageSubModal.form.tier = u.tier || 'standard'
      manageSubModal.form.auto_allocate_account = true
      manageSubModal.form.account_count = (u.tier === 'pro' || u.tier === 'ultra') ? 2 : 1
      manageSubModal.form.bonus_tokens = u.bonus_tokens || null
      manageSubModal.form.add_days = 0
      const pad = (n) => String(n).padStart(2, '0')
      if (u.subscription && u.subscription.period_end) {
        const dt = new Date(u.subscription.period_end * 1000)
        manageSubModal.form.datetime = dt.getFullYear() + '-' + pad(dt.getMonth() + 1) + '-' + pad(dt.getDate()) + 'T' + pad(dt.getHours()) + ':' + pad(dt.getMinutes())
      } else {
        const dt = new Date(Date.now() + 30 * 86400 * 1000)
        manageSubModal.form.datetime = dt.getFullYear() + '-' + pad(dt.getMonth() + 1) + '-' + pad(dt.getDate()) + 'T' + pad(dt.getHours()) + ':' + pad(dt.getMinutes())
      }
      manageSubModal.show = true
    }

    const quickSetExpiry = (days) => {
      const base = (manageSubModal.user && manageSubModal.user.subscription && manageSubModal.user.subscription.period_end && manageSubModal.user.subscription.period_end > Math.floor(Date.now() / 1000))
        ? new Date(manageSubModal.user.subscription.period_end * 1000)
        : new Date()
      let target
      if (days === 'permanent') {
        target = new Date('2036-12-31T23:59:59')
      } else {
        target = new Date(base.getTime() + Number(days) * 86400 * 1000)
      }
      const pad = (n) => String(n).padStart(2, '0')
      manageSubModal.form.datetime = target.getFullYear() + '-' + pad(target.getMonth() + 1) + '-' + pad(target.getDate()) + 'T' + pad(target.getHours()) + ':' + pad(target.getMinutes())
    }

    const submitManageSubModal = async () => {
      if (!manageSubModal.user) return
      manageSubModal.loading = true
      try {
        let period_end = null
        if (manageSubModal.form.datetime) {
          period_end = Math.floor(new Date(manageSubModal.form.datetime).getTime() / 1000)
        }
        const payload = {
          tier: manageSubModal.form.tier,
          period_end: period_end,
          auto_allocate_account: manageSubModal.form.auto_allocate_account,
          account_count: manageSubModal.form.account_count || 1,
          bonus_tokens: manageSubModal.form.bonus_tokens !== null && manageSubModal.form.bonus_tokens !== '' ? Number(manageSubModal.form.bonus_tokens) : null,
        }
        await apiRequest('/api/admin/users/' + manageSubModal.user.id + '/subscription', {
          method: 'PUT',
          body: JSON.stringify(payload),
        })
        notify('用户「' + manageSubModal.user.email + '」套餐与到期时间修改成功！', 'success')
        manageSubModal.show = false
        await loadKeys()
        if (typeof loadUsersWithTiers === 'function') await loadUsersWithTiers()
      } catch (err) {
        notify(err.message || '操纵套餐失败', 'error')
      } finally {
        manageSubModal.loading = false
      }
    }

    const autoAllocateUser = async (u) => {
      try {
        const count = (u.tier === 'pro' || u.tier === 'ultra') ? 2 : 1
        const res = await apiRequest('/api/admin/user-pools/' + u.id + '/auto-allocate?count=' + count, {
          method: 'POST',
        })
        notify('已为「' + u.email + '」智能分配专属保供号: ' + ((res.allocated || []).join(', ') || '已满额'), 'success')
        await loadKeys()
      } catch (err) {
        notify(err.message || '选号失败', 'error')
      }
    }
return {
          keyCategoryFilter, favorKeysCount, userKeysCount,
          keySubTab, userPoolsList, favorKeysList, userKeysList,
          userPoolModal, openEditUserPool, saveUserPoolModal, triggerUserReplenish,
          filteredUserPoolAccounts, toggleUserPoolAccount, quickSelectUserPool,
          manageSubModal, openManageSubModal, quickSetExpiry, submitManageSubModal, autoAllocateUser, cardKeyModal, openCreateCardKeyModal, filteredCardKeyAccounts, toggleCardKeyAccount, quickSelectCardKey, submitCreateCardKey, copyGeneratedCardKey, copyCardDeliveryText, showBatchCardModal, batchCardLoading, batchCardResult, batchCardForm, cleanExpiredCards, moveFavorKeyToPool, isKeyExpired, formatCardExpiry,
          submitBatchCards, copyBatchCards, cardKeysList,

          activeTab, tabs, stats, checkingAll, lastUpdated, wakeStatus,
          accounts, alerts, accFilter, accSearch, standbyAccounts, coldAccounts, activeAccounts, quarantineAccounts, manualTargetStatus, promoteModal, openPromoteModal, submitPromoteModal, promoteSingleAccount, demoteSingleAccount, filteredAccounts,
          getStatusBadgeClass, getStatusText, checkAllAccounts, probeAccount, reloginAccount, reloginAllAccounts,
          reloginHealth, reloginBadgeClass,
          warmupAccount, activateAccount, toggleDisposable, demoteAccount, rehabAccount, clearQuarantine, deleteAccount, loadAccounts,
          usageSummary, byKeyList, revokedByKeyList, recentRecords, loadUsage, resetKeyUsage, pruneRevokedUsage,
          keysList, activeKeys, ownershipSummary, keyUsageMap, loadKeys, toggleKeyFallback, toggleKeySensitive, toggleKeyEnabled, revokeKey, editOwnerPrompt, deleteKeyForever, purgeAllRevokedKeys,
          userTierSubtab, ordersList, orderCounts, orderStatusFilter, loadingOrders, adminPayConfig, savingPayConfig, orderStatusText, orderStatusClass, loadAdminOrders, adminConfirmOrder, adminCancelOrder, loadAdminPayConfig, saveAdminPayConfig, handleQrUpload, refreshUserTiersData, usersList, loadingUsers, loadUsersWithTiers, changeUserTier, toggleUserRole, grantLegacyAllUsers, tierClass, tierName,
          forensics, proxiesList, testingProxies, opsService, opsDate, opsLevel, opsLines, opsFilter, opsFilterPills, opsLogs, opsAutoRefresh, opsTerminalBox,
          formatRemainingSeconds, setOpsQuickDate, loadForensics, fetchProxies, loadOpsLogs, clearOpsLogs, downloadServerOpsLogs, exportOpsLogs, getLogLineClass,
          coldStats, regConfig, regCount, regMode, wakeCount, isRegisterRunning, stoppingRegister, regLogs, regTerminalBox, autoScrollRegLogs, getLogLevelColor,
          checkingTokens, tokenCheckResult, checkTokens,
          quickReg, runQuickPreflight, startQuickRegister, verifyQuickRegisterOtp, completeQuickRegister,
          loadRegisterData, saveRegConfig, startRegister, stopRegister, startWakeCold, clearRegLogs,
          regWorkMode, manualLoading, manualCandidate, showPassword, emailCopied, passwordCopied, codeCopied,
          manualCode, manualSubmitting, manualSessionAccounts, manualTokenInput, parsedTokenPreview, viaScriptCopied, copyViaScript, loadManualNext, fetchManualCode, copyText, submitManual, copyAllProduced,
          anymessageTokens, anymessageActiveToken, anymessageActiveBalance, anymessageEstAccounts, anymessageRawText,
          showTokenConfig, savingTokens, showFullToken, loadAnyMessageTokens, saveAnyMessageTokens, switchActiveToken,
          addMode, addForm, submittingAdd, submitAddAccount,
          gatewaySettings, adminPwd, settingsSaved, saveGatewaySettings, changeAdminPwd,
          assignModal, quotaModal, sensitiveModal, createKeyModal, wakeModal, inboxModal,
          openAssignModal, saveAssignModal, openQuotaModal, saveQuotaModal, openSensitiveModal, saveSensitiveModal,
          filteredAssignAccounts, toggleAssignAccount, quickSelectAssign,
          openCreateKeyModal, submitCreateKey, copyGeneratedKey, openWakeModal, submitWakeModal,
          inboxAlerts, inboxFilter, unreadAlertsCount, filteredInboxAlerts,
          openInboxModal, markAlertRead, markAllAlertsRead, clearReadAlerts, getAlertBadgeClass,
          switchTab, refreshAll, doLogout
        };
  }
}
</script>