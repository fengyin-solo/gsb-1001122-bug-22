"""外部材料接入与投影处理线。

分包内职责：

* :mod:`app.ingest.baseline`  —— 现行里程基线与桩号裁决（冲突桩号的唯一口径）；
* :mod:`app.ingest.projection` —— 桩号→空间坐标的单一投影入口；
* :mod:`app.ingest.fingerprint` —— 材料指纹，保证同一份材料并发/重连只生效一次；
* :mod:`app.ingest.pipeline` —— 解析→裁决→投影→落库→跨模块回写的事务管线；
* :mod:`app.ingest.migration` —— 存量位置不符记录按采集顺序迁移回填。
"""
