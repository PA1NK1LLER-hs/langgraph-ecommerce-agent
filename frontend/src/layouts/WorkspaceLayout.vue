<script setup lang="ts">
import { onMounted } from 'vue'
import Sidebar from '../components/Sidebar.vue'
import { useThreadsStore } from '../stores/threads'

/** 桌面端工作台壳层：左侧固定侧边栏 + 右侧主区（router-view）。
 * 对话 / 知识库管理 / 知识库 / 记忆 四个页面共享同一壳层。
 */
const threads = useThreadsStore()

onMounted(() => {
  threads.loadThreads()
})
</script>

<template>
  <div class="flex h-screen overflow-hidden text-ink">
    <Sidebar />
    <main class="flex-1 min-w-0 min-h-0 flex flex-col">
      <router-view />
    </main>
  </div>
</template>
