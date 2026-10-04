# PROGRESS · 阶段验收清单与进度

> **用途**：核对每个阶段的验收工作，记录工作进度。
> 做法：打印这一页，做完一项勾一项。**只有验收项全部通过才进入下一阶段**——
> 不要"差不多就往下走"，越往后欠的债越难还。
>
> 阶段四问总结写在 [Development progress.md](Development%20progress.md)。两份文件分工不同：
> 本文件记「验收过了没有」，那份记「学到了什么」。

## 进度总览

| 阶段 | 主题 | 状态 | 完成日期 |
| --- | --- | --- | --- |
| M0 | 准备与设计定稿 | ✅ 完成 | 2026-10-01 |
| M1 | 骨架贯通 | ✅ 完成 | 2026-10-02 |
| M2 | 主数据 | ✅ 完成 ※ | 2026-10-02 |
| M3 | 账号与权限 | ✅ 完成 ※ | 2026-10-02 |
| M4 | 前端设计系统 | ✅ 完成 | 2026-10-02 |
| M5 | 采购申请 | ✅ 完成 ※ | 2026-10-02 |
| M6 | 审批引擎 ★ | ✅ 完成 | 2026-10-03 |
| M7 | 预算并发预留 ★ | ✅ 完成 | 2026-10-04 |
| M8 | 回归验收 | ⬜ | |
| M9 | 附件与时间线 | ⬜ | |
| M10 | 履约链 | ⬜ | |
| M11 | AI 制度助手 | ⬜ | |
| M12 | 桌面壳与移动端 | ⬜ | |

---

## M0 · 准备与设计定稿 ✅ 2026-10-01

- [x] `docker compose up -d` 成功，`psql` 能连上并查到版本号（**PostgreSQL 16.15 / pgvector 0.8.6**）
- [x] 后端 `GET /health/live` 返回 `{"status":"ok"}`
- [x] 后端 `GET /health/ready` 返回 `{"status":"ok","database":"up"}`
- [x] 前端 `localhost:5173` 能打开（HTTP 200 / Vite 6.4.3）
- [x] Vite 代理 → 后端链路通（`/health/ready` 经 5173 返回 `database: up`）
- [x] `pytest` 能跑（4 passed）
- [x] `tsc --noEmit` / `vitest run` / `ruff` / `mypy` 全绿
- [x] 设计产物三份：`docs\ER-图.md`、`docs\业务规则.md`、`docs\金额-审批链对照表.md`

## M1 · 骨架贯通 ✅ 2026-10-02

- [x] `GET /api/v1/suppliers` 返回 `[]`
- [x] `POST` 后拿到带 id 的对象（`{"id":"09ad241f-…","code":"SUP-E2E-001","active":true}`）
- [x] **重复 `code` 提交返回 409**，且错误体是统一形状（`{"error":{"code":"CONFLICT","message":…,"request_id":…}}`）
- [x] **重启后端，数据还在**（停进程再起，列表仍有 `SUP-E2E-001`）
- [x] Alembic `upgrade head` 可重复执行，不报错
- [x] `0001` 迁移人工检查过（UUID 主键 / `code` 唯一约束 / `timestamptz` + `now()` / `ix_suppliers_active_name`）
- [x] 浏览器页面显示刚建的供应商（页面上已建 `供应商A/001`、`B/002`；经 5173 代理 GET 返回 200 与这两条）

> ※ 第 7 条由 `money.ts` 单测 + 预算接口集成测试共同覆盖，**未做浏览器抓包**。

## M2 · 主数据 ✅ 2026-10-02

- [x] 能创建品类、填 3 条审批规则，保存后重新打开能看到
  （`test_create_category_then_read_back`；存进去的是补齐默认值后的策略）
- [x] `role: "BOSS"` → 422（第一层 Pydantic 结构拦住）
- [x] **`{"gte": ["amount", 100_000]}`（漏 `_fen`）→ 422，且提示点名 `amount_fen`**
- [x] **没有零阈值兜底的策略 → 422**（提示里说清"会算出空审批链"）
- [x] 阈值写浮点（`10000.5`）→ 422
- [x] 同一 `order` 出现两次 → 422
- [x] 能给某部门某品类配预算；界面显示「元」、库里存「分」
  （`test_budget_stores_fen_and_rejects_duplicate_scope` 断言库里是 `100000` 分）
