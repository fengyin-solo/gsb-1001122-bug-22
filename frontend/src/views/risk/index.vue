<template>
  <section class="page" data-module="risk">
    <header class="page-head">
      <div>
        <h2>风险汇总</h2>
        <p class="page-desc">
          路面病害入库结论统一回写到病害台账、巡查清单与桥梁限载待办；本页只展示现行里程基线裁决后的桩号，
          并自动核对同一路面病害根因在三处的桩号是否一致。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="reload">刷新核对结果</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in cards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <div :class="['consistency-banner', consistency?.一致 ? 'ok' : 'bad']">
      <template v-if="consistency?.一致">
        ✓ 桩号一致性核对通过：共核对 {{ consistency.核对病害数 }} 个病害根因，
        病害台账 / 巡查清单 / 限载待办三处现行桩号与投影坐标完全一致。
      </template>
      <template v-else-if="consistency">
        ✗ 发现 {{ consistency.问题数 }} 处桩号不一致，材料接入线可能未回写完整，请先执行迁移回填。
      </template>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </div>

    <h3 class="section-title">桥梁限载提示</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in alertColumns" :key="column">{{ column }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in bridgeAlerts" :key="String(row.id)">
          <td v-for="column in alertColumns" :key="column">{{ row[column] ?? '—' }}</td>
        </tr>
        <tr v-if="!bridgeAlerts.length">
          <td :colspan="alertColumns.length" class="empty-state">暂无桥梁限载提示：病害不在限载桥梁 500m 影响范围内</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">全部风险待办</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in todoColumns" :key="column">{{ column }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in todos" :key="String(row.id)">
          <td v-for="column in todoColumns" :key="column">{{ row[column] ?? '—' }}</td>
        </tr>
        <tr v-if="!todos.length">
          <td :colspan="todoColumns.length" class="empty-state">暂无风险待办</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">桩号不一致明细</h3>
    <table class="data-table" v-if="!consistency?.一致">
      <thead>
        <tr><th>病害编号</th><th>位置</th><th>病害桩号</th><th>对方桩号</th></tr>
      </thead>
      <tbody>
        <tr v-for="(item, idx) in consistency?.问题明细 ?? []" :key="idx">
          <td>{{ item.病害编号 }}</td>
          <td>{{ item.位置 }}</td>
          <td>{{ item.病害桩号 }}</td>
          <td>{{ item.对方桩号 }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Todo = Record<string, string | number | null>
type Consistency = {
  一致: boolean
  核对病害数: number
  问题数: number
  问题明细: { 病害编号: string; 位置: string; 病害桩号: string; 对方桩号: string }[]
}

const alertColumns = ['桥梁名称', '关联编号', '现行桩号', '投影坐标', '提示内容', 'status']
const todoColumns = ['待办类型', '关联编号', '所属路段', '现行桩号', '投影坐标', '严重程度', '处置结论', 'status']

const cards = ref<{ label: string; value: number }[]>([])
const todos = ref<Todo[]>([])
const consistency = ref<Consistency | null>(null)
const errorMessage = ref('')

const bridgeAlerts = computed(() => todos.value.filter((row) => row['待办类型'] === '桥梁限载提示'))

async function reload() {
  errorMessage.value = ''
  try {
    const response = await request('/api/risk/summary')
    if (!response.ok) {
      throw new Error('风险汇总读取失败')
    }
    const payload = await response.json()
    cards.value = payload.cards ?? []
    todos.value = payload.todos ?? []
    consistency.value = payload.consistency ?? null
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '风险汇总读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.consistency-banner {
  border-radius: 8px;
  padding: 12px 16px;
  margin-bottom: 16px;
  font-size: 14px;
}
.consistency-banner.ok {
  background: #e8f7ee;
  color: #1d7a43;
  border: 1px solid #b6e3c6;
}
.consistency-banner.bad {
  background: #fdeceb;
  color: #b4382f;
  border: 1px solid #f3c1bd;
}
.section-title {
  margin: 20px 0 10px;
  font-size: 15px;
}
</style>
