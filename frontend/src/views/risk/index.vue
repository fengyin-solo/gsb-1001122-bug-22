<template>
  <section class="page" data-module="risk">
    <header class="page-head">
      <div>
        <h2>风险汇总（同桩号对照）</h2>
        <p class="page-desc">
          外部材料入库结论在同一事务回写到病害台账、巡查清单与桥梁限载待办；
          本页以现行里程基线裁决桩号为准，三处桩号对不上时会明确标红。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="migrate">迁移回填存量位置</button>
        <button class="btn ghost" type="button" @click="reload">刷新汇总</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">病害总数</span>
        <strong class="stat-value">{{ summary.total_diseases ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待处置限载</span>
        <strong class="stat-value" :class="{ warn: (summary.pending_limit_todos ?? 0) > 0 }">
          {{ summary.pending_limit_todos ?? 0 }}
        </strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">桩号冲突</span>
        <strong class="stat-value" :class="{ warn: (summary.stake_conflicts ?? 0) > 0 }">
          {{ summary.stake_conflicts ?? 0 }}
        </strong>
      </article>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th>病害编号</th>
          <th>所属路段</th>
          <th>病害类型/程度</th>
          <th>裁决桩号（现行基线）</th>
          <th>巡查清单桩号</th>
          <th>桥梁限载提示桩号</th>
          <th>限载状态</th>
          <th>一致性</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.病害编号)">
          <td>{{ row.病害编号 ?? '—' }}</td>
          <td>{{ row.所属路段 ?? '—' }}</td>
          <td>{{ row.病害类型 ?? '—' }} / {{ row.严重程度 ?? '—' }}</td>
          <td>
            <strong>{{ row.裁决桩号 ?? '—' }}</strong>
            <small v-if="row.连续里程_m != null" class="muted">（{{ row.连续里程_m }}m · {{ row.坐标来源 }}）</small>
          </td>
          <td>
            {{ row.巡查桩号 ?? '—' }}
            <small v-if="row.巡查状态" class="muted">（{{ row.巡查状态 }}）</small>
          </td>
          <td>
            <span v-if="row.桥梁名称">{{ row.桥梁名称 }} · {{ row.限载桩号 ?? '—' }}</span>
            <span v-else class="muted">无</span>
          </td>
          <td>{{ row.限载状态 ?? '—' }}</td>
          <td>
            <span :class="row.桩号一致 ? 'badge ok' : 'badge bad'">
              {{ row.桩号一致 ? '一致' : '桩号冲突' }}
            </span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td colspan="8" class="empty-state">暂无风险数据，可先在「养护材料」页接入外部材料</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span v-if="message" :class="messageError ? 'error-text' : 'ok-text'">{{ message }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type RiskRow = {
  病害编号: string | null
  所属路段: string | null
  病害类型: string | null
  严重程度: string | null
  裁决桩号: string | null
  连续里程_m: number | null
  坐标来源: string | null
  巡查桩号: string | null
  巡查状态: string | null
  桥梁名称: string | null
  限载桩号: string | null
  限载状态: string | null
  桩号一致: boolean
}

type Summary = {
  total_diseases: number
  pending_limit_todos: number
  stake_conflicts: number
  items: RiskRow[]
}

const rows = ref<RiskRow[]>([])
const summary = ref<Partial<Summary>>({})
const message = ref('')
const messageError = ref(false)

async function reload() {
  message.value = ''
  try {
    const response = await request('/api/risk/summary')
    if (!response.ok) throw new Error('风险汇总读取失败')
    const payload = (await response.json()) as Summary
    summary.value = payload
    rows.value = payload.items ?? []
  } catch (error) {
    message.value = error instanceof Error ? error.message : '风险汇总读取失败'
    messageError.value = true
  }
}

async function migrate() {
  message.value = ''
  messageError.value = false
  try {
    const response = await request('/api/material/migrate', { method: 'POST' })
    if (!response.ok) throw new Error('存量迁移失败')
    const payload = (await response.json()) as { migrated: number; preserved_historical: number }
    message.value = `迁移完成：回填 ${payload.migrated} 处位置，保留历史采集坐标 ${payload.preserved_historical} 条`
    await reload()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '存量迁移失败'
    messageError.value = true
  }
}

onMounted(reload)
</script>

<style scoped>
.warn {
  color: #c0392b;
}
.muted {
  color: #94a3b8;
  font-weight: 400;
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
.ok-text {
  color: #1a7f37;
}
</style>
