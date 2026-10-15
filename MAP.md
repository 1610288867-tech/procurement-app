# MAP · 目录、版本、配置、部署位置与环境知识

> **用途**：定位"改哪个对象""跑在哪个端口""依赖什么版本""踩过哪个环境坑"时读这一份。
> **不虚构环境**：本文件里的路径、端口、版本都是**本机实测**，换机器要重新核。
>
> 最后核实：2026-10-14

## 一、位置关系（代码与容器**刻意分开**）

| 位置 | 是什么 | 可否改动 |
| --- | --- | --- |
| `D:\procurement-app\` | **本项目代码（本仓库）** | 可改（按 [AGENTS.md](AGENTS.md) 第一章授权） |
| `D:\develop\procurement\` | 基础设施：PostgreSQL 容器与数据 | 可改，但**属仓库外**，需说明 |
| `D:\项目代码\-AI--main\` | 参考项目（下载的政务演示） | **只读，绝不改动** |

**容器与代码故意分开**：数据库坏了不影响代码仓库，代码重写也不用动容器。
数据库数据落在 `D:\develop\procurement\data\postgres`，**不在仓库里、也不在 `.gitignore` 里**（它根本不在仓库）。

> ⚠️ `docker-compose.yml` 用 `include:` 引用了 `../develop/procurement/docker-compose.yml`（**相对路径**），
> 要求"代码仓库与基础设施目录同级"。**换机器或公开仓库时需要调整这一行** ——
> 这属于待确认项，[AGENTS.md](AGENTS.md) 明确 **AI 不得擅自改**。

## 二、端口

| 端口 | 谁在用 | 备注 |
| --- | --- | --- |
| **5432** | PostgreSQL（容器 `procurement-db`） | 代码与数据分离 |
| **8000** | 后端 uvicorn | 前端 Vite 代理指向它 |
| **5173** | 前端 Vite dev server | 浏览器访问入口 |
| **11434** | 本机 Ollama | 对话模型与 embedding |
| **9222** | Edge 调试端口 | **仅 `m6-页面实测.mjs` 需要外部已开好的** |
| 9332 / 9333 / 9334 | 脚本自起的无窗口 Edge | 分别属于 `浏览器自检` / `m11-知识库实测` / `窄屏自检` —— **不要同时跑两个** |

## 三、运行环境版本（实测）

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| PostgreSQL | **16.15** | 容器 `procurement-db`（镜像 `pgvector/pgvector:pg16`） |
| pgvector | **0.8.6** | 向量检索；HNSW + `vector_cosine_ops` |
| Python | 3.12 | 虚拟环境在 `backend/.venv` |
| Node.js | 见 `frontend/package.json` | 前端 |
| PowerShell | **7.6.6** | DSH 的 `dsh-ops` 插件随包提供（宿主自带的 5.x 不用于本项目的命令） |
| Ollama | 0.40.3 | 模型目录**项目内** `.ollama\models`（已 gitignore）。**安装目录里没有 `llama-gguf-split`** |
| Edge | 126.0.2592.87 | 浏览器验收脚本用（无窗口模式） |

**本机已装模型**（`ollama.ps1 -Status` 可复核）：

| 模型 | 体积 | 用途 |
| --- | --- | --- |
| `qwen2.5-7b-instruct` | 4.7 GB | **对话模型（当前）** —— 从 ModelScope 分片 GGUF 合并后导入，`num_ctx 8192` |
| `qwen2.5-3b-instruct` | 2.1 GB | 旧对话模型，保留备用（换回去只需改 `KNOWLEDGE_LLM_MODEL`） |
| `quentinz/bge-small-zh-v1.5` | 48 MB | embedding（512 维），制度检索用 |

> ⚠️ **`ollama pull` 在本机走不通**：到 `registry.ollama.ai` 实测 **50–210 KB/s**，
> 4.4 GB 的 7B 拉过 4 次全失败（最后一次失败后 Ollama 把 blob 与 `-partial` 一起清掉）。
> 可行路径是**从 ModelScope 下 GGUF**（~850 KB/s）再
> `.\scripts\导入GGUF.ps1`（它会在需要时先合并分片）。

**关键依赖钉版本**（`backend/requirements.txt` 是**唯一事实来源**）：

| 依赖 | 约束 | 为什么钉 |
| --- | --- | --- |
| `SQLAlchemy` | `>=2.0.36,<2.1` | 领域设计 A.4 的预算写法**按 2.0 实测过**；大版本漂移（曾装出 2.1.1）会让已验证的结论失效 |

## 四、配置（`backend/app/config.py` 是唯一来源）

**配置项共 14 个**（原则：默认保持个位数，每加一个先问"能不能不给它开关"）。

| 配置项 | 默认 | 用途 |
| --- | --- | --- |
| `DATABASE_URL` | 本机 compose 的 DSN | 异步引擎（`postgresql+asyncpg`） |
| `JWT_SECRET` | `dev-only-change-me` | 双令牌签名；**真实环境必须换** |
| `UPLOAD_DIR` | `uploads` | 附件与制度原文落盘根目录；**绝不要挂成静态目录** |
| `SOURCING_THRESHOLD_FEN` | `1000000`（1 万元） | **比价线**：金额 ≥ 此线要求至少 3 家有效报价 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `KNOWLEDGE_TOP_K` | `5` | 检索返回片段数 |
| `KNOWLEDGE_MIN_SCORE` | `0.50` | 检索相似度下限（**实测标定**，不是拍的） |
| `KNOWLEDGE_LLM_BASE_URL` / `_API_KEY` / `_MODEL` | 空 | 对话模型；**三项齐了才开启 Agent 路径** |
| `KNOWLEDGE_LLM_PRICES` | 空 | 模型价格表（元/百万 token）；空 = 成本记 `NULL` |
| `KNOWLEDGE_EMBEDDING_PROVIDER` | `ollama` | `ollama`（真模型）/ `local`（离线哈希兜底） |
| `KNOWLEDGE_EMBEDDING_BASE_URL` | `http://127.0.0.1:11434` | **与对话模型地址刻意分开**（一个可能在云端） |
| `KNOWLEDGE_EMBEDDING_MODEL` | `quentinz/bge-small-zh-v1.5` | 512 维中文检索模型 |

