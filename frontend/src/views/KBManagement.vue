<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import * as api from '../api/client'
import type { KBSource } from '../api/client'
import AppIcon from '../components/AppIcon.vue'
import ViewHeader from '../components/ViewHeader.vue'

/** 知识库管理页：上传 / URL 导入 / 来源列表 / 分块查看。 */

// ── 状态 ──
const sources = ref<KBSource[]>([])
const loading = ref(false)
const uploadMsg = ref('')
const uploadError = ref('')
const urlInput = ref('')
const urlTags = ref('')
const urlMsg = ref('')
const urlError = ref('')

// 拖拽上传
const dragging = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const uploading = ref(false)
// 注：后端 upload 接口同步索引完才返回（F14），无需轮询进度，仅展示进行中状态

// 删除二次确认（替代原生 confirm/alert，U4）
const confirmingSource = ref<string | null>(null)
const deleteError = ref('')

// 更新（重新索引）：两步确认 → 选择新版文件 → 删旧导新
const updateSourceName = ref<string | null>(null)
const updateInput = ref<HTMLInputElement | null>(null)
const updating = ref(false)
const updateMsg = ref('')
const updateError = ref('')

// 选中查看分块
const selectedSource = ref<string | null>(null)
const chunks = ref<{ content: string; score?: number; chunk_id?: string }[]>([])
const chunksLoading = ref(false)

// 排序
const sortKey = ref<'source' | 'chunks'>('source')
const sortAsc = ref(true)

// ── 计算属性 ──
const sortedSources = computed(() => {
  const arr = [...sources.value]
  arr.sort((a, b) => {
    const va = a[sortKey.value]
    const vb = b[sortKey.value]
    if (typeof va === 'string' && typeof vb === 'string') {
      return sortAsc.value ? va.localeCompare(vb) : vb.localeCompare(va)
    }
    return sortAsc.value ? (va as number) - (vb as number) : (vb as number) - (va as number)
  })
  return arr
})

const totalChunks = computed(() => sources.value.reduce((s, x) => s + x.chunks, 0))

// ── 加载 ──
async function loadSources() {
  loading.value = true
  try {
    const d = await api.getKBStats()
    sources.value = (d.sources || []).map((s: any) => ({
      source: s.source || 'unknown',
      chunks: s.chunks || 0,
      last_indexed: s.last_indexed || '',
    }))
  } catch (_) { }
  loading.value = false
}

// ── 上传 ──
function triggerUpload() { fileInput.value?.click() }

async function handleFile(e: Event) {
  const target = e.target as HTMLInputElement
  const file = target.files?.[0]
  if (!file) return
  await doUpload(file)
  target.value = ''
}

async function doUpload(file: File) {
  uploading.value = true
  uploadMsg.value = ''
  uploadError.value = ''
  try {
    const result = await api.uploadFile(file)
    uploadMsg.value = `${file.name} — ${result.indexed_chunks}/${result.total_chunks} 块已索引`
    loadSources()
  } catch (e: any) {
    uploadError.value = e.message || '上传失败'
  }
  uploading.value = false
}

async function handleDrop(e: DragEvent) {
  dragging.value = false
  const file = e.dataTransfer?.files?.[0]
  if (file) await doUpload(file)
}

// ── URL 导入 ──
async function handleImportURL() {
  const url = urlInput.value.trim()
  if (!url) return
  urlMsg.value = '正在抓取并索引...'
  urlError.value = ''
  try {
    const r = await api.importURL(url, urlTags.value)
    urlMsg.value = `导入完成 — ${r.indexed_chunks}/${r.total_chunks} 块已索引`
    urlInput.value = ''
    urlTags.value = ''
    loadSources()
  } catch (e: any) {
    urlError.value = e.message || '导入失败'
  }
}

// ── 查看分块 ──
async function viewChunks(source: string) {
  if (selectedSource.value === source) {
    selectedSource.value = null
    chunks.value = []
    return
  }
  selectedSource.value = source
  chunksLoading.value = true
  try {
    const d = await api.getSourceChunks(source)
    chunks.value = d.chunks || []
  } catch (_) { }
  chunksLoading.value = false
}

// ── 删除（两步确认，替代原生 confirm/alert，U4）──
async function removeSource(source: string) {
  if (confirmingSource.value !== source) {
    confirmingSource.value = source
    deleteError.value = ''
    // 3 秒未确认则自动取消
    setTimeout(() => { if (confirmingSource.value === source) confirmingSource.value = null }, 3000)
    return
  }
  confirmingSource.value = null
  try {
    await api.deleteSource(source)
    sources.value = sources.value.filter(s => s.source !== source)
    if (selectedSource.value === source) { selectedSource.value = null; chunks.value = [] }
  } catch (e: any) {
    deleteError.value = e.message || '删除失败'
  }
}

