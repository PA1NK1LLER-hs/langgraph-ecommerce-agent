<script setup lang="ts">
import { useAuthStore } from './stores/auth'
import { onMounted } from 'vue'

const auth = useAuthStore()
onMounted(() => auth.checkToken())
</script>

<template>
  <router-view />
</template>
