"""里程基线、桩号裁决与空间投影。

路面病害外部材料接入后，巡查列表、病害详情和桥梁限载提示曾经出现对不上的不同桩号，
根因是三方各自按上报口径直写，没有统一裁决。这里把口径收死：

- **同方向桩号冲突，一律以现行里程基线为准**：上报材料若带旧基线桩号，按基线偏移
  换算到现行基线；多条上报互相冲突时，取现行基线下采集顺序最新的一条；
- **历史病害维持此前采集坐标**：迁移只补「现行桩号 / 投影坐标」，绝不覆盖采集桩号、
  采集坐标等原始字段；
- **存量位置不符记录按采集顺序迁移回填**：解析不出桩号的遗留行，沿同一道路同方向
  按采集顺序依次补位，保证巡查、病害、限载待办引用的是同一个现行桩号；
- 投影是确定性的纯函数，同一（道路、方向、现行桩号）在任何时刻投出同一坐标。
"""
from __future__ import annotations

import math
import re
from typing import Any

# 现行里程基线版本号；冲突裁决与迁移回填都归一到它。
CURRENT_BASELINE = "现行里程基线-2026"

# 同方向相邻补位记录的最小间距（米），仅用于历史遗留的无法解析桩号。
SEQUENCE_STEP_METERS = 100.0

# 桩号触发桥梁限载提示的距离阈值（米）。
BRIDGE_NEARBY_METERS = 500.0

# 各道路、各方向的基线表。旧基线相对现行基线存在整体偏移（公里）：
# 现行桩号 = 旧桩号 + offset。方向缺失时按上行处理。
_BASELINES: dict[str, dict[str, dict[str, dict[str, Any]]]] = {
    "G104": {
        "上行": {
            CURRENT_BASELINE: {"offset_km": 0.0, "origin": (0.0, 3.75), "azimuth": 0.0},
            "旧线-2018": {"offset_km": 0.2, "origin": (-12.0, 3.75), "azimuth": 0.0},
        },
        "下行": {
            CURRENT_BASELINE: {"offset_km": 0.0, "origin": (0.0, -3.75), "azimuth": 180.0},
            "旧线-2018": {"offset_km": 0.2, "origin": (-12.0, -3.75), "azimuth": 180.0},
        },
    },
}

_DEFAULT_DIRECTION = "上行"
_STATION_RE = re.compile(r"^\s*K\s*(\d+)\s*\+\s*(\d+(?:\.\d+)?)\s*(.*)$")
_ROAD_RE = re.compile(r"((?:G|S|X|Y)\d+)")


# ---- 基础解析 ---------------------------------------------------------------

def road_code(road: str | None) -> str | None:
    """从「G104杭州段」之类的道路名称中提取道路编码，未知道路返回 None。"""
    if not road:
        return None
    match = _ROAD_RE.search(str(road))
    return match.group(1) if match else None


def normalize_direction(direction: str | None) -> str:
    text = str(direction or "").strip()
    return text if text in ("上行", "下行") else _DEFAULT_DIRECTION


def parse_station(value: Any) -> float | None:
    """把 ``K3+200`` / ``K3+200.50`` 解析成米；无法识别时返回 None。"""
    if value is None:
        return None
    match = _STATION_RE.match(str(value))
    if not match:
        return None
    kilometers = int(match.group(1))
    meters = float(match.group(2))
    if meters >= 1000.0:
        return None
    return kilometers * 1000.0 + meters


def format_station(meters: float) -> str:
    """米反格式化回标准桩号文本，作为跨模块统一展示口径。"""
    meters = max(meters, 0.0)
    return f"K{int(meters // 1000)}+{meters % 1000:07.3f}"


def station_text(road: str | None, meters: float, direction: str) -> str:
    """带道路与方向的完整桩号文本，三处回写都用它，避免展示对不上。"""
    prefix = f"{road}-" if road else ""
    return f"{prefix}{format_station(meters)}（{normalize_direction(direction)}）"


# ---- 基线换算与投影 ---------------------------------------------------------

def _baseline(road: str, direction: str, version: str) -> dict[str, Any] | None:
    directions = _BASELINES.get(road, {})
    versions = directions.get(normalize_direction(direction))
    if not versions:
        return None
    return versions.get(version) or versions.get(CURRENT_BASELINE)


def to_current_meters(road: str, direction: str, raw_meters: float, version: str | None) -> float:
    """把任意已登记基线版本下的桩号换算到现行基线；未知版本按现行基线处理。"""
    params = _baseline(road, direction, version or CURRENT_BASELINE)
    offset_km = float(params["offset_km"]) if params else 0.0
    return raw_meters + offset_km * 1000.0


def project(road: str, direction: str, current_meters: float) -> dict[str, Any]:
    """确定性平面投影：同一现行桩号在任何调用中投出完全一致的坐标。"""
    direction = normalize_direction(direction)
    params = _baseline(road, direction, CURRENT_BASELINE) or {"origin": (0.0, 0.0), "azimuth": 0.0}
    origin_x, origin_y = params["origin"]
    azimuth = math.radians(float(params["azimuth"]))
    x = origin_x + current_meters * math.cos(azimuth)
    y = origin_y + current_meters * math.sin(azimuth)
    return {
        "坐标系": "养护局部平面坐标系",
        "x": round(x, 3),
        "y": round(y, 3),
        "文本": f"({round(x, 3)}, {round(y, 3)})",
    }


# ---- 冲突裁决 ---------------------------------------------------------------

