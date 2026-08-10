<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowRight, BookOpen, Check, FileText, GraduationCap, LayoutDashboard, Send, ShieldCheck, ShoppingBag, Sparkles, Users } from '@lucide/vue'
import { useTaskApi } from '@/composables/useTaskApi'
import { useTaskStore } from '@/stores/task'

const router = useRouter()
const { createTask } = useTaskApi()
const store = useTaskStore()

const userInput = ref('')
const submitting = ref(false)

/**
 * 演示模板：全部刻意收窄到 3 个左右功能模块。
 *
 * 为什么要收窄——实测（DeepSeek-V4-Pro）单次 LLM 调用就要 45~110 秒，
 * 一轮完整流程至少 5 次调用。功能模块越多，Engineer 越慢（超过 4 个还会转入分批生成）。
 * 每条需求都显式写清"仅需 Web API""不需要 XX"，一是压住模块数量，
 * 二是避免触发人工澄清打断演示（澄清阈值为 2 条待澄清问题）。
 * eta 是实测/估算的到达"人工审批"环节的耗时，用于演示时预判等待。
 */
const templates = [
  {
    label: '短链接服务', category: '工具类 · 最快', icon: ShieldCheck, tone: 'blue', eta: '约 6 分钟',
    text: '设计一个短链接服务，仅需提供 Web API：生成短链（支持自定义后缀与有效期）、短链跳转、点击量统计。单机构内部使用，不需要用户注册登录和多租户，不需要支付功能，数据量中等。',
  },
  {
    label: '待办清单 API', category: '工具类', icon: Check, tone: 'green', eta: '约 6 分钟',
    text: '设计一个待办清单后端服务，仅需提供 Web API：任务的增删改查、按标签分类筛选、到期提醒。个人使用，不需要团队协作和权限体系，不需要移动端 App，数据量小。',
  },
  {
    label: '图书借阅管理', category: '业务类', icon: BookOpen, tone: 'violet', eta: '约 7 分钟',
    text: '设计一个小型图书馆借阅管理后端，仅需提供 Web API：图书信息管理、借阅与归还登记、逾期查询提醒。单馆使用，读者身份由现有系统提供无需自建账号体系，不需要支付罚金功能。',
  },
  {
    label: '意见反馈收集', category: '业务类', icon: FileText, tone: 'orange', eta: '约 6 分钟',
    text: '设计一个意见反馈收集后端，仅需提供 Web API：提交反馈（含分类与附件链接）、按状态查看反馈列表、反馈处理状态流转。内部使用，不需要账号登录和通知推送，数据量小。',
  },
]

/** 完整规模需求：用于展示分批生成能力，但耗时明显更长，不适合现场等待 */
const largeTemplates = [
  {
    label: '在线教育平台', icon: GraduationCap, eta: '15 分钟以上',
    text: '设计一个在线教育平台，支持视频课程发布和播放、作业提交与批改、学习进度跟踪、在线考试系统、师生互动讨论区。',
  },
  {
    label: 'B2C 电商平台', icon: ShoppingBag, eta: '15 分钟以上',
    text: '设计一个 B2C 电商平台，包括商品管理（CRUD）、购物车模块、订单管理、支付对接（支付宝/微信）、用户评价系统、后台管理面板。',
  },
]

const workflowSteps = ['需求澄清', '方案设计', '工程实现', '智能评审', '交付打包']

function useTemplate(text: string) { userInput.value = text }

