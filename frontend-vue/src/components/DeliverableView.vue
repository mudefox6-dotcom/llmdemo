<script setup lang="ts">
import { ref, computed } from 'vue'
import axios from 'axios'
import { ElMessage } from 'element-plus'
import { PackageOpen, Download, Copy, Check, FileJson, FolderCode, CircleAlert } from '@lucide/vue'
import { useTaskApi, isConnectionError, downloadProjectZip } from '@/composables/useTaskApi'
import type { TaskResult } from '@/types'

const props = defineProps<{ taskId: string }>()
const { getTaskResult } = useTaskApi()
const result = ref<TaskResult | null>(null)
const loading = ref(false)
const fetched = ref(false)
const copied = ref(false)

const errorMsg = ref('')

async function fetchResult() {
  loading.value = true
  errorMsg.value = ''
  try {
    result.value = await getTaskResult(props.taskId)
    fetched.value = true
  } catch (err) {
    // 原来这里是 `catch { /* ignore */ }`——请求失败时按钮点了完全没反应，
    // 用户无从判断是后端没起、任务未完成还是别的问题。必须把原因说出来。
    if (isConnectionError(err)) {
      errorMsg.value = '连接不上后端服务，请确认后端已启动（.\\start.bat backend）'
    } else if (axios.isAxiosError(err) && err.response?.status === 400) {
      errorMsg.value = String(err.response?.data?.detail || '任务尚未完成，交付物还没生成')
    } else if (axios.isAxiosError(err) && err.response?.status === 404) {
      errorMsg.value = '任务不存在，可能已被删除'
    } else {
      errorMsg.value = '加载交付结果失败，请查看后端日志'
    }
    ElMessage.error(errorMsg.value)
  } finally {
    loading.value = false
  }
}

/** exported_files 里出现 project_zip 说明骨架项目已生成 */
const hasProject = computed(() => !!result.value?.exported_files?.project_zip)

