<script setup lang="ts">
import { ref, onMounted } from 'vue'
import * as api from '../api/client'
import AppIcon from '../components/AppIcon.vue'
import ViewHeader from '../components/ViewHeader.vue'

/** 用户记忆管理页（侧边栏「记忆」入口）：列出 / 添加 / 删除。 */

const memories = ref<{ text: string; category?: string }[]>([])
const loading = ref(false)
const adding = ref(false)
const newMemory = ref('')
const error = ref('')
const confirmingIndex = ref<number | null>(null)  // 两步删除确认

// 后端 Mem0 返回结构随版本变化，做防御性字段提取
function textOf(m: unknown): string {
  if (typeof m === 'string') return m
  if (m && typeof m === 'object') {
    const o = m as Record<string, unknown>
    const t = o.memory ?? o.text ?? o.content
    if (typeof t === 'string') return t
  }
  return String(m ?? '')
}

async function load() {
  loading.value = true
  try {
    const d = await api.getMemories() as { results?: unknown[] }
    memories.value = (d?.results || []).map(m => {
      const o = (m && typeof m === 'object' ? m : {}) as Record<string, unknown>
      return {
        text: textOf(m),
        category: typeof o.category === 'string' ? o.category : '',
      }
    }).filter(m => m.text)
  } catch (_) {
    memories.value = []
  } finally {
    loading.value = false
  }
}

async function add() {
  const content = newMemory.value.trim()
  if (!content || adding.value) return
  adding.value = true
  error.value = ''
  try {
    await api.addMemory(content)
    newMemory.value = ''
    await load()
  } catch (e) {
    error.value = (e as Error).message || '添加失败'
  } finally {
    adding.value = false
  }
}

async function remove(index: number) {
  if (confirmingIndex.value !== index) {
    confirmingIndex.value = index
    // 3 秒未确认则自动取消
    setTimeout(() => { if (confirmingIndex.value === index) confirmingIndex.value = null }, 3000)
    return
  }
  confirmingIndex.value = null
  try {
    // 后端按语义查询删除匹配记忆
    await api.deleteMemory(memories.value[index].text.slice(0, 200))
    await load()
  } catch (e) {
    error.value = (e as Error).message || '删除失败'
  }
}

onMounted(load)
</script>

<template>
  <div class="flex-1 flex flex-col min-w-0 min-h-0">
    <ViewHeader title="记忆" :subtitle="`${memories.length} 条长期记忆`">
      <template #actions>
        <button
          @click="load"
          class="flex items-center gap-1.5 px-3 py-1.5 rounded-btn text-[12px] font-medium text-ink-2
                 hover:bg-surface hover:text-ink transition-colors duration-150">
          <AppIcon name="refresh" :size="13" />
          刷新
        </button>
      </template>
    </ViewHeader>

    <div class="flex-1 min-h-0 overflow-y-auto">
      <div class="max-w-[860px] mx-auto px-6 py-8">

        <!-- 添加记忆 -->
        <div class="flex items-center gap-3 mb-8">
          <div class="flex-1 flex items-center gap-2.5 px-4 h-11 rounded-ctrl bg-white/60 border border-line
                      shadow-soft focus-within:border-ink/15 focus-within:shadow-card focus-within:bg-white/80 transition-all duration-200">
            <AppIcon name="memory" :size="15" class="text-ink-3 shrink-0" />
            <input
              v-model="newMemory"
              type="text"
              placeholder="例如：用户偏好简洁的中文回复"
              class="flex-1 bg-transparent outline-none text-[14px] placeholder:text-ink-3"
              @keyup.enter="add"
            />
          </div>
          <button
            @click="add"
            :disabled="!newMemory.trim() || adding"
            class="h-11 px-5 rounded-btn bg-accent hover:bg-accent-hover text-white text-[13px] font-semibold
                   shadow-soft disabled:bg-surface disabled:text-ink-3 disabled:shadow-none
                   transition-colors duration-150 scale-press shrink-0">
            {{ adding ? '保存中…' : '添加' }}
          </button>
        </div>

        <!-- 错误 -->
        <div v-if="error"
             class="mb-6 rounded-ctrl bg-danger/5 border border-danger/15 text-danger px-4 py-3 text-[13px] font-medium">
          {{ error }}
        </div>

        <!-- 加载中 -->
        <div v-if="loading" class="flex flex-col items-center py-20">
          <span class="text-[13px] font-medium text-ink-3 animate-pulse">正在加载记忆…</span>
        </div>

        <!-- 空状态 -->
        <div v-else-if="memories.length === 0" class="flex flex-col items-center py-24">
          <div class="w-14 h-14 rounded-card bg-surface flex items-center justify-center mb-4">
            <AppIcon name="memory" :size="26" class="text-ink-3" />
          </div>
          <p class="text-[14px] font-semibold text-ink">暂无记忆</p>
          <p class="text-[12px] text-ink-3 mt-1.5">
            添加一条记忆，Agent 将在后续对话中自动参考
          </p>
        </div>

        <!-- 记忆列表 -->
        <div v-else class="space-y-3">
          <div
            v-for="(m, i) in memories"
            :key="i"
            class="group flex items-start gap-3 glass-card rounded-card shadow-soft p-5
                   hover:shadow-card transition-shadow duration-200">
            <div class="w-8 h-8 rounded-btn bg-accent-soft text-accent flex items-center justify-center shrink-0 mt-0.5">
              <AppIcon name="memory" :size="15" />
            </div>
            <div class="flex-1 min-w-0">
              <p class="text-[14px] leading-relaxed text-ink whitespace-pre-wrap break-words">{{ m.text }}</p>
              <p v-if="m.category" class="text-[11px] font-medium text-ink-3 mt-1.5">{{ m.category }}</p>
            </div>
            <button
              @click="remove(i)"
              :aria-label="`删除记忆 ${m.text.slice(0, 20)}`"
              :class="[
                'flex items-center gap-1 px-2.5 py-1.5 rounded-btn text-[12px] font-medium shrink-0 transition-all duration-150',
                confirmingIndex === i
                  ? 'bg-danger text-white'
                  : 'text-danger opacity-0 group-hover:opacity-100 focus-visible:opacity-100 hover:bg-danger/10',
              ]">
              <AppIcon name="trash" :size="13" />
              {{ confirmingIndex === i ? '确认删除?' : '删除' }}
            </button>
          </div>
        </div>

      </div>
    </div>
  </div>
</template>
