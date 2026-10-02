# Development progress · 阶段四问总结

> **用途**：每个阶段完成后回答四问。**验收核对与工作进度在 [PROGRESS.md](PROGRESS.md)**——
> 那份记「验收过了没有」，本份记「学到了什么、为什么这么做」。
> 工作区：`D:\procurement-app`　|　**最后更新：2026-10-02**
> 记录原则：**做完一项才写**；没做的不写"基本完成"。

## 四问模板（每阶段照抄）

1. **这个阶段实际学到了什么？**
2. **哪里卡了最久，为什么？**
3. **有没有留下"以后再说"的坑？**（区分"有意留的"和"真债"）
4. **为什么使用这样的技术栈，它比别的好在哪？**

> 写要点，不写散文。**回答要精炼简洁**——三个月后回头看这几行，比读代码本身有价值。

## 阶段索引

| 阶段 | 主题 | 状态 | 四问 |
| --- | --- | --- | --- |
| M0 | 准备与设计定稿 | ✅ 2026-10-01 | 已写（见下） |
| M1 | 骨架贯通 | ✅ 2026-10-02 | 已写（见下） |
| M2 | 主数据 | ✅ 2026-10-02 | 已写（见下） |
| M3 | 账号与权限 | ✅ 2026-10-02 | 已写（见下） |
| M4–M10 | 见 [PROGRESS.md](PROGRESS.md) | ⬜ | — |

---

## M0 · 准备与设计定稿（2026-10-01）

**一句话**：环境 100% 就绪，三份设计产物齐备，前后端骨架跑通。

### 1. 这个阶段实际学到了什么？

- **文档里的代码不跑一遍就不算验证过。** 领域设计 A.4 的预算写法看着专业，实跑直接抛
  `TypeError: Boolean value of this clause is not defined`——SQLAlchemy 把 `C >= amount`
  重载成了 SQL 子句，而不是 Python 布尔值。更危险的是"看起来很对"的补括号写法：守卫会**恒真**、
  `where()` 静默丢弃，**余额检查从未生效**。读代码学不到这个。
- **单侧都测过 ≠ 链路通。** 前端能开、后端能通，但 Vite 代理只转发了 `/api`、健康检查在 `/health`，
  中间那条线是断的。两侧单测全绿，只有端到端验证能发现。"部分正确"比完全失败更危险。
- **环境问题吃时间的能力和逻辑问题一样。** 四个坑（Docker Hub 不可达 / pip 挂死 / GBK 编码 /
  大版本漂移）没有一个是架构问题，却吃掉一半以上时间，其中三个的症状还是"看起来在正常工作"。
- **三个健康检查语义不同，是性价比最高的 20 行代码。** `/db` 那条后来救了场：它把"数据库到底通没通"
  从猜测变成一行输出。
- **纪律要写进代码文件本身，不能只写在文档里。** 四层包的 `__init__.py` 顶部写了"这一层禁止什么"，
  当天就逼出了 `app/__init__.py`（mypy 报 "Source file found twice"）。

### 2. 哪里卡了最久，为什么？

| 排名 | 卡点 | 耗时 | 根因 |
| --- | --- | --- | --- |
| 1 | Docker Desktop 引擎起不来 | ~40 min | 用户级安装 + `AutoStart=false` + 引擎被停过；缺现成诊断入口 |
| 2 | `uvicorn[standard]` 把 pip 挂死 | ~30 min | 要源码编译 uvloop，**而 Windows 没有 wheel**；表面像在工作，实际一个包都没装上 |
| 3 | Docker Hub 完全不可达 | ~20 min | IPv4 不通、IPv6 超时，而 Docker 默认优先 IPv6 |
| 4 | 自己的 Vite 代理漏了 `/health` | ~10 min | 端到端验证发现的——卡得值 |
| 5 | pip 用 GBK 读 requirements.txt | ~8 min | 报错明确，但发生在装完 SQLAlchemy 之后，一度以为前面白装了 |
| 6 | SQLAlchemy 装成 2.1.1 | ~5 min | 差点漏掉：A.4 的验证按 2.0 做的，大版本漂移会让结论失效 |

