<template>
  <section class="page" data-module="pavement">
    <header class="page-head">
      <div>
        <h2>路面病害管理</h2>
        <p class="page-desc">维护病害记录，围绕病害编号、所属路段、病害类型、严重程度做登记、筛选与状态流转。外部材料入库后统一显示现行基线裁决桩号。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记病害记录</button>
        <button class="btn" type="button" @click="exportRows">导出路面病害清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

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
            <template v-if="column === '病害编号'">
              <button class="link" type="button" @click="openDetail(row)">{{ row[column] ?? '—' }}</button>
            </template>
            <template v-else>{{ row[column] ?? '—' }}</template>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(row)">详情</button>
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
          <td :colspan="columns.length + 1" class="empty-state">暂无路面病害数据，可先登记病害记录</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条路面病害记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <div v-if="detail" class="drawer-mask" @click.self="detail = null">
      <div class="drawer">
        <header class="drawer-head">
          <h3>病害详情 · {{ detail.disease['病害编号'] }}</h3>
          <button class="link" type="button" @click="detail = null">关闭</button>
        </header>
        <div class="drawer-body">
          <h4>病害台账</h4>
          <dl class="detail-grid">
            <template v-for="field in diseaseFields" :key="field">
              <dt>{{ field }}</dt>
              <dd>{{ detail.disease[field] ?? '—' }}</dd>
            </template>
          </dl>

          <h4>空间投影（现行里程基线裁决）</h4>
          <dl class="detail-grid">
            <dt>裁决桩号</dt><dd>{{ detail.risk.裁决桩号 ?? '—' }}</dd>
            <dt>连续里程(m)</dt><dd>{{ detail.risk.连续里程_m ?? '—' }}</dd>
            <dt>投影坐标</dt><dd>({{ detail.disease['投影坐标X'] ?? '—' }}, {{ detail.disease['投影坐标Y'] ?? '—' }})</dd>
            <dt>坐标来源</dt><dd>{{ detail.disease['坐标来源'] ?? (detail.disease['材料原始桩号'] ? '—' : '历史数据') }}</dd>
            <dt>材料原始桩号</dt><dd>{{ detail.disease['材料原始桩号'] ?? '—' }}</dd>
            <dt>来源批次</dt><dd>{{ detail.disease['批次号'] ?? '—' }}</dd>
          </dl>

          <h4>跨模块回写对照</h4>
          <dl class="detail-grid">
            <dt>巡查清单</dt>
            <dd>{{ detail.risk.巡查编号 ?? '未关联' }} · 桩号 {{ detail.risk.巡查桩号 ?? '—' }} · {{ detail.risk.巡查状态 ?? '—' }}</dd>
            <dt>桥梁限载提示</dt>
            <dd v-if="detail.risk.桥梁名称">
              {{ detail.risk.桥梁名称 }} · 桩号 {{ detail.risk.限载桩号 ?? '—' }} · {{ detail.risk.限载状态 ?? '—' }}
            </dd>
            <dd v-else>无关联限载待办</dd>
            <dt>桩号一致性</dt>
            <dd>
              <span :class="detail.risk.桩号一致 ? 'badge ok' : 'badge bad'">
                {{ detail.risk.桩号一致 ? '三处桩号一致' : '桩号冲突' }}
              </span>
            </dd>
          </dl>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type RiskRow = Record<string, string | number | boolean | null>

const ENDPOINT = '/api/pavement'
const columns = ["病害编号", "所属路段", "病害类型", "严重程度", "起止桩号", "裁决桩号", "面积", "发现日期", "病害状态"]
const actions = ["派发修复", "标记修复", "验收通过"]
const statuses = ["待修复", "修复中", "已修复", "已验收"]
const stats = [{"label": "待修复病害", "value": 0}, {"label": "修复中病害", "value": 0}, {"label": "已修复病害", "value": 0}]
const diseaseFields = ["病害编号", "所属路段", "病害类型", "严重程度", "起止桩号", "面积", "发现日期", "病害状态", "材料编号"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const detail = ref<{ disease: Row; risk: RiskRow } | null>(null)

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '病害记录登记入口尚未接入审批流'
}

async function openDetail(row: Row) {
  errorMessage.value = ''
  let risk: RiskRow = { 桩号一致: true }
  try {
    const response = await request('/api/risk/summary')
    if (response.ok) {
      const payload = (await response.json()) as { items: RiskRow[] }
      risk = payload.items.find((item) => String(item.病害编号) === String(row['病害编号'])) ?? risk
    }
  } catch {
    // 汇总读不到时仍可展示台账本身
  }
  detail.value = { disease: row, risk }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('路面病害动作未生效，请稍后重试')
    }
    await reload()
    if (detail.value && String(detail.value.disease.id) === String(row.id)) detail.value = null
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '路面病害操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('病害记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '路面病害列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.drawer-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: flex;
  justify-content: flex-end;
  z-index: 50;
}
.drawer {
  width: 520px;
  max-width: 92vw;
  height: 100%;
  background: #fff;
  overflow-y: auto;
  box-shadow: -8px 0 24px rgba(15, 23, 42, 0.15);
}
.drawer-head {
  position: sticky;
  top: 0;
  background: #fff;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid #e5e9f0;
}
.drawer-head h3 {
  margin: 0;
  font-size: 16px;
}
.drawer-body {
  padding: 16px 20px 32px;
}
.drawer-body h4 {
  margin: 18px 0 8px;
  font-size: 14px;
  color: #334155;
}
.detail-grid {
  display: grid;
  grid-template-columns: 110px 1fr;
  gap: 6px 12px;
  margin: 0;
  font-size: 13px;
}
.detail-grid dt {
  color: #64748b;
}
.detail-grid dd {
  margin: 0;
  word-break: break-all;
}
.badge {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 12px;
}
.badge.ok {
  background: #e6f7ec;
  color: #1a7f37;
}
.badge.bad {
  background: #fdecea;
  color: #c0392b;
}
</style>
