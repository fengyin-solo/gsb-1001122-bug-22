"""空间投影：桩号裁决结论 → 坐标的唯一映射入口。

修复前的问题是病害台账、巡查清单、桥梁限载各自换算坐标，
得到对不上的不同桩号。这里强制所有模块只能消费
:func:`project` 对同一份裁决结论产出的坐标，投影结果随入库结论一起回写，
任何模块都不允许再自行解释桩号。

历史病害（带 ``coord_origin == "collected"``）维持采集坐标，迁移与接入都不改它。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.ingest.baseline import Adjudication


@dataclass(frozen=True)
class Projection:
    stake: str            # 裁决桩号（三模块展示同一份）
    mileage_m: float      # 连续里程，米
    direction: str
    coord_x: float        # 投影平面坐标 X（演示：东向分量）
    coord_y: float        # 投影平面坐标 Y（演示：北向分量）
    coord_origin: str     # baseline：由现行基线投影；collected：历史采集坐标


def project(adj: Adjudication) -> Projection:
    """把桩号裁决结论投影成坐标。

    演示环境用局部平面：沿路线方向每米一个线性单位；
    上行 y 取里程，下行 y 反向，保证同方向可比、跨方向不串。
    """
    mileage = adj.mileage_m
    if adj.direction == "下行":
        x, y = 0.0, -mileage
    else:
        x, y = 0.0, mileage
    return Projection(
        stake=adj.stake,
        mileage_m=mileage,
        direction=adj.direction,
        coord_x=x,
        coord_y=y,
        coord_origin="baseline",
    )


def as_row_fields(proj: Projection) -> dict[str, object]:
    """投影结果转成各模块统一回写的字段子集。"""
    return {
        "裁决桩号": proj.stake,
        "连续里程_m": proj.mileage_m,
        "方向": proj.direction,
        "投影坐标X": proj.coord_x,
        "投影坐标Y": proj.coord_y,
        "坐标来源": proj.coord_origin,
    }
