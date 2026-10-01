"""风险汇总：把病害台账、巡查清单、桥梁限载待办的同一根因风险放在一页核对。

桩号统一读裁决后的「现行桩号 / 投影坐标」，并提供一致性核对结果：只要某处仍引用
采集桩号或旧桩号，就在 ``consistency`` 里暴露出来，供运营页确认材料接入线是否回写干净。
"""
from __future__ import annotations

from typing import Any

from app.alignment import CURRENT_BASELINE
from app.store import store


class RiskService:
    def todos(
        self,
        *,
        todo_type: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = store.rows("risk_todo")
        if todo_type:
            rows = [row for row in rows if row.get("待办类型") == todo_type]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        return sorted(rows, key=lambda row: (str(row.get("待办类型")), int(row.get("id", 0))))

    def bridge_alerts(self) -> list[dict[str, Any]]:
        return self.todos(todo_type="桥梁限载提示")

    def summary(self) -> dict[str, Any]:
        todos = self.todos()
        grouped: dict[str, int] = {}
        pending = 0
        for row in todos:
            grouped[str(row.get("待办类型"))] = grouped.get(str(row.get("待办类型")), 0) + 1
            if row.get("pending"):
                pending += 1
        return {
            "cards": [
                {"label": "风险待办总数", "value": len(todos)},
                {"label": "待处置", "value": pending},
                {"label": "桥梁限载提示", "value": len(self.bridge_alerts())},
            ],
            "todos": todos,
            "consistency": self.consistency(),
        }

    def consistency(self) -> dict[str, Any]:
        """核对同一病害根因在病害台账、巡查清单、限载待办三处的桩号是否一致。"""
        problems: list[dict[str, Any]] = []
        checked = 0

        for disease in store.rows("pavement"):
            code = str(disease.get("病害编号") or "")
            canonical = disease.get("现行桩号")
            if not code or not canonical:
                continue
            checked += 1
            linked_patrol = [
                row for row in store.rows("patrol")
                if str(row.get("关联病害编号")) == code
            ]
            linked_todos = [
                row for row in store.rows("risk_todo")
                if str(row.get("关联编号")) == code
            ]
            for scope, rows in (("巡查清单", linked_patrol), ("限载待办", linked_todos)):
                for row in rows:
                    if row.get("现行桩号") != canonical or row.get("投影坐标") != disease.get("投影坐标"):
                        problems.append({
                            "病害编号": code,
                            "位置": scope,
                            "记录id": row.get("id"),
                            "病害桩号": canonical,
                            "对方桩号": row.get("现行桩号"),
                        })

            # 现行基线版本必须统一为现行里程基线，历史采集坐标允许保留但不能充当展示口径。
            if disease.get("里程基线版本") != CURRENT_BASELINE:
                problems.append({
                    "病害编号": code,
                    "位置": "病害台账",
                    "记录id": disease.get("id"),
                    "病害桩号": canonical,
                    "对方桩号": disease.get("起止桩号"),
                })

        return {
            "核对病害数": checked,
            "一致": not problems,
            "问题数": len(problems),
            "问题明细": problems,
        }


risk_service = RiskService()