**共性：前四名全不是代码问题**，而是"工具在错误的环境假设下运行"。规划文档假设 Docker Hub 可达、
pip 能装 wheel、Docker 装在标准路径——**这三条在这台机器上全不成立**。

**重来一次能省 1.5 小时的两条 10 秒预检**：

1. 装任何 Python 包前先试 `--only-binary=:all:`，立刻暴露"需要源码编译"
2. 装任何镜像前先跑 `docker info --format '{{json .RegistryConfig.Mirrors}}'`，立刻暴露"没配镜像"

### 3. 有没有留下"以后再说"的坑？

**A. 有意留的**（有明确回归阶段，不算债）

| 坑 | 为什么现在不做 | 回归阶段 |
| --- | --- | --- |
| Alembic 还没 `init` | 没有模型，只能生成空迁移 | M1 第 4 步 |
| 前端没有路由 | 只有一页时引入 router 是空转 | M1 第 7 步 |
| 领域层 10 个业务文件全空 | 顺序是"先纯函数测绿再动库" | M2 起 |
| 没有 `integration` 档用例 | 需要先有表 | M1 起 |

**B. 真债**（该做但没做）

| # | 坑 | 风险 | 补的时机 |
| --- | --- | --- | --- |
| 1 | 只在 SQLite 上验证过"列表达式不能被布尔化"，没在真实 PostgreSQL 上跑过并发预留 | SQLite 没有 MVCC 行锁语义，**并发行为 M6 才第一次真验证** | M6 |
| 2 | Vite 6 + React 18 没做生产构建验证（只跑了 dev + vitest） | `npm run build` 可能有类型或打包问题 | M1 收尾 |
| 3 | `uvicorn --reload` 能否热重载没试过（没装 `watchfiles`） | 影响开发手感 | M1 开工时试 |
| 4 | 前端没有 eslint（只有 tsc） | 少一层检查，tsc 覆盖大部分 | 需要时再加 |
| 5 | pip 缓存 6.9 GB | 占磁盘 | 待定 |

> **已清完的一项**：领域设计第二轮修正（GM 角色、`payments.status` 值域、三单匹配粒度、
> A.5 粒度说明）已于 **2026-10-02 同步**，不再是"信息污染"型债务。
> 外层 compose 的 `include:` 也已改为相对路径并验证可解析。

### 4. 为什么使用这样的技术栈，它比别的好在哪？

**一句话：每一处选型都在「减少活动部件」，而不是「用更新的东西」。**

| 选型 | 替代方案 | 为什么选它 |
| --- | --- | --- |
| **FastAPI 四层**（边界/用例/领域/基础设施） | Django / Flask 一把梭 | 领域层是纯函数，毫秒级可测；换框架换库时 `domain/` 一行不改。本项目最难的几件事（审批引擎、预算策略、三单匹配）**天然是纯逻辑** |
| **纯 `uvicorn`** | `uvicorn[standard]` | 后者要源码编译 uvloop，**Windows 没有 wheel**，实测让 pip 卡死 25 分钟 |
| **`requirements.txt`**（pip freeze） | Poetry / uv / PDM | 依赖是单一事实来源；少一层工具就少一处出错点 |
| **PostgreSQL + pgvector** | MySQL / SQLite / Milvus | 一条 Docker 命令得到"关系库 + 向量库"；**pgvector 替掉 Milvus + etcd，省约 3 GB 内存和两个容器**。M9 前用不到向量，但镜像先用 `pgvector/pgvector:pg16`，将来零迁移 |
| **SQLAlchemy 钉 `>=2.0.36,<2.1`** | 追最新（默认装出 2.1.1） | A.4 的预算写法**按 2.0 实测过**，大版本漂移会让已验证的结论失效 |
| **暂不引 Redis** | 一上来就上缓存 | M0–M4 用不到，额度计数用 PG 就够。**少一个活动部件，就少一处"为什么不通"** |
| **Vite 代理，不用 CORS** | 后端加 `CORSMiddleware` | 同源，不需要 credentials 和预检；M3 上 httpOnly Cookie 时前端一行不改；排错更直白——代理直接报 `ECONNREFUSED` |
| **React + Vite + TS + TanStack Query** | Vue / 原生 JS / 手写 fetch | 就业价值最高；`invalidateQueries` 让"新增后刷新列表"永远不用手写 |
| **Tailwind v4（Vite 插件）** | 手写 CSS / Tailwind v3 | 只需一行 `@import "tailwindcss"`，**没有 config 文件** |
| **`lib/money.ts` 单独一个文件** | 各页面各转各的 | 元⇄分写错 1000 倍是这类系统的经典事故；全项目**只允许这一个文件碰金额单位** |
| **配置项只有 4 个** | 原项目 60 多个环境变量 | 每加一项先问"能不能不给它开关" |
| **pytest 分档**（`-m unit` 为默认档） | 单一档 | 最常敲的 `pytest` 永远毫秒级，测试才不会被跳过 |
| **三条分层自查做成命令** | 只写在文档里 | 写成 `Select-String` 才会当天见效——M0 当天就靠它逼出了 `app/__init__.py` |

