"""风险汇总接口：同页查看病害风险与桥梁限载提示，并核对三处桩号是否一致。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.services.risk import risk_service

router = APIRouter(prefix="/api/risk", tags=["风险汇总"])


@router.get("/summary")
def risk_summary() -> dict:
    """风险卡片 + 待办列表 + 病害台账/巡查清单/限载待办的桩号一致性核对。"""
    return risk_service.summary()


@router.get("/todos")
def list_todos(
    todo_type: str | None = Query(default=None, description="路面病害风险 / 桥梁限载提示"),
    status: str | None = Query(default=None, description="待处置 / 处置中 / 已处置 / 已闭环"),
) -> dict:
    """按类型与处置状态过滤风险待办；全部展示现行里程基线裁决后的桩号。"""
    items = risk_service.todos(todo_type=todo_type, status=status)
    return {"items": items, "total": len(items)}
