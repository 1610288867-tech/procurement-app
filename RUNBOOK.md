# RUNBOOK · 有依据的操作与验证步骤

> **用途**：执行对应操作前读这一份。它是**启动与验证流程的唯一权威** ——
> [README.md](README.md) 只留"第一次怎么跑起来"的摘要，[MAP.md](MAP.md) 只讲环境事实。
>
> ⚠️ **读法**：每条都标了验证状态。
> **`✅ 已执行验证`** = 本文档作者在本机实跑过；
> **`⚠️ 未执行`** = 命令写法有依据（脚本自身或官方文档），但本次未跑，第一次用请留意输出。
> **文档里的步骤不构成执行授权** —— 删数据、改环境这类操作仍按 [AGENTS.md](AGENTS.md) 第二章。
>
> 最后核实：2026-10-14

## 一、初始化与启动

### 1.1 最省事：一键（✅ 已执行验证）

```powershell
cd D:\procurement-app
.\scripts\dev-up.ps1 -WithApi -WithFrontend     # Docker + 数据库 + 后端 + 前端
```

**不包含**演示数据；需要时（新库第一次）单独跑 `.\scripts\seed.ps1`。

**只想双击**（`.bat` 只是转发给同名 `.ps1`，启动逻辑只有一份）：

| 双击 | 等于 |
| --- | --- |
| `启动项目.bat` | `dev-up.ps1 -WithApi -WithFrontend` + 打开浏览器 |
| `scripts\启动-数据库.bat` | `dev-up.ps1` |
| `scripts\灌演示数据.bat` | `seed.ps1` |
| `scripts\启动-前后端.bat` | `start.ps1` |
| `scripts\启动-后端.bat` / `启动-前端.bat` | `run-api.ps1` / `run-web.ps1` |

### 1.2 分步做（四步，✅ 已执行验证）

```powershell
cd D:\procurement-app

.\scripts\dev-up.ps1        # ① Docker 引擎 + PostgreSQL 容器，等 healthy 才返回
.\scripts\seed.ps1          # ② 演示数据（幂等，反复跑安全；新库建议跑一次）
.\scripts\start.ps1         # ③ 前后端各开一个新窗口；端口已有服务就跳过
                            #    地址：前端 5173 / 后端 8000
```

④ 浏览器打开 <http://localhost:5173>，登录：

| 用户名 | 角色 | 部门 |
| --- | --- | --- |
| `admin` | `ADMIN` + `GM` | —（**没有部门，所以看不到「我的申请」**） |
| `requester` | `REQUESTER` | 行政部 |
| `dept-manager` | `DEPT_MANAGER` | 行政部 |
| `dept-head` | `DEPT_HEAD` | 行政部 |
| `procurement` | `PROCUREMENT` | 采购部 |
| `finance` | `FINANCE` | 财务部 |
| `receiver` | `RECEIVER` | 行政部 |

口令统一 `Dev-Passw0rd!`（**仅本地合成数据**，换任何真实环境必须改）。
`start.ps1` 开的那两个窗口**不要关**，关掉就等于停服务。

### 1.3 只起一半（✅ 已执行验证）

```powershell
.\scripts\dev-up.ps1 -WithApi          # Docker + 数据库 + 后端
.\scripts\dev-up.ps1 -WithFrontend     # Docker + 数据库 + 前端
.\scripts\start.ps1 -ApiOnly           # 只后端
.\scripts\start.ps1 -WebOnly           # 只前端
```

分工：`dev-up.ps1` 管基础设施，`start.ps1` 管应用；`-WithApi/-WithFrontend` 只是转发给
`start.ps1`，**没有第二份实现**。

### 1.4 本地模型（AI 功能需要）（✅ 已执行验证）

```powershell
.\scripts\ollama.ps1 -Serve     # 一个窗口常驻：起服务（Ctrl+C 停止）
.\scripts\ollama.ps1 -Pull      # 另一个窗口：拉默认模型
.\scripts\ollama.ps1            # 看状态：模型目录 / 已装模型 / 该配什么
```

- 模型目录是**项目内** `.ollama\models`（已 gitignore）；本脚本只在**自己的进程里**设
  `OLLAMA_MODELS`，**不碰用户环境变量**。
- ⚠️ **两种启动方式不要同时用**：从开始菜单/托盘启动的 Ollama 读的是**用户级**环境变量，
  **不会**用项目内的模型目录。
- **不装它**：知识库上传与问答会**明确报"embedding 调用失败"**，不会静默退回哈希。

### 1.5 AI 配置（`backend/.env`）（✅ 已执行验证）