---

## M1 · 骨架贯通（2026-10-02）

**一句话**：用「供应商」把 表 → ORM → 仓库 → 协调者 → 路由 → 页面 这条链路完整走通了一次。

### 1. 这个阶段实际学到了什么？

- **容器名 ≠ compose 项目名。** 薄封装在仓库根目录，Compose 会把项目名推导成 `procurement-app`，与基础设施的 `procurement` 是两个项目；而 `container_name: procurement-db` 是写死的，于是 `up -d` 报 "container name is already in use"。修法是给薄封装加 `name: procurement`，让两者指向同一个项目。
- **模块级引擎 + 每个用例一个事件循环 = `Event loop is closed`。** `create_async_engine` 的连接池绑定"创建它时的事件循环"，pytest-asyncio 默认每个用例新建循环，于是第二个用例起就炸。用会话级循环（`asyncio_default_test_loop_scope = "session"`）解决。
- **协议匹配时 `list` 不协变。** `list[SupplierRecord]` 不满足 `list[SupplierView]`，mypy 直接报冲突；换成 `Sequence`（协变）就通了。
- **`= Depends(...)` 是旧写法。** ruff 的 B008 会拦，`Annotated[AsyncSession, Depends(get_session)]` 才是现在的推荐写法，而且类型标注和依赖声明合一。
- **新版 FastAPI 里，include 进来的 router 在 `app.routes` 中是 `_IncludedRouter` 包装对象**，不再展平。我一度以为路由没挂上，其实挂了——正确的验证方式是 `app.openapi()["paths"]`。
- **迁移 docstring 的首行会进 Alembic 日志**，中文在 GBK 控制台会乱码，首行留 ASCII。
- **"数据库不可用"抛的不一定是 `SQLAlchemyError`。** 实测：asyncpg 在**连接阶段**失败（端口没人监听）时，
  SQLAlchemy 抛的是原始的 `ConnectionRefusedError`——它是 `OSError` 的子类，**不是** `SQLAlchemyError`。
  只接后者的话，数据库没起来时仍然是裸 500。所以 503 处理器必须同时接 `OSError`。
- **"后端没起来"在前端看起来是 500，不是连接错误。** Vite 代理在后端不可达时返回
  **空响应体的 500**（`content-length: 0`），页面上只显示"请求失败：500"，
  把排查方向误导向后端 bug。诊断口诀：**看 body 长度是不是 0**。

### 2. 哪里卡了最久，为什么？

| 排名 | 卡点 | 耗时 | 根因 |
| --- | --- | --- | --- |
| 1 | integration 测试全挂 `Event loop is closed` | ~15 min | asyncio 循环作用域问题；报错栈指向 asyncpg 关闭连接，离根因隔了三层 |
| 2 | `docker compose up` 撞名 | ~10 min | 得先搞明白 Compose 的项目名是怎么推导的，以及它与 `container_name` 的关系 |
| 3 | ruff B008 / mypy 协议冲突 | ~10 min | 都是"框架的最佳实践变了"，不是我写错逻辑 |
| 4 | vitest 在受限沙箱里 `EPERM` | ~5 min | 环境限制（要写临时文件），非代码问题 |
| 5 | 页面报 500 排查 | ~10 min | 表面像后端 bug，实际是**后端没启动**；Vite 代理把连接失败翻译成了空的 500 |

