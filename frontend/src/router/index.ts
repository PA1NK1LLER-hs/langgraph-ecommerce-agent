import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('../views/LoginView.vue'),
      meta: { guest: true },
    },
    {
      path: '/register',
      name: 'register',
      component: () => import('../views/RegisterView.vue'),
      meta: { guest: true },
    },
    {
      // 工作台壳层：左侧固定侧边栏 + 右侧主区（对话 / 知识库 / 记忆）
      path: '/',
      component: () => import('../layouts/WorkspaceLayout.vue'),
      meta: { requiresAuth: true },
      children: [
        {
          path: 'chat/:threadId?',
          name: 'chat',
          component: () => import('../views/ChatView.vue'),
        },
        {
          path: 'kb',
          name: 'kb',
          component: () => import('../views/KBManagement.vue'),
        },
        {
          path: 'knowledge',
          name: 'knowledge',
          component: () => import('../views/KnowledgeView.vue'),
        },
        {
          path: 'memories',
          name: 'memories',
          component: () => import('../views/MemoriesView.vue'),
        },
        {
          path: 'rpa',
          name: 'rpa',
          component: () => import('../views/RPAJobsView.vue'),
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/chat' },
  ],
})

router.beforeEach((to, _from, next) => {
  const auth = useAuthStore()
  if (to.meta.requiresAuth && !auth.isLoggedIn) {
    next('/login')
  } else if (to.meta.guest && auth.isLoggedIn) {
    next('/chat')
  } else {
    next()
  }
})

export default router
