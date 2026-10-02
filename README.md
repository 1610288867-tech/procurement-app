# 企业采购审批系统

一套**个人学习项目**：从零重写一个企业采购审批系统，用来把「动态多级审批引擎、预算并发预留、三单匹配」这三件事真正做一遍。后端 FastAPI 模块化单体（边界 / 用例 / 领域 / 基础设施四层），前端 React SPA，PostgreSQL 是唯一数据源。

> **这不是生产系统，也不打算上线。**
> 没有真实供应商、没有真实资金、没有真实发票。全部数据为本地合成数据，仅用于学习与演示。
> 目标平台：Windows 浏览器优先，之后才考虑独立窗口与 Android。

## 为什么要做这个

参考项目是一套功能很全的政务演示系统（FastAPI + Android + MCP + Milvus + MinIO），它的**架构值得学，但功能过多**——官方数字人 SDK 一项就占了仓库 95% 的体积，还拖着 Milvus、etcd、LibreOffice 三个重依赖。本项目把它**砍到只剩骨架**，换一个更小的业务领域重写，用最少的基础设施去练最核心的那几个工程问题：

| 练什么 | 在哪一阶段 |
| --- | --- |
| 按金额动态生成审批链（数据驱动，不是硬编码 if/else） | M5 |
| 预算的并发预留与释放（条件更新，不是先查再写） | M6 |
| 三单匹配（订单 / 收货 / 发票对账） | M8 |
| 幂等、乐观锁、事件流、只追加审计 | M4 起贯穿 |

## 仓库组成

| 目录 / 文件 | 作用 |
| --- | --- |
| `backend/` | FastAPI 四层骨架（边界 / 用例 / 领域 / 基础设施）、三个健康检查、pytest 分档、ruff、mypy |
| `frontend/` | React 18 + Vite + TypeScript + Tailwind v4 + TanStack Query；M0 是环境自检页 |
| `docs/` | M0 三份设计产物（ER 图、业务规则、金额-审批链对照表）＋ 规划文档五份 |
| `docker-compose.yml` | 薄封装，用 `include:` 引用仓库外的数据库编排（见下） |
| `PROGRESS.md` | **阶段验收清单与工作进度**（做完一项勾一项） |
| `Development progress.md` | **每个阶段结束后的四问总结** |
| `AGENTS.md` | **AI 改动守则与项目技术纪律**（人可直接改，AI 必须遵守） |

规划与设计文档：

| 文档 | 内容 |
| --- | --- |
| [docs/企业采购审批系统-实施计划.md](docs/企业采购审批系统-实施计划.md) | M0–M10 十一个阶段的详细步骤、验收、常见坑 |
| [docs/企业采购审批系统-领域设计.md](docs/企业采购审批系统-领域设计.md) | 角色、状态机、审批引擎、22 张表、API、核心模块参考实现 |
| [docs/新项目搭建方案-Windows优先.md](docs/新项目搭建方案-Windows优先.md) | 平台路线与技术栈选型理由 |
| [docs/AI--main-项目技术清单.md](docs/AI--main-项目技术清单.md) | 参考项目的技术档案 |
| [docs/AI--main-改造与轻量化方案.md](docs/AI--main-改造与轻量化方案.md) | 参考项目哪些该删、哪些该换 |

M0 的设计产物也在 `docs/`：[ER-图.md](docs/ER-图.md)、[业务规则.md](docs/业务规则.md)、
[金额-审批链对照表.md](docs/金额-审批链对照表.md)。

## 代码与容器是分开的

这是本项目一个刻意决定：**容器是容器，代码是代码**。数据库坏了不影响代码仓库，代码重写也不用动容器。

| | 位置 | 说明 |
| --- | --- | --- |
| 本项目代码 | `D:\procurement-app` | 就是本仓库 |
| 基础设施 | `D:\develop\procurement` | 只有 PostgreSQL 容器与数据 |
| 参考项目 | `D:\项目代码\-AI--main` | 只当参考书，**绝不改动** |

