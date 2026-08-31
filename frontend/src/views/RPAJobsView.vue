<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import * as api from '../api/client'
import type { RpaJob } from '../api/client'
import AppIcon from '../components/AppIcon.vue'
import ViewHeader from '../components/ViewHeader.vue'

/** RPA 任务面板（侧边栏「RPA 任务」入口）：列出提交的 RPA 任务与状态，5s 自动刷新。 */

const JOB_TYPE_LABELS: Record<string, string> = {
  query_campaign_spend: '广告花费查询',
  collect_amazon_review: '评论/星级采集',
  update_track_table: '轨迹跟踪表更新',
}
const STATUS_LABELS: Record<string, string> = {
  queued: '待执行', running: '执行中', done: '完成', failed: '失败',
}

const jobs = ref<RpaJob[]>([])
const loading = ref(false)
const filter = ref<'all' | api.RpaJobStatus>('all')
const expanded = ref<Set<string>>(new Set())
let timer: ReturnType<typeof setInterval> | null = null

function typeLabel(t: string): string {
  return JOB_TYPE_LABELS[t] || t
}

function statusClass(s: api.RpaJobStatus): string {
  switch (s) {
    case 'running': return 'bg-accent-soft text-accent'
    case 'done': return 'bg-[#10b981]/10 text-[#059669]'
    case 'failed': return 'bg-danger/10 text-danger'
    default: return 'bg-surface text-ink-3'
  }
}

function fmtTime(s: string | null): string {
  if (!s) return '—'
  const d = new Date(s)
  return d.toLocaleString('zh-CN', { hour12: false })
}

function prettyJson(s: string | null): string {
  if (!s) return ''
  try { return JSON.stringify(JSON.parse(s), null, 2) } catch { return s }
}

function toggle(id: string) {
  const set = new Set(expanded.value)
  if (set.has(id)) set.delete(id)
  else set.add(id)
  expanded.value = set
}

async function load() {
  loading.value = true
  try {
    const d = await api.listRpaJobs({ limit: 100, status: filter.value === 'all' ? undefined : filter.value })
    jobs.value = d.jobs
  } catch {
    jobs.value = []
  } finally {
    loading.value = false
  }
}

function setFilter(f: 'all' | api.RpaJobStatus) {
  filter.value = f
  load()
}

onMounted(() => {
  load()
  timer = setInterval(load, 5000)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div class="flex-1 flex flex-col min-w-0 min-h-0">
    <ViewHeader title="RPA 任务" :subtitle="`${jobs.length} 条任务`">
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
      <div class="max-w-[980px] mx-auto px-6 py-6">

        <!-- 状态过滤 -->
        <div class="flex items-center gap-2 mb-5">
          <button
            v-for="f in (['all', 'queued', 'running', 'done', 'failed'] as const)"
            :key="f"
            @click="setFilter(f)"
            :class="[
              'px-3 py-1.5 rounded-btn text-[12px] font-medium transition-colors duration-150',
              filter === f ? 'bg-accent text-white' : 'text-ink-2 hover:bg-surface',
            ]">
            {{ f === 'all' ? '全部' : STATUS_LABELS[f] }}
          </button>
        </div>

        <!-- 空状态 -->
        <div v-if="!loading && jobs.length === 0" class="flex flex-col items-center py-24">
          <div class="w-14 h-14 rounded-card bg-surface flex items-center justify-center mb-4">
            <AppIcon name="rpa" :size="26" class="text-ink-3" />
          </div>
          <p class="text-[14px] font-semibold text-ink">暂无 RPA 任务</p>
          <p class="text-[12px] text-ink-3 mt-1.5">
            在对话中发起 RPA 任务并审批通过后，任务会出现在这里
          </p>
        </div>

        <!-- 加载中 -->
        <div v-else-if="loading && jobs.length === 0" class="flex flex-col items-center py-20">
          <span class="text-[13px] font-medium text-ink-3 animate-pulse">正在加载任务…</span>
        </div>

        <!-- 任务列表 -->
        <div v-else class="glass-card rounded-card shadow-soft overflow-hidden">
          <table class="w-full text-[13px]">
            <thead>
              <tr class="text-left text-[11px] font-semibold text-ink-3 border-b border-line">
                <th class="px-4 py-2.5 font-semibold">任务</th>
                <th class="px-4 py-2.5 font-semibold">状态</th>
                <th class="px-4 py-2.5 font-semibold">提交时间</th>
                <th class="px-4 py-2.5 font-semibold">开始</th>
                <th class="px-4 py-2.5 font-semibold">完成</th>
              </tr>
            </thead>
            <tbody>
              <template v-for="job in jobs" :key="job.job_id">
                <tr
                  @click="toggle(job.job_id)"
                  class="border-b border-line/60 last:border-0 cursor-pointer hover:bg-surface/60 transition-colors duration-100"
                >
                  <td class="px-4 py-3">
                    <div class="flex items-center gap-2">
                      <AppIcon name="rpa" :size="14" class="text-accent shrink-0" />
                      <div class="min-w-0">
                        <div class="text-[13px] font-medium text-ink truncate">{{ typeLabel(job.job_type) }}</div>
                        <div class="text-[11px] text-ink-3 font-mono">{{ job.job_id }}</div>
                      </div>
                    </div>
                  </td>
                  <td class="px-4 py-3">
                    <span :class="['inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-semibold', statusClass(job.status)]">
                      <span v-if="job.status === 'running'" class="w-1.5 h-1.5 rounded-full bg-current animate-pulse" />
                      {{ STATUS_LABELS[job.status] }}
                    </span>
                  </td>
                  <td class="px-4 py-3 text-[12px] text-ink-2">{{ fmtTime(job.created_at) }}</td>
                  <td class="px-4 py-3 text-[12px] text-ink-2">{{ fmtTime(job.started_at) }}</td>
                  <td class="px-4 py-3 text-[12px] text-ink-2">{{ fmtTime(job.finished_at) }}</td>
                </tr>
                <tr v-if="expanded.has(job.job_id)" class="border-b border-line/60 last:border-0 bg-surface/40">
                  <td colspan="5" class="px-4 py-3">
                    <div class="space-y-3">
                      <div>
                        <div class="text-[11px] font-semibold text-ink-3 mb-1">参数</div>
                        <pre class="text-[12px] text-ink-2 whitespace-pre-wrap break-words leading-relaxed">{{ prettyJson(job.params) || '{}' }}</pre>
                      </div>
                      <div v-if="job.result">
                        <div class="text-[11px] font-semibold text-ink-3 mb-1">结果</div>
                        <pre class="text-[12px] text-[#059669] whitespace-pre-wrap break-words leading-relaxed">{{ prettyJson(job.result) }}</pre>
                      </div>
                      <div v-if="job.error">
                        <div class="text-[11px] font-semibold text-ink-3 mb-1">错误</div>
                        <pre class="text-[12px] text-danger whitespace-pre-wrap break-words leading-relaxed">{{ job.error }}</pre>
                      </div>
                    </div>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>

      </div>
    </div>
  </div>
</template>
