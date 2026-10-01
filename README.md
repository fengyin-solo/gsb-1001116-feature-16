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

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 支座承载力与锈蚀阈值复核矩阵

`bearing` 模块内置阈值复核矩阵（`app/services/bearing_review.py`），按
**支座编号、所属桥梁、检查周期**（最近检查所属季度）判定每个支座应安排
**巡检（低风险）/ 维修（中风险）/ 更换（高风险）**。

- 判定维度：承载力比（实荷/设计）、锈蚀率、位移量、检查周期逾期天数。
- 阈值冲突（承载力与锈蚀结论不一致，或与最近专项结论不一致）时，以
  **设计承载力富余度与最近专项结论共同裁定**：承载力超硬线直接更换；
  专项结论凭承载力富余最多下调一档；无专项结论时安全优先、就高裁定。
- 历史支座（状态为已拆除/已更换或带「历史支座」标记）不重新计算，
  继续按原检查记录保留。
- 复核结论回写三处：支座台账（`bearing`）、桥梁定检清单（`bridge`，
  每桥每版本一张「支座专项复核」聚合单）、工程待办（`project`，按
  桥与处置档维护，降档取消未实施待办），并同步 `/api/overview` 风险等级。
- 规则版本（v1/v2）切换时，**未完成检查单原地迁移**并重算建议；
  已完成与历史遗留检查单保留原版本、原结论。
- 批量复核按所属桥梁分组、每组一次事务：组内任一步失败整组回滚，
  不产生部分改写；重复执行幂等（检查单/定检单/待办均为稳定业务键 upsert）。

接口前缀 `/api/bearing/review`：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/matrix` | 当前阈值矩阵与规则版本 |
| POST | `/batch` | 按桥梁分组批量复核（可带 `bridge`、`bearing_codes`、`version`） |
| GET | `/checklists` | 复核检查单分页（支持桥梁/建议/状态过滤） |
| POST | `/checklists/{id}/complete` | 检查单闭环并同步聚合，重复提交幂等 |
| POST | `/switch-version` | 切换规则版本并迁移未完成检查单 |

后端测试（仅标准库）：

```bash
cd backend && python3 -m unittest discover -s tests
```
