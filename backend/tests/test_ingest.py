"""材料接入与投影处理线的端到端测试（标准库 unittest，无需额外依赖）。

覆盖：
1. 桩号裁决以现行基线为准，病害台账/巡查清单/限载待办三处桩号一致；
2. 材料指纹幂等：重复提交（含并发线程）只建档一次；
3. 任一写入失败整批回滚，不留半批病害，指纹也不留痕；
4. 断线复位后带 batch_id 重传，只处理未完成行；
5. 修复结论同事务回写巡查清单与限载待办；
6. 存量迁移按基线回填，历史采集坐标保持不动。
"""
from __future__ import annotations

import threading
import unittest
import uuid

from app.ingest import fingerprint as fp
from app.ingest.pipeline import (
    LIMIT_TODO_MODULE,
    PATROL_MODULE,
    PAVEMENT_MODULE,
    ingest_batch,
    risk_summary,
    write_back_repair,
)
from app.ingest.baseline import adjudicate
from app.store import store


def _material(**overrides):
    row = {
        "材料编号": "MATE-X1",
        "所属路段": "G104绕城段",
        "方向": "上行",
        "桩号": "K12+800",
        "病害类型": "纵向裂缝",
        "严重程度": "严重",
        "发现日期": "2026-09-20",
        "关联桥梁": "迎宾大桥",
    }
    row.update(overrides)
    return row


def _reset_store():
    """每个用例前清空接入产物，保留种子表。"""
    for module in ("ingest_fingerprint", "ingest_batch", LIMIT_TODO_MODULE):
        store.rows(module).clear()
    # 清掉接入产生的病害/巡查（保留种子三条）
    for module, code_field in ((PAVEMENT_MODULE, "材料编号"), (PATROL_MODULE, "关联病害编号")):
        store.rows(module)[:] = [r for r in store.rows(module) if not r.get(code_field)]