/** 统计数字从技术设计里现算，避免后端再多传一份 */
const scaffold = computed(() => {
  const d = result.value?.deliverable?.technical_design as Record<string, any> | undefined
  const eps = (d?.api_endpoints as unknown[]) || []
  const tables = ((d?.db_schema as Record<string, unknown>)?.tables as unknown[]) || []
  // 模块数 = 按 URL 首段分组的组数，与后端 router 文件、前端页面一一对应
  const slugs = new Set(
    eps.map((e) => {
      const path = String((e as Record<string, unknown>).path || '')
      return path.replace(/^\//, '').split('/').find((s) => s && !s.startsWith('{')) || 'misc'
    }),
  )
  return { endpoints: eps.length, tables: tables.length, routers: slugs.size }
})

const downloading = ref(false)

/**
 * 下载生成的项目 zip。
 *
 * 不能用 window.open：那是浏览器发起的普通导航，带不上 Authorization 头，
 * 接口需要登录，于是页面上直接显示一串 {"detail":"未登录..."}。
 * 改为用 axios 拿二进制（拦截器会自动加 token），再用 Blob 触发下载。
 * 这样 token 只走请求头，不会像 SSE 那样落到 nginx 访问日志里。
 */
async function downloadProject() {
  if (downloading.value) return
  downloading.value = true
  try {
    const blob = await downloadProjectZip(props.taskId)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `project-${props.taskId}.zip`
    document.body.appendChild(a)
    a.click()
    a.remove()
    // 立刻回收，否则整个 zip 会一直占着内存
    URL.revokeObjectURL(url)
  } catch (err) {
    const msg = isConnectionError(err)
      ? '连接不上后端服务，请稍后重试'
      : axios.isAxiosError(err) && err.response?.status === 404
        ? '项目文件不存在，可能任务未生成骨架或已被清理'
        : '下载失败，请查看后端日志'
    ElMessage.error(msg)
  } finally {
    downloading.value = false
  }
}

async function copyResult() {
  if (!result.value) return
  await navigator.clipboard.writeText(JSON.stringify(result.value, null, 2))
  copied.value = true
  setTimeout(() => { copied.value = false }, 2000)
}
</script>

<template>
  <div class="panel card">
    <div class="panel-head">
      <PackageOpen :size="15" style="color:var(--success)" />
      <span>交付结果</span>
      <el-button v-if="!fetched" class="btn-load" size="small" :loading="loading" @click="fetchResult">
        <Download :size="12" style="margin-right:3px" /> 加载
      </el-button>
      <el-button v-if="fetched && result" text size="small" style="margin-left:8px" @click="copyResult">
        <Check v-if="copied" :size="12" /><Copy v-else :size="12" />
        {{ copied ? '已复制' : '复制' }}
      </el-button>
    </div>

    <div v-if="fetched && result">
      <!-- 可运行项目：文档之外最有价值的产物，单独突出并提供下载 -->
      <div v-if="hasProject" class="project-block">
        <div class="project-head">
          <FolderCode :size="15" />
          <span class="project-title">可运行项目骨架</span>
          <el-button class="btn-dl" size="small" :loading="downloading" @click="downloadProject">
            <Download v-if="!downloading" :size="12" style="margin-right:4px" />
            {{ downloading ? '打包下载中…' : '下载 zip' }}
          </el-button>
        </div>
        <p class="project-hint">
          已按技术设计生成 FastAPI 后端 + Vue 前端。解压后 <code>docker compose up --build</code>
          即可启动：前端 5174、后端 8000/docs。接口当前返回 501，填入业务逻辑即可。
        </p>
        <div class="project-stats">
          <span><strong>{{ scaffold.endpoints }}</strong> 个接口</span>
          <span><strong>{{ scaffold.tables }}</strong> 张数据表</span>
          <span><strong>{{ scaffold.routers }}</strong> 个模块</span>
        </div>
      </div>

      <el-collapse>
        <el-collapse-item title="需求澄清结果" name="clarified">
          <pre><code>{{ JSON.stringify(result.deliverable.clarified_requirement, null, 2) }}</code></pre>
        </el-collapse-item>
        <el-collapse-item title="PRD 产品方案" name="prd">
          <pre><code>{{ JSON.stringify(result.deliverable.prd_doc, null, 2) }}</code></pre>
        </el-collapse-item>
        <el-collapse-item title="技术设计方案" name="tech">
          <pre><code>{{ JSON.stringify(result.deliverable.technical_design, null, 2) }}</code></pre>
        </el-collapse-item>
        <el-collapse-item title="代码骨架" name="scaffold">
          <pre><code>{{ JSON.stringify(result.deliverable.code_scaffold, null, 2) }}</code></pre>
        </el-collapse-item>
        <el-collapse-item title="评审报告" name="review">
          <pre><code>{{ JSON.stringify(result.deliverable.review_result, null, 2) }}</code></pre>
        </el-collapse-item>
      </el-collapse>

      <div v-if="result.exported_files && Object.keys(result.exported_files).length" class="export-box">
        <h4>导出文件</h4>
        <ul>
          <li v-for="(path, name) in result.exported_files" :key="name">
            <FileJson :size="12" /> {{ name }}: {{ path }}
          </li>
        </ul>
      </div>
    </div>

    <!-- 失败原因常驻显示：ElMessage 弹窗几秒就消失，用户回头看还是"没反应" -->
    <div v-else-if="errorMsg" class="load-error">
      <CircleAlert :size="15" />
      <span>{{ errorMsg }}</span>
      <el-button text size="small" @click="fetchResult">重试</el-button>
    </div>

    <div v-else-if="!loading" class="empty-hint">点击「加载」查看任务完整交付物</div>
  </div>
</template>

<style scoped>
.load-error {
  display: flex; align-items: center; gap: 8px;
  padding: 11px 13px;
  background: var(--danger-subtle);
  border: 1px solid var(--danger);
  border-radius: var(--radius-sm);
  color: var(--danger);
  font-size: 12.5px;
}
.load-error :deep(.el-button) { margin-left: auto; color: var(--danger); font-size: 12px; }

/* ── 可运行项目区块：区别于下面的 JSON 折叠面板，用强调色突出 ── */
.project-block {
  border: 1px solid var(--success);
  background: var(--success-subtle);
  border-radius: var(--radius-sm);
  padding: 13px 15px;
  margin-bottom: 14px;
}
.project-head { display: flex; align-items: center; gap: 7px; color: var(--success); }
.project-title { font-size: 13px; font-weight: 600; }
.btn-dl {
  margin-left: auto;
  background: var(--success); border: none; color: #fff; font-size: 11.5px;
}
.btn-dl:hover { opacity: .88; color: #fff; }
.project-hint {
  font-size: 12px; color: var(--text-secondary);
  margin: 8px 0 10px; line-height: 1.6;
}
.project-hint code {
  font-family: var(--font-mono); font-size: 11.5px;
  background: var(--bg-primary); padding: 1px 5px; border-radius: 4px;
}
.project-stats { display: flex; gap: 18px; font-size: 12px; color: var(--text-secondary); }
.project-stats strong { color: var(--text-primary); font-size: 14px; margin-right: 3px; }

.panel { padding: 16px; margin-bottom: 12px; }
.panel-head { display: flex; align-items: center; gap: 6px; font-size: 14px; font-weight: 600; margin-bottom: 10px; }

.btn-load {
  margin-left: auto;
  background: var(--success-subtle);
  border: none;
  color: var(--success);
  font-weight: 600;
  font-size: 11px;
  border-radius: var(--radius-sm);
}
.btn-load:hover { opacity: 0.8; }

:deep(pre) { margin: 0; background: var(--bg-primary); border-radius: var(--radius-sm); padding: 12px; max-height: 300px; overflow-y: auto; }
:deep(code) { font-size: 10px; line-height: 1.6; color: var(--text-secondary); }

.export-box { margin-top: 12px; padding: 10px 12px; background: var(--bg-primary); border-radius: var(--radius-sm); }
.export-box h4 { font-size: 12px; font-weight: 600; margin-bottom: 6px; }
.export-box ul { list-style: none; }
.export-box li { font-size: 11px; color: var(--text-secondary); font-family: var(--font-mono); display: flex; align-items: center; gap: 5px; margin-bottom: 2px; }

.empty-hint { font-size: 12px; color: var(--text-muted); padding: 8px 0; }

:deep(.el-collapse) { border: none; --el-collapse-header-height: 36px; }
:deep(.el-collapse-item__header) { color: var(--text-secondary); font-size: 12px; font-weight: 500; border-bottom: 1px solid var(--border); background: transparent; padding: 0 6px; height: 34px; line-height: 34px; }
:deep(.el-collapse-item__header:hover) { color: var(--text-primary); }
:deep(.el-collapse-item__wrap) { background: transparent; border: none; }
:deep(.el-collapse-item__content) { padding: 6px 0; }
</style>
