<script setup lang="ts">
import { ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useThreadsStore } from '../stores/threads'
import AppIcon from './AppIcon.vue'

/** 左侧固定侧边栏（桌面端工作台）：
 * 1. 毛玻璃品牌头部（Logo + Agent Hub）
 * 2. 用户信息（账号 + 退出）
 * 3. 「+ 新建对话」圆角主按钮
 * 4. 可滚动对话历史列表
 * 5. 底部导航：知识库管理 / 知识库 / 记忆
 */
const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const threads = useThreadsStore()

const hoveringThread = ref<string | null>(null)

const navItems = [
  { to: '/kb', label: '知识库管理', icon: 'database' },
  { to: '/knowledge', label: '知识库', icon: 'search' },
  { to: '/memories', label: '记忆', icon: 'memory' },
  { to: '/rpa', label: 'RPA 任务', icon: 'rpa' },
]

async function newThread() {
  const t = await threads.createThread()
  if (t) router.push(`/chat/${t.thread_id}`)
  else router.push('/chat')  // 后端不可用时仍进入空白会话
}

function switchThread(threadId: string) {
  if (route.params.threadId === threadId) return
  router.push(`/chat/${threadId}`)
}

async function removeThread(threadId: string, event: Event) {
  event.stopPropagation()
  await threads.removeThread(threadId)
  if (route.params.threadId === threadId) router.push('/chat')
}

function logout() {
  auth.logout()
  router.push('/login')
}

function formatDate(d: string) {
  if (!d) return ''
  const now = new Date()
  const date = new Date(d)
  const diff = now.getTime() - date.getTime()
  if (diff < 86400000) return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  if (diff < 604800000) return ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][date.getDay()]
  return date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
}
</script>

<template>
  <aside class="glass w-72 min-w-72 flex flex-col shrink-0 border-r border-line">
    <!-- 1. 毛玻璃品牌头部 -->
    <div class="h-16 px-5 flex items-center gap-3 shrink-0 border-b border-line">
      <div class="w-9 h-9 rounded-[10px] flex items-center justify-center shrink-0 shadow-soft"
           style="background: linear-gradient(135deg, #4A4A4E 0%, #1D1D1F 100%);">
        <AppIcon name="hub" :size="18" :stroke-width="2.2" class="text-white" />
      </div>
      <span class="text-[15px] font-bold tracking-tight text-ink">Agent Hub</span>
    </div>

    <!-- 2. 用户信息 -->
    <div class="px-4 py-3 flex items-center gap-2.5 shrink-0 border-b border-line">
      <div class="w-8 h-8 rounded-full bg-accent-soft text-accent flex items-center justify-center shrink-0">
        <span class="text-[13px] font-semibold">{{ auth.username.charAt(0).toUpperCase() }}</span>
      </div>
      <div class="min-w-0">
        <div class="text-[13px] font-semibold text-ink truncate">{{ auth.username }}</div>
        <div class="text-[11px] text-ink-3 leading-tight">在线</div>
      </div>
      <button
        @click="logout"
        class="ml-auto flex items-center gap-1.5 px-2.5 py-1.5 rounded-btn text-[12px] font-medium text-ink-2
               hover:bg-surface hover:text-ink transition-colors duration-150 scale-press"
        aria-label="退出登录">
        <AppIcon name="logout" :size="14" />
        退出
      </button>
    </div>

    <!-- 3. 新建对话主按钮 -->
    <div class="px-4 py-3 shrink-0">
      <button
        @click="newThread"
        class="w-full h-10 flex items-center justify-center gap-2 rounded-btn text-[13px] font-semibold text-white
               bg-accent hover:bg-accent-hover shadow-soft transition-colors duration-150 scale-press">
        <AppIcon name="plus" :size="15" :stroke-width="2.2" />
        新建对话
      </button>
    </div>

    <!-- 4. 对话历史列表（可滚动） -->
    <div class="flex-1 overflow-y-auto px-3 py-2">
      <div class="px-2 pt-1 pb-2 text-[11px] font-semibold text-ink-3">对话历史</div>

      <div v-if="threads.loading" class="space-y-1.5">
        <div v-for="i in 3" :key="i" class="h-11 rounded-btn shimmer" />
      </div>

      <div v-else-if="threads.threads.length === 0" class="flex flex-col items-center py-10">
        <div class="w-10 h-10 rounded-btn bg-surface flex items-center justify-center mb-2.5">
          <AppIcon name="chat" :size="18" class="text-ink-3" />
        </div>
        <p class="text-[12px] font-medium text-ink-3">暂无对话</p>
      </div>

      <div v-else class="space-y-0.5 pb-2">
        <div
          v-for="t in threads.threads"
          :key="t.thread_id"
          @click="switchThread(t.thread_id)"
          @mouseenter="hoveringThread = t.thread_id"
          @mouseleave="hoveringThread = null"
          :class="[
            'group relative flex items-center gap-2 px-3 py-2.5 rounded-btn cursor-pointer transition-colors duration-150',
            route.params.threadId === t.thread_id
              ? 'bg-accent-soft text-accent'
              : 'text-ink-2 hover:bg-surface',
          ]">
          <div class="flex-1 min-w-0">
            <div class="text-[13px] font-medium truncate leading-snug">{{ t.title }}</div>
            <div class="text-[11px] mt-0.5" :class="route.params.threadId === t.thread_id ? 'text-accent/70' : 'text-ink-3'">
              {{ formatDate(t.updated_at) }}
            </div>
          </div>
          <button
            :aria-label="`删除对话 ${t.title}`"
            @click="(e: Event) => removeThread(t.thread_id, e)"
            :class="[
              'w-6 h-6 rounded-full flex items-center justify-center shrink-0 transition-all duration-150 text-danger',
              hoveringThread === t.thread_id
                ? 'opacity-100 bg-danger/10'
                : 'opacity-0 group-hover:opacity-100 hover:bg-danger/10 focus-visible:opacity-100',
            ]">
            <AppIcon name="trash" :size="12" />
          </button>
        </div>
      </div>
    </div>

    <!-- 5. 底部导航 -->
    <nav class="shrink-0 border-t border-line p-2 space-y-0.5">
      <router-link
        v-for="item in navItems"
        :key="item.to"
        :to="item.to"
        :class="[
          'flex items-center gap-2.5 px-3 py-2 rounded-btn text-[13px] font-medium transition-colors duration-150 no-underline',
          route.path === item.to
            ? 'bg-accent-soft text-accent'
            : 'text-ink-2 hover:bg-surface hover:text-ink',
        ]">
        <AppIcon :name="item.icon" :size="15" />
        {{ item.label }}
      </router-link>
    </nav>
  </aside>
</template>
