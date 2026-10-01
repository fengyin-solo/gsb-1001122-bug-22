"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

写入侧统一走 ``store.transaction()``：进入事务时对全表做深拷贝快照，
提交时一次性替换；上下文内任何异常都会把全部表回滚到快照，保证
「桩号裁决 + 投影落库 + 跨模块回写」处在同一个事务边界里，绝不留半批数据。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 接入管线的内部状态表，不计入运营概览的业务模块清单。
INTERNAL_MODULES = frozenset({"ingest_fingerprint", "ingest_batch", "limit_todo"})


class _Rollback(Exception):
    """事务内部用来显式触发回滚的信号。"""


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 接入/迁移可能并发到达（断线重连、多端提交），用一把可重入锁串行化写入。
        self._lock = threading.RLock()

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """单进程内存库的事务边界。

        - 进入时深拷贝全部表（含接入指纹表、限载待办表）；
        - 正常退出才用快照之后的状态提交；
        - 抛出任何异常（含下游模块写入失败）都恢复快照，整批回滚；
        - 支持嵌套：内层事务与外层共用一个提交点。
        """
        with self._lock:
            if getattr(self, "_active_snapshot", None) is not None:
                # 外层已经持有事务，内层不再重复快照，交由最外层统一提交/回滚。
                yield
                return
            self._active_snapshot = copy.deepcopy(self._tables)
            try:
                yield
            except BaseException:
                self._tables = self._active_snapshot
                raise
            finally:
                self._active_snapshot = None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            if name in INTERNAL_MODULES:
                continue
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
