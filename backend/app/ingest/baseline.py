"""现行里程基线：桩号冲突的唯一裁决依据。

外部材料（采集车、检测单位回传）里的桩号经常和现行里程对不上：
改线、短链、断链之后旧桩号会漂移。规则只有一条——

* 同方向桩号冲突，一律以**现行里程基线**为准，材料里的桩号只作为线索；
* 历史病害维持此前采集时的坐标不动（``collected`` 坐标是历史事实，不重投影）；
* 存量记录里位置与基线不符的，按采集顺序逐条迁移回填（见 :mod:`app.ingest.migration`）。

基线按「路段 + 行驶方向」给出桩号到连续里程（米）的换算锚点：
``里程 = 锚点里程 + (材料桩号数值 - 锚点桩号数值) * 1000``。
同一路段不同方向各自独立，避免上行/下行桩号互相污染。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 现行里程基线：key = (所属路段, 方向)。
# anchor_stake 为该方向基线起点桩号（公里数），anchor_mileage 为对应连续里程（米）。
# 真实项目里这张表由里程中心下发；演示环境内置 G104 绕城段基线，
# 外部材料里的 K12+800/K12+900 等旧桩号都会统一裁到基线口径。
@dataclass(frozen=True)
class Baseline:
    road: str
    direction: str
    anchor_stake_km: float
    anchor_mileage_m: float


_BASELINES: dict[tuple[str, str], Baseline] = {
    ("G104绕城段", "上行"): Baseline("G104绕城段", "上行", anchor_stake_km=10.0, anchor_mileage_m=10_000.0),
    ("G104绕城段", "下行"): Baseline("G104绕城段", "下行", anchor_stake_km=10.0, anchor_mileage_m=10_000.0),
}

DEFAULT_DIRECTION = "上行"


def all_baselines() -> list[dict[str, Any]]:
    return [
        {
            "所属路段": b.road,
            "方向": b.direction,
            "基线起点桩号_km": b.anchor_stake_km,
            "基线起点里程_m": b.anchor_mileage_m,
        }
        for b in _BASELINES.values()
    ]


def get_baseline(road: str, direction: str) -> Baseline | None:
    return _BASELINES.get((road, direction))


def parse_stake(value: Any) -> float | None:
    """把 ``K12+800`` / ``12+800`` / ``12.8`` 这类桩号解析成公里数。

    解析不出来（演示用的占位文本、空值）返回 None，由调用方决定回退口径。
    """
    if value is None:
        return None
    text = str(value).strip().upper().replace("K", "")
    if not text:
        return None
    if "+" in text:
        head, _, tail = text.partition("+")
        try:
            return float(head) + float(tail) / 1000.0
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def format_stake(stake_km: float) -> str:
    """公里数格式化为 ``K12+800`` 形式的标准桩号。"""
    km = int(stake_km)
    meter = round((stake_km - km) * 1000)
    return f"K{km}+{meter:03d}"


@dataclass(frozen=True)
class Adjudication:
    """桩号裁决结论：全平台只能引用这一份结论。"""

    road: str
    direction: str
    material_stake: str | None  # 材料原始桩号（线索）
    stake_km: float  # 裁决后的标准桩号（公里）
    stake: str  # 裁决后的标准桩号文本
    mileage_m: float  # 投影后的连续里程（米）
    source: str  # baseline / material_fallback（无基线时的回退，需显式可见）


def adjudicate(road: str, direction: str | None, material_stake: Any) -> Adjudication | None:
    """以现行里程基线裁决一条材料的桩号。

    没有基线的路段不能凭空裁决，返回 None —— 调用方应拒绝该行而不是各模块各写各的。
    """
    direction = direction or DEFAULT_DIRECTION
    stake_km = parse_stake(material_stake)
    baseline = get_baseline(road, direction)
    if baseline is None:
        return None
    if stake_km is None:
        # 桩号缺失时以基线起点为占位，仍保证三模块拿到同一个值。
        stake_km = baseline.anchor_stake_km
    mileage_m = baseline.anchor_mileage_m + (stake_km - baseline.anchor_stake_km) * 1000.0
    raw = str(material_stake).strip() if material_stake not in (None, "") else None
    return Adjudication(
        road=road,
        direction=direction,
        material_stake=raw,
        stake_km=stake_km,
        stake=format_stake(stake_km),
        mileage_m=round(mileage_m, 3),
        source="baseline",
    )
