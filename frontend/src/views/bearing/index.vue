<template>
  <section class="page" data-module="bearing">
    <header class="page-head">
      <div>
        <h2>支座维护管理</h2>
        <p class="page-desc">承载力与锈蚀阈值复核矩阵：按支座编号、桥梁支座与检查周期裁定巡检 / 维修 / 更换，结论回写台账、定检清单与工程待办。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出支座台账</button>
      </div>
    </header>

    <nav class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab-item"
        :class="{ active: activeTab === tab.key }"
        type="button"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </nav>

    <div class="stat-row">
      <article class="stat-card"><span class="stat-label">高风险（更换）</span><strong class="conclusion-更换">{{ risk.高 ?? 0 }}</strong></article>
      <article class="stat-card"><span class="stat-label">中风险（维修）</span><strong class="conclusion-维修">{{ risk.中 ?? 0 }}</strong></article>
      <article class="stat-card"><span class="stat-label">低风险（巡检）</span><strong class="conclusion-巡检">{{ risk.低 ?? 0 }}</strong></article>
      <article class="stat-card"><span class="stat-label">当前启用矩阵</span><strong>{{ activeRule || '—' }}</strong></article>
    </div>

    <span v-if="message" :class="messageOk ? '' : 'error-text'" style="font-size:13px">{{ message }}</span>

    <!-- 支座台账 -->
    <div v-if="activeTab === 'ledger'">
      <form class="filter-bar" @submit.prevent="reloadLedger">
        <label class="filter-item"><span>支座编号</span><input v-model="ledgerFilters.keyword" placeholder="按支座编号检索" /></label>
        <label class="filter-item"><span>状态</span><input v-model="ledgerFilters.status" placeholder="正常/锈蚀/偏位/需更换" /></label>
        <button class="btn" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="resetLedgerFilters">重置条件</button>
      </form>
      <table class="data-table">
        <thead>
          <tr><th v-for="column in ledgerColumns" :key="column">{{ column }}</th><th>风险</th><th>可执行动作</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="String(row.id)">
            <td v-for="column in ledgerColumns" :key="column">{{ row[column] ?? '—' }}</td>
            <td><span v-if="row['风险等级']" class="risk-tag" :class="`risk-${row['风险等级']}`">{{ row['风险等级'] }}</span><span v-else>—</span></td>
            <td class="row-actions">
              <button v-for="action in actions" :key="action" class="link" type="button" @click="runAction(action, row)">{{ action }}</button>
            </td>
          </tr>
          <tr v-if="!rows.length"><td :colspan="ledgerColumns.length + 2" class="empty-state">暂无支座台账数据</td></tr>
        </tbody>
      </table>
      <footer class="page-foot"><span>共 {{ total }} 条支座记录</span></footer>
    </div>

    <!-- 检查单与批量复核 -->
    <div v-else-if="activeTab === 'review'">
      <div class="panel-card">
        <form class="filter-bar" @submit.prevent="reloadChecks" style="margin-bottom:8px">
          <label class="filter-item"><span>所属桥梁</span><input v-model="checkFilters.bridge" placeholder="如 云溪大桥" /></label>
          <label class="filter-item"><span>支座编号</span><input v-model="checkFilters.bearing_no" /></label>
          <label class="filter-item"><span>检查周期</span><input v-model="checkFilters.cycle" placeholder="2026Q3" /></label>
          <label class="filter-item"><span>单据状态</span><input v-model="checkFilters.status" placeholder="待巡检/待复核/已复核" /></label>
          <button class="btn" type="submit">查询检查单</button>
        </form>
        <div class="page-actions" style="display:flex;gap:8px">
          <button class="btn primary" type="button" @click="runBatch(false)">批量复核（当前周期）</button>
          <button class="btn" type="button" @click="runBatch(true)">幂等重放最近批次</button>
        </div>
      </div>

      <table class="data-table">
        <thead>
          <tr><th v-for="column in checkColumns" :key="column">{{ column }}</th><th>复核结论</th><th>风险</th><th>实测填报</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in checks" :key="String(row.id)">
            <td v-for="column in checkColumns" :key="column">{{ row[column] ?? '—' }}</td>
            <td>
              <span :class="`conclusion-${row['复核结论']}`">{{ row['复核结论'] || '—' }}</span>
              <div v-if="row['裁定依据']" style="font-size:11px;color:var(--muted);max-width:340px">{{ row['裁定依据'] }}</div>
            </td>
            <td><span v-if="row['风险等级']" class="risk-tag" :class="`risk-${row['风险等级']}`">{{ row['风险等级'] }}</span><span v-else>—</span></td>
            <td>
              <button v-if="row.status !== '已复核'" class="link" type="button" @click="openMeasurement(row)">填报实测</button>
              <span v-else style="color:var(--muted)">已归档</span>
            </td>
          </tr>
          <tr v-if="!checks.length"><td :colspan="checkColumns.length + 3" class="empty-state">暂无检查单</td></tr>
        </tbody>
      </table>
      <footer class="page-foot"><span>共 {{ checkTotal }} 张检查单</span></footer>

      <h3 style="font-size:14px;margin:16px 0 8px">复核批次台账</h3>
      <table class="data-table">
        <thead><tr><th>批次号</th><th>桥梁分组</th><th>周期</th><th>更换</th><th>维修</th><th>巡检</th><th>跳过</th><th>失败</th><th>状态</th><th>时间</th></tr></thead>
        <tbody>
          <tr v-for="run in reviewRuns" :key="String(run.id)">
            <td>{{ run['批次号'] }}</td><td>{{ run['桥梁分组'] }}</td><td>{{ run['检查周期'] }}</td>
            <td class="conclusion-更换">{{ run['更换'] }}</td><td class="conclusion-维修">{{ run['维修'] }}</td>
            <td class="conclusion-巡检">{{ run['巡检'] }}</td><td>{{ run['跳过'] }}</td>
            <td :class="run['失败'] ? 'group-fail' : ''">{{ run['失败'] }}</td>
            <td :class="run.status === '已提交' ? 'group-ok' : 'group-fail'">{{ run.status }}</td>
            <td>{{ run['复核时间'] }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 阈值矩阵规则 -->
    <div v-else>
      <div class="panel-card">
        <h3 style="margin:0 0 8px;font-size:14px">登记新版本矩阵</h3>
        <p class="page-desc" style="margin:0 0 8px">新版本登记后不生效；点「启用」切换规则时，未完成检查单会迁移到新版本，历史支座检查单保留原记录。</p>
        <form class="matrix-form" @submit.prevent="createRule">
          <label>规则版本<input v-model="ruleForm.规则版本" placeholder="v3" required /></label>
          <label>规则名称<input v-model="ruleForm.规则名称" placeholder="2027版阈值" style="width:180px" /></label>
          <label>承载维修阈值<input v-model="ruleForm.承载维修阈值" type="number" step="0.01" placeholder="0.85" /></label>
          <label>承载更换阈值<input v-model="ruleForm.承载更换阈值" type="number" step="0.01" placeholder="0.95" /></label>
          <label>锈蚀维修阈值<input v-model="ruleForm.锈蚀维修阈值" type="number" step="0.01" placeholder="0.15" /></label>
          <label>锈蚀更换阈值<input v-model="ruleForm.锈蚀更换阈值" type="number" step="0.01" placeholder="0.25" /></label>
          <label>位移限值(mm)<input v-model="ruleForm.位移限值mm" type="number" placeholder="30" /></label>
          <button class="btn primary" type="submit">登记新版本</button>
        </form>
      </div>
      <table class="data-table">
        <thead><tr><th>版本</th><th>名称</th><th>承载维修/更换</th><th>锈蚀维修/更换</th><th>位移限值</th><th>检查周期</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="rule in matrixRules" :key="String(rule.id)">
            <td>{{ rule['规则版本'] }}</td><td>{{ rule['规则名称'] }}</td>
            <td>{{ rule['承载维修阈值'] }} / {{ rule['承载更换阈值'] }}</td>
            <td>{{ rule['锈蚀维修阈值'] }} / {{ rule['锈蚀更换阈值'] }}</td>
            <td>{{ rule['位移限值mm'] }}mm</td><td>{{ rule['检查周期'] }}（容差 {{ rule['周期容差天'] }} 天）</td>
            <td><span v-if="rule.active" class="risk-tag risk-低">启用中</span><span v-else class="risk-tag risk-中">未启用</span></td>
            <td><button v-if="!rule.active" class="link" type="button" @click="activateRule(String(rule['规则版本']))">启用并迁移</button><span v-else>—</span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 实测填报弹层（轻量内联实现，不引依赖） -->
    <div v-if="measuring" class="panel-card" style="position:fixed;right:24px;bottom:24px;width:340px;box-shadow:0 8px 30px rgba(0,0,0,.15)">
      <h3 style="margin:0 0 6px;font-size:14px">填报实测数据 · {{ measuring['支座编号'] }}</h3>
      <form class="matrix-form" @submit.prevent="submitMeasurement">
        <label>承载利用率<input v-model="measureForm.承载利用率" placeholder="如 88%" style="width:140px" /></label>
        <label>实测锈蚀率<input v-model="measureForm.实测锈蚀率" placeholder="如 29%" style="width:140px" /></label>
        <label>实测位移(mm)<input v-model="measureForm.实测位移量" placeholder="如 9mm" style="width:140px" /></label>
        <button class="btn primary" type="submit">提交并进入待复核</button>
        <button class="btn ghost" type="button" @click="measuring = null">取消</button>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>

const ENDPOINT = '/api/bearing'
const tabs = [
  { key: 'ledger', label: '支座台账' },
  { key: 'review', label: '检查单与批量复核' },
  { key: 'matrix', label: '阈值矩阵规则' },
]
const activeTab = ref('ledger')
const message = ref('')
const messageOk = ref(true)

const ledgerColumns = ['支座编号', '所属桥梁', '支座类型', '设计承载力', '位移量', '锈蚀程度', '最近检查', '复核结论', '规则版本']
const actions = ['防锈处理', '纠偏复位', '安排更换']
const checkColumns = ['检查单号', '支座编号', '所属桥梁', '检查周期', '规则版本', 'status', '承载利用率', '实测锈蚀率', '实测位移量', '填报日期', '复核批次']

const rows = ref<Row[]>([])
const total = ref(0)
const ledgerFilters = ref<Record<string, string>>({ keyword: '', status: '' })

const checks = ref<Row[]>([])
const checkTotal = ref(0)
const checkFilters = ref<Record<string, string>>({ bridge: '', bearing_no: '', cycle: '2026Q3', status: '' })
const reviewRuns = ref<Row[]>([])
const risk = ref<Record<string, number>>({})

const matrixRules = ref<Row[]>([])
const activeRule = ref('')
const ruleForm = ref<Record<string, string | number>>({
  规则版本: '', 规则名称: '', 承载维修阈值: 0.85, 承载更换阈值: 0.95,
  锈蚀维修阈值: 0.15, 锈蚀更换阈值: 0.25, 位移限值mm: 30,
})

const measuring = ref<Row | null>(null)
const measureForm = ref<Record<string, string>>({ 承载利用率: '', 实测锈蚀率: '', 实测位移量: '' })

function flash(text: string, ok = true) {
  message.value = text
  messageOk.value = ok
}

function switchTab(key: string) {
  activeTab.value = key
  message.value = ''
  if (key === 'ledger') void reloadLedger()
  if (key === 'review') void reloadChecks()
  if (key === 'matrix') void reloadMatrix()
}

function resetLedgerFilters() {
  ledgerFilters.value = { keyword: '', status: '' }
  void reloadLedger()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function runAction(action: string, row: Row) {
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '支座维护动作未生效')
    flash(payload.message)
    await reloadLedger()
  } catch (error) {
    flash(error instanceof Error ? error.message : '支座维护操作失败', false)
  }
}

