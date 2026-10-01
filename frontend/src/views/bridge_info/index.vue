<template>
  <section class="page" data-module="bridge_info">
    <header class="page-head">
      <div>
        <h2>桥梁档案管理</h2>
        <p class="page-desc">维护桥梁，围绕桥梁编号、桥梁名称、桥型结构、跨径组合做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记桥梁</button>
        <button class="btn" type="button" @click="exportRows">导出桥梁档案清单</button>
      </div>
    </header>

    <div v-if="limitTodos.length" class="limit-panel">
      <h3>桥梁限载提示（由路面病害材料同事务回写）</h3>
      <ul class="limit-list">
        <li v-for="todo in limitTodos" :key="String(todo.病害编号)">
          <span class="limit-stake">{{ todo.限载桩号 ?? '—' }}</span>
          <span class="limit-text">{{ todo.限载待办 }}</span>
          <span class="badge bad">{{ todo.限载状态 }}</span>
        </li>
      </ul>
    </div>

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
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
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
          <td :colspan="columns.length + 1" class="empty-state">暂无桥梁档案数据，可先登记桥梁</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条桥梁档案记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/bridge_info'
const columns = ["桥梁编号", "桥梁名称", "桥型结构", "跨径组合", "设计荷载", "建成年份", "上次评定等级", "桥梁状态"]
const actions = ["设置限载", "安排加固", "启动重建"]
const statuses = ["正常", "限载", "加固", "重建"]
const stats = [{"label": "正常桥梁", "value": 0}, {"label": "限载桥梁", "value": 0}, {"label": "加固桥梁", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

type LimitTodo = {
  病害编号: string
  桥梁名称: string | null
  限载待办: string | null
  限载状态: string | null
  限载桩号: string | null
}
const limitTodos = ref<LimitTodo[]>([])

async function loadLimitTodos() {
  try {
    const response = await request('/api/risk/summary')
    if (!response.ok) return
    const payload = (await response.json()) as { items: LimitTodo[] }
    limitTodos.value = (payload.items ?? []).filter(
      (item) => item.桥梁名称 && item.限载状态 === '待处置',
    )
  } catch {
    // 限载提示读不到不阻塞桥梁档案列表
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '桥梁登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('桥梁档案动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '桥梁档案操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('桥梁列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '桥梁档案列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadLimitTodos()})
</script>

<style scoped>
.limit-panel {
  border: 1px solid #f3c7c0;
  background: #fff7f6;
  border-radius: 10px;
  padding: 14px 16px;
  margin-bottom: 18px;
}
.limit-panel h3 {
  margin: 0 0 10px;
  font-size: 15px;
  color: #c0392b;
}
.limit-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.limit-list li {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 13px;
}
.limit-stake {
  font-weight: 600;
  min-width: 84px;
  color: #b42318;
}
.limit-text {
  flex: 1;
  color: #475467;
}
.badge {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 12px;
}
.badge.bad {
  background: #fdecea;
  color: #c0392b;
}
</style>