- [x] 前端填 1000 元 → 请求体 `100000`（`money.test.ts` 直接断言 `yuanToFen("1000") === 100000`）
- [x] 供应商能禁用，禁用后 `/api/v1/suppliers` 查不到（管理员列表里还看得到）

## M3 · 账号与权限 ✅ 2026-10-02

- [x] **8 个角色**都能登录，密码错误 401
  （`test_wrong_password_returns_401`；`user_factory` 按角色造账号，`user_roles` 里 admin 持 `ADMIN+GM`）
- [x] `REQUESTER` 的 token 调管理员接口 → **403**（不是 404、不是 200）
  （`test_admin_route_rejects_a_requester_with_403`；未登录则是 401）
- [x] 手改 Cookie 里的 token → 401（`test_tampered_cookie_is_rejected`）
- [x] **登录连错 5 次后第 6 次 → 429**，窗口过期后恢复
  （`test_sixth_attempt_after_five_failures_returns_429`；窗口行为由 `test_login_throttle.py` 单测覆盖）
- [x] 冻结账号后旧 token 立即失效（`test_freezing_an_account_kills_its_existing_token`，靠 `token_version += 1`）
- [x] `refresh_token` Cookie 的 `Path` 是 `/api/v1/auth/refresh`
  （`test_refresh_cookie_is_scoped_to_the_refresh_path`，直接断言 Set-Cookie 头）
- [x] 库里 `users.password_hash` 不含原始密码
  （`test_users_password_hash_never_holds_the_raw_password`；另在库上直接查过 `like '%Dev-Passw0rd!%'` → 0 行）

### 三项依赖后续阶段的验收（本阶段无法真跑，不许假装通过）

这三项要的是"**申请**这条数据"，而 `purchase_requests` 是 M5 才建的表。
机制本身已经在 M3 做完了（`domain/permissions.py` 的 `resolve_visibility`，
9 条单测覆盖四个角色的可见范围），缺的只是把它挂到真实查询上：

| 验收项 | 依赖 | 什么时候补测 |
| --- | --- | --- |
| 换用户 id 取别人的申请 → 404 | `purchase_requests` 表 + M5 的查询 | M5 建表后第一条就测它 |
| `PROCUREMENT` 跨部门可见，`DEPT_MANAGER` 不能 | 同上 | M5 |
| `ADMIN` 兼 `GM` 能拿到 200000 元申请的 GM 待办 | M6 的审批任务表 | M6 |

> `ADMIN` 兼 `GM` 的**数据**已经就位（`user_roles` 里 admin 有两条角色），
> 只差 M6 生成待办。M3 已能验证的是"角色解析结果是 `ADMIN+GM`"
> 与"`resolve_visibility` 对 GM 返回 `ALL`"。

## M4 · 前端设计系统 ✅ 2026-10-02

- [x] 设计令牌集中在 `index.css` 的 `@theme`，页面里不再写死色号
      （暖白极简：页面 `#fcfaf8` / 侧边栏 `#f4efe9` / 卡片白 / 标题 `#1a1a1a`；取值来自参考站的计算样式）
- [x] 外壳改成「侧边栏 + 内容区」，窄屏折到顶部；菜单按角色渲染
- [x] 共享界面原语 `components/ui.tsx`，六个页面全部改用它（删掉了重复的 `pages/admin/_ui.tsx`）
- [x] **侧边栏显示的账号与角色，与 `GET /api/v1/auth/me` 返回的一致**
      （自检 A4：账号区 `ADMIN+GM` + 管理员菜单三项齐全；B1：requester 菜单**恰好只剩两项**）
- [x] 已登录的人打开 `/login` 会跳回目标页，不再同时看到登录表单与"退出"
- [x] 浏览器自检 **11 项全过**（`node scripts\浏览器自检.mjs`，exit 0）
      （当时 11 项；M7 加了「预算看板」那一条，现为 **12 项**）
