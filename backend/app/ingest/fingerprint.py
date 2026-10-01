"""材料指纹：外部材料接入幂等的依据。

断线重连后采集端会把同一批材料再推一次，若不识别就会重复建档。
指纹取材料行的业务身份字段做规范化哈希（SHA-256）：

* 同一内容重复推送（含并发同时到达）→ 指纹相同，只生效一次；
* 内容真的变化（桩号/类型/严重程度变化）→ 指纹不同，作为新版本入库。

已生效指纹存在内存表 ``ingest_fingerprint`` 中，登记动作与业务写入
在同一个事务里提交，因此「指纹记上了但病害只写了一半」不可能发生。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app.store import store

FINGERPRINT_MODULE = "ingest_fingerprint"

# 参与指纹的业务身份字段（顺序固定）；批次号/行号不入指纹，
# 否则同一批材料拆成两批重传时就挡不住重复建档。
IDENTITY_FIELDS = (
    "材料编号",
    "所属路段",
    "方向",
    "桩号",
    "病害类型",
    "严重程度",
    "发现日期",
    "关联桥梁",
)


def normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().upper().replace(" ", "")


def _canonical(field: str, value: Any) -> str:
    """字段规范化：桩号统一按公里数归约，``K12+800``/``12+800.0`` 视为同一位置。"""
    text = normalize(value)
    if not text:
        return ""
    if field == "桩号":
        from app.ingest.baseline import format_stake, parse_stake

        stake_km = parse_stake(text)
        if stake_km is not None:
            return format_stake(stake_km)
    return text


def fingerprint_of(row: dict[str, Any]) -> str:
    payload = {field: _canonical(field, row.get(field)) for field in IDENTITY_FIELDS}
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def seen(fingerprint: str) -> bool:
    return any(row.get("fingerprint") == fingerprint for row in store.rows(FINGERPRINT_MODULE))


def register(fingerprint: str, *, batch_id: str, material_no: str) -> None:
    """登记指纹。必须在事务内调用，随业务写入一起提交或一起回滚。"""
    store.rows(FINGERPRINT_MODULE).append({
        "id": store.next_id(FINGERPRINT_MODULE),
        "fingerprint": fingerprint,
        "batch_id": batch_id,
        "材料编号": material_no,
    })