> ⚠️ **配置文件必须放在 `backend/.env`**：`Settings` 是 `env_file=".env"`，相对**工作目录**解析，
> 而后端的工作目录是 `backend/`。放仓库根**不会被读到，且不报错** ——
> 表现是"配了却没生效"。模板见仓库根 [.env.example](.env.example)（14 个键与 `config.py` 逐项对齐）。
>
> ⚠️ **环境变量与 `.env` 优先级相同**（谁后加载谁赢，而 `.env` 在后）——
> 所以测试里不能靠"不设变量"来规避 `.env`，必须**显式置空**。

## 五、目录职责（只列"改东西时要去的地方"）

```
backend/app/
├── main.py              应用入口 + 三个健康检查 + request_id 中间件 + 全局异常处理
├── config.py            配置（14 项）
├── errors.py            统一错误类型与响应体
├── domain/              纯 Python 业务逻辑 —— **禁止 import 任何框架**
├── application/         用例编排 —— **事务边界在这一层**
│   ├── ports.py         端口（Protocol）
│   └── coordinators/    一个用例域一个文件
├── boundaries/http/     一个分组一个文件（只解析 / 校验 / 鉴权）
└── infrastructure/      适配器与组合根
    ├── records/         ORM 映射
    └── repositories/    数据访问（只 flush）

frontend/src/
├── api/client.ts        统一 HTTP 客户端（401 续期重放、统一错误体）
├── api/hooks/           TanStack Query hooks
├── components/          跨页组件（ui.tsx 界面原语、两个时间轴）
├── lib/money.ts         元 ⇄ 分**唯一入口**；lib/sse.ts SSE 切帧
├── pages/               页面
├── index.css            设计令牌（Tailwind v4 @theme）—— 换风格只动这一个文件
└── App.tsx              外壳：侧边栏 + 粘性顶栏 + 内容区

scripts/                 PowerShell / Node 脚本（启动、回归、浏览器验收、模型管理）
docs/                    专题文档（领域、AI、面试、参考项目、历史归档）
resume/                  简历构建（交付物 + 构建脚本）
```

**四层的依赖方向是单向的**：`boundaries` → `application` → `domain`；`infrastructure` 实现端口，不反向依赖。

| 层 | 允许 | 禁止 |
| --- | --- | --- |
| `domain/` | 纯函数、枚举、状态迁移表、规则求值 | **import 任何框架** |
| `application/` | 编排用例、**决定事务边界**、调领域函数 | 写 SQL、出现 Request/Response |
| `boundaries/` | 解析请求、校验输入、权限与幂等依赖 | 写业务规则、编排事务 |
| `infrastructure/` | ORM 映射、仓库、组合根、外部适配器 | 决定事务边界（**只 `flush()`**） |

## 六、服务与启动入口（细节见 [RUNBOOK.md](RUNBOOK.md)）

| 想做 | 命令 |
| --- | --- |
| 一键：Docker + 数据库 + 前后端 + 打开浏览器 | `启动项目.bat`（或 `.\scripts\dev-up.ps1 -WithApi -WithFrontend`） |
| 只起数据库 | `.\scripts\dev-up.ps1` |
| 只起前后端 | `.\scripts\start.ps1` |
| 只起后端 / 前端 | `.\scripts\run-api.ps1` / `.\scripts\run-web.ps1` |
| 灌演示数据（幂等） | `.\scripts\seed.ps1` |
| 起/拉本地模型 | `.\scripts\ollama.ps1 -Serve` / `-Pull` |
| 停数据库（**保留数据**） | `cd D:\develop\procurement; docker compose stop` |