- [x] `npx tsc --noEmit` / `npx vitest run`（14 passed）/ `npm run build`（gzip 74.81 KB）全绿
- [x] 既有交互不回归：登录跳回原页面、HttpOnly 生效、无兜底策略 422 原文、预算「元⇄分」、越权提示页

> 本阶段**没有改任何后端接口**，也没有新增业务功能。
> 后端那一轮的三处改动（`budgets` CHECK 约束、供应商资质有效期过滤、自检 C3 竞态）
> 属于独立的一次修复，已在后端集成测试里覆盖（unit 50 + integration 41 = **91 passed**）。

## M5 · 采购申请 ✅ 2026-10-02

- [x] 能建草稿、改草稿、加三行明细，`amount_total` 由后端算出且正确
      （`test_draft_with_three_lines_computes_amount_on_the_server`：20000+1+9999=30000 分，并直接查库核对）
- [x] 用旧 `version` 提交修改 → **409**（`test_stale_version_returns_409`）
- [x] 同一个 `Idempotency-Key` 提交两次，只产生一条申请（第二次是重放，`version` 不再 +1）
- [x] 同一个键但内容不同（版本不同）→ **409**（`test_same_key_different_payload_returns_409`）
- [x] **并发同键提交（`asyncio.gather`）→ 恰好一个 200、一个 409**，库里状态只推进一次、只有一条提交事件
- [x] 提交后状态变 `IN_APPROVAL`，时间线上**两条**事件（创建 + 提交），并带 `DRAFT → IN_APPROVAL`
- [x] 已提交的申请不能再 `PATCH`（409），明细也不能再 `PUT`（409）
- [x] 补上 M3 挂账的两条：**换 id 取别人的申请 → 404**、`DEPT_MANAGER` 看不到别的部门
      （`test_another_requester_gets_404_not_403` / `test_department_manager_sees_own_department_but_not_others`）
- [x] 空明细 / 0 元提交 → 422，且提示是"申请没有任何明细"而不是底层解析报错
- [x] 撤回后不能再提交（409）；丢弃只能对草稿（409）

> 本轮同时修掉一个 **M3 起就存在的真 bug**：`client.ts` 的 `send()` 里 `...init` 写在
> `headers` 之后，把默认的 `Content-Type: application/json` 覆盖掉了 ——
> **只有带自定义头的接口会中招**（提交/撤回要带 `Idempotency-Key`），
> 现象是后端 422 报 "Input should be a valid dictionary…"。已在浏览器里实测复现并修复。

## M6 · 审批引擎 ★ ✅ 2026-10-03

全部通过。**证据**（可复核）：

| 验收项 | 实测依据 |
| --- | --- |
| 500 / 5000 / 50000 / 200000 元 → 1 / 2 / 4 / 5 个任务 | `pytest tests/test_api_approvals.py` 参数化 4 例；对**真实服务**跑的 `scripts/m6_smoke.py` 同样 4 例全过 |
| 500 元只有部门主管看得到 | 采购/财务的 `/approvals/tasks` 都是 `total=0` |
| 没轮到的高级别看不到，第 1 级过了才出现 | 第 2 级待办 `total=0` → 主管通过 → `current_step_order` 10→20 → 第 2 级 `total=1` |
| 越级审批 403 且任务仍 `PENDING` | 拿第 4 级任务 id 直接 POST approve → `403`，回查 `status=PENDING`、`decided_by=NULL` |
| 并发通过同一级的两个会签任务只推进一级 | `asyncio.gather` 两个 approve → 指针停在 `20`（不是 `30`）；先把"先读再写"的坑撞出来才修对 |
| 两人同时通过同一任务：一个 200、一个 409 | `asyncio.gather` 两次 approve → `[200, 409]` |
| 驳回 → 主单 `REJECTED` + 时间线有意见 | `request_status=REJECTED`，时间线含 `APPROVAL_REJECTED` 且 payload 里有驳回原文 |
| 待办一次 JOIN 查完 | 3 条待办时统计 SQL 日志里的 SELECT 条数 ≤ 4（N+1 会是 7+） |
| 全部通过 → `SOURCING` + `APPROVAL_COMPLETED` | 单级链通过后 `request_status=SOURCING`，时间线含 `APPROVAL_COMPLETED` |
| 改策略不改代码，链路跟着变 | 阈值 1,000 元 → 10,000 元后，同样 5,000 元的申请从 2 级变 1 级 |
| 历史申请仍能复现当时的策略 | `policy_snapshot` 里仍是提交那一刻的 5 步原件、`policy_version=1` |
| 缺人 → 422 并说明哪一级 | 消息原文：`审批链第 order=20 级（DEPT_HEAD）…没有任何可用审批人`；且主单**回滚仍为草稿** |
| `ALL` 会签 3 人 → 3 条任务、预分配、全过才算完 | 3 条 `assignee_id` 非空；前两人通过时 `step_completed=false`，第三人通过才推进 |
| `SINGLE` 或签 3 人 → 1 条任务、任一人认领通过即完成 | 1 条 `assignee_id=NULL`；认领 200、第二人认领 403（已分配他人）、认领人通过 → `SOURCING` |
| 无兜底 / 有死步骤的策略 → 422 | 两条都 422；死步骤那条报 `order=60 在任何探测金额下都不会被触发` |