> `docker-compose.yml` 通过 `include:` 引用 **`../develop/procurement/docker-compose.yml`**（相对路径）。
> 本机解析为 `D:\develop\procurement\docker-compose.yml`。
> 换机器时只要保持「代码仓库与基础设施目录同级」就能直接用；布局不同就改那一行。

## 前置条件

Windows 11 + Docker Desktop（Linux 容器后端）+ Python 3.12 + Node.js ≥ 20。

| 项 | 版本 | 本机实测 |
| --- | --- | --- |
| Docker Desktop / Compose | 29.8.1 / v5.5.1 | ✅ 用户级安装，**引擎需手动启动** |
| PostgreSQL | 16.15 + pgvector 0.8.6 | ✅ 容器 `pgvector/pgvector:pg16` |
| Python | **3.12**（本机没有 3.13） | ✅ 3.12.2 |
| Node.js | ≥ 20 | ✅ v24.18.0 |
| PowerShell | 7（`pwsh`）推荐，**5.1 也能跑** | ✅ 7.6.6 / 5.1.22621 都实测通过。脚本的就绪探测特意写成两个版本通吃（5.1 没有 `-SkipHttpErrorCheck`，早先会假报"未就绪"） |

### 两个必须知道的坑

1. **Docker Hub 在本机不可达**（`registry-1.docker.io` 的 A/AAAA 都连不上，而 Docker 默认优先 IPv6）。
   已配镜像加速；检查是否生效：

   ```powershell
   docker info --format '{{json .RegistryConfig.Mirrors}}'
   ```

2. **pip 直连 pypi.org 会挂起**。已配清华镜像；若换机器需重配：

   ```powershell
   python -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
   ```

另外：**不要装 `uvicorn[standard]`**。它在 Windows 上要源码编译 uvloop（没有 wheel），实测让 pip 卡死 25 分钟以上且一个包都没装上。纯 `uvicorn` 完全够用。

## 初始化与启动

### 1. 起库

Docker Desktop 引擎必须先手动启动（本机刻意关掉了开机自启）。

```powershell
cd D:\develop\procurement
docker compose up -d
docker compose ps          # 等 STATUS 出现 (healthy)
```

也可以在本仓库目录里用薄封装，效果一样：

```powershell
cd D:\procurement-app
docker compose up -d
```

### 2. 起后端

```powershell
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
$env:PYTHONUTF8 = "1"                                        # 见下方说明
python -m pip install -r requirements.txt --only-binary=:all: # 见下方说明
uvicorn app.main:app --reload --port 8000
```

装过一次之后，也可以用脚本起（会自动检查虚拟环境和端口占用）：

```powershell
.\scripts\dev-up.ps1 -WithApi -WithFrontend   # 一条命令：Docker + 数据库 + 后端 + 前端
.\scripts\dev-up.ps1                           # 只起 Docker + 数据库
.\scripts\start.ps1                            # 只起前后端，并等到就绪
.\scripts\run-api.ps1                          # 只起后端
.\scripts\seed.ps1                             # 可选：灌入演示数据（幂等，可反复跑）
```

**不想开终端、只想双击？** 仓库自带一层 `.bat` 包装（它只转发给上面这些 `.ps1`，
启动逻辑仍然只有一份）：

| 双击这个 | 等于 |
| --- | --- |
| `启动项目.bat`（仓库根目录） | Docker + 数据库 + 前后端，并打开浏览器 |
| `scripts\启动-数据库.bat` | `dev-up.ps1` |
| `scripts\启动-前后端.bat` | `start.ps1` |
| `scripts\灌演示数据.bat` | `seed.ps1`（幂等） |
| `scripts\启动-后端.bat` / `启动-前端.bat` | `run-api.ps1` / `run-web.ps1`（单独调试用） |

`.bat` 会优先调用 `pwsh`；万一机器上只有 5.1，它会提示一句然后照样跑。
失败时窗口会停住不关，好让你把报错看完。

