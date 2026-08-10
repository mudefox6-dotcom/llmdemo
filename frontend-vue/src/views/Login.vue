<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Sparkles, LogIn } from '@lucide/vue'
import { login } from '@/composables/useAuth'

const route = useRoute()
const router = useRouter()

const username = ref('')
const password = ref('')
const submitting = ref(false)
const errorMsg = ref('')

// 演示环境公开账号：这是对外演示站点，刻意把账号密码显示在页面上方便试用。
// 认证本身仍然是真的（HMAC 签名 token + 服务端校验），只是不设访问门槛。
const DEMO_USER = 'admin'
const DEMO_PASS = 'admin@123'

function fillDemo() {
  username.value = DEMO_USER
  password.value = DEMO_PASS
}

onMounted(() => {
  // 被路由守卫踢过来时会带上原目标，登录后回到那里
  if (route.query.expired) errorMsg.value = '登录已过期，请重新登录'
})

async function handleSubmit() {
  if (!username.value.trim() || !password.value) {
    errorMsg.value = '请填写账号和密码'
    return
  }
  submitting.value = true
  errorMsg.value = ''
  try {
    await login(username.value.trim(), password.value)
    ElMessage.success('登录成功')
    const redirect = (route.query.redirect as string) || '/'
    router.replace(redirect)
  } catch (err: unknown) {
    // 后端对账号错和密码错统一返回同一句话，避免被枚举账号
    const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    errorMsg.value = detail || '登录失败，请检查后端服务是否运行'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card card">
      <div class="brand">
        <Sparkles :size="18" />
        <span>多智能体需求交付系统</span>
      </div>
      <h1>登录</h1>
      <p class="sub">请输入账号密码以访问系统</p>

      <div v-if="errorMsg" class="err">{{ errorMsg }}</div>

      <div class="demo-tip">
        <div class="demo-row">
          <span class="demo-label">演示账号</span>
          <code>{{ DEMO_USER }}</code>
        </div>
        <div class="demo-row">
          <span class="demo-label">演示密码</span>
          <code>{{ DEMO_PASS }}</code>
        </div>
        <button type="button" class="demo-fill" @click="fillDemo">一键填入</button>
      </div>

      <!-- 用原生 form + @submit.prevent：回车即可提交，符合登录页的操作习惯 -->
      <form @submit.prevent="handleSubmit">
        <label>账号</label>
        <input v-model="username" autocomplete="username" placeholder="请输入账号" />

        <label style="margin-top: 12px">密码</label>
        <input
          v-model="password" type="password"
          autocomplete="current-password" placeholder="请输入密码"
        />

        <button class="submit" type="submit" :disabled="submitting">
          <LogIn :size="15" />
          {{ submitting ? '登录中…' : '登 录' }}
        </button>
      </form>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex; align-items: center; justify-content: center;
  padding: 20px;
  background: var(--bg-primary);
}
.login-card {
  width: 100%; max-width: 380px;
  padding: 32px 30px;
}
.brand {
  display: flex; align-items: center; gap: 7px;
  color: var(--accent);
  font-size: 12px; font-weight: 700;
  letter-spacing: .04em;
  margin-bottom: 18px;
}
h1 { font-size: 24px; font-weight: 700; letter-spacing: -0.02em; margin: 0 0 4px; }
.sub { font-size: 13px; color: var(--text-secondary); margin: 0 0 20px; }

label {
  display: block;
  font-size: 12px; font-weight: 500;
  color: var(--text-secondary);
  margin-bottom: 5px;
}
input {
  width: 100%; height: 38px;
  padding: 0 12px;
  background: var(--bg-primary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font-size: 13px; font-family: var(--font-sans);
  outline: none;
  transition: border-color var(--transition-fast);
}
input:focus { border-color: var(--accent); }
input::placeholder { color: var(--text-placeholder); }

.submit {
  width: 100%; height: 40px;
  margin-top: 22px;
  display: inline-flex; align-items: center; justify-content: center; gap: 7px;
  background: var(--accent-dark);
  border: 1px solid var(--accent-dark);
  border-radius: var(--radius-sm);
  color: #fff;
  font-size: 13px; font-weight: 600; font-family: var(--font-sans);
  cursor: pointer;
  transition: opacity var(--transition-fast);
}
.submit:hover:not(:disabled) { opacity: .9; }
.submit:disabled { opacity: .55; cursor: not-allowed; }

.demo-tip {
  position: relative;
  padding: 11px 13px;
  margin-bottom: 18px;
  background: var(--accent-subtle);
  border: 1px dashed var(--accent);
  border-radius: var(--radius-sm);
}
.demo-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  line-height: 1.9;
}
.demo-label { color: var(--text-secondary); }
.demo-row code {
  font-family: var(--font-mono, ui-monospace, monospace);
  color: var(--accent);
  font-weight: 600;
  /* 方便观众直接选中复制 */
  user-select: all;
}
.demo-fill {
  position: absolute;
  top: 10px; right: 11px;
  padding: 3px 9px;
  background: transparent;
  border: 1px solid var(--accent);
  border-radius: 999px;
  color: var(--accent);
  font-size: 11.5px; font-family: var(--font-sans);
  cursor: pointer;
  transition: all var(--transition-fast);
}
.demo-fill:hover { background: var(--accent); color: #fff; }

.err {
  padding: 9px 12px; margin-bottom: 14px;
  background: var(--danger-subtle);
  border: 1px solid var(--danger);
  border-radius: var(--radius-sm);
  color: var(--danger);
  font-size: 12.5px;
}
</style>