**验证入口**（都实跑过；**下面是 M6 当时的数字，最新数字见 M7 一节**）：

```powershell
cd backend;  .\.venv\Scripts\python.exe -m pytest -m ""         # 198 passed（M6 收尾时 187，内控修复后 +11）
             .\.venv\Scripts\python.exe scripts\m6_smoke.py      # 18/18，对着真实服务
cd ..;       node scripts\浏览器自检.mjs                          # 11/11
             node scripts\m6-页面实测.mjs                         # 19/19，含截图
cd backend;  .\.venv\Scripts\python.exe scripts\testdata_cleanup.py --scope all   # 清实测数据（原 m6_page_cleanup.py 已并入）
```

> **本轮实测撞出 4 个真 bug，全都是"不会报错、只会算错"的那一类**，值得记下来：
>
> 1. **bulk UPDATE 之后读对象拿到旧值**：`decide` 用条件更新改了库，会话里缓存的
>    对象却没刷新，于是"判这一级是否完成"永远判"还没完"、指针永不推进。
>    连带发现 **`select(Model.col)` 也会经过身份映射** —— 必须 `populate_existing=True`。
> 2. **每个请求一个会话，看不到别的事务未提交的改动**：两人同时通过同一级的两个会签任务时，
>    双方都看到对方还是 `PENDING`，于是**谁都不推进指针**。修法是在流那一行取行锁
>    （`SELECT … FOR UPDATE`），把"判定 + 推进"变成串行临界区。
> 3. **同事务内多条事件的时间戳完全相同**（PostgreSQL `now()` 是事务开始时间），
>    时间线顺序退化成 UUID 随机比较 —— 实测出现过提交与建链两条事件顺序颠倒。
>    改成应用侧取微秒时间戳。
> 4. **页面里写了 Markdown 的 `**…**`**（那是 JSX），星号被原样渲染出来。

### M6 之后的补充修复（2026-10-03，同一阶段的收尾）

M6 验收全过之后，做内控对账时发现**职责分离（SoD）这一整类要求从未被设计过**，
实跑复现了"申请人能批掉自己的申请"。修复与原验收是两件事，记在这里：

- **自审自批 → 三层拦截**：建链时剔除提交人（`exclude_requester`，该级只剩他则提交时 422
  `SELF_APPROVAL_NOT_ALLOWED`）＋ 待办查询 `requester_id != actor_id`（挡**或签**）＋
  `decide`/`claim` 的 SQL 守卫与 403。
  → **注意**：只做第一层是不够的 —— 或签任务 `assignee_id` 为 NULL、只按角色可见，
  第一层挡不住它。这一点是写测试时才暴露出来的。
- **供应商列表收口 → 主数据接口整体收口**：`/api/v1/suppliers` 从公开改为要求登录、`only_active`
  客户端参数收掉；同一轮把 `/api/v1/budgets/mine`（部门改从登录态推导）、`/api/v1/departments`、
  `/api/v1/categories`、`/api/v1/categories/{id}`（它会把**整份审批策略**带出来）一并收口。
