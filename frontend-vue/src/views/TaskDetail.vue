<script setup lang="ts">
import { ref, watch, onMounted, computed, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { RefreshCw, FileText, BarChart3, Braces, Trash2, WifiOff } from '@lucide/vue'
import { useTaskApi, isConnectionError } from '@/composables/useTaskApi'
import { useSSE } from '@/composables/useSSE'
import { useTaskStore } from '@/stores/task'
import TaskProgress from '@/components/TaskProgress.vue'
import ReviewResult from '@/components/ReviewResult.vue'
import PrdPreview from '@/components/PrdPreview.vue'
import TechDesignPreview from '@/components/TechDesignPreview.vue'
import ClarificationPanel from '@/components/ClarificationPanel.vue'
import ApprovalPanel from '@/components/ApprovalPanel.vue'
import DeliverableView from '@/components/DeliverableView.vue'

const route = useRoute()
const router = useRouter()
const { getTaskStatus, deleteTask } = useTaskApi()
const store = useTaskStore()

const taskId = computed(() => (route.params.id as string) || store.taskId || '')
const loading = ref(false)
const deleting = ref(false)
const activeTab = ref('products')
const connectionLost = ref(false)   // 后端暂时连不上（重启/网络抖动），轮询会自动恢复

const { lastEvent, connect: connectSSE, disconnect: disconnectSSE } = useSSE(taskId.value)

// 终态：不再需要轮询/订阅
const TERMINAL = ['completed', 'error', 'waiting_human']
// 退避式轮询：状态一直不变就逐步放慢，避免长任务（Engineer 动辄几分钟）持续刷请求；
// 一旦检测到状态推进，立刻回到快节奏，保证界面跟手。
const POLL_MIN_MS = 3000
const POLL_MAX_MS = 20000
const POLL_FACTOR = 1.6
let pollDelay = POLL_MIN_MS
let pollTimer: ReturnType<typeof setTimeout> | null = null
// 必须用独立标志位判断"是否已在轮询"：tick 执行期间 pollTimer 是 null，
// 而 tick 内部会 await queryStatus()，后者又会调 startPolling()——只看 pollTimer
// 会误判为"没在轮询"而重复起定时器，每轮翻倍。
let polling = false
let lastSignature = ''   // 用"状态+节点+产物有无"当指纹，判断这一轮有没有推进
let sseStarted = false   // SSE 只连一次，避免轮询把它反复重建

function stopPolling() {
  polling = false
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null }
}

/** 状态指纹：变了说明流程有推进，轮询该回到快节奏 */
function stateSignature(): string {
  const d = store.statusData
  if (!d) return ''
  return [
    d.status,
    d.current_node,
    d.reflow_count,
    d.prd_doc ? Object.keys(d.prd_doc).length : 0,
    d.technical_design ? Object.keys(d.technical_design).length : 0,
    d.review_result ? Object.keys(d.review_result).length : 0,
  ].join('|')
}

// 轮询兜底：SSE 可能因为“任务已在后台跑完、进度队列已释放”而收不到任何事件，
// 仅靠 SSE 会让页面停在旧快照上，所以非终态时定时拉一次状态。
// 注意这里【不】调 connectSSE —— 之前放在 queryStatus 里，导致每轮询一次就重连一次
// （SSE 收到 done 会自行 disconnect，下次轮询又 connect），后端日志被 /stream 刷屏。
function startPolling() {
  if (polling) return
  polling = true

  const tick = async () => {
    pollTimer = null
    if (!polling || TERMINAL.includes(store.taskStatus || '')) { polling = false; return }

    await queryStatus(true)

    const sig = stateSignature()
    if (sig !== lastSignature) {
      lastSignature = sig
      pollDelay = POLL_MIN_MS                                   // 有推进 → 回到快节奏
    } else {
      pollDelay = Math.min(Math.round(pollDelay * POLL_FACTOR), POLL_MAX_MS)  // 没动静 → 放慢
    }

    if (polling && !TERMINAL.includes(store.taskStatus || '')) {
      pollTimer = setTimeout(tick, pollDelay)
    } else {
      polling = false
    }
  }

  pollTimer = setTimeout(tick, pollDelay)
}

/** 外部事件（SSE / 提交反馈）触发时，让轮询立即回到快节奏 */
function resetPollBackoff() {
  pollDelay = POLL_MIN_MS
}

/** 整个页面生命周期内只订阅一次 SSE */
function ensureSSE() {
  if (sseStarted) return
  sseStarted = true
  connectSSE()
}

onMounted(() => {
  if (taskId.value) { store.setTaskId(taskId.value); queryStatus() }
})
onUnmounted(() => { disconnectSSE(); stopPolling() })

