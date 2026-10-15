# 企业采购审批系统

一套**个人学习项目**：从零重写一个企业采购审批系统，用来把「动态多级审批引擎、预算并发预留、三单匹配」这三件事真正做一遍。后端 FastAPI 模块化单体（边界 / 用例 / 领域 / 基础设施四层），前端 React SPA，PostgreSQL 是唯一数据源。

> **这不是生产系统，也不打算上线。**
> 没有真实供应商、没有真实资金、没有真实发票。全部数据为本地合成数据，仅用于学习与演示。
> 目标平台：Windows 浏览器优先，之后才考虑独立窗口与 Android。

## 为什么要做这个

参考项目是一套功能很全的政务演示系统（FastAPI + Android + MCP + Milvus + MinIO），它的**架构值得学，但功能过多**——官方数字人 SDK 一项就占了仓库 95% 的体积，还拖着 Milvus、etcd、LibreOffice 三个重依赖。本项目把它**砍到只剩骨架**，换一个更小的业务领域重写，用最少的基础设施去练最核心的那几个工程问题：

| 练什么 | 在哪一阶段 |
| --- | --- |
| 按金额动态生成审批链（数据驱动，不是硬编码 if/else） | M6 |
| 预算的并发预留与释放（条件更新，不是先查再写） | M7 |
| 三单匹配（订单 / 收货 / 发票对账） | M10 |
| 幂等、乐观锁、事件流、只追加审计 | M5 起贯穿 |
| 带权限继承与人工确认的 Agent 工具调用 | M12+ |

## 仓库组成

| 目录 / 文件 | 作用 |
| --- | --- |
| `backend/` | FastAPI 四层骨架、三个健康检查、pytest 分档、ruff、mypy |
| `frontend/` | React 18 + Vite + TypeScript + Tailwind v4 + TanStack Query |
| `docs/` | 专题文档（领域、AI、面试、参考项目、历史归档）—— 见下 |
| `scripts/` | 启动、回归、浏览器验收、本地模型管理 |
| `docker-compose.yml` | 薄封装，用 `include:` 引用仓库外的数据库编排 |
| `resume/` | 简历构建（交付物 + 构建脚本） |

## 文档导航（**按职责分工，不要重复读**）

| 我想…… | 去哪 |
| --- | --- |
| 知道现在做到哪、下一步做什么 | [NOW.md](NOW.md) |
| 找某个功能在哪个文件 | [PROJECT_INDEX.md](PROJECT_INDEX.md) |
| 跑起项目 / 提交前跑什么 | [RUNBOOK.md](RUNBOOK.md) |
| 查端口、版本、配置、环境坑 | [MAP.md](MAP.md) |
| 查"当初为什么这么选" | [DECISIONS.md](DECISIONS.md) |
| 查已知问题与真债 | [RISKS.md](RISKS.md) |
| 查某阶段的验收过了没有 | [PROGRESS.md](PROGRESS.md) |
| 查某阶段"学到了什么" | [Development progress.md](Development%20progress.md) |
| 懂业务规则 / 权限 / 接口 / 数据模型 | [docs/业务规则.md](docs/业务规则.md)、[docs/domain/权限矩阵.md](docs/domain/权限矩阵.md)、[docs/api/README.md](docs/api/README.md)、[docs/ER-图.md](docs/ER-图.md) |
| 做 AI 相关的事 | [docs/AI-评测baseline.md](docs/AI-评测baseline.md)、[docs/ai/](docs/ai/) |
| 准备面试 / 写简历 | [docs/interview/](docs/interview/) |
| AI 改动的规矩（**AI 必读**） | [AGENTS.md](AGENTS.md) |

## 快速开始

```powershell
cd D:\procurement-app
.\scripts\dev-up.ps1 -WithApi -WithFrontend     # Docker + 数据库 + 后端 + 前端
.\scripts\seed.ps1                              # 演示数据（幂等；新库建议跑一次）
```

浏览器打开 <http://localhost:5173>，用 `admin` / `Dev-Passw0rd!` 登录
（7 个演示账号见 [docs/domain/权限矩阵.md](docs/domain/权限矩阵.md) 第四节）。

**完整步骤、每一步的验证状态、以及四条常见故障的处理**在 [RUNBOOK.md](RUNBOOK.md)。
**不想开终端**可以直接双击 `启动项目.bat`。

**AI 功能需要一个本地模型**（制度助手的 embedding 用它）：

