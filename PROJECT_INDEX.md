# PROJECT_INDEX · 项目定位与"任务 → 文件"路由

> **用途**：需要定位时读这一份，不要靠翻目录。
> **本文件不复制专题正文** —— 只回答"这件事该去哪一份、哪一节"。
>
> 最后核实：2026-10-14（核实的对象是本仓库 `D:\procurement-app` 的文件与章节标题）

## 一、这是什么

**企业采购审批系统** —— 一套**个人学习项目**，不是生产系统、不打算上线。
从零重写一个企业采购审批系统，把三件事真正做一遍：
**动态多级审批引擎**（按金额生成审批链）、**预算并发预留与释放**、**三单匹配**（订单/收货/发票对账）。
后端 FastAPI 四层模块化单体，前端 React SPA，PostgreSQL 是唯一数据源。
完整定位与能力说明见 [README.md](README.md)。

## 二、按"我要做什么"找我该读哪份

| 我要…… | 读 | 具体章节 |
| --- | --- | --- |
| 接手项目，先知道有哪些规矩 | [AGENTS.md](AGENTS.md) | 全文（**尤其一、三、九**） |
| 知道"现在做到哪、下一步做什么" | [NOW.md](NOW.md) | 全文（很短） |
| 找某个功能/模块在哪个文件 | 本文件 | 下面第四、五节 |
| 跑起项目（起库/后端/前端/模型） | [RUNBOOK.md](RUNBOOK.md) | 一、初始化与启动 |
| 提交前要跑什么、跑不过怎么办 | [RUNBOOK.md](RUNBOOK.md) | 二、验证 |
| 查端口/容器/路径/依赖版本/环境坑 | [MAP.md](MAP.md) | 对应小节 |
| 查"当初为什么这么选" | [DECISIONS.md](DECISIONS.md) | 按主题找 |
| 查已知问题、风险、验证缺口 | [RISKS.md](RISKS.md) | 按类别找 |
| 查某个阶段的验收过了没有 | [PROGRESS.md](PROGRESS.md) | 顶部「进度总览」跳阶段 |
| 查某个阶段"学到了什么、为什么" | [Development progress.md](Development progress.md) | 阶段索引 |
| 追溯更早的过程细节 | [docs/history/README.md](docs/history/README.md) | 先搜索再读 |
| 懂业务规则/权限/数据模型 | 见下面第三节 | — |
| 做 AI 相关的事 | 见下面第三节 | — |
| 准备面试 / 写简历 | 见下面第三节 | — |

## 三、专题文档（按领域）

**业务与数据（权威来源）**

| 文档 | 内容 |
| --- | --- |
| [docs/业务规则.md](docs/业务规则.md) | 10 条核心规则（含职责分离与授权失败留痕） |
| [docs/金额-审批链对照表.md](docs/金额-审批链对照表.md) | 需求 = 测试用例 = 验收标准（四档金额对照） |
| [docs/ER-图.md](docs/ER-图.md) | 表结构与关系约束 |
| [docs/采购内控对照.md](docs/采购内控对照.md) | 外部依据（法规/Oracle/OWASP）↔ 本项目现状 ↔ 定稿口径 |
| [docs/domain/权限矩阵.md](docs/domain/权限矩阵.md) | 两层权限：接口级 `require_roles` + 数据级 `WHERE` |
| [docs/api/README.md](docs/api/README.md) | 全部 HTTP 接口与认证要求 |

**AI 模块**

| 文档 | 内容 |
| --- | --- |
| [docs/AI-评测baseline.md](docs/AI-评测baseline.md) | 评测方法、实测数字、阈值标定依据、诚实边界 |
| [docs/ai/AI-Agent岗-差距分析.md](docs/ai/AI-Agent岗-差距分析.md) | 岗位要求 ↔ 本项目覆盖度（含未做项） |
| [docs/ai/AI模块-可做清单.md](docs/ai/AI模块-可做清单.md) | AI 模块的成本/收益清单（哪些值得做） |

**求职材料**

