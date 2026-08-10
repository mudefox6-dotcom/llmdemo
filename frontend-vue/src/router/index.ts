import { createRouter, createWebHistory } from 'vue-router'
import { hasValidToken } from '@/composables/useAuth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/Login.vue'),
      // public: 不需要登录就能访问，也是唯一不套 AppLayout（侧边栏）的页面
      meta: { public: true },
    },
    // 根路径直接落到创建任务页：这是使用系统的第一步
    {
      path: '/',
      redirect: { name: 'create' },
    },
    {
      path: '/create',
      name: 'create',
      component: () => import('@/views/CreateTask.vue'),
    },
    {
      path: '/history',
      name: 'history',
      component: () => import('@/views/TaskHistory.vue'),
    },
    {
      path: '/task/:id',
      name: 'detail',
      component: () => import('@/views/TaskDetail.vue'),
    },
  ],
})

/**
 * 全局前置守卫。
 *
 * 只做本地 token 有效性判断（存在 + 未过期），不去请求后端校验——
 * 每次跳路由都发一个请求会让页面切换变卡；万一 token 其实已失效，
 * 后续第一个业务请求会返回 401，由 axios 响应拦截器再踢回登录页。
 *
 * 已登录时访问 /login 直接送去首页，避免出现"登录着还停在登录页"的困惑。
 */
router.beforeEach((to) => {
  const loggedIn = hasValidToken()
  if (!loggedIn && !to.meta.public) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (loggedIn && to.name === 'login') {
    return { name: 'create' }
  }
  return true
})

export default router