### 3. 有没有留下"以后再说"的坑？

**A. 有意留的**

| 坑 | 为什么现在不做 | 回归阶段 |
| --- | --- | --- |
| 没有停用/删除接口 | 停用需要 `disabled_reason` 留痕 | M2 |
| 列表没有分页 | 现在只有几条数据 | M4 |

**B. 真债**（本轮全部清零）

| # | 坑 | 处理 |
| --- | --- | --- |
| 1 | 422（校验失败）的错误体还是 FastAPI 默认 `{"detail": …}`，没统一 | ✅ 加 `RequestValidationError` 处理器，并补了一条契约测试 |
| 2 | `requirements.lock.txt` 写 `SQLAlchemy==2.1.1`，实装是 **2.0.54** | ✅ 已用 `pip freeze` 刷新 |
| 3 | `backend/` 下 3 个 pip 残留临时目录 | ✅ 已删除 |
| 4 | `domain/__init__.py` 的 docstring 里**举例写了完整 import 语句**，让三条分层自查的第一条永远命中自己 | ✅ 已改写成不含完整语句的写法 |
| 5 | `frontend/tsconfig.tsbuildinfo` 未被 `.gitignore` 忽略 | ✅ 已加 `*.tsbuildinfo` |
| 6 | 数据库连不上时返回裸 500（本该是 503） | ✅ 已加 `SQLAlchemyError` + `OSError` 处理器，并用"指向死端口的实例"实测通过 |
| 7 | 未捕获异常没有日志，`request_id` 串不起来 | ✅ 已加兜底处理器，带 `request_id` 记日志 |
| 8 | `backend/migrations/README`（Alembic 生成的英文模板） | ✅ 已删除 |
| 9 | 开工要记三条启动命令，容易漏掉后端 → 页面报 500 | ✅ 新增 `scripts/run-api.ps1` / `run-web.ps1`，`dev-up.ps1` 加 `-WithApi -WithFrontend` |
| 10 | `uvicorn --reload` 没装 `watchfiles`，改代码不生效 | ✅ 已装并写进 `requirements.txt`（实测日志出现 `Started reloader process [...] using WatchFiles`） |

> **本轮新发现并当场修掉的**：后端不可达时，**Vite 代理返回的是"空响应体的 500"**，
> 前端只能显示"请求失败：500"，把排查方向引向后端 bug（实际是 uvicorn 没启动）。
> 已在 `client.ts` 特判 5xx 且无 `error` 体的情形，明确提示"后端可能没启动"。
>
> 同一次排查还发现：**数据库不可用时抛的是 `OSError` 而不是 `SQLAlchemyError`**，
> 只接后者接不住。已改成两个都接，并用"指向死端口的实例"验证返回 503。
>
> **pip 的一个复现规律**：`pip install` 被中途终止后，会在**当前目录**留下
> `pip-install-*` / `pip-unpack-*` 等临时目录（里面有下载好的 `.whl`）。
> 这解释了之前那 3 个残留目录的来源，也说明"不要在 pip 运行时随手杀进程"。
> 已在 `.gitignore` 加 `pip-*/` 兜底。

> **已清一项**：M0 真债 #2（Vite 生产构建未验证）—— `npm run build` 已通过（213 KB / gzip 68 KB）。

### 4. 为什么使用这样的技术栈，它比别的好在哪？