> **`.bat` 只是转发给同名 `.ps1`**，启动逻辑只有一份。
> **绝对不要用 `docker compose down -v`** —— 本项目用 bind mount，它其实删不掉 `data\postgres`，
> 但别养成这个习惯，换个项目就是数据全没了。

## 七、本机环境坑（会白白烧掉半天时间，**不要重复踩**）

| # | 坑 | 正确做法 |
| --- | --- | --- |
| 1 | `uvicorn[standard]` 在 Windows 要源码编译 uvloop，pip 挂死 25 分钟以上 | **只装纯 `uvicorn`**；热重载单独装 `watchfiles` |
| 2 | pip 可能尝试源码编译并长时间挂起 | 装包一律加 `--only-binary=:all:` |
| 3 | pip 默认用 GBK 读 requirements，遇中文注释报 `UnicodeDecodeError` | 先设 `$env:PYTHONUTF8 = "1"` |
| 4 | 直连 pypi.org 会挂起 | 已配清华源（`pip config set global.index-url …tuna…`） |
| 5 | Docker Hub 在本机不可达，且 Docker 默认优先 IPv6 | 已配镜像加速；排查 `docker info --format '{{json .RegistryConfig.Mirrors}}'` |
| 6 | Docker Desktop **刻意**未设开机自启 | 每次开工先手动启动引擎，容器会自动重启 |
| 7 | 受限沙箱会话访问不了 Docker 命名管道 | 属预期限制，**如实上报，不要绕过** |
| 8 | pip 被中断会留下 `pip-install-*` / `pip-unpack-*` 目录 | 正常会自清；`.gitignore` 已兜底。**不要在 pip 运行中杀进程** |
| 9 | `pip install` 可能"装完了却不退出"（进程挂着、CPU 缓涨、无输出） | 看 `site-packages` 时间戳确认已装上，再跑 `pip --version` 秒回即正常，**结束挂起进程，不要重装** |
| 10 | **PowerShell 的 `**` 不是递归通配**（`Select-String -Path app\**\*.py` 只扫深度 1–2） | 用 `Get-ChildItem -Recurse`。**这条曾让"三条分层自查"长期假通过** |
| 11 | **PowerShell 处理中文/反引号会破坏文件内容**（实测踩到：整段被吃掉、中文变乱码） | 改文本**用编辑工具**，不要用 PowerShell 的 `-replace` / 拼接写回 |
| 12 | `docker compose down -v` 会删数据卷 | **本项目禁止**；停库用 `stop` |
| 13 | 两个浏览器验收脚本不能同时跑 | 它们各占一个固定调试端口（9332/9333/9334），后启动的会去驱动前一个的浏览器 |
| 14 | 清理脚本不能与测试并发跑 | `testdata_cleanup.py` 会把集成测试正在用的数据删掉，造成**假失败** |
| 15 | **沙箱下 Python 只能写工作区**，写 `D:\develop\...` 一律 `PermissionError`（而 PowerShell 能写、`docker cp` 也能写 —— 后者由 Docker 服务端执行） | 需要"工作区外的大文件"时：让**容器或 docker cp** 去写。合并 4.36 GB 的 GGUF 就走"写到 `backend\.tmp\`（已 gitignore）" |
| 16 | **Python 脚本的 stdout 默认按控制台代码页编码**，脚本里的 `✓`/`✅`/`⚠️` 在 GBK 终端或**输出重定向**时报 `UnicodeEncodeError` | 见 [AGENTS.md](AGENTS.md) 第 6.0 节（**已踩三次**）：每个往终端打字的脚本开头显式 `reconfigure(encoding="utf-8")` |
| 17 | **`ollama pull` 在本机不可用**（50–210 KB/s，4.4 GB 拉 4 次全失败） | 走 ModelScope 下 GGUF + `.\scripts\导入GGUF.ps1`；**Ollama 不支持多文件 GGUF 导入**，分片要先合并 |

**四条 10 秒就能暴露问题的预检**：

```powershell
pip install --only-binary=:all: --dry-run fastapi                    # 暴露"需要源码编译"
docker info --format '{{json .RegistryConfig.Mirrors}}'              # 暴露"没配镜像"
$PSVersionTable.PSVersion                                            # 确认是 7.x（本项目命令按 7.x 验证）
curl.exe -s http://127.0.0.1:11434/api/tags                          # 暴露"Ollama 没起"
```

## 八、这份映射的维护规则

- **映射变化时更新**（新端口、新容器、版本升级、路径调整）；**不虚构环境**
- 本文件是"环境事实"的**唯一权威**：[AGENTS.md](AGENTS.md) 只留规则与指针，
  [README.md](README.md) 只留"第一次怎么跑起来"