async function handleSubmit() {
  if (!userInput.value.trim()) return
  submitting.value = true
  try {
    const resp = await createTask(userInput.value.trim())
    store.setTaskId(resp.task_id)
    store.setTaskStatus('queued')
    ElMessage.success('任务已创建，正在启动协作流程')
    router.push({ name: 'detail', params: { id: resp.task_id } })
  } catch {
    ElMessage.error('创建失败，请检查后端服务是否运行')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="create-page animate-in">
    <div class="page-header">
      <div>
        <div class="eyebrow"><Sparkles :size="13" /> AI Delivery Workspace</div>
        <h1>创建任务</h1>
        <p>描述你的业务需求，AI 智能体团队将自动完成全流程交付</p>
      </div>
    </div>

    <div class="composer-grid">
      <div class="input-card card">
        <div class="card-heading">
          <span class="heading-icon"><FileText :size="18" /></span>
          <div>
            <h2>描述你的需求</h2>
            <p>写清楚目标、核心功能和约束，交付结果会更准确</p>
          </div>
        </div>
        <el-input
          v-model="userInput"
          type="textarea"
          :rows="9"
          placeholder="例如：我想开发一个在线教育平台，支持视频课程、直播教学、作业提交和成绩管理..."
          class="custom-textarea"
          :disabled="submitting"
        />
        <div class="input-footer">
          <span class="char-count">{{ userInput.length }} 字 · 建议 50 字以上</span>
          <el-button class="submit-btn" :loading="submitting" :disabled="!userInput.trim()" @click="handleSubmit">
            <Send :size="15" /> 提交需求
          </el-button>
        </div>
      </div>

      <aside class="workflow-card card">
        <div class="workflow-title"><Users :size="18" /> 智能体协作流程</div>
        <p class="workflow-desc">提交后，系统会自动组织多角色智能体完成以下步骤。</p>
        <div class="workflow-list">
          <div v-for="(step, index) in workflowSteps" :key="step" class="workflow-step">
            <span class="step-index">{{ index + 1 }}</span>
            <span>{{ step }}</span>
            <Check :size="14" />
          </div>
        </div>
        <div class="workflow-tip"><BookOpen :size="15" /> 可在任务详情中实时查看过程并参与审批</div>
      </aside>
    </div>

    <div class="section-head">
      <div>
        <h3 class="section-title">演示模板（小型需求，跑得快）</h3>
        <p>已收窄到 3 个左右功能模块，避免触发人工澄清；选择后仍可继续编辑</p>
      </div>
    </div>
    <div class="template-grid">
      <div
        v-for="tpl in templates" :key="tpl.label"
        :class="['tpl-card', 'card', `tpl-${tpl.tone}`]"
        @click="useTemplate(tpl.text)"
      >
        <span class="tpl-icon"><component :is="tpl.icon" :size="20" /></span>
        <div class="tpl-copy">
          <span class="tpl-category">{{ tpl.category }} · {{ tpl.eta }}</span>
          <span class="tpl-name">{{ tpl.label }}</span>
          <span class="tpl-preview">{{ tpl.text.substring(0, 48) }}...</span>
        </div>
        <ArrowRight :size="17" class="tpl-arrow" />
      </div>
    </div>

    <div class="section-head" style="margin-top: 26px">
      <div>
        <h3 class="section-title">完整规模需求</h3>
        <p>功能模块多，会触发分批生成，耗时明显更长——适合展示能力，不适合现场等待</p>
      </div>
    </div>
    <div class="large-row">
      <button
        v-for="tpl in largeTemplates" :key="tpl.label"
        class="large-chip" @click="useTemplate(tpl.text)"
      >
        <component :is="tpl.icon" :size="15" />
        <span>{{ tpl.label }}</span>
        <em>{{ tpl.eta }}</em>
      </button>
    </div>
  </div>
</template>

<style scoped>
.page-header { margin-bottom: 26px; }
.page-header h1 { font-size: 28px; font-weight: 700; letter-spacing: -0.03em; margin-bottom: 4px; }
.page-header p { font-size: 14px; color: var(--text-secondary); }
.eyebrow { display: flex; align-items: center; gap: 6px; margin-bottom: 7px; color: var(--accent); font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
.composer-grid { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 18px; margin-bottom: 34px; }
.input-card { padding: 24px; }
.card-heading { display: flex; align-items: center; gap: 12px; margin-bottom: 18px; }
.card-heading h2 { font-size: 16px; font-weight: 650; }
.card-heading p { margin-top: 2px; font-size: 12px; color: var(--text-muted); }
.heading-icon { display: flex; align-items: center; justify-content: center; width: 42px; height: 42px; border-radius: 12px; color: var(--accent); background: var(--accent-subtle); }

.input-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 10px;
}

.custom-textarea :deep(.el-textarea__inner) {
  background: #fafbfc;
  border: 1px solid var(--border);
  color: var(--text-primary);
  font-family: var(--font-sans);
  font-size: 14px;
  line-height: 1.7;
  border-radius: 12px;
  padding: 15px 16px;
  resize: vertical;
  transition: border-color var(--transition-fast);
}
.custom-textarea :deep(.el-textarea__inner):focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-subtle);
}