**四步启动顺序与常见问题**见 [浏览器测试-启动说明.txt](浏览器测试-启动说明.txt)。

两条命令行参数都不是多余的：

- `PYTHONUTF8=1` —— pip 在 Windows 默认用 GBK 读 requirements 文件，遇到中文注释会直接报
  `UnicodeDecodeError: 'gbk' codec can't decode byte ...`。
- `--only-binary=:all:` —— 强制只用预编译 wheel；不加它 pip 可能尝试源码编译并长时间挂起。

### 3. 起前端

```powershell
cd frontend
npm install
npm run dev
```

### 五个地址

| 地址 | 用途 |
| --- | --- |
| http://127.0.0.1:8000/docs | **Swagger 交互文档**，写前端之前先在这里验证接口 |
| http://127.0.0.1:8000/health/live | 存活检查（不碰数据库，永远 200） |
| http://127.0.0.1:8000/health/ready | 就绪检查（数据库连不上返回 503） |
| http://127.0.0.1:8000/health/db | 打印 PostgreSQL 与 pgvector 版本，排查环境用 |
| http://localhost:5173 | 前端页面（含环境自检面板） |

**前端走 Vite 代理，不用 CORS。** 浏览器请求 `/api/...` 与 `/health/...` 由 Vite 转发到 8000，
浏览器看到的始终是同源。好处是：不需要 CORS 配置、不需要 `credentials: "include"`、
M3 引入 httpOnly Cookie 时前端一行都不用改、M10 打包时也只需要换掉代理的 target。

## 目录结构

```
procurement-app/
├── backend/
│   ├── app/
│   │   ├── main.py             应用入口 + 三个健康检查 + request_id 中间件
│   │   ├── config.py           配置（只有 4 项，刻意的）
│   │   ├── database.py         异步引擎与会话
│   │   ├── errors.py           统一错误类型与响应体
│   │   ├── domain/             纯 Python 业务逻辑 —— 禁止 import 任何框架
│   │   │   ├── rules.py            审批策略的语义校验（M2）
│   │   │   └── permissions.py      数据级权限：按角色算可见范围（M3）
│   │   ├── application/        用例编排 —— 事务边界在这一层
│   │   │   ├── ports.py          端口（Protocol，不 import 框架）
│   │   │   └── coordinators/     一个用例域一个文件（catalog.py / auth.py）
│   │   ├── boundaries/         HTTP 边界 —— 只解析 / 校验 / 鉴权
│   │   │   ├── deps.py           当前用户、角色依赖（M3）
│   │   │   └── http/             一个分组一个文件（auth.py / catalog.py / admin.py）
│   │   └── infrastructure/     适配器与组合根 —— 一个聚合一个文件
│   │       ├── security.py       口令哈希（Argon2id）+ JWT 双令牌（M3）
│   │       ├── login_throttle.py 登录限流（进程内字典，不引 Redis）
│   │       ├── records/          ORM 映射（base.py / catalog.py / auth.py）
│   │       └── repositories/     数据访问（catalog_repo.py / user_repo.py）
│   ├── migrations/             Alembic（0001 suppliers / 0002 master data / 0003 users）
│   ├── tests/                  pytest（unit 默认档 / integration 分档）
│   ├── scripts/seed.py         演示数据（幂等，可反复跑）
│   ├── pyproject.toml          pytest 分档、ruff、mypy 配置
│   └── requirements.txt        唯一的依赖事实来源
├── frontend/
│   └── src/
│       ├── api/client.ts       统一 HTTP 客户端（同源，无 BASE）
│       ├── api/hooks/          TanStack Query hooks（useCatalog.ts / useAuth.ts）
│       ├── components/         跨页组件（RequireAuth.tsx 路由守卫）
│       ├── lib/money.ts        金额换算的唯一入口（元 ⇄ 分）
│       ├── pages/              页面（Home / Login / SupplierList / admin 三个管理页）
│       ├── router.tsx          路由表
│       └── App.tsx             外壳：QueryClient + Router + 布局
├── docs/
│   ├── ER-图.md                15 张表 + 三条关系约束
│   ├── 业务规则.md             8 条核心规则
│   └── 金额-审批链对照表.md    ★ 需求文档 = 测试用例 = 验收标准
├── docker-compose.yml          薄封装，指向仓库外的数据库编排
├── scripts/
│   ├── dev-up.ps1              一键拉起 Docker 引擎 + 数据库（可带 -WithApi -WithFrontend）
│   ├── start.ps1               启动前后端（各开一个新窗口，并等到就绪）
│   ├── run-api.ps1             启动后端（uvicorn --reload）
│   ├── run-web.ps1             启动前端（Vite）
│   ├── seed.ps1                灌入演示数据（部门 / 品类 / 供应商 / 预算 / 账号）
│   ├── 启动-*.bat 等           上面几个脚本的双击包装（只转发，不重复实现）
│   └── 浏览器自检.mjs          零依赖：起一个无窗口 Edge，把关键界面真跑一遍
├── 启动项目.bat                双击一键启动：Docker + 数据库 + 前后端 + 打开浏览器
├── 浏览器测试-启动说明.txt     四步启动顺序速查（含常见问题）
├── PROGRESS.md                 阶段验收清单与工作进度
├── Development progress.md     阶段四问总结
├── AGENTS.md                   AI 改动守则与项目技术纪律
└── .env.example                复制成 backend/.env（.env 不进 Git）
```

