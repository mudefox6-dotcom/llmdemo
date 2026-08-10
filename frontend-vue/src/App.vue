<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import AppLayout from './components/AppLayout.vue'

const route = useRoute()

// 登录页（meta.public）是独立的整屏页面，不套 header + 侧边栏的后台布局
const isBare = computed(() => route.meta.public === true)
</script>

<template>
  <router-view v-if="isBare" />
  <AppLayout v-else />
</template>
