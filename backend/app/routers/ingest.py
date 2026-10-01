"""外部材料接入与投影接口。

挂在养护材料前缀下，提供：

* ``POST /api/material/ingest``        一批路面病害材料接入（幂等、整批事务）；
* ``GET  /api/material/ingest/{id}``   查询批次进度（断线复位后只处理未完成行）；
* ``POST /api/material/migrate``       存量位置不符记录按采集顺序迁移回填；
* ``GET  /api/material/baselines``     现行里程基线（桩号裁决依据）；
* ``GET  /api/risk/summary``           风险汇总：病害台账 × 巡查清单 × 限载待办同桩号对照。
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.ingest import migration
from app.ingest.baseline import all_baselines
from app.ingest.pipeline import get_batch, ingest_batch, risk_summary

router = APIRouter(tags=["材料接入与投影"])


class IngestPayload(BaseModel):
    materials: list[dict[str, Any]] = Field(default_factory=list)
    batch_id: str | None = None
    source: str = "外部材料"


@router.get("/api/material/baselines")
def baselines() -> dict[str, Any]:
    """现行里程基线：冲突桩号一律以此裁决。"""
    return {"items": all_baselines()}


@router.post("/api/material/ingest")
def submit_materials(payload: IngestPayload) -> dict[str, Any]:
    """接入一批路面病害外部材料。

    断线重连时请带上首次返回的 ``batch_id``：已完成行与已生效指纹会被跳过，
    只处理未完成行；同一材料并发/重复提交只生效一次。
    """
    if not payload.materials:
        return {"ok": False, "message": "材料批次为空，未做任何写入"}
    batch_id = payload.batch_id or f"BATCH-{uuid.uuid4().hex[:12]}"
    try:
        result = ingest_batch(payload.materials, batch_id=batch_id, source=payload.source)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    except Exception as exc:  # 事务已回滚，不会留下半批病害
        return {"ok": False, "batch_id": batch_id, "message": f"接入失败已整批回滚：{exc}"}
    return {"ok": result.get("status") == "completed", "message": _summary_text(result), **result}


@router.get("/api/material/ingest/{batch_id}")
def batch_status(batch_id: str) -> dict[str, Any]:
    batch = get_batch(batch_id)
    if batch is None:
        return {"ok": False, "message": f"批次 {batch_id} 不存在"}
    return {
        "ok": True,
        "batch_id": batch_id,
        "status": batch["status"],
        "total": batch["total"],
        "done": batch["done"],
        "duplicated": batch["duplicated"],
        "failed": batch["failed"],
        "lines": batch["lines"],
    }


@router.post("/api/material/migrate")
def migrate_positions() -> dict[str, Any]:
    """存量位置不符记录迁移回填：历史采集坐标保留，其余按采集顺序重投影。"""
    result = migration.migrate()
    return {"ok": True, **result}


@router.get("/api/risk/summary")
def risk() -> dict[str, Any]:
    """风险汇总页：病害台账、巡查清单、桥梁限载提示的同桩号对照。"""
    return risk_summary()


def _summary_text(result: dict[str, Any]) -> str:
    if result.get("status") != "completed":
        return f"接入未完成：{result.get('failed', 0)} 行校验失败，未写入任何病害"
    if result.get("idempotent"):
        return "该批次已接入完成，指纹命中，未重复建档"
    return (
        f"接入完成：新增 {result.get('created', 0)} 条、更新 {result.get('updated', 0)} 条、"
        f"指纹去重 {result.get('duplicated', 0)} 条；结论已同事务回写巡查清单与限载待办"
    )