- 新增测试 10 条（纯函数 5 + 集成 5），**全量 187 → 197 passed**；
  随后主数据接口收口（下表第 13 条）再 +1，**累计 198 passed**。
- 依据、对账与八条定稿口径见 [`docs/采购内控对照.md`](docs/采购内控对照.md)；
  规则写进 [`docs/业务规则.md`](docs/业务规则.md) 第 9、10 条。

## M7 · 预算并发预留 ★ ✅ 2026-10-04

全部通过。**证据**（可复核）：

| 验收项 | 实测依据 |
| --- | --- |
| 总额 1000 元时并发提交两笔 800 元，只有一笔成功，另一笔 422 | `test_api_budget.py::test_concurrent_submits_never_overdraw`：`asyncio.gather` 两个提交 → `[200, 422]`，`BUDGET_INSUFFICIENT` 文案带"可用 20000 分 / 需要 80000 分"；库里 `reserved=80000`；失败那笔**整体回滚**（仍是草稿、`budget_id` 为空、无流水） |
| 驳回后可用额度恢复 | `test_reject_releases_the_reservation`：驳回后 `reserved` 归零，流水 `[RESERVE 80000, RELEASE 80000]` |
| 撤回也要释放 | `test_withdraw_releases_the_reservation`：同上（三个释放入口：驳回 / 撤回 / 付款结转） |
| `budget_transactions` 三类流水齐全 | `test_ledger_covers_all_three_kinds_and_sums_to_zero`；页面上也能看（`GET /api/v1/budgets/transactions` + 「预算看板」卡片） |
| 同一申请重放不产生第二条 `RESERVE` | `test_idempotent_replay_does_not_reserve_twice`：同键重放响应逐字节相同、流水仍只有一条；直接调仓库再预留一次会撞 `UNIQUE(request_id, kind)` → 409 |
| 申请 1000 元、实付 900 元：`amount_spent` +900、预留归零、总额不变 | 同一条用例：`{"total": 100000, "reserved": 0, "spent": 90000}` |
| 三类流水相加（RESERVE − RELEASE − SPEND）= 0 | 同上：`[(RESERVE,100000),(SPEND,90000),(RELEASE,10000)]` → 归零 |
| 并发测试连跑 20 次稳定 | 单条用例连跑 20 轮：**20/20 通过**（失败轮次为空） |
| 全量回归 | `pytest -m ""` → **222 passed**（198 + 纯函数 14 + 集成 10）；ruff / mypy 干净；三条分层自查无输出 |
| 前端 | `tsc --noEmit` 无错误 / `vitest` 14 passed / `npm run build` 通过 |
| 浏览器 | 自检 **12/12**（新增 A7：预算看板列出预算与可用额）；`m6-页面实测.mjs` **20/20**（新增"先配预算再提交"一步） |
| 真实链路 | 以 `requester` 提交 `PR-2026-DA3A9C82`（800 元）→ 库里 `RESERVE 80000`；办公用品可用 10000000 → 9920000 分；看板实时显示 已预留 ¥800.00 / 可用 ¥149,200.00 |

**验证入口**（都实跑过）：

```powershell
cd backend;  .\.venv\Scripts\python.exe -m pytest -m ""                    # 222 passed
             .\.venv\Scripts\python.exe -m pytest tests/test_api_budget.py -m integration -q   # 10 passed
             .\.venv\Scripts\python.exe scripts\m6_smoke.py                # 19/19，对着真实服务
cd ..;       node scripts\浏览器自检.mjs                                       # 12/12
             node scripts\m6-页面实测.mjs                                      # 20/20，含截图
cd backend;  .\.venv\Scripts\python.exe scripts\testdata_cleanup.py --scope all   # 清测试数据
```

