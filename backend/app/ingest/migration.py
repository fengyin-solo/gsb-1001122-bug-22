"""存量位置迁移回填。

适用场景：基线更新后，存量病害/巡查记录的桩号与现行里程基线对不上。

规则（严格区分两种记录）：

* 带采集坐标（``坐标来源 == collected``）的**历史病害**：维持此前采集坐标，
  不做重投影，只补记现行裁决桩号供展示对照；
* 没有历史坐标保护、且桩号/里程与基线不符的**存量病害**：按采集顺序
  （``发现日期`` 升序、id 次序）逐条迁移，重算桩号与投影；
* 无论哪条病害，关联的巡查清单与限载待办都**跟随病害台账同一份投影**回填，
  三个页面因此不可能再出现对不上的桩号；
* 迁移全程在同一事务内，任一回填失败整批回滚。
"""
from __future__ import annotations

from typing import Any

from app.ingest.baseline import adjudicate, parse_stake
from app.ingest.pipeline import LIMIT_TODO_MODULE, PATROL_MODULE, PAVEMENT_MODULE
from app.ingest.projection import as_row_fields, project
from app.store import store

_PROJECT_FIELDS = ("裁决桩号", "连续里程_m", "方向", "投影坐标X", "投影坐标Y", "坐标来源")
_LINK_FIELDS = {  # 跨模块关联到同一条病害的键
    PATROL_MODULE: "关联病害编号",
    LIMIT_TODO_MODULE: "病害编号",
}


def _collection_order(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: (str(r.get("发现日期") or ""), int(r.get("id", 0))))


def _disease_stake(disease: dict[str, Any]) -> str | None:
    value = disease.get("裁决桩号") or disease.get("起止桩号")
    return str(value) if value not in (None, "") else None


def _sync_linked_rows(code: str, fields: dict[str, Any], stake: str) -> list[dict[str, Any]]:
    """巡查/限载跟随病害台账的同一份投影，不允许各自再解释桩号。"""
    changes: list[dict[str, Any]] = []
    for module in (PATROL_MODULE, LIMIT_TODO_MODULE):
        link = _LINK_FIELDS[module]
        for row in store.rows(module):
            if str(row.get(link) or "") != code:
                continue
            old = row.get("裁决桩号")
            row.update(fields)
            row["裁决桩号"] = stake
            changes.append({"模块": module, "病害编号": code, "原桩号": old, "新桩号": stake})
    return changes


def migrate() -> dict[str, Any]:
    """扫描三个模块，把与现行基线不符的存量位置迁移回填。返回迁移摘要。"""
    migrated: list[dict[str, Any]] = []
    preserved = 0

    with store.transaction():
        diseases = _collection_order(store.rows(PAVEMENT_MODULE))

        for disease in diseases:
            road = str(disease.get("所属路段") or "").strip()
            direction = disease.get("方向") or "上行"
            raw_stake = _disease_stake(disease)
            if not raw_stake or parse_stake(raw_stake) is None:
                continue  # 占位文本（演示样例）没有迁移依据，跳过
            adj = adjudicate(road, direction, raw_stake)
            if adj is None:
                continue  # 该路段没有现行基线，不凭空迁移
            code = str(disease.get("病害编号") or "")

            if disease.get("坐标来源") == "collected":
                # 历史病害：坐标与桩号维持采集事实，只补记现行裁决桩号；
                # 关联行跟随病害现有投影，保证三页一致而不改动历史坐标本身。
                disease["现行裁决桩号"] = adj.stake
                fields = {f: disease.get(f) for f in _PROJECT_FIELDS if f in disease}
                fields["裁决桩号"] = disease.get("裁决桩号") or adj.stake
                preserved += 1
                migrated.extend(_sync_linked_rows(code, fields, str(fields["裁决桩号"])))
                continue

            current_mileage = disease.get("连续里程_m")
            mismatched = (
                disease.get("裁决桩号") != adj.stake
                or current_mileage is None
                or abs(float(current_mileage) - adj.mileage_m) > 0.01
            )
            if mismatched:
                proj_fields = as_row_fields(project(adj))
                old_stake = disease.get("裁决桩号") or disease.get("起止桩号")
                disease["起止桩号"] = adj.stake
                disease.update(proj_fields)
                migrated.append({"模块": PAVEMENT_MODULE, "病害编号": code,
                                 "原桩号": old_stake, "新桩号": adj.stake})
                migrated.extend(_sync_linked_rows(code, proj_fields, adj.stake))
            else:
                # 病害本身已对齐：仍要校正关联行可能存在的漂移桩号。
                fields = {f: disease[f] for f in _PROJECT_FIELDS if f in disease}
                migrated.extend(_sync_linked_rows(code, fields, adj.stake))

        # 巡查/限载里可能存在病害表已无对应行的孤儿记录，按采集顺序兜底回填。
        for module in (PATROL_MODULE, LIMIT_TODO_MODULE):
            for row in _collection_order(store.rows(module)):
                if row.get("裁决桩号") and row.get("连续里程_m") is not None:
                    continue
                road = str(row.get("所属路段") or "").strip()
                raw = row.get("裁决桩号")
                if raw in (None, "") or parse_stake(raw) is None:
                    continue
                adj = adjudicate(road, row.get("方向") or "上行", raw)
                if adj is None:
                    continue
                old = raw
                row.update(as_row_fields(project(adj)))
                migrated.append({"模块": module,
                                 "病害编号": row.get("病害编号") or row.get("关联病害编号"),
                                 "原桩号": old, "新桩号": adj.stake})

    return {
        "status": "completed",
        "migrated": len(migrated),
        "preserved_historical": preserved,
        "details": migrated,
    }