async function reloadLedger() {
  const params = new URLSearchParams()
  if (ledgerFilters.value.keyword) params.set('keyword', ledgerFilters.value.keyword)
  if (ledgerFilters.value.status) params.set('status', ledgerFilters.value.status)
  try {
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? 0
  } catch (error) {
    flash(error instanceof Error ? error.message : '支座台账读取失败', false)
  }
}

async function reloadChecks() {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(checkFilters.value)) {
    if (value) params.set(key, value)
  }
  try {
    const [checkRes, runRes] = await Promise.all([
      request(`${ENDPOINT}/checks?${params.toString()}`),
      request(`${ENDPOINT}/reviews`),
    ])
    const checkPayload = await checkRes.json()
    checks.value = checkPayload.items ?? []
    checkTotal.value = checkPayload.total ?? 0
    const runPayload = await runRes.json()
    reviewRuns.value = runPayload.items ?? []
    risk.value = runPayload.risk ?? {}
  } catch (error) {
    flash(error instanceof Error ? error.message : '检查单读取失败', false)
  }
}

async function runBatch(replay: boolean) {
  try {
    const values: Record<string, string> = { cycle: checkFilters.value.cycle || '' }
    if (checkFilters.value.bridge) values.bridge = checkFilters.value.bridge
    const response = await request(`${ENDPOINT}/reviews/batch`, {
      method: 'POST',
      body: JSON.stringify(replay ? {} : { values }),
    })
    const payload = await response.json()
    flash(payload.message, payload.ok)
    await Promise.all([reloadChecks(), reloadLedger(), reloadMatrix()])
  } catch (error) {
    flash(error instanceof Error ? error.message : '批量复核失败', false)
  }
}

