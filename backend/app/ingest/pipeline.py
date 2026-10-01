"""材料接入与投影处理线（核心管线）。

一批外部路面病害材料的处理顺序固定为：

    解析  →  桩号裁决（现行里程基线）  →  空间投影（唯一映射）
          →  病害台账 / 巡查清单 / 桥梁限载待办 三模块同事务回写
          →  材料指纹登记（幂等）

事务边界：

* 裁决、投影、三模块落库、指纹登记在**同一个 store 事务**里提交；
* 任一环节（任一模块写入）抛错，整批回滚，不会留下半批病害，指纹也不会误登记；
* 指纹在写事务内二次校验，并发接入同一材料只有一个请求能生效；
* 批次分两行落库状态：先持久化批次（可断线恢复），业务写入成功才置 completed；
  断线复位后带原 batch_id 重传，只处理未完成行。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.ingest import fingerprint as fp
from app.ingest.baseline import adjudicate
from app.ingest.projection import as_row_fields, project
from app.store import store

BATCH_MODULE = "ingest_batch"
PAVEMENT_MODULE = "pavement"
PATROL_MODULE = "patrol"
LIMIT_TODO_MODULE = "limit_todo"

REQUIRED_FIELDS = ("材料编号", "所属路段", "桩号", "病害类型")
# 达到该严重程度且关联桥梁时，生成桥梁限载待办（限载提示必须与病害同桩号）。
LIMIT_SEVERITIES = {"严重", "较重", "危险"}


def _now() -> str:
    return date.today().isoformat()


def get_batch(batch_id: str) -> dict[str, Any] | None:
    for row in store.rows(BATCH_MODULE):
        if row.get("batch_id") == batch_id:
            return row
    return None


def _line_key(line: dict[str, Any]) -> str:
    return str(line.get("材料编号") or "").strip()


# --------------------------------------------------------------------------- #
# 阶段 1：批次建档（独立事务，先落状态，断线后可凭 batch_id 恢复）
# --------------------------------------------------------------------------- #
def register_batch(batch_id: str, source: str, materials: list[dict[str, Any]]) -> dict[str, Any]:
    with store.transaction():
        existing = get_batch(batch_id)
        if existing is not None:
            return existing
        batch = {
            "id": store.next_id(BATCH_MODULE),
            "batch_id": batch_id,
            "source": source,
            "status": "processing",
            "total": len(materials),
            "done": 0,
            "duplicated": 0,
            "failed": 0,
            "created_at": _now(),
            "lines": [
                {"材料编号": _line_key(row), "status": "pending", "message": ""}
                for row in materials
            ],
            "materials": [dict(row) for row in materials],
        }
        store.rows(BATCH_MODULE).append(batch)
        return dict(batch)


# --------------------------------------------------------------------------- #
# 阶段 2：单行预检（不落任何业务数据）
# --------------------------------------------------------------------------- #
def _prepare_line(material: dict[str, Any]) -> dict[str, Any]:
    missing = [f for f in REQUIRED_FIELDS if not str(material.get(f) or "").strip()]
    if missing:
        return {"ok": False, "stage": "parse", "message": f"缺少必填字段：{'、'.join(missing)}"}

    fingerprint = fp.fingerprint_of(material)
    if fp.seen(fingerprint):
        return {"ok": True, "duplicated": True, "fingerprint": fingerprint}

    adj = adjudicate(
        road=str(material["所属路段"]).strip(),
        direction=material.get("方向"),
        material_stake=material.get("桩号"),
    )
    if adj is None:
        return {
            "ok": False,
            "stage": "adjudicate",
            "message": f"路段「{material.get('所属路段')}」缺少现行里程基线，拒绝按材料桩号直接落库",
        }

    proj = project(adj)
    return {
        "ok": True,
        "duplicated": False,
        "fingerprint": fingerprint,
        "adjudication": adj,
        "projection": proj,
    }


# --------------------------------------------------------------------------- #
# 阶段 3：同事务写入三模块
# --------------------------------------------------------------------------- #
def _disease_code(material: dict[str, Any], disease_id: int) -> str:
    code = str(material.get("病害编号") or "").strip()
    return code or f"PAVE-{disease_id:04d}"


def _find_row(module: str, code_field: str, code: str) -> dict[str, Any] | None:
    for row in store.rows(module):
        if str(row.get(code_field) or "") == code:
            return row
    return None


def _write_line(batch_id: str, material: dict[str, Any], prepared: dict[str, Any]) -> str:
    """在调用方持有的事务内写入一行。返回 duplicated / created / updated。"""
    fingerprint = prepared["fingerprint"]
    # 写事务内二次校验指纹：挡住并发接入（两个请求同时通过只读预检的情况）。
    if fp.seen(fingerprint):
        return "duplicated"

    proj_fields = as_row_fields(prepared["projection"])
    adj = prepared["adjudication"]
    road = adj.road
    disease_id = store.next_id(PAVEMENT_MODULE)
    disease_code = _disease_code(material, disease_id)
    today = _now()
    stake = proj_fields["裁决桩号"]

    effect = "created"
    disease = _find_row(PAVEMENT_MODULE, "病害编号", disease_code)
    if disease is None:
        disease = {"id": disease_id}
        store.rows(PAVEMENT_MODULE).append(disease)
    else:
        effect = "updated"

    disease.update({
        "病害编号": disease_code,
        "所属路段": road,
        "病害类型": str(material["病害类型"]).strip(),
        "严重程度": str(material.get("严重程度") or "一般").strip(),
        "起止桩号": stake,                      # 列表/详情统一显示裁决桩号
        "面积": material.get("面积") or "—",
        "发现日期": str(material.get("发现日期") or today),
        "病害状态": "待修复",
        "status": "待修复",
        "pending": True,
        "abnormal": False,
        "材料编号": str(material["材料编号"]).strip(),
        "材料原始桩号": adj.material_stake or stake,
        "批次号": batch_id,
        **proj_fields,
    })

    # 巡查清单回写：同一病害编号挂一条巡查记录，带同一份裁决桩号与投影。
    patrol = _find_row(PATROL_MODULE, "关联病害编号", disease_code)
    if patrol is None:
        patrol = {"id": store.next_id(PATROL_MODULE)}
        store.rows(PATROL_MODULE).append(patrol)
    patrol.update({
        "巡查编号": str(patrol.get("巡查编号") or f"PATR-{patrol['id']:04d}"),
        "巡查路段": road,
        "巡查日期": str(material.get("发现日期") or today),
        "巡查人员": str(material.get("来源") or "外部材料接入"),
        "巡查车辆": "材料接入",
        "发现问题": f"{disease['病害类型']}（材料桩号 {adj.material_stake or '—'} → 现行桩号 {stake}）",
        "处置措施": str(patrol.get("处置措施") or "待修复"),
        "巡查状态": "待巡查",
        "status": "待巡查",
        "pending": True,
        "abnormal": False,
        "关联病害编号": disease_code,
        "批次号": batch_id,
        **proj_fields,
    })

    # 桥梁限载待办：严重病害且关联桥梁时生成，限载提示与病害共用同一桩号/坐标。
    bridge_name = str(material.get("关联桥梁") or "").strip()
    if bridge_name and disease["严重程度"] in LIMIT_SEVERITIES:
        todo = _find_row(LIMIT_TODO_MODULE, "病害编号", disease_code)
        if todo is None:
            todo = {"id": store.next_id(LIMIT_TODO_MODULE)}
            store.rows(LIMIT_TODO_MODULE).append(todo)
        todo.update({
            "桥梁名称": bridge_name,
            "病害编号": disease_code,
            "所属路段": road,
            "限载提示": f"{bridge_name}方向存在{disease['严重程度']}{disease['病害类型']}，请按 {stake}（{adj.direction}）核实并提示限载",
            "状态": "待处置",
            "status": "待处置",
            "pending": True,
            "abnormal": True,
            "处置结论": "",
            "批次号": batch_id,
            **proj_fields,
        })

    fp.register(fingerprint, batch_id=batch_id, material_no=str(material["材料编号"]).strip())
    return effect


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
def ingest_batch(
    materials: list[dict[str, Any]],
    *,
    batch_id: str,
    source: str = "外部材料",
) -> dict[str, Any]:
    """执行一整个批次的接入处理。返回批次结果摘要。"""
    if not materials:
        raise ValueError("材料批次为空，无需接入")

    # 阶段 1：批次建档（已完成批次直接回放结果，天然幂等）。
    batch = register_batch(batch_id, source, materials)
    if batch["status"] == "completed":
        return {"status": "completed", "idempotent": True, **{k: batch[k] for k in ("batch_id", "total", "done", "duplicated", "failed")}}

    # 复位重传：只挑未完成行；已完成/重复行不再处理。
    pending_keys = {
        line["材料编号"]
        for line in batch["lines"]
        if line["status"] in ("pending", "failed")
    }
    targets = [row for row in batch["materials"] if _line_key(row) in pending_keys]

    # 纯字段解析预检（不读指纹、不写数据），结构性错误整批拒绝。
    errors: list[dict[str, str]] = []
    for material in targets:
        key = _line_key(material)
        missing = [f for f in REQUIRED_FIELDS if not str(material.get(f) or "").strip()]
        if missing:
            errors.append({"材料编号": key, "stage": "parse",
                           "message": f"缺少必填字段：{'、'.join(missing)}"})
    if errors:
        with store.transaction():
            latest = get_batch(batch_id)
            if latest is not None:
                latest["status"] = "failed"
                by_key = {e["材料编号"]: e for e in errors}
                for line in latest["lines"]:
                    if line["材料编号"] in by_key:
                        err = by_key[line["材料编号"]]
                        line["status"] = "failed"
                        line["message"] = f"[{err['stage']}] {err['message']}"
                latest["failed"] = sum(1 for line in latest["lines"] if line["status"] == "failed")
        return {
            "status": "failed",
            "batch_id": batch_id,
            "total": batch["total"],
            "done": 0,
            "duplicated": 0,
            "failed": len(errors),
            "errors": errors,
        }

    # 桩号裁决 / 指纹判重 / 三模块落库全部放在同一个写事务里：
    # store 锁串行化并发接入，事务内指纹二次判重保证同一材料只生效一次；
    # 任一环节抛错整批回滚，不会留下半批病害，指纹也不会误登记。
    effects: dict[str, str] = {}
    duplicated_keys: set[str] = set()
    adjudicate_errors: list[dict[str, str]] = []
    try:
        with store.transaction():
            latest = get_batch(batch_id)
            assert latest is not None
            for material in targets:
                key = _line_key(material)
                prepared = _prepare_line(material)
                if not prepared["ok"]:
                    # 触发回滚：无基线等裁决错误不允许写入半批
                    raise _LineError(key, prepared["stage"], prepared["message"])
                if prepared["duplicated"]:
                    duplicated_keys.add(key)
                    continue
                effects[key] = _write_line(batch_id, material, prepared)

            for line in latest["lines"]:
                if line["材料编号"] in duplicated_keys:
                    line["status"] = "duplicated"
                    line["message"] = "材料指纹已存在，跳过建档"
                elif line["status"] in ("pending", "failed") and line["材料编号"] in effects:
                    line["status"] = "done"
                    line["message"] = "已入库并回写巡查清单/限载待办"
            latest["done"] = sum(1 for line in latest["lines"] if line["status"] == "done")
            latest["duplicated"] = sum(1 for line in latest["lines"] if line["status"] == "duplicated")
            latest["failed"] = 0
            latest["status"] = "completed"
    except _LineError as exc:
        adjudicate_errors.append({"材料编号": exc.key, "stage": exc.stage, "message": exc.message})
        with store.transaction():
            latest = get_batch(batch_id)
            if latest is not None:
                latest["status"] = "failed"
                for line in latest["lines"]:
                    if line["材料编号"] == exc.key:
                        line["status"] = "failed"
                        line["message"] = f"[{exc.stage}] {exc.message}"
                latest["failed"] = sum(1 for line in latest["lines"] if line["status"] == "failed")
        return {
            "status": "failed",
            "batch_id": batch_id,
            "total": batch["total"],
            "done": 0,
            "duplicated": 0,
            "failed": 1,
            "errors": adjudicate_errors,
        }
    except Exception as exc:  # 整批回滚：store 事务已恢复快照，这里只更新批次状态
        with store.transaction():
            latest = get_batch(batch_id)
            if latest is not None:
                latest["status"] = "failed"
                for line in latest["lines"]:
                    if line["status"] == "pending":
                        line["status"] = "failed"
                        line["message"] = f"[write] 写入失败已整批回滚：{exc}"
                latest["failed"] = sum(1 for line in latest["lines"] if line["status"] == "failed")
        raise

    return {
        "status": "completed",
        "batch_id": batch_id,
        "idempotent": False,
        "total": batch["total"],
        "done": len(effects),
        "created": sum(1 for v in effects.values() if v == "created"),
        "updated": sum(1 for v in effects.values() if v == "updated"),
        "duplicated": len(duplicated_keys),
        "failed": 0,
    }


class _LineError(Exception):
    """单行裁决失败，用来在写事务内触发整批回滚。"""

    def __init__(self, key: str, stage: str, message: str) -> None:
        super().__init__(message)
        self.key = key
        self.stage = stage


# --------------------------------------------------------------------------- #
# 修复结论回写：病害台账状态变化后，同事务回写巡查清单与桥梁限载待办
# --------------------------------------------------------------------------- #
_PATROL_CONCLUSION = {
    "修复中": ("巡查中", "已派发修复"),
    "已修复": ("已完成", "病害已标记修复，待验收"),
    "已验收": ("已复核", "病害修复已验收通过"),
}


def write_back_repair(disease: dict[str, Any], disease_status: str) -> dict[str, int]:
    """修复结论回写。调用方必须在 ``store.transaction()`` 内。

    三处写入共用病害行上的裁决桩号/投影，因此巡查清单、限载提示看到的
    桩号永远与病害详情一致；任一写入抛错由外层事务整批回滚。
    """
    code = str(disease.get("病害编号") or "")
    patrol_status, conclusion = _PATROL_CONCLUSION.get(disease_status, ("", ""))
    touched = {"patrol": 0, "limit_todo": 0}

    stake = disease.get("裁决桩号") or disease.get("起止桩号")
    proj_fields = {
        field: disease[field]
        for field in ("裁决桩号", "连续里程_m", "方向", "投影坐标X", "投影坐标Y", "坐标来源")
        if field in disease
    }

    if code:
        for patrol in store.rows(PATROL_MODULE):
            if str(patrol.get("关联病害编号") or "") != code:
                continue
            if patrol_status:
                patrol["巡查状态"] = patrol_status
                patrol["status"] = patrol_status
                patrol["pending"] = patrol_status != "已复核"
            patrol["处置措施"] = conclusion or str(patrol.get("处置措施") or "")
            patrol["修复结论"] = conclusion
            if stake:
                patrol["裁决桩号"] = stake
            patrol.update(proj_fields)
            touched["patrol"] += 1

        for todo in store.rows(LIMIT_TODO_MODULE):
            if str(todo.get("病害编号") or "") != code:
                continue
            if disease_status in ("已修复", "已验收"):
                todo["状态"] = "已解除"
                todo["status"] = "已解除"
                todo["pending"] = False
                todo["abnormal"] = False
                todo["处置结论"] = f"病害已{disease_status}，限载提示解除（桩号 {stake or '—'}）"
            else:
                todo["处置结论"] = f"病害进入{disease_status}，限载提示保持（桩号 {stake or '—'}）"
            if stake:
                todo["裁决桩号"] = stake
            todo.update(proj_fields)
            touched["limit_todo"] += 1

    return touched


def risk_summary() -> dict[str, Any]:
    """风险汇总页：以病害台账为主表，串起巡查清单与限载待办。

    三个来源的桩号都取自接入时同一份投影字段，页面不会再出现对不上的桩号。
    """
    patrol_by_code = {
        str(p.get("关联病害编号")): p for p in store.rows(PATROL_MODULE) if p.get("关联病害编号")
    }
    todos = [t for t in store.rows(LIMIT_TODO_MODULE) if t.get("病害编号")]
    todo_by_code = {str(t.get("病害编号")): t for t in todos}

    risks: list[dict[str, Any]] = []
    for disease in store.rows(PAVEMENT_MODULE):
        code = str(disease.get("病害编号") or "")
        stake = disease.get("裁决桩号") or disease.get("起止桩号")
        patrol = patrol_by_code.get(code)
        todo = todo_by_code.get(code)
        risks.append({
            "病害编号": code,
            "所属路段": disease.get("所属路段"),
            "病害类型": disease.get("病害类型"),
            "严重程度": disease.get("严重程度"),
            "病害状态": disease.get("病害状态") or disease.get("status"),
            "裁决桩号": stake,
            "连续里程_m": disease.get("连续里程_m"),
            "投影坐标": [disease.get("投影坐标X"), disease.get("投影坐标Y")],
            "坐标来源": disease.get("坐标来源"),
            "巡查编号": patrol.get("巡查编号") if patrol else None,
            "巡查状态": patrol.get("巡查状态") if patrol else None,
            "巡查桩号": (patrol.get("裁决桩号") or patrol.get("起止桩号")) if patrol else None,
            "桥梁名称": todo.get("桥梁名称") if todo else None,
            "限载待办": todo.get("限载提示") if todo else None,
            "限载状态": todo.get("状态") if todo else None,
            "限载桩号": todo.get("裁决桩号") if todo else None,
            "桩号一致": (
                (patrol is None or (patrol.get("裁决桩号") or patrol.get("起止桩号")) == stake)
                and (todo is None or todo.get("裁决桩号") == stake)
            ),
        })

    pending_todos = [t for t in todos if t.get("status") == "待处置"]
    return {
        "total_diseases": len(risks),
        "pending_limit_todos": len(pending_todos),
        "stake_conflicts": sum(1 for r in risks if not r["桩号一致"]),
        "items": risks,
    }
