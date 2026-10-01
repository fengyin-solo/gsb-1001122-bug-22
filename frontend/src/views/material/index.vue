<template>
  <section class="page" data-module="material">
    <header class="page-head">
      <div>
        <h2>养护材料管理</h2>
        <p class="page-desc">维护养护材料，围绕材料编号、材料名称、材料类别、规格型号做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记养护材料</button>
        <button class="btn" type="button" @click="exportRows">导出养护材料清单</button>
      </div>
    </header>

    <div class="ingest-panel">
      <h3 class="section-title">路面病害外部材料接入</h3>
      <p class="ingest-hint">
        材料解析 → 桩号裁决（现行里程基线）→ 空间投影 → 病害台账 / 巡查清单 / 桥梁限载待办同事务回写；
        材料指纹保证幂等，断线后用同一批次号续传，只处理未完成行。
      </p>
      <div class="ingest-controls">
        <label class="filter-item">
          <span>批次号（可空，断线续传请填原批次号）</span>
          <input v-model="batchId" placeholder="例如 BATCH-20260930-01" />
        </label>
        <button class="btn" type="button" @click="fillDemo">填入示例材料</button>
      </div>
      <textarea v-model="materialText" class="ingest-textarea" rows="10" :placeholder="materialPlaceholder"></textarea>
      <div class="ingest-actions">
        <button class="btn primary" type="button" :disabled="submitting" @click="submitMaterial">
          {{ submitting ? '接入处理中…' : '提交材料接入' }}
        </button>
        <button class="btn" type="button" @click="loadBatches">刷新批次进度</button>
        <span v-if="ingestMessage" class="ingest-message" :class="ingestOk ? 'ok' : 'bad'">{{ ingestMessage }}</span>
      </div>

      <table v-if="batches.length" class="data-table ingest-batches">
        <thead>
          <tr>
            <th>批次号</th>
            <th>来源</th>
            <th>状态</th>
            <th>成功</th>
            <th>判重</th>
            <th>失败</th>
            <th>材料指纹（前 16 位）</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="batch in batches" :key="batch.批次编号">
            <td>{{ batch.批次编号 }}</td>
            <td>{{ batch.来源 }}</td>
            <td>{{ batch.状态 }}</td>
            <td>{{ batch.成功数 }}</td>
            <td>{{ batch.判重数 }}</td>
            <td>{{ batch.失败数 }}</td>
            <td>{{ String(batch.材料指纹).slice(0, 16) }}…</td>
            <td>
              <button
                v-if="batch.失败数 > 0 || batch.状态 === '待处理' || batch.状态 === '部分完成'"
                class="link"
                type="button"
                @click="resumeBatch(batch.批次编号)"
              >
                续传未完成行
              </button>
              <button class="link" type="button" @click="toggleDetail(batch.批次编号)">
                {{ expanded === batch.批次编号 ? '收起明细' : '查看逐行结果' }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <table v-if="expanded" class="data-table ingest-detail">
        <thead>
          <tr><th>采集序号</th><th>外部材料编号</th><th>状态</th><th>裁决现行桩号</th><th>投影坐标</th><th>失败原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="record in currentDetail" :key="String(record.id)">
            <td>{{ record.采集序号 }}</td>
            <td>{{ record.外部材料编号 }}</td>
            <td>{{ record.状态 }}</td>
            <td>{{ record.现行桩号 ?? '—' }}</td>
            <td>{{ record.投影坐标 ?? '—' }}</td>
            <td>{{ record.失败原因 || '—' }}</td>
          </tr>
        </tbody>
      </table>
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
          <td :colspan="columns.length + 1" class="empty-state">暂无养护材料数据，可先登记养护材料</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条养护材料记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type Batch = {
  批次编号: string
  来源: string
  状态: string
  成功数: number
  判重数: number
  失败数: number
  材料指纹: string
  明细?: Record<string, string | number | null>[]
}

const ENDPOINT = '/api/material'
const columns = ["材料编号", "材料名称", "材料类别", "规格型号", "供应商", "进场日期", "存放地点", "材料状态"]
const actions = ["领用材料", "送检材料", "退还不合格"]
const statuses = ["在库", "已领用", "待检测", "不合格"]
const stats = [{"label": "在库材料", "value": 0}, {"label": "待检测材料", "value": 0}, {"label": "不合格材料", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const materialPlaceholder = `[
  {
    "外部材料编号": "EXT-20260930-01",
    "病害编号": "PAVE-2001",
    "所属路段": "G104国道杭州段",
    "病害类型": "龟裂",
    "严重程度": "重",
    "桩号": "K3+300",
    "行向": "上行",
    "里程基线版本": "旧线-2018",
    "面积": "3.2㎡",
    "发现日期": "2026-09-30"
  }
]`

const batchId = ref('')
const materialText = ref('')
const submitting = ref(false)
const ingestMessage = ref('')
const ingestOk = ref(true)
const batches = ref<Batch[]>([])
const expanded = ref('')

const currentDetail = computed(() =>
  batches.value.find((batch) => batch.批次编号 === expanded.value)?.明细 ?? [],
)

function fillDemo() {
  materialText.value = materialPlaceholder
}

function showIngestMessage(message: string, ok: boolean) {
  ingestMessage.value = message
  ingestOk.value = ok
}

async function submitMaterial() {
  ingestMessage.value = ''
  let parsed: unknown
  try {
    parsed = JSON.parse(materialText.value || '[]')
  } catch {
    showIngestMessage('材料不是合法 JSON，请按数组格式整理后再提交', false)
    return
  }
  const list = Array.isArray(parsed) ? parsed : [parsed]
  submitting.value = true
  try {
    const response = await request(`${ENDPOINT}/ingest`, {
      method: 'POST',
      body: JSON.stringify({ rows: list, batch_id: batchId.value || null }),
    })
    const payload = await response.json()
    if (!response.ok) {
      throw new Error(payload.detail || '材料接入失败')
    }
    if (payload.判重) {
      showIngestMessage(`${payload.message}（原批次：${payload.批次编号 ?? '—'}）`, true)
    } else {
      showIngestMessage(
        `批次 ${payload.批次编号}：${payload.状态}，成功 ${payload.成功数}、判重 ${payload.判重数}、失败 ${payload.失败数}`,
        payload.失败数 === 0,
      )
      batchId.value = payload.批次编号
    }
    await loadBatches()
    await reload()
  } catch (error) {
    showIngestMessage(error instanceof Error ? error.message : '材料接入失败', false)
  } finally {
    submitting.value = false
  }
}

async function resumeBatch(id: string) {
  ingestMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/ingest/resume/${id}`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok) {
      throw new Error(payload.detail || '续传失败')
    }
    showIngestMessage(
      `批次 ${payload.批次编号} 续传完成：成功 ${payload.成功数}、失败 ${payload.失败数}`,
      payload.失败数 === 0,
    )
    batchId.value = id
    await loadBatches()
    await reload()
  } catch (error) {
    showIngestMessage(error instanceof Error ? error.message : '续传失败', false)
  }
}

function toggleDetail(id: string) {
  expanded.value = expanded.value === id ? '' : id
}

async function loadBatches() {
  try {
    const response = await request(`${ENDPOINT}/ingest/batches`)
    if (!response.ok) {
      return
    }
    const payload = await response.json()
    batches.value = payload.items ?? []
  } catch {
    // 批次进度是辅助信息，静默不打断主列表
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
  errorMessage.value = '养护材料登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('养护材料动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '养护材料操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('养护材料列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '养护材料列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadBatches()
})
</script>

<style scoped>
.ingest-panel {
  background: #f7f9fc;
  border: 1px solid #dce3ee;
  border-radius: 10px;
  padding: 14px 16px 18px;
  margin-bottom: 18px;
}
.ingest-hint {
  font-size: 13px;
  color: #5b6577;
  margin: 6px 0 12px;
}
.ingest-controls {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  margin-bottom: 10px;
}
.ingest-textarea {
  width: 100%;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  border: 1px solid #c8d1e0;
  border-radius: 8px;
  padding: 10px 12px;
  resize: vertical;
}
.ingest-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
}
.ingest-message {
  font-size: 13px;
}
.ingest-message.ok {
  color: #1d7a43;
}
.ingest-message.bad {
  color: #b4382f;
}
.ingest-batches,
.ingest-detail {
  margin-top: 14px;
}
.section-title {
  margin: 0;
  font-size: 15px;
}
</style>
