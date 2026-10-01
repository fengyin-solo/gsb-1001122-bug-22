# 市政道路桥梁养护管理平台

覆盖道路巡查、桥隧定检、路面病害、交安设施、绿化管养、除雪防汛及养护工程管理的市政道桥全要素养护后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 路段管理 | `road_section` | 管养路段 | 路段编号、路段名称、起止桩号 |
| 日常巡查 | `patrol` | 巡查记录 | 巡查编号、巡查路段、巡查日期 |
| 路面病害 | `pavement` | 病害记录 | 病害编号、所属路段、病害类型 |
| 桥梁定检 | `bridge` | 检测记录 | 检测编号、桥梁名称、检测类型 |
| 桥梁档案 | `bridge_info` | 桥梁 | 桥梁编号、桥梁名称、桥型结构 |
| 隧道管养 | `tunnel` | 隧道 | 隧道编号、隧道名称、隧道长度 |
| 交安设施 | `traffic_facility` | 交安设施 | 设施编号、设施类型、所属路段 |
| 排水设施 | `drainage` | 排水设施 | 设施编号、设施类型、所属路段 |
| 绿化管养 | `green` | 绿化区域 | 区域编号、区域名称、植物品种 |
| 路灯照明 | `lighting` | 路灯设施 | 灯具编号、灯具类型、功率 |
| 除雪防滑 | `winter` | 除雪作业 | 作业编号、作业路段、作业日期 |
| 防汛应急 | `flood` | 防汛记录 | 记录编号、预警级别、影响路段 |
| 边坡防护 | `slope` | 边坡 | 边坡编号、所属路段、边坡类型 |
| 伸缩缝管理 | `expansion` | 伸缩缝 | 缝编号、所属桥梁、缝类型 |
| 支座维护 | `bearing` | 桥梁支座 | 支座编号、所属桥梁、支座类型 |
| 养护工程 | `project` | 养护工程 | 工程编号、工程名称、工程类型 |
| 养护车辆 | `vehicle` | 养护车辆 | 车辆编号、车辆类型、车牌号 |
| 养护材料 | `material` | 养护材料 | 材料编号、材料名称、材料类别 |
| 风险汇总 | `risk` | 风险待办/桥梁限载提示 | 待办类型、关联编号、现行桩号、投影坐标 |

## 路面病害外部材料接入与投影处理线

外部路面病害材料从「养护材料」页提交，处理线为：

```text
解析材料 → 桩号裁决（现行里程基线）→ 空间投影 → 病害台账落库
        → 巡查清单回写 → 风险汇总/桥梁限载待办回写
```

口径与一致性约束（实现见 `backend/app/alignment.py`、`backend/app/services/ingest.py`）：

- **同一事务边界**：每行材料的桩号裁决、投影落库和三处跨模块回写在同一个
  `store.transaction()` 内完成，任一写入失败整行回滚，不允许留下半批病害；
  修复结论（派发/修复/验收）同样在同一事务内回写巡查清单与限载待办。
- **冲突桩号以现行里程基线为准**：旧基线桩号（如「旧线-2018」）按偏移换算到
  `现行里程基线-2026`；新旧基线候选冲突时现行基线优先。历史病害的采集桩号与
  采集坐标原样保留，仅补「现行桩号 / 投影坐标」；解析不出的存量位置按采集顺序
  迁移回填（启动时自动执行，幂等）。
- **材料指纹幂等**：整批材料 SHA-256 指纹 + 行级指纹双重判重，并发接入由临界区
  串行化，同一份材料只生效一次，断线重提不会重复建档。
- **断线复位只处理未完成行**：批次与逐行明细持久化，用同一批次号重提或调用
  `POST /api/material/ingest/resume/{batch_id}` 即可续传，已成功/判重的行跳过。
- 接口：`POST /api/material/ingest`、`GET /api/material/ingest/batches`、
  `POST /api/material/ingest/resume/{batch_id}`、`GET /api/risk/summary`。

### 后端测试

```bash
cd backend
python3 -m unittest discover -s tests
```

覆盖：旧基线换算与采集顺序补位、三处桩号一致性、事务失败整批回滚、并发指纹幂等、
断线续传只跑未完成行、修复结论跨模块回写。

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。
