import axios from 'axios'
import type {
  CreateTaskResponse,
  TaskStatusResponse,
  FeedbackRequest,
  FeedbackResponse,
  TaskResult,
  TaskListItem,
  DeleteTaskResponse,
} from '@/types'
import { clearAuth, getToken } from './useAuth'

const api = axios.create({
  baseURL: '/api',
  timeout: 120000,
})

// 请求拦截器：每个请求自动带上登录 token，业务代码不用关心认证
api.interceptors.request.use((config) => {
  const token = getToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

// 响应拦截器：token 过期/无效（401）时清掉本地登录态并跳登录页。
// 这里用动态 import 拿 router：本模块被 views 引用，而 router 又懒加载 views，
// 顶层静态 import 会形成环——放到回调里等运行时再取就没有这个问题。
api.interceptors.response.use(
  (resp) => resp,
  async (err) => {
    if (axios.isAxiosError(err) && err.response?.status === 401) {
      clearAuth()
      const { default: router } = await import('@/router')
      if (router.currentRoute.value.name !== 'login') {
        router.replace({
          name: 'login',
          query: { redirect: router.currentRoute.value.fullPath, expired: '1' },
        })
      }
    }
    return Promise.reject(err)
  }
)

/**
 * 判断一个错误是否属于"后端暂时连不上"，而不是业务错误。
 *
 * 典型场景：重启后端的那几秒，Vite 代理转发不到 127.0.0.1:8000 会返回 502；
 * 或者请求直接超时/网络中断（此时 axios 的 error.response 为空）。
 * 这类错误应该提示"正在重连"并继续轮询，而不是笼统报"查询失败"让用户以为卡死。
 */
export function isConnectionError(err: unknown): boolean {
  if (!axios.isAxiosError(err)) return false
  const status = err.response?.status
  if (status === undefined) return true          // 无响应：网络中断 / 超时 / 连接被拒
  return status === 502 || status === 503 || status === 504
}

/**
 * 下载生成的项目 zip，返回二进制 Blob。
 *
 * 单独放在 useTaskApi 外面导出，是因为它走的是同一个 api 实例（自动带 token），
 * 但返回类型和其他接口不同——不是 JSON，而是 responseType: 'blob'。
 */
export async function downloadProjectZip(taskId: string): Promise<Blob> {
  const { data } = await api.get<Blob>(`/tasks/${taskId}/download/project`, {
    responseType: 'blob',
    // zip 可能有几 MB，比默认 120 秒更宽松一些
    timeout: 300000,
  })
  return data
}

export function useTaskApi() {
  async function createTask(userInput: string): Promise<CreateTaskResponse> {
    const { data } = await api.post<CreateTaskResponse>('/tasks', {
      user_input: userInput,
    })
    return data
  }

  async function getTaskStatus(taskId: string): Promise<TaskStatusResponse> {
    const { data } = await api.get<TaskStatusResponse>(`/tasks/${taskId}`)
    return data
  }

  async function submitFeedback(
    taskId: string,
    payload: FeedbackRequest
  ): Promise<FeedbackResponse> {
    const { data } = await api.post<FeedbackResponse>(
      `/tasks/${taskId}/feedback`,
      payload
    )
    return data
  }

  async function getTaskResult(taskId: string): Promise<TaskResult> {
    const { data } = await api.get<TaskResult>(`/tasks/${taskId}/result`)
    return data
  }

  async function listTasks(limit = 50, offset = 0): Promise<TaskListItem[]> {
    const { data } = await api.get<TaskListItem[]>('/tasks', {
      params: { limit, offset },
    })
    return data
  }

  /** 删除任务（连同反馈记录与 LangGraph 检查点）。was_running 表示后台那一轮仍会自行跑完 */
  async function deleteTask(taskId: string): Promise<DeleteTaskResponse> {
    const { data } = await api.delete<DeleteTaskResponse>(`/tasks/${taskId}`)
    return data
  }

  return { createTask, getTaskStatus, submitFeedback, getTaskResult, listTasks, deleteTask }
}
