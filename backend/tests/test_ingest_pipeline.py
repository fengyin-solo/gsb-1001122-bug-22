"""材料接入与投影处理线测试。

覆盖需求逐条：
1. 桩号裁决/投影落库/跨模块回写同一事务边界，任一失败整批回滚；
2. 冲突桩号以现行里程基线为准，历史采集坐标保留，存量位置按采集顺序迁移回填；
3. 材料指纹幂等，并发接入只生效一次，断线复位只处理未完成行；
4. 入库结论在病害台账、巡查清单、风险汇总（含桥梁限载提示）三处桩号一致。
"""
from __future__ import annotations

import threading
import unittest
from unittest import mock

from app.alignment import (
    CURRENT_BASELINE,
    adjudicate,
    migrate_legacy_positions,
    parse_station,
)
from app.services.ingest import ingest_service
from app.services.pavement import PavementService
from app.services.risk import risk_service
from app.store import store


def disease_row(code: str, **overrides) -> dict:
    row = {
        "病害编号": code,
        "所属路段": "G104国道杭州段",
        "病害类型": "龟裂",
        "严重程度": "重",
        "桩号": "K3+300",
        "行向": "上行",
        "里程基线版本": "旧线-2018",
        "面积": "3.2㎡",
        "发现日期": "2026-09-28",
    }
    row.update(overrides)
    return row


class IngestPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        migrate_legacy_positions(store)

    # ---- 存量迁移 ---------------------------------------------------------

    def test_legacy_positions_migrated_to_current_baseline_keeping_raw(self) -> None:
        first = store.find("pavement", 1)
        # 旧基线 K1+200 加 200m 偏移 → 现行 K1+400，投影同步换算。
        self.assertEqual(first["现行桩号"], "G104-K1+400.000（上行）")
        self.assertEqual(first["投影坐标"], "(1400.0, 3.75)")
        self.assertEqual(first["里程基线版本"], CURRENT_BASELINE)
        # 历史采集坐标/桩号原样保留，没有被覆盖。
        self.assertEqual(first["采集桩号"], "K1+200")
        self.assertEqual(first["历史桩号"], "K1+200")
        self.assertEqual(first["采集坐标x"], 1200.0)
        self.assertEqual(first["裁决说明"], "旧基线换算")

    def test_unparseable_legacy_position_filled_in_collection_order(self) -> None:
        second = store.find("pavement", 2)
        # 「桩号待核」解析不出，沿下行按采集顺序补位到 K0+100。
        self.assertEqual(second["现行桩号"], "G104-K0+100.000（下行）")
        self.assertEqual(second["裁决说明"], "采集顺序补位")
        self.assertEqual(second["采集桩号"], "桩号待核")

    def test_patrol_inherits_disease_verdict(self) -> None:
        patrol = store.find("patrol", 1)
        disease = store.find("pavement", 1)
        self.assertEqual(patrol["现行桩号"], disease["现行桩号"])
        self.assertEqual(patrol["投影坐标"], disease["投影坐标"])
        self.assertEqual(patrol["裁决说明"], "继承关联病害")

    def test_migration_is_idempotent(self) -> None:
        before = [dict(row) for row in store.rows("pavement")]
        migrate_legacy_positions(store)
        migrate_legacy_positions(store)
        after = store.rows("pavement")
        self.assertEqual(before, after)

    # ---- 接入 + 三处回写 --------------------------------------------------

    def test_ingest_writes_same_verdict_to_three_places(self) -> None:
        result, error = ingest_service.submit([disease_row("PAVE-N1")], batch_id="B1")
        self.assertIsNone(error)
        self.assertEqual(result["状态"], "已完成")

        disease = next(row for row in store.rows("pavement") if row["病害编号"] == "PAVE-N1")
        patrol = next(row for row in store.rows("patrol") if row["关联病害编号"] == "PAVE-N1")
        todo = next(
            row for row in store.rows("risk_todo")
            if row["关联编号"] == "PAVE-N1" and row["待办类型"] == "路面病害风险"
        )
        alert = next(
            row for row in store.rows("risk_todo")
            if row["关联编号"] == "PAVE-N1" and row["待办类型"] == "桥梁限载提示"
        )
        for row in (patrol, todo, alert):
            self.assertEqual(row["现行桩号"], disease["现行桩号"])
            self.assertEqual(row["投影坐标"], disease["投影坐标"])
        # 旧线 K3+300 + 200m = 现行 K3+500，落在限载桥 K3+400 的 500m 阈值内。
        self.assertEqual(disease["现行桩号"], "G104-K3+500.000（上行）")
        self.assertEqual(alert["桥梁名称"], "G104-限载桥（K3+400）")
        self.assertEqual(risk_service.consistency()["一致"], True)

    def test_current_baseline_conflict_wins(self) -> None:
        # 旧基线桩号接入时按现行基线换算（+200m），原始采集桩号仍保留。
        ingest_service.submit([disease_row("PAVE-N2", 桩号="K9+000", 里程基线版本="旧线-2018")], batch_id="B2")
        disease = next(row for row in store.rows("pavement") if row["病害编号"] == "PAVE-N2")
        self.assertEqual(disease["现行桩号"], "G104-K9+200.000（上行）")
        self.assertEqual(disease["采集桩号"], "K9+000")
        self.assertEqual(disease["历史桩号"], "K9+000")

    def test_adjudicator_prefers_current_baseline_on_station_conflict(self) -> None:
        # 同一位置两份上报：旧基线 K3+000（换算后 K3+200）与现行 K4+900 冲突，
        # 即使旧基线候选采集更晚，也必须以现行里程基线为准。
        verdict = adjudicate("G104", "上行", [
            {"桩号": "K3+000", "里程基线版本": "旧线-2018", "采集序号": 99},
            {"桩号": "K4+900", "里程基线版本": CURRENT_BASELINE, "采集序号": 1},
        ])
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["现行里程"], parse_station("K4+900"))
        self.assertFalse(verdict["是否换算"])
        self.assertEqual(verdict["来源版本"], CURRENT_BASELINE)

    # ---- 事务边界：整批回滚 ----------------------------------------------

    def test_writeback_failure_rolls_back_all_three_tables(self) -> None:
        before = {
            name: [dict(row) for row in store.rows(name)]
            for name in ("pavement", "patrol", "risk_todo")
        }
        with mock.patch.object(
            ingest_service, "_upsert_risk_todos", side_effect=RuntimeError("限载待办写入失败")
        ):
            result, error = ingest_service.submit([disease_row("PAVE-ROLL")], batch_id="BROLL")
        self.assertIsNone(error)
        self.assertEqual(result["明细"][0]["状态"], "失败")
        for name in ("pavement", "patrol", "risk_todo"):
            self.assertEqual(
                [dict(row) for row in store.rows(name)],
                before[name],
                f"{name} 出现半批残留",
            )
        # 失败明细不参与业务回滚：必须在事务外稳定保留「失败」与原因，供续传定位。
        records = ingest_service.batch_records("BROLL")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["状态"], "失败")
        self.assertIn("限载待办写入失败", records[0]["失败原因"])

        # 续传会重跑「失败」行并成功，业务表此时才出现该病害。
        resumed, error = ingest_service.resume("BROLL")
        self.assertIsNone(error)
        self.assertEqual(resumed["状态"], "已完成")
        self.assertTrue(
            any(row["病害编号"] == "PAVE-ROLL" for row in store.rows("pavement"))
        )
        self.assertEqual(
            ingest_service.batch_records("BROLL")[0]["状态"], "成功"
        )

    # ---- 指纹幂等 + 断线续传 + 并发 --------------------------------------

    def test_duplicate_material_fingerprint_does_not_recreate(self) -> None:
        payload = [disease_row("PAVE-DUP")]
        first, _ = ingest_service.submit(payload, batch_id="BD1")
        second, _ = ingest_service.submit(payload, batch_id="BD2")
        self.assertFalse(first["判重"])
        self.assertTrue(second["判重"])
        self.assertEqual(
            len([row for row in store.rows("pavement") if row["病害编号"] == "PAVE-DUP"]),
            1,
        )

    def test_resume_only_processes_unfinished_rows(self) -> None:
        payload = [
            {"病害编号": "BAD", "所属路段": "未知道路", "病害类型": "坑槽", "桩号": "K1+000", "行向": "上行"},
            disease_row("PAVE-OK"),
        ]
        first, _ = ingest_service.submit(payload, batch_id="BMIX")
        self.assertEqual(first["成功数"], 1)
        self.assertEqual(first["失败数"], 1)
        # 已成功行不允许重复建档。
        self.assertEqual(
            len([row for row in store.rows("pavement") if row["病害编号"] == "PAVE-OK"]),
            1,
        )
        # 修正失败行后续传：原先成功行保持不动，修正行补跑成功。
        records = ingest_service.batch_records("BMIX")
        records[0]["原始内容"] = __import__("json").dumps(
            disease_row("PAVE-FIXED"), ensure_ascii=False, sort_keys=True
        )
        resumed, _ = ingest_service.resume("BMIX")
        self.assertEqual(resumed["成功数"], 2)
        self.assertEqual(resumed["失败数"], 0)
        self.assertFalse(
            any(row["病害编号"] == "BAD" for row in store.rows("pavement"))
        )

    def test_concurrent_ingest_with_same_fingerprint_applies_once(self) -> None:
        payload = disease_row("PAVE-THR")
        outcomes: list[bool] = []

        def worker() -> None:
            result, _ = ingest_service.submit([dict(payload)], batch_id=None)
            outcomes.append(bool(result.get("判重")))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        created = len([
            row for row in store.rows("pavement") if row["病害编号"] == "PAVE-THR"
        ])
        self.assertEqual(created, 1)
        self.assertEqual(sum(outcomes), 9)

    # ---- 修复结论跨模块回写 ----------------------------------------------

    def test_repair_conclusion_writeback_is_transactional(self) -> None:
        ingest_service.submit([disease_row("PAVE-REP")], batch_id="BREP")
        disease = next(row for row in store.rows("pavement") if row["病害编号"] == "PAVE-REP")

        entry, message = PavementService().run_action(int(disease["id"]), "标记修复")
        self.assertEqual(entry["status"], "已修复")
        patrol = next(row for row in store.rows("patrol") if row["关联病害编号"] == "PAVE-REP")
        todo = next(
            row for row in store.rows("risk_todo")
            if row["关联编号"] == "PAVE-REP" and row["待办类型"] == "路面病害风险"
        )
        self.assertEqual(patrol["status"], "已完成")
        self.assertEqual(patrol["处置措施"], "病害已修复")
        self.assertEqual(todo["status"], "已处置")
        self.assertIn("同事务回写", message)
        self.assertEqual(risk_service.consistency()["一致"], True)

    def test_repair_conclusion_rolls_back_on_writeback_error(self) -> None:
        ingest_service.submit([disease_row("PAVE-REP2")], batch_id="BREP2")
        disease = next(row for row in store.rows("pavement") if row["病害编号"] == "PAVE-REP2")
        original_status = disease["status"]

        real_rows = store.rows
        def rows_with_failure(module: str):
            table = real_rows(module)
            if module == "risk_todo":
                class Failing(list):
                    def __iter__(self_inner):
                        raise RuntimeError("风险汇总写入失败")
                return Failing(table)
            return table

        with mock.patch.object(store, "rows", side_effect=rows_with_failure):
            with self.assertRaises(RuntimeError):
                PavementService().run_action(int(disease["id"]), "标记修复")

        self.assertEqual(store.find("pavement", int(disease["id"]))["status"], original_status)
        patrol = next(row for row in store.rows("patrol") if row["关联病害编号"] == "PAVE-REP2")
        self.assertNotEqual(patrol["status"], "已完成")


if __name__ == "__main__":
    unittest.main()