**四层的依赖方向是单向的**：边界 → 用例 → 领域；基础设施实现端口，不反向依赖。

| 层 | 允许做什么 | 禁止做什么 |
| --- | --- | --- |
| `domain/` | 纯函数、枚举、状态迁移表、规则求值 | import 任何框架 |
| `application/` | 编排用例、**决定事务边界**、调用领域函数 | 写 SQL、出现 Request/Response |
| `boundaries/` | 解析请求、校验输入、权限与幂等依赖 | 写业务规则、编排事务 |
| `infrastructure/` | ORM 映射、仓库、组合根、外部适配器 | 决定事务边界（只 `flush()`） |

## 阶段与进度

计划共 11 个阶段（M0–M10），约 13–14 周（按每周 10–15 小时估算）。

| 阶段 | 主题 | 状态 |
| --- | --- | --- |
| **M0** | 准备与设计定稿 | ✅ 完成（2026-10-01） |
| M1 | 骨架贯通（用「供应商」打通全链路） | ✅ 完成（2026-10-02） |
| M2 | 主数据（部门 / 品类 / 供应商 / 预算） | ✅ 完成（2026-10-02） |
| M3 | 账号与权限（多角色 + JWT + 数据级权限） | ✅ 完成（2026-10-02）※ |
| M4 | 采购申请（草稿 / 明细 / 提交 / 撤回） | ⬜ |
| **M5** | **审批引擎**（动态多级审批） | ⬜ ★ 核心 |
| **M6** | **预算并发预留** | ⬜ ★ 核心 |
| M7 | 附件与时间线 | ⬜ |
| M8 | 履约链（询价 → 定标 → 订单 → 收货 → 发票 → 付款） | ⬜ |
| M9 | AI 制度助手 | ⬜ 可不做 |
| M10 | 桌面壳与移动端 | ⬜ 可不做 |

**每个阶段的验收项见 [PROGRESS.md](PROGRESS.md)，只有全部通过才能进入下一阶段。**
阶段总结见 [Development progress.md](Development%20progress.md)。

> ※ M3 的 10 条验收里 7 条当场通过；另 3 条（换 id 取别人的申请 → 404、
> `PROCUREMENT` 跨部门可见性、GM 待办）要等 `purchase_requests` 表建出来才能真跑，
> 已明确记在 [PROGRESS.md](PROGRESS.md) 里，**没有当作通过**。