| 选型 | 替代方案 | 为什么选它 |
| --- | --- | --- |
| **`Annotated[AsyncSession, Depends(...)]`** | `session: AsyncSession = Depends(...)` | 不触发 ruff B008；类型标注与依赖声明合一，是 FastAPI 现在的推荐写法 |
| **`Protocol` 端口**（`application/ports.py`） | 让协调者直接 import `CatalogRepository` | `application/` 不依赖 SQLAlchemy，换 ORM 这层不用动；实现方**在结构上**满足协议即可，不需要额外适配器 |
| **`Sequence` 而非 `list`**（端口返回值） | `list` | `list` 不协变，`list[SupplierRecord]` 不满足 `list[SupplierView]`；`Sequence` 协变，类型检查能过 |
| **仓库 `flush()` + 协调者 `commit()`** | 路由里直接 commit | 事务边界集中在一处；一次用例里的多个仓库操作能进同一个事务 |
| **Vite 代理**（继承 M0） | 后端加 CORS | 同源，M3 上 httpOnly Cookie 时前端零改动 |
| **TanStack Query 的 `invalidateQueries`** | 手写"新增后刷新列表" | 缓存失效交给库，页面代码少一半 |
| **测试会话级事件循环** | 每个用例一个循环 | 模块级引擎的连接池要求整个会话共用一个循环 |

---

## M2 · 主数据（2026-10-02）

**一句话**：四类主数据（部门 / 品类 / 供应商 / 预算）+ 三层校验的前两层 + 三个管理页 + 演示数据脚本。

### 1. 这个阶段实际学到了什么？

- **"结构校验"和"语义校验"是两件事，后者才是关键。** Pydantic 只能挡"形状不对"（`role: "BOSS"`）。
  而 `{"gte": ["amount", 100_000]}` 形状完全合法，却会让整条规则**静默失效**——求值器对"缺字段"
  的语义是**返回 False（不满足）**，于是那一级审批不生效，申请**以更低的层级被批准**。
  这类错误不抛异常，只能靠专门的语义校验器拦（路径白名单 / 整数分 / 零阈值兜底 / order 唯一）。
- **写入时把默认值补齐**，M5 的求值器就不用再猜默认值。代价是存进去的策略比手填的多几个字段
  （`mode: "SINGLE"`、`skip_when_framework_contract: false`），换来的是"读出来即可直接用"。
- **mypy 抓到了 `Result.rowcount` 不存在**：SQLAlchemy 异步 `Result` 的类型标注里没有它。
  改用 `.returning(id)` 既类型干净又少一次查询 —— 这个写法 M6 的预算预留会直接沿用。
- **规格会改变既有接口的语义。** M2 把"创建供应商"从 `/api/v1/suppliers` 移到
  `/api/v1/admin/suppliers`，M1 的测试立刻 405。读规格时要连"旧接口怎么办"一起看，
  否则第一反应会是"接口坏了"。
- **同一个坑会踩第二次**：Python 脚本的中文输出在 Windows 上默认走 cp936，终端按 UTF-8 解码就是乱码。
  上次是 Alembic 的迁移消息，这次是 seed 脚本。`$env:PYTHONUTF8 = '1'` 解决。

### 2. 哪里卡了最久，为什么？

| 排名 | 卡点 | 耗时 | 根因 |
| --- | --- | --- | --- |
| 1 | Docker 引擎又停了 | ~15 min | 上次修好后又被关掉。这次直接用 `dev-up.ps1` 一条命令拉起（**14 秒**），脚本的价值当场体现 |
| 2 | M1 的测试集体 405 | ~10 min | M2 把创建移到 admin 路径 —— 是规格变更，不是接口坏了 |
| 3 | mypy 报 `Result.rowcount` 不存在 | ~8 min | SQLAlchemy 异步 Result 标注不全；用 `RETURNING` 绕开比 `type: ignore` 干净 |
| 4 | seed 脚本中文乱码 | ~5 min | Python 输出编码与终端解码不一致 |

### 3. 有没有留下"以后再说"的坑？

**A. 有意留的**

| 坑 | 为什么现在不做 | 回归阶段 |
| --- | --- | --- |
| 没有校验 `form_schema` 的语义 | 只校验了 `approval_policy`；动态表单是 M4 的事 | M4 |
| 品类没有停用接口 | 停用要和权限一起做才有意义 | M3 |
| `/budgets/mine` 还靠调用方传 `department_id` | 还没有登录态 | M3 |

