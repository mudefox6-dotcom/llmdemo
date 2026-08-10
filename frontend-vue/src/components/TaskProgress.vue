<script setup lang="ts">
import { computed } from 'vue'
import { FileInput, Brain, Lightbulb, Code, Eye, UserCheck, PackageOpen, Check } from '@lucide/vue'
import type { TaskStatusResponse } from '@/types'

const props = defineProps<{ data: TaskStatusResponse }>()

interface Node { key: string; label: string; icon: any }
const nodes: Node[] = [
  { key: 'queued',              label: '排队中',     icon: Check },
  { key: 'input_normalize',     label: '输入规范化', icon: FileInput },
  { key: 'planner',             label: '需求分析',   icon: Brain },
  { key: 'human_clarification', label: '人工澄清',   icon: Check },
  { key: 'solution',            label: '方案设计',   icon: Lightbulb },
  { key: 'engineer',            label: '工程实现',   icon: Code },
  { key: 'reviewer',            label: '智能评审',   icon: Eye },
  { key: 'human_approval',      label: '人工审批',   icon: UserCheck },
  { key: 'package_output',      label: '产物打包',   icon: PackageOpen },
]

// 图里有些节点不单独展示（方案择优、精准修复、多轮对话），把它们映射到
// 最接近的可见步骤上；否则 findIndex 返回 -1，进度条会停在 0% 且无高亮。
const NODE_ALIAS: Record<string, string> = {
  plan_evaluator: 'planner',        // 方案择优属于需求分析阶段
  repairer: 'engineer',             // 精准修复属于工程实现
  dialogue_reviewer: 'reviewer',    // 多轮对话由评审发起
  dialogue_engineer: 'engineer',
  dialogue_solution: 'solution',
}

const currentNodeIndex = computed(() => {
  const raw = props.data.current_node
  const key = NODE_ALIAS[raw] ?? raw
  const idx = nodes.findIndex(n => n.key === key)
  if (idx >= 0) return idx
  // 未知节点但已在等待人工：至少让进度停在对应的人工环节上
  if (props.data.status === 'waiting_human') {
    return nodes.findIndex(n => n.key === (props.data.review_result ? 'human_approval' : 'human_clarification'))
  }
  return -1
})

function nodeStatus(idx: number) {
  if (props.data.status === 'completed') return 'done'
  const cur = currentNodeIndex.value
  if (idx < cur) return 'done'
  if (idx === cur) return 'active'
  return 'pending'
}

const progressPercent = computed(() => {
  if (props.data.status === 'completed') return 100
  const idx = currentNodeIndex.value
  if (idx >= 0) return Math.round(((idx + 1) / nodes.length) * 100)
  return 0
})

const statusMeta = computed(() => {
  const m: Record<string, { color: string; label: string }> = {
    queued: { color: 'var(--info)', label: '排队中' },
    running: { color: 'var(--warning)', label: '执行中' },
    waiting_human: { color: 'var(--warning)', label: '等待人工' },
    completed: { color: 'var(--success)', label: '已完成' },
    error: { color: 'var(--danger)', label: '执行失败' },
  }
  return m[props.data.status] || m.queued
})

/**
 * 排队提示：只显示"排队中"会让人以为卡死了。这里把真实原因讲出来——
 * 前面有几个任务在执行、自己排第几。后台并发数由 WORKER_CONCURRENCY 控制。
 */
const queueHint = computed(() => {
  if (props.data.status !== 'queued') return ''
  const q = props.data.queue_info
  if (!q) return '等待后台调度…'
  if (q.queue_position > 1) return `前面还有 ${q.queue_position - 1} 个任务在排队，${q.running_count} 个执行中`
  if (q.running_count > 0) return `${q.running_count} 个任务执行中，即将轮到本任务`
  return '即将开始执行…'
})
</script>

<template>
  <div class="panel card">
    <div class="panel-head">
      <span class="panel-title">工作流进度</span>
      <span class="panel-badge" :style="{ color: statusMeta.color }">{{ statusMeta.label }}</span>
    </div>

    <!-- Bar -->
    <div class="bar-wrap">
      <div class="bar-track">
        <div class="bar-fill" :style="{ width: progressPercent + '%' }" />
      </div>
      <span class="bar-pct">{{ progressPercent }}%</span>
    </div>

    <!-- 排队原因提示 -->
    <div v-if="queueHint" class="queue-hint">{{ queueHint }}</div>

    <!-- Node Steps -->
    <div class="steps">
      <template v-for="(node, idx) in nodes" :key="node.key">
        <div v-if="idx > 0" :class="['step-line', nodeStatus(idx)]" />
        <div :class="['step', nodeStatus(idx)]">
          <div class="step-dot">
            <Check v-if="nodeStatus(idx) === 'done'" :size="12" />
            <component v-else :is="node.icon" :size="14" />
          </div>
          <span class="step-label">{{ node.label }}</span>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.panel {
  padding: 20px; margin-bottom: 18px;
}

.panel-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 14px;
}
.panel-title { font-size: 14px; font-weight: 600; }
.panel-badge {
  font-size: 11px; font-weight: 600;
  padding: 2px 8px; border-radius: 4px;
  background: var(--bg-primary);
}

/* ── Bar ─────────────────────────────────── */
.bar-wrap { display: flex; align-items: center; gap: 10px; margin-bottom: 18px; }
.bar-track {
  flex: 1; height: 4px;
  background: var(--bg-primary);
  border-radius: 2px; overflow: hidden;
}
.bar-fill {
  height: 100%;
  background: var(--accent);
  border-radius: 2px;
  transition: width 0.5s ease;
}
.bar-pct {
  font-size: 11px; font-weight: 700; color: var(--text-muted);
  font-family: var(--font-mono); min-width: 30px; text-align: right;
}

.queue-hint {
  font-size: 12px;
  color: var(--text-muted);
  margin: -8px 0 14px;
}

/* ── Steps ────────────────────────────────── */
.steps {
  display: flex; align-items: center; flex-wrap: wrap;
}
.step-line {
  width: 20px; height: 1px;
  background: var(--border);
  flex-shrink: 0;
  margin: 0 2px;
}
.step-line.done, .step-line.active { background: var(--accent); }

.step {
  display: flex; align-items: center; gap: 6px;
  padding: 3px 0;
  transition: opacity var(--transition-fast);
}
.step.pending { opacity: 0.3; }
.step-label {
  font-size: 11px; font-weight: 500;
  color: var(--text-muted);
  white-space: nowrap;
}
.step.active .step-label { color: var(--accent); font-weight: 600; }
.step.done .step-label { color: var(--text-secondary); }

.step-dot {
  width: 26px; height: 26px;
  border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  background: var(--bg-primary);
  border: 1.5px solid var(--border);
  color: var(--text-muted);
  flex-shrink: 0;
  transition: all var(--transition-base);
}
.step.done .step-dot {
  border-color: var(--accent);
  background: var(--accent);
  color: #fff;
}
.step.active .step-dot {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-subtle);
}
.step.pending .step-dot {
  border-color: var(--border);
  color: var(--text-muted);
}
</style>