watch(lastEvent, (event) => {
  if (event && (event.type === 'node_end' || event.type === 'done' || event.type === 'interrupt')) {
    resetPollBackoff()   // 后端有实时事件，说明正在推进，收紧轮询节奏
    queryStatus()
  }
})

watch(() => route.params.id, (id) => {
  if (id && typeof id === 'string') { store.setTaskId(id); queryStatus() }
})

/** silent=true 时不显示 loading（供轮询静默刷新使用） */
async function queryStatus(silent = false) {
  const id = taskId.value
  if (!id) return
  if (!silent) loading.value = true
  try {
    const data = await getTaskStatus(id)
    store.setStatusData(data)
    connectionLost.value = false      // 拿到数据说明后端已恢复
    if (TERMINAL.includes(data.status)) {
      stopPolling()
    } else {
      // 非终态才需要实时通道 + 轮询兜底；SSE 只在第一次订阅。
      // （原先无论如何都 connectSSE，且早期还有 `status !== 'queued'` 的判断，
      //  前者造成 /stream 刷屏，后者造成刚创建的任务永远停在“排队中”。）
      ensureSSE()
      startPolling()
    }
  } catch (err) {
    if (isConnectionError(err)) {
      // 后端重启/网络抖动：明确提示"正在重连"，并把轮询压回最快节奏尽早恢复；
      // 轮询本身是 setTimeout 递归，单次失败不会中断，所以无需用户手动刷新。
      connectionLost.value = true
      resetPollBackoff()
      startPolling()
    } else if (!silent) {
      ElMessage.error('查询失败')
    }
  }
  finally { if (!silent) loading.value = false }
}

function onFeedbackSubmitted() {
  resetPollBackoff()   // 刚提交反馈，任务马上会恢复执行，需要盯紧一点
  queryStatus()
}

