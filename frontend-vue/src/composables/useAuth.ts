/**
 * 登录状态管理。
 *
 * token 存在 localStorage：刷新页面、关掉标签页再打开都还在登录态，
 * 这是演示场景想要的（sessionStorage 一关标签就没了）。
 *
 * 后端签发的 token 是 HMAC 签名的无状态串，服务端不存 session，
 * 所以"退出登录"只需要本地删掉它，不必调接口。
 */
import { ref, computed } from 'vue'
import axios from 'axios'

const TOKEN_KEY = 'mad_token'
const USER_KEY = 'mad_username'
const EXPIRES_KEY = 'mad_expires_at'

// 用 ref 而不是每次读 localStorage：让 header 上的用户名等 UI 能响应式更新
const token = ref<string>(localStorage.getItem(TOKEN_KEY) || '')
const username = ref<string>(localStorage.getItem(USER_KEY) || '')

/** token 是否存在且未过期。过期时间是签发时后端返回的，本地先判一次，省掉一次必然 401 的请求。 */
export function hasValidToken(): boolean {
  if (!token.value) return false
  const expiresAt = Number(localStorage.getItem(EXPIRES_KEY) || 0)
  if (expiresAt && expiresAt * 1000 <= Date.now()) {
    clearAuth()
    return false
  }
  return true
}

export function getToken(): string {
  return token.value
}

export function clearAuth() {
  token.value = ''
  username.value = ''
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
  localStorage.removeItem(EXPIRES_KEY)
}

/** 调登录接口。成功后写入 token；失败时把 axios 错误抛给调用方展示 detail。 */
export async function login(user: string, password: string): Promise<void> {
  const { data } = await axios.post<{
    token: string
    username: string
    expires_at: number
  }>('/api/auth/login', { username: user, password })

  token.value = data.token
  username.value = data.username
  localStorage.setItem(TOKEN_KEY, data.token)
  localStorage.setItem(USER_KEY, data.username)
  localStorage.setItem(EXPIRES_KEY, String(data.expires_at))
}

export function useAuth() {
  return {
    username: computed(() => username.value),
    isLoggedIn: computed(() => !!token.value),
    logout: clearAuth,
  }
}