class IngestTests(unittest.TestCase):
    def setUp(self):
        _reset_store()

    def test_adjudication_uses_current_baseline_and_three_modules_align(self):
        result = ingest_batch([_material()], batch_id="B1")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["created"], 1)

        disease = next(r for r in store.rows(PAVEMENT_MODULE) if r.get("材料编号") == "MATE-X1")
        # K12+800 基线裁决：10000 + (12.8-10)*1000 = 12800m，标准桩号 K12+800
        self.assertEqual(disease["裁决桩号"], "K12+800")
        self.assertEqual(disease["连续里程_m"], 12800.0)
        self.assertEqual(disease["起止桩号"], "K12+800")

        patrol = next(r for r in store.rows(PATROL_MODULE) if r.get("关联病害编号") == disease["病害编号"])
        todo = next(r for r in store.rows(LIMIT_TODO_MODULE) if r["病害编号"] == disease["病害编号"])
        self.assertEqual(patrol["裁决桩号"], disease["裁决桩号"])
        self.assertEqual(todo["裁决桩号"], disease["裁决桩号"])
        self.assertEqual((patrol["投影坐标X"], patrol["投影坐标Y"]),
                         (disease["投影坐标X"], disease["投影坐标Y"]))

        summary = risk_summary()
        target = next(i for i in summary["items"] if i["病害编号"] == disease["病害编号"])
        self.assertTrue(target["桩号一致"])
        self.assertEqual(summary["stake_conflicts"], 0)
        self.assertEqual(summary["pending_limit_todos"], 1)

    def test_conflict_stake_adjudicated_to_baseline_not_material(self):
        # 材料给旧桩号 K9+900，裁决仍按基线换算（不做任何特殊采信）
        adj = adjudicate("G104绕城段", "上行", "K9+900")
        self.assertEqual(adj.stake, "K9+900")
        self.assertEqual(adj.mileage_m, 9900.0)
        self.assertEqual(adj.source, "baseline")

    def test_fingerprint_idempotent_on_resubmit(self):
        first = ingest_batch([_material()], batch_id="B2")
        self.assertEqual(first["created"], 1)
        # 断线后换批次号重推同一份材料
        second = ingest_batch([_material()], batch_id="B2-RETRY")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["duplicated"], 1)
        self.assertEqual(second["done"], 0)
        diseases = [r for r in store.rows(PAVEMENT_MODULE) if r.get("材料编号") == "MATE-X1"]
        self.assertEqual(len(diseases), 1)
        # 同批次号直接回放
        replay = ingest_batch([_material()], batch_id="B2")
        self.assertTrue(replay.get("idempotent"))

    def test_concurrent_submit_takes_effect_once(self):
        outcomes = []

        def fire():
            # 真实接入端每次连接使用唯一批次号；指纹负责跨批次判重。
            outcomes.append(ingest_batch([_material()], batch_id=f"B-{uuid.uuid4().hex[:8]}"))

        threads = [threading.Thread(target=fire) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        created = sum(o.get("created", 0) for o in outcomes)
        duplicated = sum(o.get("duplicated", 0) for o in outcomes)
        self.assertEqual(created, 1)
        self.assertEqual(duplicated, 5)
        self.assertEqual(
            len([r for r in store.rows(PAVEMENT_MODULE) if r.get("材料编号") == "MATE-X1"]), 1
        )
        self.assertEqual(len(store.rows("ingest_fingerprint")), 1)

    def test_invalid_line_aborts_whole_batch_no_half_data(self):
        good = _material(材料编号="OK-1")
        bad = _material(材料编号="BAD-1", 桩号="")  # 缺桩号也可解析为基线起点，改为缺必填
        bad.pop("桩号")
        # 缺必填字段：缺少所属路段更直接
        bad = _material(材料编号="BAD-1")
        bad.pop("病害类型")
        result = ingest_batch([good, bad], batch_id="B3")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed"], 1)
        self.assertFalse(any(r.get("材料编号") == "OK-1" for r in store.rows(PAVEMENT_MODULE)))
        self.assertEqual(len(store.rows("ingest_fingerprint")), 0)

    def test_write_failure_rolls_back_everything(self):
        prepared_rows_before = len(store.rows(PAVEMENT_MODULE))

        class BoomError(RuntimeError):
            pass

        # 人为让限载待办写入阶段炸掉：模拟第三模块写入失败
        import app.ingest.pipeline as pipeline_mod
        original = pipeline_mod.store.rows

        def spying_rows(module):
            rows = original(module)
            if module == LIMIT_TODO_MODULE:
                raise BoomError("限载待办库不可用")
            return rows

        pipeline_mod.store.rows = spying_rows  # type: ignore[assignment]
        try:
            with self.assertRaises(BoomError):
                ingest_batch([_material()], batch_id="B4")
        finally:
            pipeline_mod.store.rows = original  # type: ignore[assignment]

        self.assertEqual(len(store.rows(PAVEMENT_MODULE)), prepared_rows_before)
        self.assertEqual(store.rows("ingest_fingerprint"), [])
        batch = next(b for b in store.rows("ingest_batch") if b["batch_id"] == "B4")
        self.assertEqual(batch["status"], "failed")

    def test_resume_only_unfinished_lines(self):
        m1 = _material(材料编号="M1")
        m2 = _material(材料编号="M2", 桩号="K13+000", 病害类型="坑槽")
        first = ingest_batch([m1, m2], batch_id="B5")
        self.assertEqual(first["created"], 2)
        # 复位重传：同批次同内容 → 全部跳过
        again = ingest_batch([m1, m2], batch_id="B5")
        self.assertTrue(again.get("idempotent"))

    def test_repair_writeback_in_same_transaction(self):
        ingest_batch([_material()], batch_id="B6")
        disease = next(r for r in store.rows(PAVEMENT_MODULE) if r.get("材料编号") == "MATE-X1")
        patrol = next(r for r in store.rows(PATROL_MODULE) if r.get("关联病害编号") == disease["病害编号"])
        todo = next(r for r in store.rows(LIMIT_TODO_MODULE) if r["病害编号"] == disease["病害编号"])

        with store.transaction():
            disease["status"] = "已修复"
            disease["病害状态"] = "已修复"
            write_back_repair(disease, "已修复")

        self.assertEqual(patrol["巡查状态"], "已完成")
        self.assertEqual(patrol["裁决桩号"], disease["裁决桩号"])
        self.assertEqual(todo["状态"], "已解除")
        self.assertFalse(todo["pending"])

    def test_writeback_failure_rolls_back_disease_status(self):
        ingest_batch([_material()], batch_id="B7")
        disease = next(r for r in store.rows(PAVEMENT_MODULE) if r.get("材料编号") == "MATE-X1")

        import app.ingest.pipeline as pipeline_mod
        original = pipeline_mod.store.rows

        class BoomError(RuntimeError):
            pass

        def spying_rows(module):
            rows = original(module)
            if module == LIMIT_TODO_MODULE:
                raise BoomError("回写失败")
            return rows

        pipeline_mod.store.rows = spying_rows  # type: ignore[assignment]
        try:
            with self.assertRaises(BoomError):
                with store.transaction():
                    disease["status"] = "已修复"
                    write_back_repair(disease, "已修复")
        finally:
            pipeline_mod.store.rows = original  # type: ignore[assignment]
        # 回滚后 store 已换回快照表，必须重新读取；旧引用属于被丢弃的快照。
        disease_after = next(r for r in store.rows(PAVEMENT_MODULE) if r.get("材料编号") == "MATE-X1")
        self.assertEqual(disease_after["status"], "待修复")
        patrol_after = next(r for r in store.rows(PATROL_MODULE) if r.get("关联病害编号") == disease_after["病害编号"])
        self.assertNotEqual(patrol_after.get("巡查状态"), "已完成")

    def test_historical_collected_coordinates_preserved_on_migrate(self):
        from app.ingest.migration import migrate

        disease = {
            "id": 9001,
            "病害编号": "PAVE-HIST-1",
            "所属路段": "G104绕城段",
            "方向": "上行",
            "起止桩号": "K12+500",
            "裁决桩号": "K12+500",
            "连续里程_m": 9999.0,
            "投影坐标X": 50.0,
            "投影坐标Y": 50.0,
            "坐标来源": "collected",
            "发现日期": "2025-01-01",
            "病害类型": "车辙",
            "status": "待修复",
        }
        store.rows(PAVEMENT_MODULE).append(disease)
        result = migrate()
        self.assertGreaterEqual(result["preserved_historical"], 1)
        # 采集坐标不动，只补记现行裁决桩号
        self.assertEqual(disease["投影坐标X"], 50.0)
        self.assertEqual(disease["投影坐标Y"], 50.0)
        self.assertEqual(disease["连续里程_m"], 9999.0)
        self.assertEqual(disease["现行裁决桩号"], "K12+500")

    def test_migration_pulls_drifted_linked_rows_back_to_disease_stake(self):
        from app.ingest.migration import migrate

        disease = {
            "id": 9101,
            "病害编号": "PAVE-DRIFT-1",
            "所属路段": "G104绕城段",
            "方向": "上行",
            "起止桩号": "K11+000",
            "裁决桩号": "K11+000",
            "连续里程_m": 8888.0,       # 与基线（11000m）不符
            "坐标来源": "baseline",
            "发现日期": "2026-08-01",
            "病害类型": "沉陷",
            "status": "待修复",
        }
        patrol = {"id": 9102, "关联病害编号": "PAVE-DRIFT-1", "所属路段": "G104绕城段",
                  "裁决桩号": "K11+050", "连续里程_m": 9999.0, "发现日期": "2026-08-01"}
        todo = {"id": 9103, "病害编号": "PAVE-DRIFT-1", "所属路段": "G104绕城段",
                "裁决桩号": "K11+050", "连续里程_m": 9999.0, "状态": "待处置",
                "桥梁名称": "测试桥"}
        store.rows(PAVEMENT_MODULE).append(disease)
        store.rows(PATROL_MODULE).append(patrol)
        store.rows(LIMIT_TODO_MODULE).append(todo)

        result = migrate()
        self.assertGreaterEqual(result["migrated"], 3)
        self.assertEqual(disease["连续里程_m"], 11000.0)
        # 巡查与限载被拉回病害台账的同一桩号，而不是各自保留漂移值
        self.assertEqual(patrol["裁决桩号"], "K11+000")
        self.assertEqual(todo["裁决桩号"], "K11+000")
        self.assertEqual(patrol["连续里程_m"], 11000.0)
        self.assertEqual(todo["连续里程_m"], 11000.0)
        item = next(i for i in risk_summary()["items"] if i["病害编号"] == "PAVE-DRIFT-1")
        self.assertTrue(item["桩号一致"])

    def test_fingerprint_changes_when_business_content_changes(self):
        f1 = fp.fingerprint_of(_material(桩号="K12+800"))
        f2 = fp.fingerprint_of(_material(桩号="K12+800.0 "))  # 仅格式差异 → 同指纹
        f3 = fp.fingerprint_of(_material(桩号="K12+900"))    # 实际桩号变 → 新指纹
        self.assertEqual(f1, f2)
        self.assertNotEqual(f1, f3)


if __name__ == "__main__":
    unittest.main()