> **本阶段新增的三件事**（验收清单之外，但都是这一步必须一起做的）：
>
> 1. **迁移 `0007` 建 `budget_transactions`**：`UNIQUE(request_id, kind)` 挡"重试重复扣钱"，
>    `amount > 0`；`budget_id` 用 RESTRICT（有流水的预算行不许被删）、
>    `request_id` 用 CASCADE（流水是申请的从属数据）。
> 2. **"计划外"口径落地**（2026-10-03 定稿）：该部门 × 品类 × 年度**根本没有预算行 → 422**
>    （`NO_BUDGET`），与"超预算"同一个处置 —— 先配 / 调预算再重新提交。**失败关闭**，不留静默放行。
>    `budget_id` 在提交那一次条件更新里**一起写死**（此后部门 / 品类变更都不影响它）。
> 3. **只读预算看板**（`FINANCE` + `ADMIN`）：`GET /api/v1/budgets` 带部门名与品类名、
>    `GET /api/v1/budgets/transactions` 带申请单号；页面 `/budgets` 有合计、使用率条与流水。
>    **看钱与改钱分开**：改预算仍然只走 `/api/v1/admin/budgets`。
>
> **踩到的坑（一个，但是老坑的新形态）**：并发失败的那一方如果拿**本次请求开始时读到的**
> 预算数字去解释原因，会得出"钱明明够啊"的结论，422 于是退化成一句含糊的 409（实测撞到）。
> 修法是**失败后强制重读**（`select(...).execution_options(populate_existing=True)`）再算差额 ——
> 判定始终只由条件更新做，重读只用来把话说清楚。这与 M5「条件更新后回读旧对象」是同一类问题。

> **付款结转的调用方在 M10**：`BudgetRepository.settle()`（`SPEND(实付) + RELEASE(预留 − 实付)`）
> 已经写好并被集成测试钉住，M10 的付款接口直接调它，**不要再写第二份算术**。

## M8 · 回归验收（2026-10-02 插入）

> **这不是新功能阶段**，而是"回头把已经做完的东西整体再撞一遍"的验收关口。
> 放在 M7 之后是刻意的：此时项目已经有了账目（预算流水）、有了权限边界、
> 也有了跨模块的数据流，**接缝最多、最适合整体验一次**；再往后堆功能，
> 出问题时要排查的范围只会更大。

- [ ] **一次可复跑的回归入口**：一条命令跑完"三条分层自查 + 后端 unit/integration + 前端 tsc/vitest/build + 浏览器自检"，
      退出码 0 才算过（不新增配置项，脚本固定写死要跑什么）
- [ ] 历史验收全量复跑：M0–M7 的验收项逐条重放，结果写进报告（命令、条数、状态码）
- [ ] **三条"静默失败"约束各撞一次**：无兜底策略 → 422；实付 < 预留时断言 `reserved` 归零；换 id 取别人的数据 → 404
- [ ] **跨阶段接缝逐条验**：登录 → 跳回原目标页；界面「元」→ 请求体「分」→ 库里「分」三处一致；
      事件条数与状态迁移一致；同键重放不产生第二条记录
- [ ] **测试污染物零残留**：跑完全套测试后，库里不残留不可辨认的测试数据（前缀约定见 `tests/test_api_purchase*.py` 的 `_code()`）
- [ ] `AGENTS.md` 第四节全部检查全绿；三条分层自查无输出
- [ ] 回归中发现的问题：**能当轮修的当轮修**，修不了的写进"真债"并注明风险与补的时机

## M9 · 附件与时间线

- [ ] 上传 PDF 成功，返回附件 id
- [ ] 上传 11 MB → 422
- [ ] 上传改名成 `.pdf` 的 exe → 422
- [ ] **文件头像 PNG、内容非图片 → 422（解码失败）**
- [ ] **超大尺寸 PNG（如 20000×20000）→ 422（压缩炸弹防护）**
- [ ] 越权下载附件 → **403**
- [ ] 直接访问 `http://127.0.0.1:8000/uploads/<文件名>` 拿不到文件
- [ ] 下载响应带 `Content-Disposition: attachment`
- [ ] 时间线按时间正序展示全部事件

## M10 · 履约链