// ── 更新（删旧 → 导新）──
async function requestUpdate(source: string) {
  if (updateSourceName.value !== source) {
    updateSourceName.value = source
    updateError.value = ''
    // 3 秒未确认则自动取消
    setTimeout(() => { if (updateSourceName.value === source) updateSourceName.value = null }, 3000)
    return
  }
  updateInput.value?.click()
}

async function onUpdateFile(e: Event) {
  const target = e.target as HTMLInputElement
  const file = target.files?.[0]
  const source = updateSourceName.value
  target.value = ''
  if (!file || !source) { updateSourceName.value = null; return }
  await doUpdate(source, file)
  updateSourceName.value = null
}

async function doUpdate(source: string, file: File) {
  updating.value = true
  updateMsg.value = ''
  updateError.value = ''
  try {
    const r = await api.reindexSource(source, file)
    updateMsg.value = `${source} 已更新 — 移除旧索引 ${r.removed_old_docs} 篇，重新索引 ${r.indexed_chunks}/${r.total_chunks} 块`
    loadSources()
  } catch (e: any) {
    updateError.value = e.message || '更新失败'
  }
  updating.value = false
}

function formatDate(d: string): string {
  if (!d) return '—'
  return new Date(d).toLocaleDateString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function fileIcon(source: string): string {
  const ext = source.split('.').pop()?.toLowerCase() || ''
  const icons: Record<string, string> = { pdf: '📄', docx: '📝', doc: '📝', xlsx: '📊', xls: '📊', pptx: '📽️', ppt: '📽️', txt: '📃', md: '📝', csv: '📊', html: '🌐', htm: '🌐', json: '📋', xml: '📋', png: '🖼️', jpg: '🖼️', jpeg: '🖼️', gif: '🖼️', webp: '🖼️' }
  return icons[ext] || '📁'
}

// ── 生命周期 ──
onMounted(() => { loadSources() })
</script>

<template>
  <div class="flex-1 flex flex-col min-w-0 min-h-0">
    <ViewHeader title="知识库管理" :subtitle="`${sources.length} 个来源 · ${totalChunks} 个分块`">
      <template #actions>
        <button
          @click="loadSources"
          class="flex items-center gap-1.5 px-3 py-1.5 rounded-btn text-[12px] font-medium text-ink-2
                 hover:bg-surface hover:text-ink transition-colors duration-150">
          <AppIcon name="refresh" :size="13" />
          刷新
        </button>
        <button
          @click="triggerUpload"
          class="flex items-center gap-1.5 h-9 px-4 rounded-btn bg-accent hover:bg-accent-hover text-white
                 text-[13px] font-semibold shadow-soft transition-colors duration-150 scale-press">
          <AppIcon name="plus" :size="14" :stroke-width="2.2" />
          上传文档
        </button>
      </template>
    </ViewHeader>

    <div class="flex-1 min-h-0 overflow-y-auto">
      <div class="max-w-[860px] mx-auto px-6 py-8 space-y-6">

        <!-- 上传区 -->
        <div
          :class="[
            'relative rounded-card border-2 border-dashed p-10 text-center transition-all duration-200',
            dragging ? 'border-ink/30 bg-black/5' : 'border-line bg-white/40 hover:border-ink/20',
          ]"
          @dragover.prevent="dragging = true"
          @dragleave="dragging = false"
          @drop.prevent="handleDrop"
        >
          <div v-if="uploading" class="space-y-3">
            <div class="flex items-center justify-center gap-2 text-[14px] font-semibold text-ink">
              <span class="w-2 h-2 rounded-full bg-accent animate-pulse" />
              正在解析并索引...
            </div>
            <p class="text-[12px] text-ink-3">正在解析文档、分块并写入向量库，请稍候…</p>
          </div>
          <div v-else>
            <div class="w-12 h-12 rounded-card bg-accent-soft text-accent flex items-center justify-center mx-auto mb-4">
              <AppIcon name="upload" :size="22" />
            </div>
            <p class="text-[14px] font-semibold text-ink">拖拽文件到此处上传</p>
            <p class="text-[12px] mt-1.5 text-ink-3">
              支持 PDF · DOCX · XLSX · PPTX · TXT · CSV · HTML · MD · 图片
            </p>
            <button
              @click="triggerUpload"
              class="mt-5 text-[12px] font-semibold px-4 py-2 rounded-btn bg-surface text-ink-2
                     hover:text-ink transition-colors duration-150">
              选择文件
            </button>
          </div>
          <input ref="fileInput" type="file" class="hidden"
            accept=".pdf,.docx,.doc,.xlsx,.xls,.pptx,.ppt,.txt,.csv,.md,.html,.htm,.json,.xml,.png,.jpg,.jpeg,.gif,.bmp,.webp"
            @change="handleFile" />

          <!-- Messages -->
          <div v-if="uploadMsg" class="mt-3 text-[13px] font-medium text-ok">{{ uploadMsg }}</div>
          <div v-if="uploadError" class="mt-3 text-[13px] font-medium text-danger">{{ uploadError }}</div>
        </div>

        <!-- URL 导入 -->
        <div class="glass-card rounded-card p-5 shadow-soft">
          <h3 class="flex items-center gap-2 text-[13px] font-semibold text-ink mb-3">
            <AppIcon name="link" :size="14" class="text-ink-2" />
            从 URL 导入
          </h3>
          <div class="flex gap-3">
            <input v-model="urlInput" type="url" placeholder="https://example.com/doc"
              class="flex-1 px-4 py-2.5 rounded-ctrl text-[13px] bg-surface border border-line
                     focus:outline-none focus:border-accent/40 focus:bg-white transition-all duration-200
                     placeholder:text-ink-3"
              @keyup.enter="handleImportURL" />
            <input v-model="urlTags" type="text" placeholder="标签（逗号分隔）"
              class="w-44 px-4 py-2.5 rounded-ctrl text-[13px] bg-surface border border-line
                     focus:outline-none focus:border-accent/40 focus:bg-white transition-all duration-200
                     placeholder:text-ink-3" />
            <button @click="handleImportURL" :disabled="!urlInput.trim()"
              class="px-6 py-2.5 rounded-btn text-[13px] font-semibold text-white bg-accent hover:bg-accent-hover
                     shadow-soft transition-colors duration-150 scale-press disabled:bg-surface
                     disabled:text-ink-3 disabled:shadow-none">
              导入
            </button>
          </div>
          <div v-if="urlMsg" class="mt-2.5 text-[12px] font-medium text-ok">{{ urlMsg }}</div>
          <div v-if="urlError" class="mt-2.5 text-[12px] font-medium text-danger">{{ urlError }}</div>
        </div>

        <!-- 删除/更新错误与提示 -->
        <div v-if="deleteError"
             class="rounded-ctrl px-4 py-3 text-[13px] font-medium bg-danger/5 border border-danger/15 text-danger">
          {{ deleteError }}
        </div>
        <div v-if="updateError"
             class="rounded-ctrl px-4 py-3 text-[13px] font-medium bg-danger/5 border border-danger/15 text-danger">
          {{ updateError }}
        </div>
        <div v-if="updateMsg"
             class="rounded-ctrl px-4 py-3 text-[13px] font-medium bg-ok/5 border border-ok/15 text-ok">
          {{ updateMsg }}
        </div>

        <!-- 来源列表 -->
        <div class="glass-card rounded-card shadow-soft overflow-hidden">
          <!-- 表头 -->
          <div class="flex items-center px-5 py-3 text-[11px] font-semibold uppercase tracking-wider text-ink-3
                      border-b border-line">
            <div class="flex-1 flex items-center gap-1.5 cursor-pointer select-none" @click="sortKey='source'; sortAsc=!sortAsc">
              来源名称
              <span v-if="sortKey==='source'" class="text-[9px]">{{ sortAsc ? '▲' : '▼' }}</span>
            </div>
            <div class="w-24 text-center cursor-pointer select-none" @click="sortKey='chunks'; sortAsc=!sortAsc">
              分块数
              <span v-if="sortKey==='chunks'" class="text-[9px]">{{ sortAsc ? '▲' : '▼' }}</span>
            </div>
            <div class="w-36 text-center">最后更新</div>
            <div class="w-28 text-center">操作</div>
          </div>

          <!-- Loading -->
          <div v-if="loading" class="px-5 py-12 text-center">
            <span class="text-[13px] font-medium text-ink-3 animate-pulse">加载中...</span>
          </div>

          <!-- Empty -->
          <div v-else-if="sources.length === 0" class="px-5 py-14 text-center">
            <div class="w-12 h-12 rounded-card bg-surface flex items-center justify-center mx-auto mb-3">
              <AppIcon name="database" :size="22" class="text-ink-3" />
            </div>
            <p class="text-[14px] font-semibold text-ink">知识库为空</p>
            <p class="text-[12px] mt-1 text-ink-3">上传文档或导入 URL 开始构建知识库</p>
          </div>

          <!-- Rows -->
          <div v-else>
            <div v-for="s in sortedSources" :key="s.source" class="group">
              <div class="flex items-center px-5 py-3.5 transition-colors duration-150 hover:bg-surface/60"
                   :class="{ 'bg-accent-soft/40': selectedSource === s.source }"
                   style="border-bottom: 1px solid var(--color-line);">
                <div class="flex-1 flex items-center gap-3 min-w-0 cursor-pointer" @click="viewChunks(s.source)">
                  <span class="text-base shrink-0">{{ fileIcon(s.source) }}</span>
                  <div class="truncate">
                    <div class="text-[13px] font-semibold truncate text-ink">{{ s.source }}</div>
                    <div class="text-[11px] text-ink-3">
                      {{ selectedSource === s.source ? '已展开分块' : '点击查看分块' }}
                    </div>
                  </div>
                </div>
                <div class="w-24 text-center">
                  <span class="text-[12px] font-semibold px-2.5 py-1 rounded-full bg-surface text-ink-2">
                    {{ s.chunks }}
                  </span>
                </div>
                <div class="w-36 text-center">
                  <span class="text-[12px] text-ink-3">{{ formatDate(s.last_indexed || '') }}</span>
                </div>
                <div class="w-28 flex items-center justify-center gap-2">
                  <button @click="viewChunks(s.source)"
                    class="text-[11px] font-medium px-2.5 py-1.5 rounded-btn text-accent bg-accent-soft
                           hover:opacity-80 transition-opacity duration-150">
                    查看
                  </button>
                  <button @click="requestUpdate(s.source)" :aria-label="`更新来源 ${s.source}`" :disabled="updating"
                    :class="[
                      'text-[11px] font-medium px-2.5 py-1.5 rounded-btn transition-all duration-150',
                      updateSourceName === s.source
                        ? 'bg-ok text-white'
                        : 'text-ink-2 opacity-0 group-hover:opacity-100 focus-visible:opacity-100 hover:bg-surface hover:text-ink',
                    ]">
                    {{ updateSourceName === s.source ? (updating ? '更新中...' : '确认更新?') : '更新' }}
                  </button>
                  <button @click="removeSource(s.source)" :aria-label="`删除来源 ${s.source}`"
                    :class="[
                      'text-[11px] font-medium px-2.5 py-1.5 rounded-btn transition-all duration-150',
                      confirmingSource === s.source
                        ? 'bg-danger text-white'
                        : 'text-danger opacity-0 group-hover:opacity-100 focus-visible:opacity-100 hover:bg-danger/10',
                    ]">
                    {{ confirmingSource === s.source ? '确认删除?' : '删除' }}
                  </button>
                </div>
              </div>

              <!-- Expanded Chunks -->
              <div v-if="selectedSource === s.source"
                   class="px-5 pb-4 pt-1 bg-surface/40" style="border-bottom: 1px solid var(--color-line);">
                <div v-if="chunksLoading" class="text-[12px] py-3 text-center text-ink-3">
                  加载分块...
                </div>
                <div v-else-if="chunks.length === 0" class="text-[12px] py-3 text-center text-ink-3">
                  暂无分块数据
                </div>
                <div v-else class="space-y-2 max-h-80 overflow-y-auto py-2">
                  <div v-for="(c, i) in chunks" :key="i"
                       class="p-3 rounded-ctrl text-[12px] leading-relaxed bg-white/60 border border-line">
                    <div class="flex items-center gap-2 mb-1">
                      <span class="text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-accent-soft text-accent">
                        #{{ i + 1 }}
                      </span>
                      <span v-if="c.score !== undefined" class="text-[10px] text-ink-3">
                        相关度: {{ (c.score * 100).toFixed(1) }}%
                      </span>
                    </div>
                    <div class="whitespace-pre-wrap break-words text-ink-2">
                      {{ c.content?.substring(0, 500) }}{{ c.content?.length > 500 ? '...' : '' }}
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

      </div>
    </div>

    <!-- 更新用隐藏文件选择器（source 由 updateSourceName 决定） -->
    <input ref="updateInput" type="file" class="hidden"
      accept=".pdf,.docx,.doc,.xlsx,.xls,.pptx,.ppt,.txt,.csv,.md,.html,.htm,.json,.xml,.png,.jpg,.jpeg,.gif,.bmp,.webp"
      @change="onUpdateFile" />
  </div>
</template>
