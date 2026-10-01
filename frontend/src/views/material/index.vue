<template>
  <section class="page" data-module="material">
    <header class="page-head">
      <div>
        <h2>养护材料管理</h2>
        <p class="page-desc">维护养护材料，围绕材料编号、材料名称、材料类别、规格型号做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openIngest">接入外部材料</button>
        <button class="btn" type="button" @click="exportRows">导出养护材料清单</button>
      </div>
    </header>

    <div v-if="ingestOpen" class="ingest-panel">
      <div class="ingest-head">
        <h3>路面病害外部材料接入</h3>
        <button class="link" type="button" @click="ingestOpen = false">收起</button>
      </div>
      <p class="ingest-tip">
        一批材料按「解析 → 现行里程基线裁决桩号 → 空间投影 → 病害台账/巡查清单/桥梁限载待办同事务回写」处理；
        同方向桩号冲突一律以现行基线为准。材料指纹保证断线重连或并发重复提交只建档一次，任一写入失败整批回滚。
      </p>
      <div class="ingest-form">
        <label class="filter-item">
          <span>批次号（断线重传请填写原批次号）</span>
          <input v-model="batchId" placeholder="留空自动生成，如 BATCH-20260928-01" />
        </label>
        <textarea
          v-model="materialsText"
          class="ingest-textarea"
          rows="10"
          :placeholder="samplePlaceholder"
        ></textarea>
        <div class="ingest-actions">
          <button class="btn primary" type="button" :disabled="submitting" @click="submitIngest">
            {{ submitting ? '接入处理中…' : '提交接入' }}
          </button>
          <button class="btn ghost" type="button" @click="fillSample">填入示例</button>
        </div>
      </div>
      <pre v-if="ingestResult" class="ingest-result" :class="{ bad: !ingestResult.ok }">{{ ingestResult.message }}</pre>
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
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

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

const SAMPLE = `[
  {
    "材料编号": "EXT-20260928-01",
    "所属路段": "G104绕城段",
    "方向": "上行",
    "桩号": "K12+800",
    "病害类型": "纵向裂缝",
    "严重程度": "严重",
    "发现日期": "2026-09-28",
    "关联桥梁": "迎宾大桥",
    "面积": "12㎡"
  }
]`

const ingestOpen = ref(false)
const materialsText = ref('')
const batchId = ref('')
const submitting = ref(false)
const ingestResult = ref<{ ok: boolean; message: string } | null>(null)
const samplePlaceholder = 'JSON 数组：材料编号/所属路段/方向/桩号/病害类型/严重程度/发现日期/关联桥梁'

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openIngest() {
  ingestOpen.value = true
  ingestResult.value = null
}

function fillSample() {
  materialsText.value = SAMPLE
}

async function submitIngest() {
  errorMessage.value = ''
  ingestResult.value = null
  let materials: unknown
  try {
    materials = JSON.parse(materialsText.value || '[]')
  } catch {
    ingestResult.value = { ok: false, message: '材料不是合法 JSON，请检查后重试（未做任何写入）' }
    return
  }
  if (!Array.isArray(materials) || materials.length === 0) {
    ingestResult.value = { ok: false, message: '请提交非空的材料数组' }
    return
  }
  submitting.value = true
  try {
    const response = await request(`${ENDPOINT}/ingest`, {
      method: 'POST',
      body: JSON.stringify({ materials, batch_id: batchId.value || null }),
    })
    const payload = (await response.json()) as {
      ok: boolean
      message: string
      batch_id?: string
      created?: number
      updated?: number
      duplicated?: number
      failed?: number
    }
    if (payload.batch_id) batchId.value = payload.batch_id
    const lines = [payload.message]
    if (typeof payload.created === 'number') {
      lines.push(`（新增 ${payload.created ?? 0} / 更新 ${payload.updated ?? 0} / 指纹去重 ${payload.duplicated ?? 0} / 失败 ${payload.failed ?? 0}）`)
    }
    ingestResult.value = { ok: payload.ok, message: lines.join(' ') }
    if (payload.ok) await reload()
  } catch (error) {
    ingestResult.value = {
      ok: false,
      message: error instanceof Error ? error.message : '接入请求未送达，材料未入库；请带原批次号重试',
    }
  } finally {
    submitting.value = false
  }
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

onMounted(reload)
</script>

<style scoped>
.ingest-panel {
  border: 1px solid #d8e0ea;
  border-radius: 10px;
  padding: 16px;
  margin-bottom: 18px;
  background: #f8fafc;
}
.ingest-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.ingest-head h3 {
  margin: 0;
  font-size: 16px;
}
.ingest-tip {
  margin: 8px 0 12px;
  color: #5b6b7f;
  font-size: 13px;
  line-height: 1.7;
}
.ingest-form {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.ingest-textarea {
  width: 100%;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  padding: 10px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px;
  resize: vertical;
}
.ingest-actions {
  display: flex;
  gap: 10px;
}
.ingest-result {
  margin: 12px 0 0;
  padding: 10px 12px;
  border-radius: 8px;
  background: #e6f7ec;
  color: #1a7f37;
  font-size: 13px;
  white-space: pre-wrap;
}
.ingest-result.bad {
  background: #fdecea;
  color: #c0392b;
}
</style>