## 业务规则（M0 定稿，8 条）

1. **预算**提交时预留，驳回/撤回时释放，付款时按**实付**转已花并**显式释放差额**
2. **按金额分级审批**，规则存在品类上，**改规则不改代码**
3. 金额超比价线需至少 **3 家有效报价**才能定标
4. 只能用合格且未禁用的供应商（资质过期自动不可选）
5. 同一人同品类短期拆单要有警示（第一版只 warning）
6. **三单匹配**：超收 / 金额超容差 / 供应商不一致 → 拒绝付款并列出具体问题
7. **收货完成后才能付款**（状态机强制，不靠前端按钮）
8. 金额一律**整数「分」**，禁止 float

完整定义见 [docs/业务规则.md](docs/业务规则.md)。

### 金额 → 审批链（M5 的验收基准）

| 金额 | 应走的审批级 | 审批任务数 |
| --- | --- | --- |
| 500 元 | 部门主管 | 1 |
| 5,000 元 | 部门主管 → 部门负责人 | 2 |
| 50,000 元 | 部门主管 → 部门负责人 → 采购 → 财务 | 4 |
| 200,000 元 | 再加总经理 | 5 |

这四行同时是需求、测试用例和验收标准，详见
[docs/金额-审批链对照表.md](docs/金额-审批链对照表.md)。

## 权限矩阵（M3）

两套权限是**分开**的，缺一不可：

- **能不能用这个接口** → `boundaries/deps.py` 的 `require_roles(...)`，越权返回 **401**（没登录）或 **403**（角色不够）
- **能看到哪些行** → `domain/permissions.py` 算出的可见范围，**写进 `WHERE`**，查不到就是 **404**

角色可见范围：

| 角色 | 可见范围 | 典型用途 |
| --- | --- | --- |
| `REQUESTER` | 只看自己提的申请 | 我的申请、进度、驳回原因 |
| `DEPT_MANAGER` / `DEPT_HEAD` | 本部门（含子部门） | 待我审批、部门预算 |
| `PROCUREMENT` / `FINANCE` / `RECEIVER` / `GM` | **跨部门看全公司**（职责要求） | 询价、付款、收货、大额审批 |
| `ADMIN` | 全部（含配置） | 品类与审批规则、供应商准入、账号 |

接口级权限（当前已生效的部分）：

| 接口组 | 谁能用 |
| --- | --- |
| `GET /health/*`、`GET /api/v1/suppliers`、`GET /api/v1/departments`、`GET /api/v1/categories` | 任何人（公开只读） |
| `POST /api/v1/auth/login` | 任何人 |
| `POST /api/v1/auth/refresh`、`POST /api/v1/auth/logout`、`GET /api/v1/auth/me` | 登录用户 |
| `/api/v1/admin/*` | 仅 `ADMIN` |

演示账号（`scripts/seed.ps1` 生成，**8 个角色 / 7 个人**，`GM` 由 `ADMIN` 兼任）：

| 用户名 | 角色 | 部门 |
| --- | --- | --- |
| `admin` | `ADMIN` + `GM` | — |
| `requester` | `REQUESTER` | 行政部 |
| `dept-manager` | `DEPT_MANAGER` | 行政部 |
| `dept-head` | `DEPT_HEAD` | 行政部 |
| `procurement` | `PROCUREMENT` | 采购部 |
| `finance` | `FINANCE` | 财务部 |
| `receiver` | `RECEIVER` | 行政部 |

口令统一为 `Dev-Passw0rd!`。**仅本地合成数据**，换任何真实环境都必须改掉。

## 主要 API

M3 已完成认证与权限；其余业务接口按阶段逐个补齐。