**B. 真债**

| # | 坑 | 风险 | 补的时机 |
| --- | --- | --- | --- |
| 1 | **M2 的 admin 接口完全没有权限校验** | 任何人（含匿名）都能改主数据 | M3（规格里就是 M3） |
| 2 | 三个管理页只过了 `tsc` / `vitest` / `build`，**没做浏览器验证** | 交互细节可能有问题 | 你点一遍 |
| 3 | `backend/` 下还有 pip 残留目录 | 已在 `.gitignore` 兜底，不会进 Git | 你手动删 |

### 4. 为什么使用这样的技术栈，它比别的好在哪？

| 选型 | 替代方案 | 为什么选它 |
| --- | --- | --- |
| **JSONB 存策略与表单** | TEXT | 将来能用 PostgreSQL 的 JSON 查询（M5 要按策略反查） |
| **三层校验**（Pydantic / domain / M5 可达性） | 全塞进 Pydantic 或全塞进 domain | 每层只负责自己能负责的：形状 / 语义 / 可达性。混在一起要么太松、要么重复 |
| **校验器收 `Mapping`，不收 Pydantic 模型** | 直接收 `ApprovalPolicyIn` | `domain/` 因此不依赖 Pydantic；M5 的求值器和它共用同一份遍历代码（`collect_paths`） |
| **先查部门/品类是否存在，再插预算** | 直接插、靠外键报错 | 能给出"部门不存在"这种**可自救**的提示；真正的并发冲突仍交给唯一约束 |
| **`RETURNING` 而不是 `rowcount`** | `result.rowcount` | 类型干净 + 少一次查询；M6 的条件更新预留会沿用 |
| **一个端口 `CatalogRepositoryPort`** | 拆成四个小端口 | 它们本来就是同一组聚合、同一个仓库类；拆开只会让协调者的构造函数变长 |
| **种子脚本用 ORM 不用 SQL** | 手写 INSERT | 迁移一改种子跟着改，不会悄悄失效 |
| **种子里"已存在就跳过"** | 先删后插 | 反复跑安全，也不会覆盖手工改过的审批策略 |

---

## M3 · 账号与权限（2026-10-02）

**一句话**：8 个角色能登录（7 个人，GM 由 ADMIN 兼任），每个接口都有明确的权限边界，
数据级权限的判定函数写好并测绿，等 M4 把它挂进真实查询。

### 1. 这个阶段实际学到了什么？

- **"能不能用这个接口"和"能看到哪些行"是两套权限，后者才容易出事。**
  角色依赖只挡住"谁来敲门"；数据级权限决定"进门后能翻哪几个抽屉"。
  而且漏掉后者**不会报错**，只会安静地把别人的数据查出来 —— 所以它必须做成
  能直接塞进 `WHERE` 的条件函数，而不是"先查出来再判断能不能看"。
- **Cookie 的 `Path` 是一道免费的安全边界。** 把 refresh 限定到
  `/api/v1/auth/refresh` 一个路径，它就再也不会出现在别的请求里，泄露面当场变小，
  而代价只是 `set_cookie` 上一行参数。
- **`token_version` 一个整数字段替掉了两张表。** 冻结账号要"旧令牌立即失效"，
  直觉方案是黑名单 + refresh rotation；实际把版本号写进令牌载荷、每次比对即可。
  **同一件事不要叠两套机制**，否则调试时要同时怀疑两处。
- **库的时间校验会跟测试的注入时间打架。** PyJWT 内部读真实时钟，
  测试里"现在是几点"就注入不进去；关掉它的 `exp`/`iat` 校验、拿自己的时钟算一次，
  "已过期"才能在毫秒内被构造出来。
- **加权限会让既有测试集体变红，这是规格变更而不是测试坏了。**
  M2 踩过一次（接口搬家 405），M3 又踩一次（接口上锁 401）。
  第一反应应该是"读规格"，不是"回滚改动"。
- **登录失败的响应必须一模一样。** 区分"用户不存在"和"密码错误"，
  等于免费提供一个账号枚举接口。

