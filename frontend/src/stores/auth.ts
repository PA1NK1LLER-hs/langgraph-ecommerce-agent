import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import * as api from '../api/client'
import { getToken, setToken, clearToken } from '../api/client'

export const useAuthStore = defineStore('auth', () => {
  // token 统一由 api/client 的 getToken/setToken/clearToken 管理（F12）
  const token = ref(getToken())
  const username = ref(localStorage.getItem('username') || '')

  const isLoggedIn = computed(() => !!token.value)

  function setAuth(t: string, u: string) {
    token.value = t
    username.value = u
    setToken(t)
    localStorage.setItem('username', u)
  }

  function logout() {
    token.value = ''
    username.value = ''
    clearToken()
    localStorage.removeItem('username')
  }

  function checkToken() {
    const t = getToken()
    if (t) token.value = t
  }

  async function login(u: string, p: string) {
    const data = await api.login(u, p)
    setAuth(data.access_token, data.username)
    return data
  }

  async function register(u: string, p: string) {
    const data = await api.register(u, p)
    setAuth(data.access_token, data.username)
    return data
  }

  return { token, username, isLoggedIn, login, register, logout, checkToken }
})