| 分组 | 说明 | 认证 | 状态 |
| --- | --- | --- | --- |
| `GET /health/live`、`/health/ready`、`/health/db` | 存活 / 就绪 / 数据库版本 | 否 | ✅ M0 |
| `GET /api/v1/suppliers` | 合格供应商（只读，不含已停用） | 否 | ✅ M1 |
| `GET /api/v1/departments`、`/api/v1/categories`、`/api/v1/budgets/mine` | 主数据（只读；`/budgets/mine` 现由调用方带 `department_id`） | 否 | ✅ M2 |
| `/api/v1/admin/departments`、`/categories`、`/suppliers`、`/budgets` | 主数据写入与配置 | **`ADMIN`** | ✅ M3 |
| `POST /api/v1/auth/login` | 登录（下发 httpOnly Cookie） | 否 | ✅ M3 |
| `POST /api/v1/auth/refresh`、`/logout`、`GET /me` | 刷新、登出、当前账号 | 登录 | ✅ M3 |
| `/api/v1/requests/*` | 采购申请（草稿、明细、提交、撤回、时间线） | 群众 | M4 |
| `/api/v1/approvals/tasks/*` | 待办、认领、通过、驳回 | 审批角色 | M5 |
| `/api/v1/sourcing/*`、`/orders/*`、`/receipts/*`、`/payments/*` | 询价、定标、订单、收货、付款 | 按角色 | M8 |
| `/api/v1/chat/*` | 制度问答（流式） | 登录 | M9 |

认证用 **JWT 双令牌放 httpOnly Cookie**（access 15 分钟 / refresh 7 天，
refresh 的 `Path` 限定到 `/api/v1/auth/refresh`）；登录连续失败 5 次返回 **429**。
前端拿到 401 时会**自动续期一次并重放请求**（并发的多个 401 只续一次），
所以令牌过期不会把正在用的人踢回登录页。
写操作统一带 `Idempotency-Key`；并发编辑以响应中的 `version` 为准，冲突返回 **409**。
统一错误体固定为：

```json
{ "error": { "code": "...", "message": "...", "request_id": "..." } }
```

`request_id` 同时进响应头和错误体——出错时搜这 12 个字符就能还原整条请求链路。

## 验收与测试

测试刻意分成两档，让最常敲的命令永远秒回：

```powershell
cd backend
.\.venv\Scripts\Activate.ps1

pytest -m unit            # 纯函数，不连数据库，毫秒级（不带参数时的默认档）
pytest -m integration     # 需要 PostgreSQL 在跑
pytest -m ""              # 全量
```

质量检查：

```powershell
cd backend
ruff check app tests
mypy app --ignore-missing-imports

cd ..\frontend
npx tsc --noEmit
npx vitest run
npm run build
```

### 浏览器自检（渲染与交互）

上面那些检查看不到"页面渲染成什么样、点了会不会动"。这一条补上：

```powershell
# 前后端在跑的前提下执行（先 .\scripts\启动-前后端.bat）
node scripts\浏览器自检.mjs
```

它会自己起一个**无窗口** Edge（独立配置目录，不碰你自己开着的那份），连它的调试
接口，在真实浏览器内核里走一遍：路由守卫、按角色渲染的菜单、无权限提示页、
登录页的显示/隐藏密码开关、新建品类、审批策略 422 的原文、预算的元⇄分。
退出码 0 = 全过。

**零依赖**——只用 Node 自带的 `fetch` 和 `WebSocket`，不需要装 Playwright 之类的库。
注意 C 组会往开发库写一条 `SELFCHECK-*` 前缀的品类和一条预算（方便你认出来）。

### 三条分层自查（提交前跑，应全部无输出）

纪律只写在文档里会失效，所以这三条做成了可执行的检查：

```powershell
Select-String -Path backend\app\domain\*.py -Pattern "import (fastapi|sqlalchemy|redis|httpx|minio)"
Select-String -Path backend\app\**\*.py -Pattern "Mapped\[float\]|Float\("
Select-String -Path backend\app\infrastructure\repositories\*.py -Pattern "\.commit\(\)"
```