- [ ] 全链路走通：申请 → 审批 → 询价 → 定标 → 订单 → 收货 → 发票 → 付款 → 归档
- [ ] 发票超容差 → 付款 422 并列出具体差异
- [ ] 未收货就创建付款单 → 422
- [ ] **两人同时付款，只有一个成功；`amount_spent` 只增加一次**
- [ ] **预留 1000 实付 900：流水里有 `SPEND 900` 和 `RELEASE 100`**
- [ ] **登记两张发票，匹配用的是两张之和（订单级粒度）**
- [ ] 三单匹配对比页正确显示三列和差异高亮

## M11 · AI 制度助手

- [ ] 上传制度文档，`knowledge_chunks` 里能看到切分结果
- [ ] 问文档里有的问题，回答带出处
- [ ] **问文档里没有的问题，明确说"未找到相关规定"，不编造**
- [ ] 流式回答逐字出现
- [ ] `meta` 事件里有来源列表
- [ ] 检索为空时日志里能看到"未调用模型"
- [ ] **引用不存在编号的回答能被 `check_citation_integrity` 发现**
- [ ] **上传同一制度第 2 版，检索只命中第 2 版**

## M12 · 桌面壳与移动端

- [ ] 双击 `.exe` 能打开，窗口无浏览器地址栏
- [ ] APK 装到手机上能登录、能看到待办
- [ ] 桌面端和移动端用**同一份 `frontend/src`**，没有分叉出第二套代码

---

## 三条"静默失败"约束（每阶段都要回头看）

违反**不会报错，只会算错**，所以必须靠验收项主动去撞：

| 约束 | 怎么撞它 |
| --- | --- |
| **审批链不许为空** | M2 提交无兜底策略 → 422 |
| **预留与实付差额必须显式释放** | ✅ M7 已撞过：`test_ledger_covers_all_three_kinds_and_sums_to_zero`（实付 < 预留时断言 `reserved` 归零、三类流水相加为 0）；M10 的付款接口再撞一次 |
| **权限范围必须写进 `WHERE`** | M3 换 id 取别人的申请 → 404 |

## 待修：内控缺口（2026-10-03 记录）

> 起因：`业务规则.md` 缺了"职责分离"整整一类要求，实跑发现**申请人能批掉自己的申请**。
> 依据与逐条对账见 [`docs/采购内控对照.md`](docs/采购内控对照.md)（法规原文 + Oracle 官方文档 + 开源实现）。

### ✅ 已修（2026-10-03）