.input-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 12px;
}
.char-count { font-size: 11px; color: var(--text-muted); }

.submit-btn {
  height: 36px;
  padding: 0 18px;
  font-weight: 600;
  font-size: 13px;
  gap: 6px;
  border-radius: 8px;
  background: var(--accent-dark);
  border: 1px solid var(--accent-dark);
  color: #fff;
}
.submit-btn:hover { background: var(--accent); border-color: var(--accent); }
.submit-btn:disabled { opacity: 0.4; }
.workflow-card { padding: 22px; background: linear-gradient(160deg, #fff 45%, #f3f7ff 100%); }
.workflow-title { display: flex; align-items: center; gap: 8px; font-size: 15px; font-weight: 650; }
.workflow-title svg { color: var(--accent); }
.workflow-desc { margin: 8px 0 18px; font-size: 12px; line-height: 1.7; color: var(--text-secondary); }
.workflow-list { display: flex; flex-direction: column; }
.workflow-step { position: relative; display: grid; grid-template-columns: 28px 1fr 16px; align-items: center; gap: 9px; min-height: 42px; font-size: 13px; color: var(--text-secondary); }
.workflow-step:not(:last-child)::after { position: absolute; top: 34px; bottom: -8px; left: 13px; width: 1px; content: ''; background: #dce6fb; }
.step-index { display: flex; align-items: center; justify-content: center; width: 28px; height: 28px; border-radius: 50%; color: var(--accent); background: var(--accent-subtle); font-size: 11px; font-weight: 700; }
.workflow-step > svg { color: var(--success); }
.workflow-tip { display: flex; gap: 7px; margin-top: 17px; padding: 11px; border-radius: 10px; background: rgba(255,255,255,.72); color: var(--text-muted); font-size: 11px; line-height: 1.55; }

/* ── Templates ───────────────────────────── */
.section-head { margin-bottom: 12px; }
.section-title { font-size: 16px; font-weight: 650; }
.section-head p { margin-top: 2px; font-size: 12px; color: var(--text-muted); }
.template-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; }

/* 完整规模需求用弱化的 chip 呈现，视觉上让位给上方的演示模板 */
.large-row { display: flex; gap: 10px; flex-wrap: wrap; }
.large-chip {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 8px 13px;
  background: var(--bg-card);
  border: 1px dashed var(--border);
  border-radius: 8px;
  color: var(--text-secondary);
  font-size: 12.5px; font-family: var(--font-sans);
  cursor: pointer;
  transition: all var(--transition-fast);
}
.large-chip:hover { border-color: var(--text-muted); color: var(--text-primary); }
.large-chip em { font-style: normal; font-size: 11px; color: var(--warning); }
.tpl-card {
  display: flex; align-items: center; gap: 13px;
  min-width: 0;
  padding: 16px;
  cursor: pointer;
  transition: all var(--transition-fast);
}
.tpl-card:hover { border-color: #bdcdf3; transform: translateY(-2px); }
.tpl-icon { display: flex; align-items: center; justify-content: center; width: 44px; height: 44px; flex-shrink: 0; border-radius: 13px; background: var(--accent-subtle); color: var(--accent); }
.tpl-orange .tpl-icon { color: #d97706; background: #fff4e5; }
.tpl-green .tpl-icon { color: var(--success); background: var(--success-subtle); }
.tpl-violet .tpl-icon { color: #7955d9; background: #f2efff; }
.tpl-copy { display: flex; flex: 1; min-width: 0; flex-direction: column; }
.tpl-category { font-size: 10px; color: var(--text-muted); }
.tpl-name { font-size: 13px; font-weight: 600; }
.tpl-preview {
  font-size: 11px;
  color: var(--text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.tpl-arrow { flex-shrink: 0; color: var(--text-muted); }
.tpl-card:hover .tpl-arrow { color: var(--accent); transform: translateX(2px); }

@media (max-width: 900px) { .composer-grid { grid-template-columns: 1fr; } .workflow-card { display: none; } }
@media (max-width: 620px) { .page-header h1 { font-size: 24px; } .template-grid { grid-template-columns: 1fr; } .input-card { padding: 18px; } }
</style>