```powershell
.\scripts\ollama.ps1 -Serve      # 一个窗口常驻
.\scripts\ollama.ps1 -Pull       # 另一个窗口：拉默认模型
```

不装它的话，知识库上传与问答会**明确报"embedding 调用失败"**，不会静默退回哈希。

## 目录结构与四层依赖

```
backend/app/
├── domain/          纯 Python 业务逻辑 —— 禁止 import 任何框架
├── application/     用例编排 —— 事务边界在这一层（ports.py + coordinators/）
├── boundaries/http/ HTTP 边界 —— 只解析 / 校验 / 鉴权
└── infrastructure/  适配器与组合根（records/ ORM、repositories/ 数据访问）

frontend/src/
├── api/client.ts    统一 HTTP 客户端（401 续期重放、统一错误体）
├── components/      跨页组件（ui.tsx 界面原语、两个时间轴）
├── lib/money.ts     元 ⇄ 分**唯一入口**；lib/sse.ts SSE 切帧
├── pages/           页面
└── index.css        设计令牌（Tailwind v4 @theme）—— 换风格只动这一个文件
```

**依赖方向是单向的**：`boundaries` → `application` → `domain`；`infrastructure` 实现端口，不反向依赖。

| 层 | 允许 | 禁止 |
| --- | --- | --- |
| `domain/` | 纯函数、枚举、状态迁移表、规则求值 | **import 任何框架** |
| `application/` | 编排用例、**决定事务边界** | 写 SQL、出现 Request/Response |
| `boundaries/` | 解析请求、校验输入、权限与幂等依赖 | 写业务规则、编排事务 |
| `infrastructure/` | ORM 映射、仓库、组合根、适配器 | 决定事务边界（**只 `flush()`**） |

逐文件说明在 [PROJECT_INDEX.md](PROJECT_INDEX.md) 第五节的"关键入口文件"。

## 阶段与进度

计划共 13 个阶段（M0–M12），约 15–18 周（按每周 10–15 小时估算）。

| 阶段 | 主题 | 状态 |
| --- | --- | --- |
| **M0** | 准备与设计定稿 | ✅ 2026-10-01 |
| **M1** | 骨架贯通 | ✅ 2026-10-02 |
| **M2** | 主数据 | ✅ 2026-10-02 |
| **M3** | 账号与权限 | ✅ 2026-10-02 |
| **M4** | 前端设计系统 | ✅ 2026-10-02 |
| **M5** | 采购申请 | ✅ 2026-10-02 |
| **M6** | 审批引擎 ★ | ✅ 2026-10-03 |
| **M7** | 预算并发预留 ★ | ✅ 2026-10-04 |
| **M8** | 回归验收 | ✅ 2026-10-04 |
| **M9** | 附件与时间线 | ✅ 2026-10-04 |
| **M10** | 履约链（询价→下单→收货→发票→付款→归档） | ✅ 2026-10-06 |
| **M11** | AI 制度助手 | ✅ 2026-10-06 |
| **M11+** | AI 模块增强（真 embedding / 评测门禁 / 引用支撑性 / 可观测 / 限流） | ✅ 2026-10-14 |
| **M12+** | Function Calling 与最小 ReAct | ✅ 2026-10-14 |
| **M12** | 桌面壳与移动端 | ⏸️ **暂不做**（加分项，与领域建模不重叠） |

每个阶段的**逐条验收证据**在 [PROGRESS.md](PROGRESS.md)；
**"学到了什么、为什么这么选"**在 [Development progress.md](Development%20progress.md)。

## 规模（2026-10-14 实测）

| 维度 | 数值 |
| --- | --- |
| 后端源码 | **88** 个 `.py` |
| 数据表 / 迁移 | **25** 张（ORM 模型）/ **19** 个迁移，head `0e9f1c4a77b2` |
| HTTP 接口 | **59** 条路径 / **70** 个操作 |
| 前端源码 | **38** 个 `.ts/.tsx` |
| 测试 | `pytest -m ""` **680 passed**（unit 428 / integration 252） |
| 一条命令回归 | `.\scripts\回归验收.ps1` **14 步，退出码 0** |

## 验收与测试

```powershell
cd D:\procurement-app
.\scripts\回归验收.ps1        # 14 步：分层自查 → 后端全量 → ruff/mypy → 前端三件
                              # → 起服务 → 冒烟 → 探针 → 浏览器自检 → 知识库实测
                              # → 窄屏自检 → 知识库评测门禁 → 清理与残留回查
```

