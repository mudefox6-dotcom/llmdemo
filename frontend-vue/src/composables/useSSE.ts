import { ref, onUnmounted } from 'vue'
import type { SSEEvent } from '@/types'
import { getToken } from './useAuth'

export function useSSE(taskId: string | null) {
  const lastEvent = ref<SSEEvent | null>(null)
  const isConnected = ref(false)
  const error = ref<string | null>(null)
  let eventSource: EventSource | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null

  function connect() {
    if (!taskId) return
    // 幂等：已有活跃连接时直接返回。否则轮询里每次调用 connect() 都会
    // 先 disconnect 再重建，导致连接被反复杀掉重连（日志里每 3 秒一条 disconnected）。
    if (eventSource && eventSource.readyState !== EventSource.CLOSED) return
    disconnect()

    // EventSource 不支持自定义请求头，没法带 Authorization，
    // 所以把 token 放在查询参数里（后端 require_auth_or_query_token 专门为此放行）
    const token = getToken()
    const url = `/api/tasks/${taskId}/stream${token ? `?token=${encodeURIComponent(token)}` : ''}`
    eventSource = new EventSource(url)
    isConnected.value = true
    error.value = null

    eventSource.onmessage = (e) => {
      try {
        const parsed: SSEEvent = JSON.parse(e.data)
        lastEvent.value = parsed
        if (parsed.type === 'error') {
          error.value = parsed.message || 'Unknown error'
        }
        if (parsed.type === 'done') {
          disconnect()
        }
      } catch {
        // ignore malformed events
      }
    }

    eventSource.onerror = () => {
      isConnected.value = false
      eventSource?.close()
      // auto-reconnect after 3s
      reconnectTimer = setTimeout(() => connect(), 3000)
    }
  }

  function disconnect() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    eventSource?.close()
    eventSource = null
    isConnected.value = false
  }

  onUnmounted(() => disconnect())

  return { lastEvent, isConnected, error, connect, disconnect }
}
