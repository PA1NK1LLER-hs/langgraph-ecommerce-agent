<script setup lang="ts">
import { ref } from 'vue'
import * as api from '../api/client'
import AppIcon from '../components/AppIcon.vue'
import ViewHeader from '../components/ViewHeader.vue'

/** 知识库语义搜索页（侧边栏「知识库」入口）。 */

const query = ref('')
const mode = ref<'dense' | 'hybrid'>('hybrid')
const searching = ref(false)
const searched = ref(false)
const results = ref<Record<string, unknown>[]>([])
const error = ref('')

async function doSearch() {
  const q = query.value.trim()
  if (!q || searching.value) return
  searching.value = true
  searched.value = true
  error.value = ''
  try {
    const d = await api.searchKB(q, 5, mode.value) as { results?: Record<string, unknown>[] }
    results.value = Array.isArray(d?.results) ? d.results : []
  } catch (e) {
    error.value = (e as Error).message || '搜索失败'
    results.value = []
  } finally {
    searching.value = false
  }
}

// ── 后端 dense/hybrid 两种模式返回结构不同，做防御性字段提取 ──
function textOf(r: Record<string, unknown>): string {
  const t = r.text ?? r.content ?? r.snippet ?? r.chunk ?? r.title
  return typeof t === 'string' ? t : ''
}
function scoreOf(r: Record<string, unknown>): number | null {
  return typeof r.score === 'number' ? r.score : null
}
function sourceOf(r: Record<string, unknown>): string {
  const s = r.source ?? r.file_name ?? r.doc_id ?? r.doc_name
  return typeof s === 'string' ? s : ''
}

const modeOptions = [
  { value: 'hybrid', label: '混合检索' },
  { value: 'dense', label: '语义检索' },
] as const
</script>

<template>
  <div class="flex-1 flex flex-col min-w-0 min-h-0">
    <ViewHeader title="知识库" subtitle="语义搜索已索引的文档与网页" />

    <div class="flex-1 min-h-0 overflow-y-auto">
      <div class="max-w-[860px] mx-auto px-6 py-8">

        <!-- 搜索框 + 模式切换 -->
        <div class="flex items-center gap-3 mb-8">
          <div class="flex-1 flex items-center gap-2.5 px-4 h-11 rounded-ctrl bg-white/60 border border-line
                      shadow-soft focus-within:border-ink/15 focus-within:shadow-card focus-within:bg-white/80 transition-all duration-200">
            <AppIcon name="search" :size="15" class="text-ink-3 shrink-0" />
            <input
              v-model="query"
              type="text"
              placeholder="输入关键词，搜索知识库…"
              class="flex-1 bg-transparent outline-none text-[14px] placeholder:text-ink-3"
              @keyup.enter="doSearch"
            />
            <button
              v-if="query"
              @click="query = ''"
              class="w-5 h-5 flex items-center justify-center rounded-full text-ink-3 hover:bg-surface hover:text-ink transition-colors">
              <AppIcon name="x" :size="12" />
            </button>
          </div>

          <!-- 分段控件（Apple 风格） -->
          <div class="flex items-center bg-surface rounded-ctrl p-1 shrink-0">
            <button
              v-for="opt in modeOptions"
              :key="opt.value"
              @click="mode = opt.value"
              :class="[
                'px-3.5 py-1.5 rounded-[9px] text-[12px] font-medium transition-all duration-150',
                mode === opt.value ? 'bg-white shadow-soft text-ink' : 'text-ink-2 hover:text-ink',
              ]">
              {{ opt.label }}
            </button>
          </div>

          <button
            @click="doSearch"
            :disabled="!query.trim() || searching"
            class="h-11 px-5 rounded-btn bg-accent hover:bg-accent-hover text-white text-[13px] font-semibold
                   shadow-soft disabled:bg-surface disabled:text-ink-3 disabled:shadow-none
                   transition-colors duration-150 scale-press shrink-0">
            {{ searching ? '搜索中…' : '搜索' }}
          </button>
        </div>

        <!-- 错误 -->
        <div v-if="error"
             class="mb-6 rounded-ctrl bg-danger/5 border border-danger/15 text-danger px-4 py-3 text-[13px] font-medium">
          {{ error }}
        </div>

        <!-- 加载中 -->
        <div v-if="searching" class="flex flex-col items-center py-20">
          <span class="text-[13px] font-medium text-ink-3 animate-pulse">正在检索知识库…</span>
        </div>

        <!-- 空初始状态 -->
        <div v-else-if="!searched" class="flex flex-col items-center py-24">
          <div class="w-14 h-14 rounded-card bg-surface flex items-center justify-center mb-4">
            <AppIcon name="book" :size="26" class="text-ink-3" />
          </div>
          <p class="text-[14px] font-semibold text-ink">搜索知识库</p>
          <p class="text-[12px] text-ink-3 mt-1.5">输入关键词，检索已索引的文档与网页内容</p>
        </div>

        <!-- 无结果 -->
        <div v-else-if="results.length === 0" class="flex flex-col items-center py-20">
          <p class="text-[14px] font-semibold text-ink">未找到相关内容</p>
          <p class="text-[12px] text-ink-3 mt-1.5">换个关键词，或先到「知识库管理」上传文档</p>
        </div>

        <!-- 结果列表 -->
        <div v-else class="space-y-3">
          <div
            v-for="(r, i) in results"
            :key="i"
            class="glass-card rounded-card shadow-soft p-5 hover:shadow-card transition-shadow duration-200">
            <div class="whitespace-pre-wrap break-words text-[14px] leading-relaxed text-ink">
              {{ textOf(r) }}
            </div>
            <div class="flex items-center gap-3 mt-3 text-[11px] font-medium text-ink-3">
              <span v-if="scoreOf(r) !== null"
                    class="px-2 py-0.5 rounded-full bg-accent-soft text-accent font-semibold">
                相关度 {{ ((scoreOf(r) ?? 0) * 100).toFixed(0) }}%
              </span>
              <span v-if="sourceOf(r)" class="truncate">{{ sourceOf(r) }}</span>
            </div>
          </div>
        </div>

      </div>
    </div>
  </div>
</template>