```ini
KNOWLEDGE_LLM_BASE_URL=http://127.0.0.1:11434/v1
KNOWLEDGE_LLM_API_KEY=ollama
KNOWLEDGE_LLM_MODEL=qwen2.5-3b-instruct
```

- **三项齐了才会开启 Agent（工具调用）路径**；缺任何一项都退回本地抽取式兜底，
  那时问业务数据问题只会得到"制度中没有找到相关规定"。
- **必须放在 `backend/.env`**（不是仓库根）——原因见 [MAP.md](MAP.md) 第四节。
- 改完**要重启后端**：运行中的实例不会重读 `.env`。
- 完整键表与本机实际值见仓库根 [.env.example](.env.example)。

### 1.6 换机器时的一次性步骤（⚠️ 未执行：本机 `.venv` 与 `node_modules` 已就绪）

```powershell
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
$env:PYTHONUTF8 = "1"                                        # 中文注释会撞 GBK 解码
python -m pip install -r requirements.txt --only-binary=:all: # 不加会尝试源码编译并挂死
alembic upgrade head

cd ..\frontend
npm install
```

## 二、验证（提交前必跑）

### 2.1 一条命令跑完（✅ 已执行验证，退出码 0）

```powershell
cd D:\procurement-app
.\scripts\回归验收.ps1
```

**14 步、不接受任何开关、退出码 0 = 全过**；失败会**停在那一步并打印原始输出**。

```
[1]  三条分层自查（应无任何输出）
[2]  后端全量 pytest -m ""（unit + integration）
[3]  后端静态检查 ruff + mypy
[4]  前端 tsc --noEmit
[5]  前端 vitest run
[6]  前端 npm run build（生产构建）
[7]  确保前后端在跑（不在就起，最多等 90 秒）
[8]  真实服务冒烟 m6_smoke.py（19 项，跑完自清理）
[9]  静默失败与接缝探针 regression_probes.py（21 项，跑完自清理）
[10] 浏览器自检 浏览器自检.mjs（17 项，无窗口 Edge）
[11] 知识库实测 m11-知识库实测.mjs（6 项，无窗口 Edge）
[12] 窄屏自检 窄屏自检.mjs（20 项，375×812）
[13] 知识库评测质量门禁（27 条 Golden QA，embedding 走 Ollama）
[14] 测试数据清理与残留回查
```

**跑之前先做两件事**（都踩过）：

| # | 事项 | 为什么 |
| --- | --- | --- |
| 1 | `backend\scripts\testdata_cleanup.py --scope all`（且**不要与测试并发跑**） | 并发跑会造成**假失败**（实测：清理把集成测试正在用的制度文档删了） |
| 2 | 起 Ollama：`.\scripts\ollama.ps1 -Serve` | 第 13 步依赖它；没起会**明确报 embedding 调用失败**，不会悄悄退回哈希 |

**不要同时跑两个用浏览器的脚本**：`浏览器自检` / `知识库实测` / `窄屏自检` 各占一个固定调试端口
（9332/9333/9334）。手动同时跑两个，后启动的会去驱动**前一个的浏览器**，
症状是"页面里看到的和我点的不一样"，且极难定位（实测踩到过一次）。

### 2.2 手工分步跑（✅ 每条都执行验证过）

```powershell
$env:PYTHONUTF8 = "1"
cd backend
.\.venv\Scripts\Activate.ps1
pytest -m unit                # 纯函数，不连数据库，毫秒级（不带参数的默认档）
pytest -m integration         # 需要 PostgreSQL 在跑
pytest -m ""                  # 全量：680 passed
ruff check app tests scripts  # scripts/ 里也有代码
mypy app --ignore-missing-imports

cd ..\frontend
npx tsc --noEmit
npx vitest run
npm run build
```

**不允许**：跳过后端测试只跑前端、或跳过集成档就宣布阶段完成。

### 2.3 三条分层自查（✅ 已执行验证，且带**自证探针**）

```powershell
$appDir = (Resolve-Path .\backend\app).Path

# ① domain 层不许 import 框架
Get-ChildItem "$appDir\domain" -Recurse -Filter *.py |
  Select-String -Pattern "import (fastapi|sqlalchemy|redis|httpx|minio)"

# ② 不许有**浮点金额列**（口径 = ORM 记录里的 Mapped[float，不是"出现 float 这个词"）
$allowedFloatColumns = @("support_rate", "top_score")
Get-ChildItem "$appDir\infrastructure\records" -Recurse -Filter *.py |
  Select-String -Pattern "^\s*(\w+):\s*Mapped\[float" |
  Where-Object { $n = ($_.Line -split ":")[0].Trim(); $n -notin $allowedFloatColumns }

# ③ 仓库层不许 commit
Get-ChildItem "$appDir\infrastructure\repositories" -Recurse -Filter *.py |
  Select-String -Pattern "\.commit\(\)"
```