| 文档 | 内容 |
| --- | --- |
| [docs/interview/面试.md](docs/interview/面试.md) | 面试题与答案（与本项目相关） |
| [docs/interview/面试素材.md](docs/interview/面试素材.md) | 项目可讲的细节与数字 |
| [docs/interview/简历-项目写法.md](docs/interview/简历-项目写法.md) | 这个项目该怎么写进简历 |

**规划基线（M0 定稿，不改）**

| 文档 | 内容 |
| --- | --- |
| [docs/企业采购审批系统-实施计划.md](docs/企业采购审批系统-实施计划.md) | M0–M12 各阶段步骤、验收、常见坑 |
| [docs/企业采购审批系统-领域设计.md](docs/企业采购审批系统-领域设计.md) | 角色、状态机、审批引擎、表、API、参考实现 |
| [docs/新项目搭建方案-Windows优先.md](docs/新项目搭建方案-Windows优先.md) | 平台路线与选型理由 |
| [docs/reference/AI--main/](docs/reference/AI--main/) | 参考项目的技术档案与裁剪方案（**别人项目的资料**） |

## 四、代码分工（改代码时去哪一层）

依赖方向**单向**：`boundaries/` → `application/` → `domain/`；`infrastructure/` 实现端口。

| 任务 | 去哪 | 不要去哪 |
| --- | --- | --- |
| 加/改一个 HTTP 接口 | `backend/app/boundaries/http/<域>.py` | 不要在边界里写业务规则 |
| 改用例流程、事务边界 | `backend/app/application/coordinators/<域>.py` | 不要在仓库里 `commit()` |
| 改业务规则/算法（纯逻辑） | `backend/app/domain/<模块>.py` | **`domain/` 不许 import 任何框架** |
| 改数据访问 | `backend/app/infrastructure/repositories/<域>_repo.py` | 只 `flush()`，不 `commit()` |
| 改表结构 | `backend/migrations/versions/` + `infrastructure/records/<域>.py` | **两者必须同一次改**（见 RISKS） |
| 加/改页面前端 | `frontend/src/pages/<页>.tsx` | 金额换算不要自己写，走 `lib/money.ts` |
| 改视觉风格 | `frontend/src/index.css` 的 `@theme` | 页面里不写死色号 |
| 加/改浏览器验收 | `scripts/*.mjs` | 参考 `scripts/浏览器自检.mjs` 的写法 |
| 加/改后端验收 | `backend/scripts/`（冒烟、探针、清理） | — |
| 加纯函数领域的逻辑，先写测试 | `backend/tests/test_<模块>.py` | 见 AGENTS 三.11 |

## 五、关键入口文件（改东西常要打开的几个）

| 文件 | 为什么关键 |
| --- | --- |
| `backend/app/main.py` | 应用入口、健康检查、`request_id` 中间件、全局异常处理器 |
| `backend/app/config.py` | **全部配置项的唯一来源** |
| `backend/app/errors.py` | 统一错误类型与响应体形状 |
| `backend/app/application/ports.py` | 端口（Protocol）—— 分层的关键接缝 |
| `backend/app/domain/permissions.py` | 数据级权限：按角色算可见范围（**范围必须进 `WHERE`**） |
| `backend/app/domain/approval_engine.py` | 审批链解析（M6 的核心纯函数） |
| `frontend/src/api/client.ts` | 统一 HTTP 客户端（401 续期重放、统一错误体） |
| `frontend/src/lib/money.ts` | 元 ⇄ 分的**唯一入口** |
| `scripts/回归验收.ps1` | 一条命令跑完 14 步回归（**不接受任何开关**） |

## 六、这份索引的维护规则

- **入口或职责变化时更新**；不复制专题正文（正文只在其权威文件里）
- 新增专题文档时，必须在这里加一行路由，否则等于没建
- 本文件与 [AGENTS.md](AGENTS.md) 第三节的"文档地图"是**同一份事实**：
  地图在那里给规则、路由在这里给细节
- **移动或改名文档后跑一次** `node scripts\检查文档链接.mjs`（只读巡检，
  相对链接按所在目录解析；`docs/history/` 默认跳过，原因见那份 README）
