"""路面病害业务规则：状态流转、字段校验与筛选口径都收在这里。

修复结论（派发/修复/验收）不只是病害台账自家的事：同一路面病害根因同时影响
路面病害列表、路面病害详情和桥梁限载提示。因此状态流转时把结论在同一事务边界内
回写到巡查清单与限载待办，任一写入失败三处一起回滚，避免出现列表已修复但限载
提示仍在催办之类的对不上。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "pavement"
REQUIRED_FIELDS = ["病害编号", "所属路段", "病害类型"]
STATUS_ORDER = ["待修复", "修复中", "已修复", "已验收"]
ACTION_RULES = {"派发修复": "修复中", "标记修复": "已修复", "验收通过": "已验收"}
NEGATIVE_ACTIONS = []

# 修复结论到巡查清单、限载待办的回写口径。
_PATROL_STATUS = {"派发修复": "巡查中", "标记修复": "已完成", "验收通过": "已复核"}
_PATROL_DISPOSAL = {"派发修复": "已派发修复", "标记修复": "病害已修复", "验收通过": "修复已验收闭环"}
_TODO_STATUS = {"派发修复": "处置中", "标记修复": "已处置", "验收通过": "已闭环"}


class PavementService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("病害编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于路面病害可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        # 桩号裁决/投影已在材料接入时落定，这里在同一事务边界内回写修复结论：
        # 病害详情、巡查清单、限载待办三处同生共死。
        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"病害记录 {entry_id} 不存在或已归档"
            code = str(entry.get("病害编号") or "")
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            entry["病害状态"] = target

            patrol_synced = 0
            for patrol in store.rows("patrol"):
                if str(patrol.get("关联病害编号")) == code:
                    patrol["status"] = _PATROL_STATUS[action]
                    patrol["巡查状态"] = _PATROL_STATUS[action]
                    patrol["处置措施"] = _PATROL_DISPOSAL[action]
                    patrol["pending"] = target == "待修复"
                    patrol_synced += 1

            todo_synced = 0
            for todo in store.rows("risk_todo"):
                if str(todo.get("关联编号")) == code:
                    todo["status"] = _TODO_STATUS[action]
                    todo["pending"] = target not in ("已修复", "已验收")
                    todo["abnormal"] = target == "待修复"
                    todo["处置结论"] = _PATROL_DISPOSAL[action]
                    todo_synced += 1

            return entry, (
                f"病害记录已{action}，修复结论已同事务回写"
                f"（巡查清单 {patrol_synced} 条、限载/风险待办 {todo_synced} 条）"
            )