function openMeasurement(row: Row) {
  measuring.value = row
  measureForm.value = {
    承载利用率: String(row['承载利用率'] ?? ''),
    实测锈蚀率: String(row['实测锈蚀率'] ?? ''),
    实测位移量: String(row['实测位移量'] ?? ''),
  }
}

async function submitMeasurement() {
  if (!measuring.value) return
  try {
    const response = await request(`${ENDPOINT}/checks/${measuring.value.id}/measurement`, {
      method: 'POST',
      body: JSON.stringify({ values: measureForm.value }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '实测填报失败')
    measuring.value = null
    flash(payload.message)
    await reloadChecks()
  } catch (error) {
    flash(error instanceof Error ? error.message : '实测填报失败', false)
  }
}

async function reloadMatrix() {
  try {
    const response = await request(`${ENDPOINT}/matrix`)
    const payload = await response.json()
    matrixRules.value = payload.items ?? []
    activeRule.value = payload.active ?? ''
  } catch (error) {
    flash(error instanceof Error ? error.message : '矩阵规则读取失败', false)
  }
}

async function createRule() {
  try {
    const response = await request(`${ENDPOINT}/matrix`, {
      method: 'POST',
      body: JSON.stringify({ values: ruleForm.value }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '矩阵登记失败')
    flash(payload.message)
    ruleForm.value.规则版本 = ''
    await reloadMatrix()
  } catch (error) {
    flash(error instanceof Error ? error.message : '矩阵登记失败', false)
  }
}

async function activateRule(version: string) {
  try {
    const response = await request(`${ENDPOINT}/matrix/${version}/activate`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '规则切换失败')
    flash(payload.message)
    await Promise.all([reloadMatrix(), reloadChecks()])
  } catch (error) {
    flash(error instanceof Error ? error.message : '规则切换失败', false)
  }
}

onMounted(() => {
  void reloadLedger()
  void reloadMatrix()
  void reloadChecks()
})
</script>