### 2. 哪里卡了最久，为什么？

| 排名 | 卡点 | 耗时 | 根因 |
| --- | --- | --- | --- |
| 1 | `pip install` 装完不退出 | ~10 min | 包 5 秒就装好了（site-packages 时间戳为证），进程却一直挂着、CPU 缓慢上涨；`pip --version` 却秒回。属工具自身的偶发挂起，与代码无关 |
| 2 | PyJWT 拒绝"未来签发的令牌" | ~5 min | 测试注入的 `NOW` 晚于真实时间，`iat` 成了"未来"；改成自己做时间校验 |
| 3 | M1/M2 老测试集体 401 | ~5 min | admin 接口加了 `ADMIN` 依赖 —— 规格变更 |

### 3. 有没有留下"以后再说"的坑？

**A. 有意留的**

| 坑 | 为什么现在不做 | 回归阶段 |
| --- | --- | --- |
| 换 id 取别人申请 → 404、跨部门可见性 | 要 `purchase_requests` 表 | M4（建表后第一条就测） |
| GM 待办 | 要 M5 的审批任务表 | M5 |
| 写操作强制 `Idempotency-Key`（兼 CSRF 第三道防线） | 幂等机制本来就是 M4 的 | M4 |
| `secure=False`（Cookie） | 本地是 http，设 True 浏览器直接不发 | 上 HTTPS 时 |

**B. 真债**

| # | 坑 | 风险 | 补的时机 |
| --- | --- | --- | --- |
| 1 | 前端守卫与登录页只过了 `tsc` / `build` | 交互细节可能有问题 | 本轮浏览器验证 |
| 2 | 前端没有 token 自动续期（access 15 分钟过期后要手动重新登录） | 用久了会被踢回登录页 | 有需要再说 |

> **M2 真债的现状**：三个管理页的浏览器验证在本轮 M3 一起做了（见下）；
> `backend/` 下 5 个 `pip-*` 残留目录仍在（属删除操作，留给你确认），
> 已在 `.gitignore` 兜底，不会进 Git。

### 4. 为什么使用这样的技术栈，它比别的好在哪？

| 选型 | 替代方案 | 为什么选它 |
| --- | --- | --- |
| **Argon2id**（argon2-cffi） | MD5 / SHA256 | 加盐 + 可调慢，抗彩虹表和暴力破解；校验失败一律返回 False，不把异常抛给登录接口 |
| **httpOnly Cookie** | localStorage + `Authorization` 头 | JS 读不到令牌，XSS 能拿走的东西少一半；Vite 同源代理让前端一行不用改 |
| **`token_version`** | 黑名单表 / refresh rotation | 一个整数字段替掉两张表和两套机制，冻结即失效 |
| **refresh Cookie 限定 `Path`** | 全站可见 | 一行参数换一个更小的泄露面 |
| **进程内限流字典** | Redis | 单进程本地跑没有共享状态问题；引 Redis 是这里最典型的过度设计 |
| **`resolve_visibility` 放在 `domain/`** | 写在路由或仓库里 | 纯函数、毫秒级可测（9 条单测），M4 的查询直接复用它，不会漏一处 |
| **测试自造账号（`user_factory`）** | 依赖 seed 数据 | 用例之间不互相污染，也不要求开发库处于某个状态 |

---

## 当前状态速查

**M3 完成** → 下一步 **M4 采购申请**：草稿 / 明细 / 提交 / 撤回 + 幂等。
账号齐了，`.\scripts\seed.ps1` 一条命令就能拿到 7 个演示账号（口令 `Dev-Passw0rd!`）。

| 想看什么 | 去哪 |
| --- | --- |
| 阶段验收清单与进度 | [PROGRESS.md](PROGRESS.md) |
| M3 详细步骤 | [docs/企业采购审批系统-实施计划.md](docs/企业采购审批系统-实施计划.md) 第四节 |
| 环境速查与命令 | [README.md](README.md) |
| AI 改动守则与技术纪律 | [AGENTS.md](AGENTS.md) |
