<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>运营概览</h2>
        <p class="page-desc">汇总各业务模块的关键指标，先看总量再看异常；支座复核风险按当前生效规则版本统计。</p>
      </div>
    </header>
    <div class="stat-row">
      <article v-for="card in cards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>

    <section class="risk-panel" v-if="riskLevels.version">
      <header class="risk-head">
        <h3>支座复核风险等级</h3>
        <span class="risk-version">当前规则版本：{{ riskLevels.version }}</span>
      </header>
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">低风险 · 安排巡检</span>
          <strong class="stat-value risk-low">{{ riskLevels['低'] }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">中风险 · 安排维修</span>
          <strong class="stat-value risk-mid">{{ riskLevels['中'] }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">高风险 · 安排更换</span>
          <strong class="stat-value risk-high">{{ riskLevels['高'] }}</strong>
        </article>
      </div>
    </section>

    <table class="data-table">
      <thead>
        <tr><th>业务模块</th><th>今日新增</th><th>待处理</th><th>异常量</th><th>复核风险（低/中/高）</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in moduleRows" :key="row.name">
          <td>{{ row.name }}</td>
          <td>{{ row.created }}</td>
          <td>{{ row.pending }}</td>
          <td>{{ row.abnormal }}</td>
          <td v-if="row.risks">
            <span class="risk-low">{{ row.risks['低'] }}</span>
            <span class="risk-sep">/</span>
            <span class="risk-mid">{{ row.risks['中'] }}</span>
            <span class="risk-sep">/</span>
            <span class="risk-high">{{ row.risks['高'] }}</span>
          </td>
          <td v-else class="muted-text">—</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchJson } from '@/api/client'

type ModuleRow = {
  name: string
  created: number
  pending: number
  abnormal: number
  risks?: Record<string, number>
}

type Overview = {
  cards: { label: string; value: number }[]
  modules: ModuleRow[]
  risk_levels: { version: string; '低': number; '中': number; '高': number }
}

const cards = ref<Overview['cards']>([])
const moduleRows = ref<ModuleRow[]>([])
const riskLevels = ref<Overview['risk_levels']>({ version: '', '低': 0, '中': 0, '高': 0 })

onMounted(async () => {
  try {
    const payload = await fetchJson<Overview>('/api/overview')
    cards.value = payload.cards
    moduleRows.value = payload.modules
    riskLevels.value = payload.risk_levels ?? { version: '', '低': 0, '中': 0, '高': 0 }
  } catch {
    cards.value = [{ label: '业务模块', value: 0 }, { label: '今日新增', value: 0 }]
  }
})
</script>

<style scoped>
.risk-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}
.risk-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 8px;
}
.risk-head h3 {
  margin: 0;
  font-size: 15px;
}
.risk-version {
  color: var(--muted);
  font-size: 12px;
}
.risk-low { color: #2f7d32; }
.risk-mid { color: #b87a00; }
.risk-high { color: #c62828; }
.risk-sep { color: var(--muted); margin: 0 4px; }
.muted-text { color: var(--muted); }
</style>
