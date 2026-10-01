"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

与材料接入/投影处理线相关的能力也收在这里：

- ``transaction()`` 事务边界：桩号裁决、空间投影、落库与跨模块回写在同一事务内完成，
  任一步抛出异常都会用快照整批回滚，保证不会留下「半批病害」；
- ``RLock`` 串行化并发接入，配合材料指纹索引，同一指纹在并发下只生效一次；
- 接入批次、接入明细、限载待办属于管线内部表，不计入运营概览。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 管线内部表：不参与运营概览的模块计数，也不在业务模块清单里暴露。
INTERNAL_TABLES = {"ingest_run", "ingest_record", "risk_todo"}


class Store:
    def __init__(self) -> None:
        # 接入临界区用不可重入锁：指纹占位与批次登记必须互斥，不能被同线程的事务重入。
        self._ingest_lock = threading.Lock()
        # 事务用可重入锁：事务内部允许再开短临界区/嵌套调用服务方法。
        self._lock = threading.RLock()
        self._fingerprints: set[str] = set()
        self._tables: dict[str, list[dict[str, Any]]] = {}
        self._reset_data()

    def _reset_data(self) -> None:
        """从种子数据重建全表（测试复位与服务启动共用同一份口径）。"""
        self._tables = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        for name in INTERNAL_TABLES:
            self._tables.setdefault(name, [])
        # 已成功接入过的材料指纹在复位后依旧生效，重复提交只能跳过。
        self._fingerprints = {
            str(row.get("材料指纹"))
            for row in self.rows("ingest_record")
            if row.get("材料指纹") and row.get("状态") == "成功"
        }

    def reset(self) -> None:
        """测试辅助：丢弃运行期改动，回到种子状态。"""
        with self._lock:
            self._reset_data()

    # ---- 事务边界 ---------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator["Store"]:
        """事务上下文：进入时拍快照，异常时整批回滚，正常退出才提交。

        解析、桩号裁决、空间投影和对病害台账/巡查清单/限载待办的回写必须全部落在
        同一个 ``transaction()`` 里，任何一处写入失败都会把三处改动一起撤销。
        """
        with self._lock:
            snapshot = copy.deepcopy(self._tables)
            fingerprint_snapshot = set(self._fingerprints)
            try:
                yield self
            except BaseException:
                self._tables = snapshot
                self._fingerprints = fingerprint_snapshot
                raise

    # ---- 材料指纹（幂等） -------------------------------------------------

    def has_fingerprint(self, fingerprint: str) -> bool:
        return fingerprint in self._fingerprints

    @contextmanager
    def locked(self) -> Iterator[None]:
        """只做串行化的短临界区（不快照、不回滚），用于并发接入的指纹占位。"""
        with self._ingest_lock:
            yield

    def commit_fingerprint(self, fingerprint: str) -> bool:
        """登记材料指纹。必须在事务内调用；已存在返回 False（并发只生效一次）。"""
        if not fingerprint or fingerprint in self._fingerprints:
            return False
        self._fingerprints.add(fingerprint)
        return True

    def release_fingerprint(self, fingerprint: str) -> None:
        """放弃未完成接入的指纹占位，允许客户端修正材料后重新提交。"""
        self._fingerprints.discard(fingerprint)

    # ---- 表访问 -----------------------------------------------------------

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        """在事务内取新主键；调用方随后的 append 与它处于同一原子区间。"""
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in INTERNAL_TABLES)

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