def adjudicate(
    road: str,
    direction: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """同方向桩号冲突裁决：现行基线优先，其次采集顺序最新。

    每个候选形如 ``{"桩号": "K3+200", "里程基线版本": ..., "采集序号": 1}``。
    裁决只产出「现行桩号」，不删除候选自带的原始桩号与采集坐标。
    """
    parsed: list[tuple[int, float, str, dict[str, Any]]] = []
    for index, candidate in enumerate(candidates):
        meters = parse_station(candidate.get("桩号"))
        if meters is None:
            continue
        version = str(candidate.get("里程基线版本") or CURRENT_BASELINE)
        order = int(candidate.get("采集序号", index))
        parsed.append((0 if version == CURRENT_BASELINE else 1, order, meters, candidate | {"版本": version}))
    if not parsed:
        return None
    # 现行基线候选排在旧基线之前；同版本内采集序号大的（后采集）优先。
    _, _, meters, winner = sorted(parsed, key=lambda item: (item[0], -item[1]))[0]
    version = winner["版本"]
    current_meters = to_current_meters(road, direction, meters, version)
    return {
        "现行里程": current_meters,
        "现行桩号": station_text(road, current_meters, direction),
        "投影坐标": project(road, direction, current_meters),
        "来源版本": version,
        "来源桩号": str(winner.get("桩号")),
        "是否换算": version != CURRENT_BASELINE,
    }


# ---- 存量迁移回填 -----------------------------------------------------------

def _collection_key(row: dict[str, Any]) -> tuple[str, int]:
    order = row.get("采集序号")
    if order is None:
        return (str(row.get("发现日期") or row.get("巡查日期") or ""), int(row.get("id", 0)))
    return (str(row.get("发现日期") or row.get("巡查日期") or ""), int(order))


def _fill_row(
    row: dict[str, Any],
    road: str,
    direction: str,
    cursor: dict[tuple[str, str], float],
) -> None:
    """按现行基线补齐单行；解析不出的遗留桩号沿采集顺序补位。"""
    key = (road, direction)
    raw_station = row.get("采集桩号") or row.get("起止桩号")
    version = str(row.get("里程基线版本") or CURRENT_BASELINE)
    raw_meters = parse_station(raw_station)

    converted = False
    if raw_meters is not None:
        current_meters = to_current_meters(road, direction, raw_meters, version)
        converted = version != CURRENT_BASELINE
        reason = "旧基线换算" if converted else "现行基线"
    else:
        # 无法解析的遗留位置：沿同方向按采集顺序补位，仍保留原始采集信息。
        current_meters = max(cursor.get(key, 0.0), SEQUENCE_STEP_METERS)
        cursor[key] = current_meters + SEQUENCE_STEP_METERS
        reason = "采集顺序补位"

    projected = project(road, direction, current_meters)
    row["采集桩号"] = str(raw_station or "")
    row["历史桩号"] = str(raw_station or "") if converted or raw_station else ""
    row["现行桩号"] = station_text(road, current_meters, direction)
    row["投影坐标"] = projected["文本"]
    row["投影坐标x"] = projected["x"]
    row["投影坐标y"] = projected["y"]
    row["行向"] = direction
    row["道路编码"] = road
    row["里程基线版本"] = CURRENT_BASELINE
    row["裁决说明"] = reason
    row["已迁移"] = True
    # 列表页展示的「起止桩号」统一指向现行桩号，根因上消除三处对不上。
    row["起止桩号"] = row["现行桩号"]


def migrate_legacy_positions(svc_store) -> dict[str, int]:
    """把存量病害/巡查的位置迁移回填到现行里程基线。

    病害先行，巡查若挂在同一病害上直接继承其裁决结论，保证同根因的两处记录桩号一致；
    整段迁移在同一事务内执行，中途失败全部回滚；已迁移行跳过，重复执行幂等。
    """
    from app.store import store as default_store

    target = svc_store or default_store
    with target.transaction():
        cursor: dict[tuple[str, str], float] = {}
        disease_index: dict[str, dict[str, Any]] = {}

        pavement_rows = sorted(target.rows("pavement"), key=_collection_key)
        migrated_pavement = 0
        for row in pavement_rows:
            road = road_code(row.get("所属路段"))
            if road is None:
                continue
            direction = normalize_direction(row.get("行向"))
            if not row.get("已迁移"):
                _fill_row(row, road, direction, cursor)
                migrated_pavement += 1
            disease_index[str(row.get("病害编号"))] = row
            disease_index[str(row.get("id"))] = row

        patrol_rows = sorted(target.rows("patrol"), key=_collection_key)
        migrated_patrol = 0
        for row in patrol_rows:
            if row.get("已迁移"):
                continue
            linked = disease_index.get(str(row.get("关联病害编号") or ""))
            if linked is not None:
                row["采集桩号"] = str(row.get("采集桩号") or row.get("起止桩号") or "")
                row["历史桩号"] = linked.get("历史桩号", "")
                row["现行桩号"] = linked["现行桩号"]
                row["投影坐标"] = linked["投影坐标"]
                row["投影坐标x"] = linked.get("投影坐标x")
                row["投影坐标y"] = linked.get("投影坐标y")
                row["行向"] = linked.get("行向")
                row["道路编码"] = linked.get("道路编码")
                row["里程基线版本"] = CURRENT_BASELINE
                row["裁决说明"] = "继承关联病害"
                row["已迁移"] = True
                migrated_patrol += 1
                continue
            road = road_code(row.get("巡查路段"))
            if road is None:
                continue
            direction = normalize_direction(row.get("行向"))
            _fill_row(row, road, direction, cursor)
            migrated_patrol += 1

    return {"pavement": migrated_pavement, "patrol": migrated_patrol}