三条**均应无任何输出**。

> ⚠️ **为什么必须用 `Get-ChildItem -Recurse`，不能用 `Select-String -Path app\**\*.py`**：
> PowerShell 的 `-Path` 只把 `**` 当**一层**通配、**不递归**。实测 `backend\app\**\*.py`
> 只扫到深度 1–2，`app\infrastructure\records\knowledge.py`（深度 3）**从来没被扫过** ——
> 而那里早就写了 `Mapped[float | None]`，自查却一直报"0 命中"：**自查一直是假通过**。
>
> ⚠️ **改自查逻辑时，"自证探针"必须一起保留**：`回归验收.ps1` 第 1 步每次都会造一个
> **必然违约的临时文件**，确认扫描器真的抓得到，抓不到就报错。
> 没有这一步，"0 命中"可能只是"什么都没扫到" —— 上面那条 bug 正是如此。

### 2.4 单独的验收脚本（✅ 已执行验证）

```powershell
cd D:\procurement-app
node scripts\浏览器自检.mjs          # 17 项；自起无窗口 Edge（9332），不碰你自己的 Edge
node scripts\m11-知识库实测.mjs      # 6 项；自起 Edge（9333）
node scripts\窄屏自检.mjs            # 20 项；自设视口 375×812（9334）
node scripts\m6-页面实测.mjs         # 51 项 + 截图；⚠️ 需外部已开好 9222 端口的 Edge

cd backend
.\.venv\Scripts\python.exe scripts\m6_smoke.py            # 19 项，对着真实服务打 HTTP
.\.venv\Scripts\python.exe scripts\regression_probes.py   # 21 项，静默失败与接缝
.\.venv\Scripts\python.exe scripts\knowledge_eval.py --gate  # 只看 AI 质量门禁
```

### 2.5 清理测试数据（✅ 已执行验证）

```powershell
cd backend
.\.venv\Scripts\python.exe scripts\testdata_cleanup.py --dry-run     # 先看会删什么
.\.venv\Scripts\python.exe scripts\testdata_cleanup.py               # 真删（默认只清页面实测那批）
.\.venv\Scripts\python.exe scripts\testdata_cleanup.py --scope all   # 连浏览器自检/探针那批一起清
```

判据是**写死的测试前缀**：申请标题以「M6 页面实测」开头，品类 `TCAT-PAGE-` / `SELFCHECK-` / `TCAT-`，
部门 `TDEP-`，账号 `t-`，知识库 `M11PAGE-` / `M11TEST-` / `EVAL-` / `PROBE%`。
**种子数据和你自己建的数据不在删除范围内。**

### 2.6 迁移（⚠️ 未执行：本次不改表结构，仅记录标准用法）

```powershell
cd backend
.\.venv\Scripts\alembic.exe upgrade head     # 当前 head：0e9f1c4a77b2
.\.venv\Scripts\alembic.exe heads            # 应只有一个 head
```

> ⚠️ **新增枚举值必须同时改三处**，漏一处会被包装成"数据库暂不可用"（完全指错方向）：
> Python 枚举、状态机转移表、**数据库 CHECK 约束**。
> Alembic autogenerate **看不见 CHECK 约束的内容变化** —— 至今已手工补过四次
> （`AWARDED` / `ORDERED`+`RECEIVED` / `INVOICED` / `PAID`+`CLOSED`）。

### 2.7 备份与恢复演练（✅ 已执行验证，2026-10-15）

```powershell
cd D:\procurement-app
.\scripts\备份数据库.ps1        # pg_dump 全量 + gzip → D:\develop\procurement\backups
.\scripts\恢复演练.ps1          # 真恢复进临时库 + 逐表核对行数 + 自动 DROP
```

**实测数字**（2026-10-15）：备份 **31.5 KB**；演练恢复 **26 张表**、
**12 张关键表行数全部一致**、**RTO 下界 4.1 秒**。

**为什么必须跑第二个脚本**：备份没恢复过就等于没有备份。
`恢复演练.ps1` 才回答"这份备份能不能用、要多久、数据对不对"。

| 判据 | 值 |
| --- | --- |
| 备份落盘目录 | `D:\develop\procurement\backups`（**仓库外**，默认留 14 份） |
| 临时库命名 | `{源库}_restore_check_{UTC 时间戳}`，**跑完自动 DROP**（失败也清理） |
| 拒绝条件 | `-BackupFile` 不存在、容器没跑、临时库名与源库相同 |
| 源库只读 | 演练对源库**只做 SELECT**（数行数）；所有写操作都在临时库上 |

