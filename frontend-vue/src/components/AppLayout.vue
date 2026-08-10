<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { Menu, X, Zap, LogOut, User } from '@lucide/vue'
import Sidebar from './Sidebar.vue'
import { useAuth } from '@/composables/useAuth'

const router = useRouter()
const route = useRoute()
const sidebarCollapsed = ref(false)
const { username, logout } = useAuth()

function handleLogout() {
  logout()
  router.replace({ name: 'login' })
}

const pageTitles: Record<string, string> = {
  create: '创建任务',
  history: '历史任务',
  detail: '任务详情',
}

const currentTitle = computed(() => {
  const name = (route.name as string) || 'create'
  return pageTitles[name] || name
})

function navigate(name: string) {
  if (name === 'create' || name === 'history') {
    router.push({ name })
  }
}
</script>

<template>
  <div class="app-shell">
    <!-- Header -->
    <header class="app-header">
      <div class="header-left">
        <button class="icon-btn" @click="sidebarCollapsed = !sidebarCollapsed">
          <Menu :size="20" />
        </button>
        <span class="header-brand">
          <span class="brand-icon"><Zap :size="18" /></span>
          MultiSync
        </span>
        <span class="header-sep">/</span>
        <span class="header-page">{{ currentTitle }}</span>
      </div>
      <div class="header-right">
        <span class="status-dot" title="API 已连接"></span>
        <span class="header-status">API v1.0</span>
        <span v-if="username" class="header-user">
          <User :size="13" />
          {{ username }}
        </span>
        <button v-if="username" class="icon-btn" title="退出登录" @click="handleLogout">
          <LogOut :size="17" />
        </button>
      </div>
    </header>

    <div class="app-body">
      <!-- Sidebar -->
      <aside :class="['app-sidebar', { collapsed: sidebarCollapsed }]">
        <Sidebar :collapsed="sidebarCollapsed" @navigate="navigate" />
      </aside>

      <!-- Main Content -->
      <main class="app-main">
        <router-view v-slot="{ Component }">
          <transition name="page" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </main>
    </div>
  </div>
</template>

<style scoped>
.app-shell {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: var(--bg-primary);
}

/* ── Header ──────────────────────────────── */
.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 64px;
  padding: 0 24px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border-light);
  flex-shrink: 0;
  z-index: 100;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.icon-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
}
.icon-btn:hover {
  background: var(--bg-overlay);
  color: var(--text-primary);
}

.header-brand {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 700;
  font-size: 16px;
  color: var(--text-primary);
  letter-spacing: -0.02em;
}
.brand-icon {
  color: var(--accent);
  display: flex;
  width: 30px;
  height: 30px;
  align-items: center;
  justify-content: center;
  border-radius: 9px;
  background: var(--accent-subtle);
}

.header-sep {
  color: var(--border);
  font-size: 18px;
  font-weight: 100;
}

.header-page {
  color: var(--text-secondary);
  font-size: 14px;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--success);
  box-shadow: 0 0 0 4px var(--success-subtle);
}

.header-status {
  font-size: 12px;
  color: var(--text-muted);
}

.header-user {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-left: 8px;
  padding: 4px 9px;
  background: var(--bg-overlay);
  border-radius: 999px;
  font-size: 12px;
  color: var(--text-secondary);
}

/* ── Body ─────────────────────────────────── */
.app-body {
  display: flex;
  flex: 1;
  overflow: hidden;
}

/* ── Sidebar ──────────────────────────────── */
.app-sidebar {
  width: 232px;
  flex-shrink: 0;
  background: var(--bg-secondary);
  border-right: 1px solid var(--border-light);
  transition: width var(--transition-base);
  overflow-y: auto;
  overflow-x: hidden;
}
.app-sidebar.collapsed {
  width: 64px;
}

/* ── Main ─────────────────────────────────── */
.app-main {
  flex: 1;
  overflow-y: auto;
  padding: 36px 44px 56px;
  max-width: none;
  margin: 0;
  width: 100%;
}

.app-main > :deep(*) {
  max-width: 1180px;
  margin-left: auto;
  margin-right: auto;
}

@media (max-width: 760px) {
  .app-header { padding: 0 14px; }
  .header-sep, .header-page, .header-status { display: none; }
  .app-sidebar { width: 64px; }
  .app-main { padding: 24px 16px 40px; }
}

/* ── Page Transition ──────────────────────── */
.page-enter-active,
.page-leave-active {
  transition: opacity 0.2s ease, transform 0.2s ease;
}
.page-enter-from {
  opacity: 0;
  transform: translateY(8px);
}
.page-leave-to {
  opacity: 0;
  transform: translateY(-8px);
}
</style>
