<template>
  <section class="page" data-module="bearing">
    <header class="page-head">
      <div>
        <h2>支座维护管理</h2>
        <p class="page-desc">基于承载力与锈蚀阈值复核矩阵，按支座编号、所属桥梁和检查周期判定巡检、维修或更换，结论回写台账、定检清单与工程待办。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" :disabled="busy" @click="runBatch()">批量复核（按桥梁分组提交）</button>
        <button class="btn" type="button" :disabled="busy" @click="runBatch(activeBridge || undefined)">仅复核当前桥梁</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value" :class="item.cls">{{ item.value }}</strong>
      </article>
    </div>

    <!-- 阈值矩阵 -->
    <section class="matrix-panel">
      <div class="matrix-head">
        <h3>承载力与锈蚀阈值复核矩阵 · {{ matrix.active_version }}（{{ activeRule?.label }}）</h3>
        <div class="version-switch">
          <button
            v-for="ver in matrix.versions"
            :key="ver.version"
            class="btn"
            :class="{ primary: !ver.active, ghost: ver.active }"
            type="button"
            :disabled="busy || ver.active"
            @click="switchVersion(ver.version)"
          >
            {{ ver.version }}{{ ver.active ? '（生效中）' : '' }}
          </button>
        </div>
      </div>
      <table class="data-table" v-if="activeRule">
        <thead>
          <tr><th>判定维度</th><th>安排巡检</th><th>安排维修</th><th>安排更换</th></tr>
        </thead>
        <tbody>
          <tr>
            <td>承载力比（实荷/设计）</td>
            <td>&lt; {{ pct(activeRule.capacity_repair) }}</td>
            <td>≥ {{ pct(activeRule.capacity_repair) }}</td>
            <td>≥ {{ pct(activeRule.capacity_replace) }}（硬线 {{ pct(activeRule.capacity_hardline) }}）</td>
          </tr>
          <tr>
            <td>锈蚀率</td>
            <td>&lt; {{ pct(activeRule.rust_repair) }}</td>
            <td>≥ {{ pct(activeRule.rust_repair) }}</td>
            <td>≥ {{ pct(activeRule.rust_replace) }}</td>
          </tr>
          <tr>
            <td>位移量</td>
            <td>&lt; {{ activeRule.displace_repair }}mm</td>
            <td>≥ {{ activeRule.displace_repair }}mm</td>
            <td>≥ {{ activeRule.displace_replace }}mm</td>
          </tr>
          <tr>
            <td>检查周期</td>
            <td colspan="3">距最近检查超过 {{ activeRule.overdue_days }} 天未到期检，至少安排巡检</td>
          </tr>
          <tr>
            <td>阈值冲突裁定</td>
            <td colspan="3">承载力与锈蚀结论不一致时，以设计承载力富余度与最近专项结论共同裁定；承载力超硬线直接更换，专项结论最多下调一档。历史支座保留原检查记录，不重新判定。</td>
          </tr>
        </tbody>
      </table>
    </section>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">
            <span v-if="column === '复核建议'" class="risk-tag" :class="riskClass(row['复核风险'])">{{ row[column] ?? '—' }}</span>
            <span v-else-if="column === '复核风险'" class="risk-tag" :class="riskClass(row[column])">{{ row[column] ?? '—' }}</span>
            <template v-else>{{ row[column] ?? '—' }}</template>
          </td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无支座维护数据，可先登记桥梁支座</td>
        </tr>
      </tbody>
    </table>

    <!-- 复核检查单 -->
    <section class="checklist-panel">
      <div class="matrix-head">
        <h3>复核检查单（{{ checklists.length }}/{{ checklistTotal }}）</h3>
        <div class="version-switch">
          <button
            v-for="opt in adviceFilters"
            :key="opt.value ?? 'all'"
            class="btn"
            :class="{ ghost: adviceFilter !== opt.value, primary: adviceFilter === opt.value }"
            type="button"
            @click="changeAdviceFilter(opt.value)"
          >
            {{ opt.label }}
          </button>
        </div>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>支座编号</th><th>所属桥梁</th><th>检查周期</th><th>规则版本</th>
            <th>复核建议</th><th>风险</th><th>承载力比</th><th>锈蚀率</th><th>状态</th><th>裁定依据</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in checklists" :key="String(item.id)" :class="{ legacy: item['历史遗留'] }">
            <td>{{ item['支座编号'] }}</td>
            <td>{{ item['所属桥梁'] }}</td>
            <td>{{ item['检查周期'] }}</td>
            <td>{{ item['规则版本'] }}</td>
            <td><span class="risk-tag" :class="riskClass(item['风险等级'])">{{ item['建议'] }}</span></td>
            <td><span class="risk-tag" :class="riskClass(item['风险等级'])">{{ item['风险等级'] }}</span></td>
            <td>{{ item['承载力比'] ?? '—' }}</td>
            <td>{{ item['锈蚀率'] != null ? pct(item['锈蚀率']) : '—' }}</td>
            <td>{{ item['历史遗留'] ? '历史保留' : item['检查单状态'] }}</td>
            <td class="basis-cell" :title="asText(item['裁定依据'])">{{ item['裁定依据'] }}</td>
            <td>
              <button
                v-if="item['检查单状态'] === '待处置'"
                class="link"
                type="button"
                :disabled="busy"
                @click="completeChecklist(item.id)"
              >
                闭环检查单
              </button>
              <span v-else class="muted-text">{{ item['完成时间'] }}</span>
            </td>
          </tr>
          <tr v-if="!checklists.length">
            <td colspan="11" class="empty-state">尚未执行批量复核，点击右上角按钮生成检查单</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条支座维护记录</span>
      <span v-if="message" :class="errorMessage ? 'error-text' : 'ok-text'">{{ message }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>
type Checklist = Row

const ENDPOINT = '/api/bearing'
const REVIEW_ENDPOINT = '/api/bearing/review'
const columns = ["支座编号", "所属桥梁", "支座类型", "设计承载力", "实际荷载", "位移量", "锈蚀程度", "最近检查", "最近专项结论", "复核建议", "复核风险", "复核周期", "支座状态"]
const actions = ["防锈处理", "纠偏复位", "安排更换"]
const filterFields = columns.slice(0, 3)

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const message = ref('')
const busy = ref(false)
const filters = ref<Record<string, string>>({})

type MatrixThresholds = {
  label?: string
  capacity_replace?: number
  capacity_repair?: number
  rust_replace?: number
  rust_repair?: number
  displace_replace?: number
  displace_repair?: number
  overdue_days?: number
  capacity_hardline?: number
  [key: string]: string | number | undefined
}

type Matrix = {
  active_version: string
  versions: { version: string; label: string; active: boolean }[]
  thresholds: MatrixThresholds
}

const matrix = ref<Matrix>({ active_version: 'v1', versions: [], thresholds: {} })

const activeRule = computed<MatrixThresholds>(() => ({
  label: matrix.value.versions.find((v) => v.version === matrix.value.active_version)?.label ?? '',
  ...matrix.value.thresholds,
}))

const checklists = ref<Checklist[]>([])
const checklistTotal = ref(0)
const adviceFilter = ref<string | null>(null)
const adviceFilters = [
  { label: '全部', value: null },
  { label: '待处置', value: '__open__' },
  { label: '巡检', value: '巡检' },
  { label: '维修', value: '维修' },
  { label: '更换', value: '更换' },
]

const stats = computed(() => {
  const low = rows.value.filter((r) => r['复核风险'] === '低').length
  const mid = rows.value.filter((r) => r['复核风险'] === '中').length
  const high = rows.value.filter((r) => r['复核风险'] === '高').length
  const open = checklists.value.filter((c) => c['检查单状态'] === '待处置').length
  return [
    { label: '复核低风险（巡检）', value: low, cls: 'risk-low' },
    { label: '复核中风险（维修）', value: mid, cls: 'risk-mid' },
    { label: '复核高风险（更换）', value: high, cls: 'risk-high' },
    { label: '待处置检查单', value: open, cls: '' },
  ]
})

const activeBridge = ref<string | null>(null)

function pct(value: number | string | boolean | null | undefined): string {
  return `${Math.round(Number(value) * 100)}%`
}

function riskClass(risk: unknown): string {
  if (risk === '高') return 'risk-high'
  if (risk === '中') return 'risk-mid'
  if (risk === '低') return 'risk-low'
  return ''
}

function asText(value: unknown): string {
  return value == null ? '' : String(value)
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  message.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('支座维护动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '支座维护操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('桥梁支座列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    if (filters.value['所属桥梁']) {
      activeBridge.value = filters.value['所属桥梁']
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '支座维护列表读取失败'
  }
}

async function loadMatrix() {
  try {
    const response = await request(`${REVIEW_ENDPOINT}/matrix`)
    if (response.ok) {
      matrix.value = await response.json()
    }
  } catch {
    /* 矩阵读取失败不阻塞台账页 */
  }
}

async function loadChecklists() {
  const params = new URLSearchParams({ page: '1', size: '100' })
  if (adviceFilter.value === '__open__') {
    params.set('status', '待处置')
  } else if (adviceFilter.value) {
    params.set('advice', adviceFilter.value)
  }
  try {
    const response = await request(`${REVIEW_ENDPOINT}/checklists?${params.toString()}`)
    if (response.ok) {
      const payload = await response.json()
      checklists.value = payload.items ?? []
      checklistTotal.value = payload.total ?? 0
    }
  } catch {
    /* 检查单读取失败保留旧数据 */
  }
}

async function refreshAll() {
  await Promise.all([reload(), loadMatrix(), loadChecklists()])
}

async function changeAdviceFilter(value: string | null) {
  adviceFilter.value = value
  void loadChecklists()
}

async function runBatch(bridge?: string) {
  busy.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    const response = await request(`${REVIEW_ENDPOINT}/batch`, {
      method: 'POST',
      body: JSON.stringify(bridge ? { bridge } : {}),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.detail || payload.message || '批量复核未生效')
    }
    message.value = payload.message
    await refreshAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批量复核失败'
  } finally {
    busy.value = false
  }
}

async function completeChecklist(idValue: unknown) {
  const id = Number(idValue)
  busy.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    const response = await request(`${REVIEW_ENDPOINT}/checklists/${id}/complete`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.detail || payload.message || '检查单闭环失败')
    }
    message.value = payload.message
    await refreshAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '检查单闭环失败'
  } finally {
    busy.value = false
  }
}

async function switchVersion(version: string) {
  busy.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    const response = await request(`${REVIEW_ENDPOINT}/switch-version`, {
      method: 'POST',
      body: JSON.stringify({ version }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.detail || payload.message || '规则切换失败')
    }
    message.value = payload.message
    await refreshAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '规则切换失败'
  } finally {
    busy.value = false
  }
}

onMounted(refreshAll)
</script>

<style scoped>
.matrix-panel,
.checklist-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  margin: 12px 0;
}
.matrix-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}
.matrix-head h3 {
  margin: 0;
  font-size: 15px;
}
.version-switch {
  display: flex;
  gap: 6px;
}
.risk-tag {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 12px;
  border: 1px solid currentColor;
}
.risk-low { color: #2f7d32; }
.risk-mid { color: #b87a00; }
.risk-high { color: #c62828; }
.basis-cell {
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--muted);
}
tr.legacy td {
  background: #f7f7f7;
  color: var(--muted);
}
.ok-text { color: #2f7d32; }
.muted-text { color: var(--muted); font-size: 12px; }
</style>
