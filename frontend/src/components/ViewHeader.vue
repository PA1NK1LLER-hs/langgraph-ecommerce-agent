<script setup lang="ts">
import AppIcon from './AppIcon.vue'

/** 工作台子页面（知识库管理 / 知识库 / 记忆）的毛玻璃页头。
 * 左侧返回对话入口 + 标题，右侧动作插槽。
 */
defineProps<{ title: string; subtitle?: string }>()
</script>

<template>
  <header class="glass h-14 px-6 flex items-center gap-3 shrink-0 border-b border-line">
    <router-link
      to="/chat"
      class="w-7 h-7 flex items-center justify-center rounded-btn text-ink-2 hover:bg-surface hover:text-ink
             transition-colors duration-150 no-underline"
      aria-label="返回对话">
      <AppIcon name="back" :size="16" />
    </router-link>
    <div class="min-w-0">
      <h1 class="text-[15px] font-semibold text-ink leading-tight truncate">{{ title }}</h1>
      <p v-if="subtitle" class="text-[11px] text-ink-3 leading-tight truncate">{{ subtitle }}</p>
    </div>
    <div class="ml-auto flex items-center gap-2.5">
      <slot name="actions" />
    </div>
  </header>
</template>
