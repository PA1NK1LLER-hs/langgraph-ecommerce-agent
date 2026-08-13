<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import AppIcon from '../components/AppIcon.vue'

const router = useRouter()
const auth = useAuthStore()
const username = ref('')
const password = ref('')
const error = ref('')
const loading = ref(false)

async function handleLogin() {
  error.value = ''
  loading.value = true
  try {
    await auth.login(username.value, password.value)
    router.push('/chat')
  } catch (e: any) {
    error.value = e.message || '登录失败'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="min-h-screen flex items-center justify-center px-4">
    <div class="w-full max-w-sm glass-card rounded-card shadow-float p-10">
      <!-- Logo -->
      <div class="text-center mb-8">
        <div class="inline-flex items-center justify-center w-16 h-16 rounded-[18px] mb-6 shadow-card"
             style="background: linear-gradient(135deg, #4A4A4E 0%, #1D1D1F 100%);">
          <AppIcon name="hub" :size="32" :stroke-width="2" class="text-white" />
        </div>
        <h1 class="text-[26px] font-bold tracking-[-0.02em] text-ink">Agent Hub</h1>
        <p class="text-[13px] text-ink-3 mt-2 font-normal">知识检索 · 联网搜索 · 代码执行 · RPA 自动化</p>
      </div>

      <!-- Form -->
      <form @submit.prevent="handleLogin" class="space-y-4">
        <div>
          <input v-model="username" type="text" required
            class="w-full h-12 px-4 text-[14px] rounded-ctrl border border-line bg-white/60
                   placeholder:text-ink-3 focus:outline-none focus:border-ink/20 focus:ring-2 focus:ring-ink/10 focus:bg-white
                   transition-all duration-200"
            placeholder="用户名" />
        </div>
        <div>
          <input v-model="password" type="password" required
            class="w-full h-12 px-4 text-[14px] rounded-ctrl border border-line bg-white/60
                   placeholder:text-ink-3 focus:outline-none focus:border-ink/20 focus:ring-2 focus:ring-ink/10 focus:bg-white
                   transition-all duration-200"
            placeholder="密码" />
        </div>

        <div v-if="error"
          class="text-[13px] text-danger bg-danger/5 rounded-ctrl px-4 py-2.5 font-medium">
          {{ error }}
        </div>

        <button type="submit" :disabled="loading"
          class="w-full h-12 text-[14px] font-semibold text-white rounded-btn
                 transition-all duration-200 cursor-pointer
                 disabled:opacity-40 disabled:cursor-not-allowed
                 bg-ink hover:bg-[#3A3A3C] shadow-soft">
          {{ loading ? '登录中...' : '登录' }}
        </button>
      </form>

      <p class="text-center mt-8 text-[13px] text-ink-3">
        还没有账号？
        <router-link to="/register" class="text-accent font-medium hover:underline">注册</router-link>
      </p>
    </div>
  </div>
</template>
