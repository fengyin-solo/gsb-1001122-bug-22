"""养护材料接口：维护养护材料，覆盖领用材料、送检材料、退还不合格等动作。

另承担「路面病害外部材料接入」：外部材料在 :mod:`app.services.ingest` 中完成解析、
桩号裁决、空间投影与跨模块回写，材料指纹保证幂等，断线复位只处理未完成行。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, IngestRowsPayload, PageResult
from app.services.ingest import compute_fingerprint, ingest_service
from app.services.material import MaterialService

router = APIRouter(prefix="/api/material", tags=["养护材料"])

service = MaterialService()

LIST_FIELDS = ["材料编号", "材料名称", "材料类别", "规格型号", "供应商", "进场日期", "存放地点", "材料状态"]
STATUSES = ["在库", "已领用", "待检测", "不合格"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按材料编号检索"),
    status: str | None = Query(default=None, description="在库、已领用、待检测、不合格"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按材料编号与状态过滤养护材料列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# ---- 外部材料接入：固定路径必须声明在 /{entry_id} 之前，避免被动态路径遮蔽 --------


@router.get("/ingest/batches")
def list_ingest_batches() -> dict[str, Any]:
    """外部材料接入批次清单：含每行状态，供断线复位后定位未完成批次。"""
    batches = []
    for run in ingest_service.list_batches():
        batches.append({**run, "明细": ingest_service.batch_records(str(run["批次编号"]))})
    return {"items": batches, "total": len(batches)}


@router.get("/ingest/batches/{batch_id}")
def get_ingest_batch(batch_id: str) -> dict[str, Any]:
    """读取单个接入批次与其逐行处理结论。"""
    run = ingest_service.get_batch(batch_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"接入批次 {batch_id} 不存在")
    return {**run, "明细": ingest_service.batch_records(batch_id)}


@router.post("/ingest/resume/{batch_id}")
def resume_ingest(batch_id: str) -> dict[str, Any]:
    """连接断开复位后续传：只处理仍为「待处理/失败」的未完成行。"""
    result, error = ingest_service.resume(batch_id)
    if error:
        raise HTTPException(status_code=404, detail=error)
    return result


@router.post("/ingest/fingerprint")
def preview_fingerprint(payload: IngestRowsPayload) -> dict[str, str]:
    """只计算材料指纹，不入库：供客户端在提交前做内容比对。"""
    return {"材料指纹": compute_fingerprint(payload.rows)}


@router.post("/ingest")
def ingest_external_material(payload: IngestRowsPayload) -> dict[str, Any]:
    """接入路面病害外部材料：解析→桩号裁决→空间投影→病害台账/巡查清单/限载待办回写。

    每行材料的全部写入在同一事务内完成，失败整行回滚；材料指纹已生效时整批判重，
    同一批次号重复提交只处理未完成行。
    """
    result, error = ingest_service.submit(
        payload.rows,
        batch_id=payload.batch_id,
        source=payload.source,
        material_fingerprint=payload.material_fingerprint,
    )
    if error:
        raise HTTPException(status_code=400, detail=error)
    return result


# ---- 养护材料台账 ----------------------------------------------------------------


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条养护材料明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"养护材料 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条养护材料，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="养护材料已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条养护材料执行领用材料、送检材料、退还不合格；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出养护材料清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "material", "total": total, "items": items}