| # | 缺口 | 修法与验证 |
| --- | --- | --- |
| 1 | **提交人可自审自批** | **三层拦截**：① 建链时 `exclude_requester` 剔除提交人（该级只剩他 → 提交时 422 `SELF_APPROVAL_NOT_ALLOWED`）；② 待办查询加 `requester_id != actor_id`（挡**或签** —— 或签任务没有预分配人，①挡不住）；③ `decide`/`claim` 的 SQL 加 `NOT EXISTS(该申请是 actor 提的)` + `_assert_can_act` 403。判定是 `domain/` 纯函数 + 失败关闭。**真实服务验证**：`dept-manager` 提交自己的申请 → `422 SELF_APPROVAL_NOT_ALLOWED`，且自己待办里看不到 |
| 2 | **`/api/v1/suppliers` 未登录可读、`only_active` 客户端可控** | 该路由加 `CurrentUser`；`only_active` 参数**收掉**（合格过滤写死；看全部走 `/admin/suppliers`）。**真实服务验证**：未登录 → `401`；`requester` 带 `?only_active=false` → 与不带时**完全一致** |
| 9 | **无部门账号"填完一整张表才被拒"**（浏览器审查时发现） | 前端新建页加**顶部红字提示 + 禁用「保存草稿」**，并给 `handleSubmit` 加早退（提示里说清"部门决定预算与审批链，是必填项"以及该找谁）；后端文案从"无法**提交**采购申请"改为"无法**创建**采购申请"（同一条路径也服务"保存草稿"，旧文案让人困惑）。**浏览器实测**：`admin` 打开新建页 → 提示出现在表单最上方、保存按钮 `disabled=true`、次要按钮变「返回我的申请」 |
| 10 | **管理员能看到「管理供应商」入口**（浏览器审查时发现） | `SupplierList.tsx` 的页头链接**无条件渲染**，而侧边栏那三项是按 `isAdmin` 判的 —— 同一个"管理端入口"两处口径不一致。改为按角色显示；空状态文案也分角色（非管理员看到的是"请联系采购部门维护"，而不是一个点进去会 403 的入口） |
| 11 | **无部门账号仍能看到「我的申请」入口**（审查后定为方案①） | **判据换成"有没有部门"而不是"是不是 ADMIN"**（与后端 `NO_DEPARTMENT` 同源；哪天给管理员指定了部门，他照样能申请）。侧边栏隐藏「我的申请」（**保留「我的待办」**——审批与部门无关，GM 要审大额单）；`/requests`、`/requests/new`、`/requests/:id` 三条路由加 `RequireAuth requireDepartment`（拦下时显示专门说明页，不渲染表单）；新建页那段红字降级为**兜底** |
| 12 | **`/api/v1/budgets/mine` 匿名可读、且部门可由调用方指定**（本轮对账查出） | M2 留下的两个问题叠加：不要求登录 + `department_id` 是**客户端参数**（注释里写着"M3 起改为从登录态推导"，一直没做）。改为**要求登录 + 部门只从登录态取**；无部门账号返回空列表。**实测**：匿名 → `401`；登录后带 `?department_id=<别的部门>` 结果与不带**完全一致** |
| 13 | **三个主数据接口匿名可读**（本轮对账查出，已按授权修复） | `/api/v1/departments`、`/api/v1/categories`、`/api/v1/categories/{id}` 全部**改为要求登录**（第三个会把**整份 `approval_policy`** 带出来 —— 金额阈值 + 各级角色，属内控结构信息）；顺带把 `only_active` 这个**客户端可控**过滤器一并收掉（与供应商那次同形）。**实测**：匿名三个全 `401`；登录后照常返回（品类 3 / 部门 4 / 供应商 4）；`only_active=false` 结果与不带一致；**浏览器实测**品类下拉与部门下拉照常加载、供应商页照常列数据 |

### ⏳ 按定稿口径执行（2026-10-03 定，尚未到落地阶段）

| # | 口径 | 何时做 |
| --- | --- | --- |
| 3 | **超预算直接 422**，先调预算再重新提交；不自动放行、不加一级审批 | M7 |
| 4 | **询价与定标 v1 不分离**（同一采购角色），靠"≥3 家有效报价 + 供应商合格 + 报价完整 + 记成交单价"兜底 | M10 |
| 5 | **财务 v1 不拆出纳**（同一 `FINANCE` 审核并付款），靠"三单匹配 + 状态机"兜底 | M10 |
| 8 | **不做委托/转办**；将来做时一次性设计"转派后重新校验权限/角色/范围/历史责任链" | 暂缓清单 |

### ⏳ 仍待落地

| # | 缺口 | 性质 | 何时做 |
| --- | --- | --- | --- |
| 6 | **被拒的尝试未留痕**（越权 403 / 越级 403 / 无权操作） | 🟡 可补强 | M8 或 M9 之后；新表存**安全/审计事件**，字段 `actor/action/resource/timestamp/result=DENIED/request_id`，**不要**混进 `request_events`（那是申请人看的时间线） |
| 7 | **菜单/按钮权限由前端自判**（`App.tsx` 用 `me.roles.includes("ADMIN")`） | 🟡 取舍已定：**继续前端自判**，后端始终是最终边界 | 角色/权限复杂度明显上升时再改 |

**说明**：原第 1、2 条是最严重的两条，已修完并全量验证（**198 passed**）。
第 9–13 条是浏览器审查与接口对账期间发现、并当轮修掉的。

## 阶段结束时的两件事

1. **本文件**：把该阶段的验收项逐条勾掉，并更新上方「进度总览」的状态与完成日期。
2. **[Development progress.md](Development%20progress.md)**：回答四问——
   学到了什么 / 卡在哪 / 有没有留坑 / 为什么用这套技术栈。**回答要精炼简洁。**

验收没过就不进下一阶段；四问没写就不算收尾。
