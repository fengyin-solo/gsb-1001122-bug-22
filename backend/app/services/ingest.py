"""路面病害外部材料接入与投影处理线。

一条外部材料从接入到三处可见，完整步骤是：

    解析材料 → 桩号裁决（现行里程基线）→ 空间投影 → 病害台账落库
            → 巡查清单回写 → 风险汇总/桥梁限载待办回写

约束（与缺陷一一对应）：

- 上述步骤对**每一行材料**都在同一个 ``store.transaction()`` 边界内完成，任一写入失败
  该行整批回滚，不会留下半条病害；
- 材料指纹（SHA-256）在事务内登记，全局只生效一次；并发接入由仓库锁串行化，重复指纹
  直接判重，断线再次提交不会重复建档；
- 批次与每行明细持久化，连接断开复位后只处理仍是「待处理」的行，已成功/已判重的行
  不会重复执行；
- 病害台账、巡查清单、限载待办三处写入使用同一份裁决结论（现行桩号 + 投影坐标），
  根因上消除「列表、详情、限载提示桩号对不上」。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app import alignment
from app.alignment import (
    BRIDGE_NEARBY_METERS,
    CURRENT_BASELINE,
)
from app.store import store

REQUIRED_ROW_FIELDS = ["病害编号", "所属路段", "病害类型", "桩号", "行向"]

# 桥梁中心桩号：用于判断病害是否落在限载桥梁附近（同道路同方向内比较）。
BRIDGE_STATIONS: dict[str, dict[str, float]] = {
    "G104": {"上行": 3_400.0, "下行": 3_400.0},
}


def compute_fingerprint(payload: dict[str, Any] | list[Any] | str) -> str:
    """对材料内容做稳定哈希：键排序 + 紧凑分隔，避免空白/键序差异绕过幂等。"""
    if isinstance(payload, str):
        normalized = payload.strip()
    else:
        normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _row_fingerprint(batch_id: str, row: dict[str, Any], index: int) -> str:
    """行级指纹：材料行内容 + 批次标识，既支持全材料判重，也支持断线续传单行跳过。"""
    material_id = str(row.get("外部材料编号") or row.get("病害编号") or index)
    return compute_fingerprint({"批次": batch_id, "材料行": material_id, "内容": row})


class IngestService:
    # ---- 查询 -------------------------------------------------------------

    def list_batches(self) -> list[dict[str, Any]]:
        return list(reversed(store.rows("ingest_run")))

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        for run in store.rows("ingest_run"):
            if run.get("批次编号") == batch_id:
                return run
        return None

    def batch_records(self, batch_id: str) -> list[dict[str, Any]]:
        return [row for row in store.rows("ingest_record") if row.get("批次编号") == batch_id]

    def _refresh_run(self, run: dict[str, Any]) -> None:
        records = self.batch_records(str(run["批次编号"]))
        run["总行数"] = len(records)
        run["成功数"] = sum(1 for row in records if row["状态"] == "成功")
        run["判重数"] = sum(1 for row in records if row["状态"] == "判重跳过")
        run["失败数"] = sum(1 for row in records if row["状态"] == "失败")
        if records and run["失败数"] == 0:
            run["状态"] = "已完成"
        elif run["成功数"] or run["判重数"]:
            run["状态"] = "部分完成"
        else:
            run["状态"] = "待处理"

    # ---- 提交与续传 -------------------------------------------------------

    def submit(
        self,
        rows: list[dict[str, Any]],
        *,
        batch_id: str | None = None,
        source: str = "外部材料",
        material_fingerprint: str | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """接入一批外部材料。

        同批次编号重复提交即为断线复位后的续传：只处理明细中尚未完成的行；
        提供材料指纹且已成功接入过时，整批直接判重返回，不会重复建档。
        """
        if not isinstance(rows, list) or not rows:
            return {}, "材料内容为空：至少提交一行路面病害材料"

        fingerprint = material_fingerprint or compute_fingerprint(rows)

        # 批次登记 + 材料指纹占位放在同一串行临界区：并发接入同一份材料时，
        # 只有一个请求能完成占位并建档，其余整批判重，保证「材料指纹只生效一次」。
        with store.locked():
            if store.has_fingerprint(fingerprint):
                existing = next(
                    (run for run in self.list_batches() if run.get("材料指纹") == fingerprint),
                    None,
                )
                return {
                    "判重": True,
                    "批次编号": existing.get("批次编号") if existing else None,
                    "材料指纹": fingerprint,
                    "message": "该材料已接入生效，按材料指纹判重，未重复建档",
                }, None

            run = self.get_batch(batch_id) if batch_id else None
            claimed = False
            if run is None:
                run = {
                    "id": store.next_id("ingest_run"),
                    "批次编号": batch_id or f"BATCH-{store.next_id('ingest_run'):04d}",
                    "来源": source,
                    "材料指纹": fingerprint,
                    "状态": "待处理",
                    "总行数": len(rows),
                    "成功数": 0,
                    "判重数": 0,
                    "失败数": 0,
                }
                store.rows("ingest_run").append(run)
                for index, raw_row in enumerate(rows):
                    if not isinstance(raw_row, dict):
                        raw_row = {"材料编号": raw_row}
                    record = {
                        "id": store.next_id("ingest_record"),
                        "批次编号": run["批次编号"],
                        "采集序号": index + 1,
                        "外部材料编号": str(raw_row.get("外部材料编号") or raw_row.get("病害编号") or index + 1),
                        "原始内容": json.dumps(raw_row, ensure_ascii=False, sort_keys=True),
                        "行指纹": _row_fingerprint(run["批次编号"], raw_row, index),
                        "状态": "待处理",
                        "失败原因": "",
                        "病害台账id": None,
                    }
                    store.rows("ingest_record").append(record)
                # 占位表示「该材料正在接入」：整批失败时在 _process 末尾释放，允许修正重交。
                store.commit_fingerprint(fingerprint)
                claimed = True

        try:
            result, error = self._process(run, fingerprint)
        except Exception:
            if claimed:
                store.release_fingerprint(fingerprint)
            raise
        if error and claimed:
            store.release_fingerprint(fingerprint)
        # 整批没有任何一行成功时，释放占位，客户端可以带同一批次号/新批次号修正后重交。
        if not error and result.get("成功数", 0) == 0 and result.get("判重数", 0) == 0 and claimed:
            store.release_fingerprint(fingerprint)
        return result, error

    def resume(self, batch_id: str) -> tuple[dict[str, Any], str | None]:
        """断线复位后显式续传：只跑未完成行。"""
        run = self.get_batch(batch_id)
        if run is None:
            return {}, f"批次 {batch_id} 不存在"
        return self._process(run, str(run.get("材料指纹") or ""))

    def _process(self, run: dict[str, Any], material_fingerprint: str) -> tuple[dict[str, Any], str | None]:
        # 整个批次的已成功行指纹也要拦截，保证「同批次重复行」与「跨批次同材料」都只生效一次。
        for record in self.batch_records(str(run["批次编号"])):
            if record["状态"] in ("成功", "判重跳过") and record.get("行指纹"):
                store.commit_fingerprint(str(record["行指纹"]))

        processed: list[dict[str, Any]] = []
        for record in self.batch_records(str(run["批次编号"])):
            # 连接断开复位后只处理未完成行：成功/判重的行跳过，待处理/失败行允许重跑。
            if record["状态"] in ("成功", "判重跳过"):
                continue
            processed.append(self._process_record(run, record, material_fingerprint))

        self._refresh_run(run)
        return {
            "判重": False,
            "批次编号": run["批次编号"],
            "材料指纹": run["材料指纹"],
            "状态": run["状态"],
            "总行数": run["总行数"],
            "成功数": run["成功数"],
            "判重数": run["判重数"],
            "失败数": run["失败数"],
            "明细": processed,
        }, None

    # ---- 单行处理：解析 → 裁决 → 投影 → 三处回写（同一事务） --------------

    def _process_record(
        self,
        run: dict[str, Any],
        record: dict[str, Any],
        material_fingerprint: str,
    ) -> dict[str, Any]:
        try:
            raw_row = json.loads(str(record["原始内容"]))
        except (ValueError, TypeError):
            record["状态"] = "失败"
            record["失败原因"] = "材料解析失败：原始内容不是合法结构"
            return dict(record)

        missing = [field for field in REQUIRED_ROW_FIELDS if not str(raw_row.get(field) or "").strip()]
        if missing:
            record["状态"] = "失败"
            record["失败原因"] = f"缺少必填字段：{'、'.join(missing)}"
            return dict(record)

        road = alignment.road_code(raw_row.get("所属路段"))
        if road is None:
            record["状态"] = "失败"
            record["失败原因"] = f"所属路段「{raw_row.get('所属路段')}」无法识别道路编码，无法裁决桩号"
            return dict(record)

        row_fingerprint = str(record["行指纹"])

        # 桩号裁决 + 投影在事务外先做纯计算（不产生副作用）；事务内只做落库与回写，
        # 这样裁决失败不会污染任何表，落库失败则三处回写一起回滚。
        direction = alignment.normalize_direction(raw_row.get("行向"))
        verdict = alignment.adjudicate(road, direction, [{
            "桩号": raw_row.get("桩号"),
            "里程基线版本": raw_row.get("里程基线版本"),
            "采集序号": record["采集序号"],
        }])
        if verdict is None:
            record["状态"] = "失败"
            record["失败原因"] = f"桩号「{raw_row.get('桩号')}」无法解析，未纳入现行里程基线"
            return dict(record)

        # 失败原因必须在事务外落明细：事务回滚会把整组表换成快照对象，事务里对明细行
        # 的改动会一起撤销，因此事务结束后要按 id 重新取回「活动」记录再落失败结论，
        # 否则接口返回「失败」，批次查询里该行仍停在「待处理」，断线续传会丢原因。
        failure_reason: str | None = None
        success_payload: dict[str, Any] | None = None
        try:
            with store.transaction():
                # 行级指纹幂等：并发接入或断线重提同一材料行，只生效一次。
                if not store.commit_fingerprint(row_fingerprint):
                    record["状态"] = "判重跳过"
                    record["失败原因"] = ""
                    return dict(record)

                disease_id = self._upsert_pavement(raw_row, road, direction, verdict, row_fingerprint)
                patrol_id = self._upsert_patrol(raw_row, road, direction, verdict, disease_id)
                todos = self._upsert_risk_todos(raw_row, road, direction, verdict, disease_id, patrol_id)

                # 整份材料指纹在三处回写全部成功后才随事务生效；任一异常都会回滚，
                # 指纹与业务表改动一并撤销，复位后可安全重跑该行。
                store.commit_fingerprint(material_fingerprint)
                success_payload = {
                    "状态": "成功",
                    "失败原因": "",
                    "病害台账id": disease_id,
                    "现行桩号": verdict["现行桩号"],
                    "投影坐标": verdict["投影坐标"]["文本"],
                    "限载待办数": len(todos),
                }
        except Exception as exc:  # noqa: BLE001 - 单行失败要落明细并继续处理后续行
            failure_reason = f"落库/投影/回写失败，已整批回滚：{exc}"

        active_record = self._find_record(int(record["id"]))
        if active_record is None:
            return dict(record)
        if failure_reason is not None:
            active_record["状态"] = "失败"
            active_record["失败原因"] = failure_reason
            return dict(active_record)
        if success_payload is not None:
            active_record.update(success_payload)
        return dict(active_record)

    def _find_record(self, record_id: int) -> dict[str, Any] | None:
        return next(
            (row for row in store.rows("ingest_record") if int(row.get("id", 0)) == record_id),
            None,
        )

    # ---- 三处回写 ---------------------------------------------------------

    def _upsert_pavement(
        self,
        raw_row: dict[str, Any],
        road: str,
        direction: str,
        verdict: dict[str, Any],
        row_fingerprint: str,
    ) -> int:
        rows = store.rows("pavement")
        existing = next(
            (row for row in rows if str(row.get("病害编号")) == str(raw_row["病害编号"])),
            None,
        )
        base = {
            "所属路段": str(raw_row["所属路段"]),
            "病害类型": str(raw_row["病害类型"]),
            "严重程度": str(raw_row.get("严重程度") or "一般"),
            "面积": str(raw_row.get("面积") or "待量测"),
            "发现日期": str(raw_row.get("发现日期") or "2026-10-01"),
            "病害状态": "待修复",
        }
        position = {
            "采集桩号": str(raw_row.get("桩号")),
            "历史桩号": verdict["来源桩号"] if verdict["是否换算"] else "",
            "起止桩号": verdict["现行桩号"],
            "现行桩号": verdict["现行桩号"],
            "投影坐标": verdict["投影坐标"]["文本"],
            "投影坐标x": verdict["投影坐标"]["x"],
            "投影坐标y": verdict["投影坐标"]["y"],
            "行向": direction,
            "道路编码": road,
            "里程基线版本": CURRENT_BASELINE,
            "上报基线版本": verdict["来源版本"],
            "裁决说明": "旧基线换算" if verdict["是否换算"] else "现行基线",
            "材料指纹": row_fingerprint,
            "已迁移": True,
        }
        if existing is None:
            entry_id = store.next_id("pavement")
            entry = {
                "id": entry_id,
                "病害编号": str(raw_row["病害编号"]),
                "status": "待修复",
                "pending": True,
                "abnormal": True,
                **base,
                **position,
            }
            rows.append(entry)
            return entry_id
        existing.update(base)
        existing.update(position)
        return int(existing["id"])

    def _upsert_patrol(
        self,
        raw_row: dict[str, Any],
        road: str,
        direction: str,
        verdict: dict[str, Any],
        disease_id: int,
    ) -> int:
        rows = store.rows("patrol")
        disease_code = str(raw_row["病害编号"])
        existing = next(
            (row for row in rows if str(row.get("关联病害编号")) == disease_code),
            None,
        )
        finding = f"接入材料发现{raw_row['病害类型']}：{disease_code} @ {verdict['现行桩号']}"
        payload = {
            "巡查路段": str(raw_row["所属路段"]),
            "巡查日期": str(raw_row.get("发现日期") or "2026-10-01"),
            "巡查人员": str(raw_row.get("采集人员") or "外部材料接入"),
            "巡查车辆": str(raw_row.get("采集车辆") or "采集终端"),
            "发现问题": finding,
            "处置措施": "待修复",
            "巡查状态": "待巡查",
            "关联病害编号": disease_code,
            "关联病害id": disease_id,
            "采集桩号": str(raw_row.get("桩号")),
            "历史桩号": verdict["来源桩号"] if verdict["是否换算"] else "",
            "起止桩号": verdict["现行桩号"],
            "现行桩号": verdict["现行桩号"],
            "投影坐标": verdict["投影坐标"]["文本"],
            "投影坐标x": verdict["投影坐标"]["x"],
            "投影坐标y": verdict["投影坐标"]["y"],
            "行向": direction,
            "道路编码": road,
            "里程基线版本": CURRENT_BASELINE,
            "裁决说明": "继承关联病害" if verdict["是否换算"] else "现行基线",
            "已迁移": True,
        }
        if existing is None:
            entry_id = store.next_id("patrol")
            entry = {
                "id": entry_id,
                "巡查编号": f"PATR-AUTO-{entry_id:04d}",
                "status": "待巡查",
                "pending": True,
                "abnormal": True,
                **payload,
            }
            rows.append(entry)
            return entry_id
        existing.update(payload)
        return int(existing["id"])

    def _upsert_risk_todos(
        self,
        raw_row: dict[str, Any],
        road: str,
        direction: str,
        verdict: dict[str, Any],
        disease_id: int,
        patrol_id: int,
    ) -> list[int]:
        todos: list[int] = []
        disease_code = str(raw_row["病害编号"])

        # 1) 病害风险待办：风险汇总页按同一现行桩号展示。
        todos.append(self._upsert_todo(
            key=f"病害:{disease_code}",
            payload={
                "待办类型": "路面病害风险",
                "关联编号": disease_code,
                "所属路段": str(raw_row["所属路段"]),
                "提示内容": f"{raw_row['病害类型']}（{raw_row.get('严重程度') or '一般'}）需修复",
                "严重程度": str(raw_row.get("严重程度") or "一般"),
                "关联病害id": disease_id,
                "关联巡查id": patrol_id,
            },
            verdict=verdict,
            road=road,
            direction=direction,
        ))

        # 2) 桥梁限载待办：现行桩号落在限载桥梁阈值内才生成，桩号直接取裁决结论，
        #    与病害列表/详情天然一致，不会再出现限载提示另一个桩号。
        bridge_meters = BRIDGE_STATIONS.get(road, {}).get(direction)
        if bridge_meters is not None and abs(verdict["现行里程"] - bridge_meters) <= BRIDGE_NEARBY_METERS:
            todos.append(self._upsert_todo(
                key=f"限载:{disease_code}",
                payload={
                    "待办类型": "桥梁限载提示",
                    "关联编号": disease_code,
                    "所属路段": str(raw_row["所属路段"]),
                    "桥梁名称": f"{road}-限载桥（K3+400）",
                    "提示内容": "病害位于限载桥梁 500m 范围，重型车辆绕行，限载 13t",
                    "严重程度": "高",
                    "限载吨位": 13,
                    "关联病害id": disease_id,
                    "关联巡查id": patrol_id,
                },
                verdict=verdict,
                road=road,
                direction=direction,
            ))
        return todos

    def _upsert_todo(
        self,
        *,
        key: str,
        payload: dict[str, Any],
        verdict: dict[str, Any],
        road: str,
        direction: str,
    ) -> int:
        rows = store.rows("risk_todo")
        existing = next((row for row in rows if row.get("待办键") == key), None)
        position = {
            "采集桩号": verdict["来源桩号"],
            "历史桩号": verdict["来源桩号"] if verdict["是否换算"] else "",
            "起止桩号": verdict["现行桩号"],
            "现行桩号": verdict["现行桩号"],
            "投影坐标": verdict["投影坐标"]["文本"],
            "投影坐标x": verdict["投影坐标"]["x"],
            "投影坐标y": verdict["投影坐标"]["y"],
            "行向": direction,
            "道路编码": road,
            "里程基线版本": CURRENT_BASELINE,
        }
        if existing is None:
            entry_id = store.next_id("risk_todo")
            rows.append({
                "id": entry_id,
                "待办键": key,
                "status": "待处置",
                "pending": True,
                "abnormal": True,
                **payload,
                **position,
            })
            return entry_id
        existing.update(payload)
        existing.update(position)
        return int(existing["id"])


ingest_service = IngestService()