依次对应：领域层不许依赖框架、**不许有浮点列**、仓库层不许决定事务边界。

### 实测结果（最近一轮全量）

| 验收项 | 结果 |
| --- | --- |
| 数据库连通 | ✅ PostgreSQL 16.15 / pgvector 0.8.6 |
| `GET /health/live` / `/health/ready` / `/health/db` | ✅ 全部通过 |
| 前端 `localhost:5173` | ✅ HTTP 200，Vite 6.4.3 |
| Vite 代理 → 后端 | ✅ `/health/ready` 经 5173 返回 `database: up` |
| `pytest`（M3 收尾） | ✅ 88 passed（unit 50 + integration 38） |
| `tsc --noEmit` / `vitest run` / `npm run build` | ✅ 无错误 / 14 passed / 234.71 KB（gzip 73.86） |
| `node scripts\浏览器自检.mjs`（无窗口 Edge + CDP） | ✅ 11 项全过 |
| `ruff` / `mypy` | ✅ All checks passed / 29 files, no issues |
| 三条分层自查 | ✅ 全部无输出 |

## 停止与数据

```powershell
cd D:\develop\procurement
docker compose stop        # 停止但保留数据
```

**绝对不要用 `docker compose down -v`。** 本项目用 bind mount，
`down -v` 其实删不掉 `data\postgres`，但别养成这个习惯——换个项目就是数据全没了。

数据库数据落在 `D:\develop\procurement\data\postgres`，与代码仓库解耦，
所以它**不在 `.gitignore` 里，也不在仓库里**。

## 协作约定

本仓库有 AI 助手参与开发，因此对"改动边界"有明确约定，写在 [AGENTS.md](AGENTS.md)：

- 改动只允许落在**用户点名的对象**，以及**为完成它必须动用的工具**上；其余一律只提不做。
- 点名的改动做完即停，不做"顺手优化"和"进一步完善"。
- 顺带发现的问题写进「待你确认的改动」清单（标 P1/P2/P3），当场不改。
- 不得用推测补足需求；需要更多信息时先问清，或只做能确定的部分。

技术纪律（`domain/` 不依赖框架、仓库只 `flush()`、金额整数分、幂等、乐观锁条件更新、
事件只追加、附件 UUID 存盘、权限写进 `WHERE` 等十一条）同样在 [AGENTS.md](AGENTS.md)。

## 已知限制

| 限制 | 说明 |
| --- | --- |
| Docker Desktop 未设开机自启（**刻意**） | 每次开工要手动启动引擎；引擎起来后容器会自动重启 |
| `docker-compose.yml` 依赖目录布局 | `include:` 用相对路径 `../develop/procurement/`，要求代码仓库与基础设施目录同级；否则需改一行 |
| 受限沙箱会话访问不了 Docker 命名管道 | 属环境限制，非故障；本机终端正常 |
| M9 / M10 可永久不做 | AI 助手与桌面壳都是加分项，不影响主干学习目标 |

## 参考项目

本项目在架构上参考了一套下载来的政务演示系统（**只当参考书，绝不改动它**）：

- 位置：`D:\项目代码\-AI--main`
- 技术档案：[docs/AI--main-项目技术清单.md](docs/AI--main-项目技术清单.md)
- 裁剪与替换思路：[docs/AI--main-改造与轻量化方案.md](docs/AI--main-改造与轻量化方案.md)

主要借鉴的是它的**分层方式**（边界 / 协调者 / 领域 / 基础设施、端口抽象、组合根）、
**受限规则 DSL**、**幂等与乐观锁的写法**、**事件与审计只追加**，
以及**「没有来源就不回答」的溯源策略**。代码全部重写，不复制。

刻意不借鉴的：它的巨型文件（单个 `repositories.py` 3,589 行）、
它的重依赖（Milvus / etcd / MinIO / LibreOffice）、它 60 多个环境变量。

## 许可

个人学习项目，未声明开源许可。参考项目的许可以其自身仓库为准。