**退出码 0 = 全过**；任何一步失败会**停在那一步**并打印原始输出。
它**没有任何开关** —— 有开关就一定会出现"这次时间紧、少跑一项"。

最近一次全绿的各步数字：

```
三条分层自查        命中 0 处（自证探针通过）
pytest -m ""        680 passed
ruff + mypy         0 问题 / 88 files
前端 tsc            0 错误
前端 vitest         28 passed
前端 build          built
真实服务冒烟        19/19（m6_smoke.py，跑完自清理）
静默失败与接缝探针  21/21（regression_probes.py）
浏览器自检          17/17（自起无窗口 Edge）
知识库实测          6/6
窄屏自检            20/20（375×812）
知识库评测门禁      通过（27 条 Golden QA，真 embedding）
测试数据清理        干净（残留 0）
```

**单独跑（唯一不在回归入口里的）**：`node scripts\m6-页面实测.mjs` —— **51/51**，含截图。
它需要外部已开好的 Edge（9222 端口），与"自起自收"的生命周期不同。

> 想单独跑某一项、或想清测试数据：见 [RUNBOOK.md](RUNBOOK.md) 第二、三节。

## 停止与数据

```powershell
cd D:\develop\procurement
docker compose stop        # 停止但保留数据
```

**绝对不要用 `docker compose down -v`。** 本项目用 bind mount，
`down -v` 其实删不掉 `data\postgres`，但别养成这个习惯——换个项目就是数据全没了。

数据库数据落在 `D:\develop\procurement\data\postgres`，与代码仓库解耦。

## 协作约定

本仓库有 AI 助手参与开发，因此对"改动边界"有明确约定，写在 [AGENTS.md](AGENTS.md)：

- 改动只允许落在**用户点名的对象**，以及**为完成它必须动用的工具**上；其余一律只提不做。
- 点名的改动做完即停，不做"顺手优化"和"进一步完善"。
- 顺带发现的问题写进「待你确认的改动」清单（标 P1/P2/P3），当场不改。
- 不得用推测补足需求；需要更多信息时先问清，或只做能确定的部分。

**技术纪律十一条**（`domain/` 不依赖框架、仓库只 `flush()`、金额整数分、幂等、乐观锁条件更新、
事件只追加、附件 UUID 存盘、权限写进 `WHERE` 等）以及**文档读写规则**同样在 [AGENTS.md](AGENTS.md)。

## 已知限制

完整清单（含真债与保护措施）在 [RISKS.md](RISKS.md)。最需要知道的四条：

| 限制 | 说明 |
| --- | --- |
| **没有时间点恢复（PITR）** | 已有 `scripts\备份数据库.ps1`（全量 + gzip + 三重校验，落仓库外的 `D:\develop\procurement\backups`）与 `scripts\恢复演练.ps1`（真恢复进临时库并逐表核对，**实测 RTO 下界 4.1 秒**）。但**没有 WAL 归档** → 只能恢复到上一次全量备份的时刻；且**备份与本机同盘**，本机坏了一起没。见 [RISKS.md](RISKS.md) 第一节第 11 条 |
| Docker Desktop **刻意**未设开机自启 | 每次开工要手动启动引擎（`dev-up.ps1` 一条命令搞定） |
| `docker-compose.yml` 依赖目录布局 | `include:` 用相对路径，要求代码仓库与基础设施目录**同级**；否则需改一行 |
| M12 桌面壳与移动端暂不做 | 加分项，不影响主干学习目标 |

## 参考项目

本项目在架构上参考了一套下载来的政务演示系统（**只当参考书，绝不改动它**）：

- 位置：`D:\项目代码\-AI--main`
- 技术档案：[docs/reference/AI--main/项目技术清单.md](docs/reference/AI--main/项目技术清单.md)
- 裁剪与替换思路：[docs/reference/AI--main/改造与轻量化方案.md](docs/reference/AI--main/改造与轻量化方案.md)

主要借鉴的是它的**分层方式**（边界 / 协调者 / 领域 / 基础设施、端口抽象、组合根）、
**受限规则 DSL**、**幂等与乐观锁的写法**、**事件与审计只追加**，
以及**「没有来源就不回答」的溯源策略**。代码全部重写，不复制。

刻意不借鉴的：它的巨型文件（单个 `repositories.py` 3,589 行）、
它的重依赖（Milvus / etcd / MinIO / LibreOffice）、它 60 多个环境变量。

## 许可

个人学习项目，未声明开源许可。参考项目的许可以其自身仓库为准。
