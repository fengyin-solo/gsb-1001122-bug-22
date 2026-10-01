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
│   ├── app/ingest/           外部材料接入与投影处理线
│   └── app/store.py          内存数据仓库、事务边界与示例数据
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

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 外部材料接入与投影处理线

路面病害外部材料（采集车/检测单位回传）统一从「养护材料 → 接入外部材料」
（`POST /api/material/ingest`）入库，处理顺序固定：

```
解析 → 桩号裁决（现行里程基线）→ 空间投影（唯一映射）
     → 病害台账 / 巡查清单 / 桥梁限载待办 同事务回写 → 材料指纹登记
```

- **同一事务边界**：裁决、投影、三模块落库、指纹登记在 `store.transaction()` 内提交，
  任一写入失败整批回滚，不留半批病害，指纹也不会误登记（`app/store.py`）。
- **桩号裁决**：同方向桩号冲突一律以现行里程基线为准（`app/ingest/baseline.py`），
  材料原始桩号只作留痕；无基线路段拒绝落库。
- **空间投影**：三模块只能消费 `app/ingest/projection.py` 对同一裁决结论产出的坐标，
  病害列表、病害详情、桥梁限载提示因此显示同一个桩号。
- **材料指纹幂等**：按业务身份字段做规范化 SHA-256（`app/ingest/fingerprint.py`），
  同一份材料并发接入或断线重连只生效一次；桩号 `K12+800` 与 `12+800.0` 视为同位置。
- **断线复位**：批次先建档（`ingest_batch` 表），成功才置 completed；
  重连带原 `batch_id` 只处理未完成行，已完成批次直接幂等回放。
- **存量迁移**：`POST /api/material/migrate` 按采集顺序（发现日期、id）回填位置不符记录，
  历史病害（`坐标来源=collected`）维持采集坐标不动，关联巡查/限载跟随病害台账同一投影。
- **修复结论回写**：病害「标记修复/验收通过」在同一事务内回写巡查清单状态与限载待办
  （待处置→已解除）。
- **风险汇总页**：前端「风险汇总」（`GET /api/risk/summary`）把病害台账、巡查清单、
  限载待办按同一裁决桩号对照，桩号不一致会直接标红。

后端测试（标准库，无额外依赖）：

```bash
cd backend && python3 -m unittest discover -s tests
```