> ⚠️ **本机两条实测约束**（换环境时会影响实现方式）：
> 1. **宿主机没有 `pg_dump` / `psql`** —— 只能用容器里的（`pg_dump 16.15`）。
> 2. **二进制不能走 PowerShell 管道**：`docker exec ... cat 二进制 | Set-Content -AsByteStream`
>    实测得到 **0 字节**。所以两个脚本都是"**容器内落盘 → `docker cp` 取出**"。

**这份演练不覆盖**：备份与本机同盘（本机坏了一起没）、WAL 归档与时间点恢复（PITR）。
后者见 [RISKS.md](RISKS.md) 第一节第 11 条。

## 三、出问题时先看这四条（✅ 均已验证）

| 现象 | 真因 | 处理 |
| --- | --- | --- |
| 页面「请求失败：500 —— 后端可能没启动」 | **后端没跑**。Vite 代理在后端不可达时返回的是**空响应体的 500**，不是连接错误，看起来像后端 bug | 跑 `.\scripts\start.ps1`。**判断口诀：看响应体长度是不是 0** |
| 页面「请求失败：503」/「数据库暂不可用」 | 后端在跑，但连不上数据库 | 跑 `.\scripts\dev-up.ps1` 拉起容器 |
| 端口被占用 | 脚本会直接告诉你被哪个 PID 占了（8000 后端 / 5173 前端） | `Stop-Process -Id <PID>` |
| 登录报"用户名或密码不正确"/"次数过多" | 账号是 `seed.ps1` 建的（确认跑过）；连错 5 次锁 5 分钟 | 等窗口过去即可；**限流在进程内存里，重启也会清空** |

**另一个高频现象**：评测门禁报"embedding 调用失败" → Ollama 没起，跑 `.\scripts\ollama.ps1 -Serve`。

## 四、收工（✅ 已执行验证）

```powershell
# 关掉 start.ps1 开的那两个窗口（或窗口里按 Ctrl+C）
cd D:\develop\procurement
docker compose stop        # 停库但保留数据
```

**绝对不要用 `docker compose down -v`** —— 本项目用 bind mount，它其实删不掉 `data\postgres`，
但换个项目就是数据全没了。

## 五、脚本清单

| 脚本 | 作用 |
| --- | --- |
| `scripts\dev-up.ps1` | Docker 引擎 + 数据库（可加 `-WithApi -WithFrontend`） |
| `scripts\start.ps1` | 前后端（`-ApiOnly` / `-WebOnly`） |
| `scripts\run-api.ps1` / `run-web.ps1` | 只起后端 / 只起前端 |
| `scripts\seed.ps1` | 演示数据（幂等） |
| `scripts\ollama.ps1` | 本地模型启停与拉取（`-Serve` / `-Pull` / `-Status`） |
| `scripts\回归验收.ps1` | **★ 一条命令 14 步回归，无开关，退出码 0 = 全过** |
| `scripts\备份数据库.ps1` | `pg_dump` 全量 + gzip + 三重校验（落仓库外，默认留 14 份） |
| `scripts\恢复演练.ps1` | 真恢复进临时库 + 逐表核对行数 + 自动 DROP（**备份没恢复过等于没有备份**） |
| `scripts\浏览器自检.mjs` | 17 项浏览器验收（自起 Edge） |
| `scripts\m6-页面实测.mjs` | 51 项 + 截图（需外部 9222 Edge） |
| `scripts\m11-知识库实测.mjs` | 6 项知识库全链路 |
| `scripts\窄屏自检.mjs` | 20 项，375×812 |
| `backend\scripts\m6_smoke.py` | 19 项真实服务 HTTP 冒烟 |
| `backend\scripts\regression_probes.py` | 20 项静默失败与接缝探针 |
| `backend\scripts\knowledge_eval.py` | 27 条 Golden QA 评测（`--gate` = 只跑门禁） |
| `backend\scripts\testdata_cleanup.py` | 清测试前缀数据（`--dry-run` 可选） |

## 六、这份手册的维护规则

- **方法变化时更新**；**未执行的命令必须标注**（不要让人以为它验证过）
- 启动流程**只在这里写一份** —— [README.md](README.md) 给摘要与指针，
  脚本注释只解释脚本自身
- 新增脚本时在第五节加一行；从第 2.1 节的回归里加/减步骤时，同步更新那份编号清单