/** 删除当前任务：先停掉轮询与 SSE，再删，最后回到历史列表 */
async function onDelete() {
  const id = taskId.value
  if (!id) return
  const busy = !TERMINAL.includes(store.taskStatus || '')
  try {
    await ElMessageBox.confirm(
      busy
        ? '该任务尚未结束。删除后无法恢复，后台已开始的那一轮会自行结束且结果不再保存。'
        : '删除后无法恢复（含交付产物记录与执行检查点）。确定删除吗？',
      '删除任务',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch { return }

  deleting.value = true
  try {
    const res = await deleteTask(id)
    stopPolling()
    disconnectSSE()
    store.setTaskId('')
    ElMessage.success(res.message || '任务已删除')
    router.push({ name: 'history' })
  } catch {
    ElMessage.error('删除失败')
  } finally {
    deleting.value = false
  }
}

/**
 * 后端会把状态里所有字段都序列化返回，未产出的产物是【空对象 {}】而不是 null。
 * 而 JS 里 {} 是 truthy，直接 `v-if="prd_doc"` 会渲染出一堆 undefined，
 * 审批面板也会在"人工澄清"阶段被误显示。所以统一用"非空"判断。
 */
function notEmpty(obj: unknown): boolean {
  return !!obj && typeof obj === 'object' && Object.keys(obj as object).length > 0
}

const hasPrd = computed(() => notEmpty(store.statusData?.prd_doc))
const hasDesign = computed(() => notEmpty(store.statusData?.technical_design))
const hasProducts = computed(() => hasPrd.value || hasDesign.value)
const hasReview = computed(() => notEmpty(store.statusData?.review_result))
// 澄清面板：等待人工 且 已有需求澄清结果 且 还没进入评审阶段
const needsClarification = computed(() =>
  store.taskStatus === 'waiting_human' && notEmpty(store.statusData?.clarified_requirement) && !hasReview.value
)
// 审批面板：等待人工 且 已有评审结果（有评审才谈得上审批）
const needsApproval = computed(() => store.taskStatus === 'waiting_human' && hasReview.value)
</script>

<template>
  <div class="animate-in">
    <div class="page-header">
      <div>
        <h1>任务详情</h1>
        <p class="task-id-label">{{ taskId }}</p>
      </div>
      <div class="header-actions">
        <el-button class="btn-secondary" :loading="loading" @click="queryStatus">
          <RefreshCw :size="14" style="margin-right: 5px" /> 刷新
        </el-button>
        <el-button class="btn-danger" :loading="deleting" @click="onDelete">
          <Trash2 :size="14" style="margin-right: 5px" /> 删除
        </el-button>
      </div>
    </div>

    <!-- 后端暂时不可达（多见于重启后端的几秒）：说明状态并告知会自动恢复，
         避免用户以为任务卡死而反复刷新或重复提交需求 -->
    <div v-if="connectionLost" class="conn-banner">
      <WifiOff :size="15" />
      <span>与后端的连接中断（可能正在重启），正在自动重连…… 无需刷新页面</span>
    </div>

    <div v-if="store.statusData">
      <TaskProgress :data="store.statusData" />

      <ClarificationPanel
        v-if="needsClarification"
        :task-id="taskId"
        :clarified-requirement="store.statusData.clarified_requirement"
        @feedback-submitted="onFeedbackSubmitted"
      />

      <ApprovalPanel
        v-if="needsApproval"
        :task-id="taskId"
        @feedback-submitted="onFeedbackSubmitted"
      />

      <!-- Tabs -->
      <div class="tabs-bar">
        <button :class="['tab', { active: activeTab === 'products' }]" @click="activeTab = 'products'">
          <FileText :size="14" /> 交付产物
        </button>
        <button :class="['tab', { active: activeTab === 'review' }]" @click="activeTab = 'review'" :disabled="!hasReview">
          <BarChart3 :size="14" /> 评审分析
        </button>
        <button :class="['tab', { active: activeTab === 'raw' }]" @click="activeTab = 'raw'">
          <Braces :size="14" /> 原始数据
        </button>
      </div>

      <div v-if="activeTab === 'products'">
        <PrdPreview v-if="hasPrd" :prd="store.statusData.prd_doc!" />
        <TechDesignPreview v-if="hasDesign" :design="store.statusData.technical_design!" />
        <DeliverableView v-if="store.taskStatus === 'completed'" :task-id="taskId" />
        <div v-if="!hasProducts && store.taskStatus !== 'completed'" class="empty-block">产物生成中...</div>
      </div>

      <div v-if="activeTab === 'review'">
        <ReviewResult v-if="hasReview" :review="store.statusData.review_result!" />
        <div v-else class="empty-block">暂无评审结果</div>
      </div>

      <div v-if="activeTab === 'raw'" class="raw-block">
        <pre>{{ JSON.stringify(store.statusData, null, 2) }}</pre>
      </div>
    </div>

    <div v-else-if="!loading" class="empty-center">
      <p>输入任务 ID 查看详情</p>
    </div>
  </div>
</template>

<style scoped>
.page-header {
  display: flex; justify-content: space-between; align-items: flex-start;
  margin-bottom: 20px;
}
.page-header h1 { font-size: 22px; font-weight: 700; letter-spacing: -0.02em; margin-bottom: 2px; }
.task-id-label { font-size: 12px; color: var(--text-muted); font-family: var(--font-mono); }

.btn-secondary {
  background: var(--bg-card);
  border: 1px solid var(--border);
  color: var(--text-secondary);
  font-size: 12px;
  border-radius: var(--radius-sm);
  height: 32px;
}
.btn-secondary:hover { border-color: var(--text-muted); color: var(--text-primary); }

.header-actions { display: flex; gap: 8px; }

.conn-banner {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 14px; margin-bottom: 14px;
  background: var(--warning-subtle);
  border: 1px solid var(--warning);
  border-radius: var(--radius-sm);
  color: var(--warning);
  font-size: 12.5px;
}
.btn-danger {
  background: var(--bg-card);
  border: 1px solid var(--border);
  color: var(--text-secondary);
  font-size: 12px;
  border-radius: var(--radius-sm);
  height: 32px;
}
.btn-danger:hover {
  border-color: var(--danger);
  color: var(--danger);
  background: var(--danger-subtle);
}

/* ── Tabs ────────────────────────────────── */
.tabs-bar {
  display: flex; gap: 0;
  border-bottom: 1px solid var(--border);
  margin-bottom: 18px;
}
.tab {
  display: flex; align-items: center; gap: 6px;
  padding: 8px 16px;
  background: none; border: none; border-bottom: 2px solid transparent;
  color: var(--text-secondary);
  font-size: 13px; font-weight: 500; font-family: var(--font-sans);
  cursor: pointer;
  transition: all var(--transition-fast);
  margin-bottom: -1px;
}
.tab:hover:not(:disabled) { color: var(--text-primary); }
.tab.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}
.tab:disabled { opacity: 0.3; cursor: not-allowed; }

.empty-block {
  padding: 40px 0; text-align: center; color: var(--text-muted); font-size: 13px;
}
.empty-center {
  padding: 80px 0; text-align: center; color: var(--text-muted);
}
.raw-block {
  background: var(--bg-primary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 16px;
}
.raw-block pre {
  font-size: 11px;
  color: var(--text-secondary);
  white-space: pre-wrap;
  max-height: 500px;
  overflow-y: auto;
  background: transparent;
  border: none;
  padding: 0;
}
</style>
