import { defineStore } from 'pinia'
import { ref } from 'vue'
import * as api from '../api/client'
import type { ThreadItem } from '../api/client'

/** 对话线程列表（侧边栏历史）。侧边栏与聊天视图共用：
 * ChatView 在 title_updated / done 时 upsert / 刷新，保证标题与排序实时。
 */
export const useThreadsStore = defineStore('threads', () => {
  const threads = ref<ThreadItem[]>([])
  const loading = ref(false)

  async function loadThreads() {
    loading.value = true
    try {
      const d = await api.listThreads()
      threads.value = d.threads || []
    } catch (_) {
      // 静默失败：侧边栏保持现状，避免每次刷新弹错
    } finally {
      loading.value = false
    }
  }

  async function createThread(): Promise<ThreadItem | null> {
    try {
      const t = await api.createThread()
      threads.value.unshift(t)
      return t
    } catch (_) {
      return null
    }
  }

  async function removeThread(threadId: string) {
    try {
      await api.deleteThread(threadId)
      threads.value = threads.value.filter(t => t.thread_id !== threadId)
    } catch (_) {
      // 删除失败保持列表原样
    }
  }

  /** 局部更新（标题变更）或插入不存在的线程。 */
  function upsert(patch: Partial<ThreadItem> & { thread_id: string }) {
    const i = threads.value.findIndex(t => t.thread_id === patch.thread_id)
    if (i >= 0) {
      threads.value[i] = { ...threads.value[i], ...patch }
    }
  }

  function titleOf(threadId: string): string {
    return threads.value.find(t => t.thread_id === threadId)?.title || '新对话'
  }

  return { threads, loading, loadThreads, createThread, removeThread, upsert, titleOf }
})
