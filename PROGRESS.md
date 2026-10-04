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
| M8 | 回归验收 | ✅ 完成 | 2026-10-04 |
| M9 | 附件与时间线 | ✅ 完成 | 2026-10-04 |
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

### M4 之后的前端再优化（2026-10-04，参考主流后台再刷一遍）

不是新阶段，是**同一套设计系统的第二次打磨**（令牌、原语、外壳三处一起动，页面不改写法）。
参考对象：小米 MiMo 文档站的侧栏分组、小米账号 / 知乎 / 高校融合门户的**分栏登录页**、
博客的**右侧本页导航**、GitHub 仪表盘的**问好 + 快捷入口 + 指标卡**。

| 改了什么 | 落在哪 | 为什么 |
| --- | --- | --- |
| 新增 `--radius-lg` 与两级阴影 `--shadow-card` / `--shadow-pop` | `index.css` 的 `@theme` | 卡片需要"被托起来"一点点；登录面板与粘性目录需要更明显的浮起层次 |
| 面包屑（宽屏走顶栏、窄屏走页内）、`Stat` 指标卡、`PageToc` 本页导航、`Badge`、`EmptyState` 带行动入口、`Button` 的 `size/block` | `components/ui.tsx` | 这些"每个长页面都要有"的排版件，一次做成原语，九个页面一起受益 |
| 外壳：侧栏加**图标 + 分组标题 + 账号卡片**，新增**粘性顶栏**（面包屑 + 环境标记 + 接口文档入口） | `App.tsx` | 侧栏回答"我能去哪"，顶栏回答"我在哪、这套是不是演示环境" |
| **登录页改独立全屏分栏**（左侧品牌与四条能力，右侧表单） | `pages/Login.tsx` | 登录是"还没有身份"的时刻：给他讲清这是什么系统，比摆一个侧边栏有用 |
| 首页加**工作台**（问好 + 快捷入口 + 三个数字），环境自检保留在下方 | `pages/Home.tsx` | 先回答"我该做什么"，再回答"系统正不正常"；三个数字全部来自既有接口，**没开新接口** |
| 申请详情改**两栏 + 右侧「本页导航」**（滚动高亮） | `pages/requester/RequestDetail.tsx` | 四段内容滚到中段容易忘了下面还有什么 |
| 表格行悬停高亮、表头小一号、单元格留白加大、加载态不再借用空状态样式 | `ui.tsx` + 三个列表页 | 待办/申请列表一行六七列，看串行是真问题 |

**没有违反边界**（这是本次最要紧的一条，逐条核过）：

- 自检脚本依赖的锚点一个没动：`input[autocomplete=username]`、`input[type=password]`、
  **第一个 `<form>` 就是登录表单**、常驻的「显示/隐藏」按钮、`[data-testid=app-nav]` 里的
  `<a>` 集合与顺序、`data-testid=approval-timeline`、`data-testid=no-department-notice`；
- **草稿详情页仍然不含「审批进度」字样** —— 右侧目录按"实际渲染出来的分区"生成，
  草稿没有审批流就没有那一条（页面实测第 4 项专测这个）；
- 分类/预算表单的字段顺序与 `byLabel('预算金额')` 文本没动（自检的 C1/C3 靠它驱动）；
- 视觉改动全部走令牌与原语：页面里**写死色号 0 处**，换风格仍然只动 `index.css`。

**验证**（改动后重跑）：浏览器自检 **12/12**、`m6-页面实测` **20/20**、
`tsc` 无错误 / `vitest` 14 passed / `npm run build` 通过（286.96 KB，gzip 88.53），
回归入口 `.\scripts\回归验收.ps1` **退出码 0**（11 步全绿）。

### 回归入口的一个"假通过"缺陷（2026-10-04 修）

上一次回归的"退出码 0"其实**不可信**：`pytest` 输出 `1 failed, 221 passed` 时入口仍判 ✓。
根因是 `Invoke-Step` 在函数作用域读 `$LASTEXITCODE`，而外部命令是在 `& $Body` 的**子作用域**里跑的
（我在函数里那句 `$LASTEXITCODE = 0` 又在本地建了副本），比较的永远是 0。

修法：退出码检查放进**每个脚本块内部**（与外部命令同作用域），失败即 `throw`；
已用 `cmd /c exit 7` 单独验证这条判据能捕获失败。

> 教训记在这里：**回归入口本身也会坏，而且它坏了会让人以为一切正常。**
> 修好之后第一次跑，它立刻报出全量测试的偶发失败（见下）。

### ✅ 已修：只追加日志的排序退化（P1，2026-10-04 当日确认后修复）

全量测试 5 轮里失败 2 轮，两个不同用例、同一根因：

| 用例 | 现象 |
| --- | --- |
| `test_timeline_is_append_only_across_actions` | 时间线里「提交」与「生成审批链」顺序颠倒 |
| `test_ledger_covers_all_three_kinds_and_sums_to_zero` | 预算流水里 `SPEND` 与 `RELEASE` 顺序颠倒 |

**根因**：同一事务里写的多条记录，`datetime.now(UTC)` 可能落在**同一微秒**，
于是 `ORDER BY created_at` 退化成 `id`（UUID）随机比较 —— M6 那次的应用侧时间戳
只把概率压小、没有消除。

**改法**（按建议方案落地）：迁移 `b7d3f0c41a26_log_sequence_columns.py`
给 `request_events` 与 `budget_transactions` 各加一列 `seq BIGINT GENERATED BY DEFAULT AS IDENTITY`，
排序改用它，`created_at` 只用于显示。迁移分三步走：加可空列 → 按原排序键回填并推进序列 →
补 `NOT NULL`（旧行的真实落盘顺序已不可还原，只能保持它们原来的顺序，这一点写在迁移注释里）。

| 改动点 | 文件 |
| --- | --- |
| 两个模型的 `seq` 列 + 为什么必须有序号 | `records/purchase.py`、`records/catalog.py` |
| 写入路径不再靠时间戳排序 | `event_repo.append/list_for_request`、`budget_repo._append/list_transactions` |
| 测试/探针的读法改为 `ORDER BY seq` | `tests/test_api_budget.py`、`tests/test_api_requests.py`、`scripts/regression_probes.py` |

**过程中撞出的坑（值得记）**：`budget_transactions.created_at` **没有数据库默认值**
（`request_events` 那一列有 `now()`）。我先把"不再由应用侧写 `created_at`"当成对称清理，
结果该列 `NOT NULL` 违约 —— 而违约发生在 `flush()` 里，正好被 `_append` 的
`except IntegrityError` 接住，报出来的是"**这笔申请已经有一条 RESERVE 流水**"，
指错方向整整一轮（7 条预算用例全挂在这句上）。
教训：**SQLAlchemy 的 `IntegrityError` 不只是唯一约束，也可能只是 NOT NULL** ——
转换错误码时不要把底层原因盖掉。

**验证**：

- `pytest -m unit` **129 passed**；`pytest -m integration` **93 passed**；
- **全量 5 轮连跑 5/5 全绿**（222 passed ×5，用时 37.2–40.6s）—— 这正是当时用来暴露它的跑法；
- ORM 插入后 `seq` 由数据库发回（实测 flush 后拿到 10、11，回滚无残留）；
- 迁移可逆：`downgrade -1` 后列与序列都被带走、**9 条事件 / 1 条流水一行没少**；
  再 `upgrade head` 回填结果与第一次**逐行一致**，序列停在 `last_value=10, is_called=f`。

### 窄屏（<768px）响应式实测（2026-10-04，新增 `scripts\窄屏自检.mjs`）

上一条"窄屏未验证"补上了。做法不是肉眼看截图，而是把视口真的切成 **375×812**
（CDP 的 `Emulation.setDeviceMetricsOverride`），再量断点两侧的**客观差异**。

```powershell
node scripts\窄屏自检.mjs      # 20 项，退出码 0 = 全过；不写库、只读页面
```

| 组 | 验什么 | 实测 |
| --- | --- | --- |
| A | 登录页：无横向溢出、表单未超出视口、**不套外壳**（没有可见 `<main>`、侧边栏不可见） | ✅ 表单宽 327 / 视口 375 |
| B | 外壳断点：侧栏窄屏是**页顶整宽横条**（375px），宽屏是**固定左栏**（256px）； 顶栏面包屑宽屏显示、页内面包屑窄屏显示（**两处不同时出现**）；菜单链接数不变 | ✅ 375 → 256 的跳变就是断点生效的硬证据 |
| C | 申请列表与详情：无横向溢出、正文非空、详情页右侧「本页导航」窄屏隐藏 / 宽屏显示 | ✅ `display:none` → `display:block`（宽 180） |

**结论**：四个视口（登录 / 工作台 / 申请列表 / 申请详情）在 375px 下 `scrollWidth` 全部等于 375，
**0 处横向溢出**。过程中顺手补了一处真缺口：**首页原本没有页内面包屑**，
而窄屏偏偏只有页内那一处 —— 手机上就完全没有"我在哪"这层信息，已补上
（`pages/Home.tsx` 的 `PageHeader` 加 `breadcrumb`）。另给 `PageToc` 加了
`data-testid="page-toc"`，让"本页导航在窄屏隐藏"可被脚本断言，而不是靠人看。

> 这个脚本**还没有接进** `回归验收.ps1` 的固定步骤（那要改回归入口本身，属另行决定）。


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

## M8 · 回归验收 ✅ 2026-10-04

> **这不是新功能阶段**，而是"回头把已经做完的东西整体再撞一遍"的验收关口。
> 放在 M7 之后是刻意的：此时项目已经有了账目（预算流水）、有了权限边界、
> 也有了跨模块的数据流，**接缝最多、最适合整体验一次**；再往后堆功能，
> 出问题时要排查的范围只会更大。

### 一条命令的回归入口 ✅

```powershell
cd D:\procurement-app
.\scripts\回归验收.ps1        # 退出码 0 = 全过；失败会停在那一步并打印原始输出
```

固定跑 11 步（**没有任何开关** —— 有开关就一定会出现"这次少跑一项"）：

| # | 步骤 | 本次实测 |
| --- | --- | --- |
| 1 | 三条分层自查（应无输出） | ✅ 命中 0 处 |
| 2 | 后端全量 `pytest -m ""`（**不跳集成档**） | ✅ **222 passed** in 37.44s |
| 3 | `ruff check app tests scripts` + `mypy app` | ✅ All checks passed / 48 files |
| 4 | `npx tsc --noEmit` | ✅ 无错误 |
| 5 | `npx vitest run` | ✅ 14 passed |
| 6 | `npm run build` | ✅ built in 755ms（272.81 KB / gzip 84.17） |
| 7 | 确保前后端在跑（不在就调 `start.ps1`，最多等 90s） | ✅ 都已在跑 |
| 8 | `backend\scripts\m6_smoke.py`（真实服务 19 项） | ✅ 19/19 |
| 9 | `backend\scripts\regression_probes.py`（**新增**：静默失败 3 条 + 接缝 4 条） | ✅ 14/14 |
| 10 | `scripts\浏览器自检.mjs`（无窗口 Edge 12 项） | ✅ 12/12 |
| 11 | `testdata_cleanup.py --scope all`（清理 + 残留回查） | ✅ 干净（残留 0） |

> **2026-10-04 修复排序退化后复跑**（同一入口、同一 11 步）：仍然**退出码 0**，
> 第 2 步 **222 passed in 40.76s**，其余各步数字不变（冒烟 19/19、探针 14/14、自检 12/12、残留 0）。
> 也就是说：**这次修复没有把任何一项验收撞坏**，而这次复跑的意义在于——
> 上面那张"首次实测"表是在**回归入口自身那个假通过缺陷**存在时得到的，
> 下面这次才是"判定修好之后"的真实结论。

### 历史验收全量复跑（M0–M7）✅

| 阶段 | 复跑方式与依据 |
| --- | --- |
| **M0** | 现场：PostgreSQL **16.15** / pgvector **0.8.6**；`/health/live` → `{"status":"ok"}`、`/health/ready` → `{"status":"ok","database":"up"}`、`/health/db` 打印版本；前端 5173 → HTTP 200（Vite 已注入客户端）；容器 `procurement-db Up 34 minutes (healthy)`；三份设计产物仍在 `docs/` |
| **M1** | 现场：**重启后数据还在** —— 服务今天 10:0x 重启过，2026-10-02 建的供应商 `001/002`、2026-10-03 建的两条申请仍能查到；`alembic upgrade head` **连跑两次无报错**，`current` 停在 `ea2846f5497f (head)`；重复 `code` → **409**；浏览器页面显示供应商（自检 A 组） |
| **M2** | 测试：`test_api_master_data.py`（16 条）+ `test_rules.py`（16 条）覆盖七条 422（`BOSS` 角色 / 漏 `_fen` / 无兜底 / 浮点阈值 / `order` 重复 / 死步骤）；**元⇄分三处一致**由探针接缝②现场验（请求体 100000 → 响应 100000 → 库里 100000）+ 自检 C3（界面填 1000 元 → 列表 ¥1,000.00） |
| **M3** | 测试：`test_api_auth.py`（19 条）覆盖 8 角色登录 / 密码错 401 / 连错 5 次第 6 次 429 / 冻结即失效 / refresh Cookie 的 `Path` / 库里无原始口令。**现场补验状态码**：未登录 → **401**；手改 Cookie → **401**；`requester` 调 `/admin/suppliers`、`/admin/categories`、`/admin/budgets` → **403 / 403 / 403**，`admin` 调同样的路径 → **200**（4 / 3 / 2 条） |
| **M4** | 现场：`@theme` 令牌仍在 `index.css`（2 处），页面里写死色号 **0 处**，`components/ui.tsx` 在、旧的 `pages/admin/_ui.tsx` **已不存在**，字体栈显式列了西文 + 中文；自检 A4（侧边栏账号与 `/auth/me` 一致、管理员菜单齐全，含 M7 的「预算看板」）；**已登录打开 `/login` 会跳回 `/`** —— 浏览器实测 `location.pathname === "/"` 且页面上没有登录表单 |
| **M5** | 测试：`test_api_requests.py`（17 条）+ `test_purchase_rules.py`（21 条）覆盖草稿/明细/`amount_total` 重算 / 旧 `version` 409 / 同键重放 / 并发同键 / 提交后两条事件 / 换 id 404；探针接缝③④现场复验事件顺序与幂等重放 |
| **M6** | 测试：`test_api_approvals.py`（22 条）覆盖四档金额 → 1/2/4/5、当前级可见性、越级 403 且仍 `PENDING`、并发推进只走一级、两人同任务 200/409、驳回、SOURCING、改策略不改代码、`policy_snapshot`、缺人 422、会签/或签、**待办一次 JOIN（SELECT ≤ 4）**；`m6_smoke.py` 19/19 对着真接口再走一遍 |
| **M7** | 测试：`test_api_budget.py`（10 条）+ `test_budget_policy.py`（14 条）覆盖并发只有一笔成功、驳回/撤回释放、重放不重复预留、1000/900 结转与三类流水归零、计划外 422、看板权限；**并发用例连跑 20 次 → 20/20** |

### 三条"静默失败"约束各撞一次 ✅（新增 `regression_probes.py`，14 项全过）

| 约束 | 怎么撞的 | 实测结果 |
| --- | --- | --- |
| **审批链不许为空** | 直连库造一个 `approval_policy = {}` 的品类（接口层会拦住这种策略，所以只能造历史脏数据）→ 用申请人身份提交 | **422 `EMPTY_APPROVAL_CHAIN`**，且主单**回滚仍为 `DRAFT`**（没留下半截审批链） |
| **预留与实付差额必须显式释放** | 预留 800 元 → 撤回 → 读库 | 该申请占的额度 `reserved` 从 **80000 → 0**，流水 `[RESERVE 80000, RELEASE 80000]` **相加 = 0**，`amount_spent` 仍为 0（付款接口 M10 才做，"实付 < 预留"那条算术已由 M7 的集成测试钉住） |
| **权限范围必须写进 `WHERE`** | 另一个部门的申请人拿 id 读/改我的申请 | 读 → **404**（本人 200）；改 → **404**（不是 403、更不是 200） |

### 跨阶段接缝四条 ✅

| 接缝 | 实测结果 |
| --- | --- |
| 登录 → 跳回原目标页 | 自检 A1/A2：未登录访问 `/admin/suppliers` → `/login`（cookie 数 0）；登录后**回到 `/admin/suppliers`** 而不是首页 |
| 界面「元」→ 请求体「分」→ 库里「分」 | 自检 C3：界面填 1000 → 列表显示 ¥1,000.00；探针接缝②：请求体 100000 → 响应 100000 → 库里 `unit_price=100000`、`amount=100000` |
| 事件条数与状态迁移一致 | 探针接缝③：提交后 3 条事件（`REQUEST_CREATED` / `REQUEST_SUBMITTED` / `APPROVAL_STARTED`），其中**恰好两条状态迁移**（`None → DRAFT`、`DRAFT → IN_APPROVAL`）—— 第三条 `from == to`，说明"建链"没有伪装成状态变更 |
| 同键重放不产生第二条记录 | 探针接缝④：同键两次提交**响应逐字节相同**（HTTP 200 / 200），事件仍 3 条、`budget_transactions` 仍只有一条 `RESERVE`；`test_api_budget.py` 另有一条撞 `UNIQUE(request_id, kind)` 的用例 |

### 测试污染物零残留 ✅

回归入口最后一步跑 `testdata_cleanup.py --scope all`：本次删除浏览器自检造的数据并**回查残留 = 0**（品类 / 部门 / 账号 / 申请四项全 0）。
测试自身（222 条）全程自清理：跑完库里仍是**品类 3 / 部门 4 / 账号 7 / 预算 2 / 申请 3 / 流水 1**（其中申请 3 条 = 你自己的 2 条样例 + M7 演示的 1 条）。

### 本轮回归的结论与遗留

- **没有发现功能退化**：M0–M7 的验收项在自动化与现场复跑下全部通过，没有一条降级。
- **当轮修掉的三处**（都是"文档/卫生"层面，与功能无关）：
  1. `AGENTS.md` 第 4 节的 ruff 范围补上 `scripts`（那个目录现在有真代码）；
  2. 5 处注释里的阶段号过期（附件 M7→**M9**、打包 M10→**M12**）；
  3. `pip-*` 残留目录 19 个（0.95 MB）按前缀核对绝对路径后删除，复查 0 个。
- **遗留问题已分类**（详见 `Development progress.md` 的 M8 四问与各阶段"真债"表）：
  - **有意留的**：付款结转的调用方在 M10；`audit_logs` 与 M10 的 7 张表待建；驳回只做终止式；看板无分页。
  - **真债三条**：① `application/coordinators/` 仍直接 import `app.database`（分层不够干净，等组合根收口）；② **没有建号 HTTP 端点**（脚本直连库建号，回归期间再次确认这是脚本的硬依赖）；③ 被拒的尝试仍未留痕（M8 之后补独立审计事件表）。


## M9 · 附件与时间线 ✅ 2026-10-04

> **状态**：后端（六步校验 / UUID 落盘 / 鉴权下载）与前端（`RequestTimeline` /
> `AttachmentSection`）都已完成，验收在接口层与页面层各过一遍。

### 新增的东西

| 位置 | 是什么 | 为什么单独一个文件 |
| --- | --- | --- |
| `domain/attachments.py` | 六步校验的**规则**（大小 / 类型 / magic bytes / 解码 / 摘要） | 纯函数、不依赖解码库，可用替身解码器毫秒级测完 |
| `infrastructure/image_probe.py` | 真解码器（Pillow） | **全项目唯一 import Pillow 的地方**；domain 通过回调拿"能不能解码" |
| `infrastructure/object_store.py` | 本地对象存储 | UUID 键 + 两层目录；磁盘上永不用用户文件名 |
| `infrastructure/records/attachment.py` | `attachments` 表 | 从属数据、关注点与主单/审批表不同 |
| `infrastructure/repositories/attachment_repo.py` | 数据访问 | 只 `flush()`；**刻意不提供物理删除**，只有 `soft_delete` |
| `boundaries/http/attachments.py` | 三条路由 | 上传 / 列表 / 下载（下载强制 `attachment` + `nosniff`） |
| 迁移 `b1c02cfdf8e5` | `attachments` 表 | 人工核对过：CHECK ×2、外键 ×2、唯一键、两个索引都生成到了 |

### 验收项逐条对账 ✅（9/9）

| # | 验收项 | 实测 |
| --- | --- | --- |
| 1 | 上传 PDF 成功，返回附件 id | ✅ `201` + 附件 id；列表接口能查到同一条（`test_upload_pdf_returns_id_and_is_listed`） |
| 2 | 上传 11 MB → 422 | ✅ `422 FILE_TOO_LARGE`，且**落盘目录仍然为空**（先校验、后落盘） |
| 3 | 改名成 `.pdf` 的 exe → 422 | ✅ `422 MAGIC_BYTES_MISMATCH`（PE 头 `MZ` 露馅） |
| 4 | 文件头是 PNG、内容非图片 → 422 | ✅ `422 IMAGE_DECODE_FAILED`（第 5 步真解码拦住） |
| 5 | 20000×20000 PNG → 422（压缩炸弹） | ✅ `422 IMAGE_TOO_LARGE`：改 IHDR 尺寸造出"文件几百字节、解出来 4 亿像素" |
| 6 | 越权下载 → **403** | ✅ 外部门账号 `403 FORBIDDEN`（本人 200）—— 附件存在但看不到它挂的申请 |
| 7 | 直接访问 `/uploads/<文件名>` 拿不到 | ✅ `404`；登录后照旧 404；**拿到真实存储文件名也一样 404** |
| 8 | 下载带 `Content-Disposition: attachment` | ✅ 中文名走 `filename*=UTF-8''…`，另有 ASCII 兜底；`X-Content-Type-Options: nosniff` |
| 9 | 时间线按正序展示全部事件 | ✅ 附件事件 `ATTACHMENT_ADDED` 进时间线且**不改变状态**（`from == to`）；连传 5 个附件，顺序 = 上传顺序（这条正是 M8 修的排序退化最容易复发的场景，现在由 `seq` 保证） |

### 实现里三个"想清楚才敢写"的点

1. **磁盘与数据库的一致性**：顺序是"先发键 → 再落盘 → 后写库"。
   `ObjectStore.new_key()` 单独存在就是为了这一步 —— 写库失败时手里得有键才能把文件删掉。
   反过来先写库的话，会留下"库里有记录、文件不存在"的附件，点下载必然 500，而且没人知道有多少条。
2. **`Content-Type` 只做兜底、不做判定**：`python-multipart` 对没带类型的 multipart 部分
   会填 `application/octet-stream`，`curl -F`、脚本客户端对 `.pdf` 也常送它。
   所以第 3 步加了**扩展名兜底**（详见下面"类型兜底"一节）——
   但兜底只补"按哪种类型核对"，第 4、5 步一步都不会少。
3. **`verify()` 不够，必须再 `load()` 一次**：本机实测五种损坏构造文件，
   `verify()` 拦不住"截断的数据流"（PNG 只校验各 chunk 的 CRC，JPEG 的扫描数据它根本没碰）。
   那次实测结果直接写进了 `image_probe.py` 的模块注释，并由 `test_image_probe.py` 钉住。

### 类型兜底：扩展名只补全声明，绝不代替校验（2026-10-04 追加）

用户点名的规则，落地在 `domain/attachments.py`：

```
content_type 缺失 或 = application/octet-stream
        ↓  只在这一种情况下启用
按扩展名补全：.pdf → application/pdf；.jpg/.jpeg → image/jpeg；.png → image/png
             其他扩展名 → 422（MISSING_CONTENT_TYPE）
        ↓
继续原有 magic bytes 校验（第 4 步）
        ↓
图片继续真实解码（第 5 步）
```

**为什么需要它**：`application/octet-stream` 不是"另一种类型"，而是"**没告诉我**"——
`python-multipart` 对没带类型的部分填它，`curl -F` 与脚本客户端对 PDF 也送它。
把它当"声明了别的类型"拒掉，挡掉的是**工具**而不是攻击者（攻击者会老实写
`application/pdf`，那正是我们要的诚实声明）。

**为什么它不是漏洞**：扩展名只回答"打算按哪种类型去核对内容"，不回答"内容是不是这种类型"。
补出来的类型照样要过文件头与真解码，`evil.exe` 改名成 `evil.pdf` 依然 422。
代码上这也是**分开的两件事**：`infer_type_from_filename` 只定类型，
`_check_magic_bytes` / `_check_image` 只管核对。

| 用例 | 实测 |
| --- | --- |
| 声明 octet-stream + 真 PDF | ✅ `201`，落库类型是**补出来的** `application/pdf`；事件快照里记了 `type_inferred: true` |
| 声明 octet-stream + `evil.exe` 改名 `evil.pdf` | ✅ `422 MAGIC_BYTES_MISMATCH`（补全没有绕过核对） |
| 声明 octet-stream + 假 PNG 内容 | ✅ `422 IMAGE_DECODE_FAILED` |
| 声明 octet-stream + `.bin`（补不出来） | ✅ `422 MISSING_CONTENT_TYPE`（不猜） |
| 声明了**具体**类型（`application/pdf`）但内容是 PNG | ✅ `422 MAGIC_BYTES_MISMATCH`（具体声明优先于扩展名） |

单测 12 条 + 集成 4 条，含边界（大小写扩展名、只有扩展名没有主名的 `.pdf`、`a.tar.gz` 只看最后一段）。

### 前端三个组件（2026-10-04 完成，M9 就此收口）

| 组件 | 数据来源 | 回答的问题 |
| --- | --- | --- |
| `components/RequestTimeline.tsx` | `request_events` | 这笔申请**经历过什么**（含上传附件，将来的询价/收货/付款） |
| `components/ApprovalTimeline.tsx`（M6 已有，未改） | `approval_tasks` + `policy_snapshot` | 现在**卡在谁那里**、还有几级 |
| `components/AttachmentSection.tsx` | 附件三接口 | 上传（用途 + 选文件）、列表、下载 |

三个刻意的取舍：

1. **两个时间轴是两个组件、两个分区**：数据来源不同、语义不同（一个会变、一个只追加），
   计划里明确要求别混 —— 混了以后 M10 想加"付款完成"就无处安放（它不是审批链上的一级）。
   `RequestTimeline` **不自己排序**：后端按 `seq` 正序给，前端再排一次等于把保证复制一份。
2. **下载走 `fetch` + Blob，不用 `<a href>`**：直接指过去会在新标签页里显示一段 JSON，
   用户只看到"无权访问"而不知道发生了什么；走 fetch 还能复用客户端的 401 → 续期 → 重放，
   并且能把 403/404 的**原文**显示出来。
3. **前端预检只是体验，边界永远在后端**：大小与扩展名在页面上当场提示（不发请求），
   但改个扩展名就能绕过它 —— 后端六步校验才是边界（页面实测里两条都验了）。

### 页面验收（`scripts\m6-页面实测.mjs` 20 → **32 项，32/32 通过**）

新增 12 项，其中 3 项是**真的在页面上操作**而不是调接口：

| 新增验收 | 实测 |
| --- | --- |
| 详情页同时有两个时间轴（事件流水 + 审批进度） | ✅ 事件 5 条，两个组件都在 |
| 事件流水按正序：创建草稿 → 提交 → 生成审批链 → 认领 → 审批通过 | ✅ 顺序与写入一致 |
| 附件区有 5 种用途 + 上传按钮 | ✅ 报价单/合同/发票/验收单/其他 |
| **在页面上选文件 → 上传成功 → 列表出现**（造真实 `FileList` + 派发 `change`，走 React 事件） | ✅ 列表显示「报价单 页面实测报价单.pdf 43 B … 下载」 |
| 上传后时间线多出「上传附件」 | ✅ 事件 5 → 6，末条说明了文件名与大小 |
| 选 11 MB → 页面当场提示「超过 10 MB」 | ✅ 不发请求 |
| 选 `.exe` → 当场提示只接受 PDF/JPG/PNG | ✅ |
| 改名成 `.pdf` 的 exe → 后端 422 `MAGIC_BYTES_MISMATCH` | ✅ 页面预检与后端边界**两条都验** |
| octet-stream 兜底没有绕过内容校验 | ✅ 同样 422 `MAGIC_BYTES_MISMATCH` |

> 光看"上传按钮点了没反应"是查不出问题的：这一轮实测撞到并修掉了一个真 bug ——
> `fetch` 里写 `Content-Type: ""` **不等于"没设置"**，浏览器会真的发一个空头，
> multipart 解析失败，后端报 `422 file: Field required`（指错方向）。
> 正确做法是 `Headers.delete("Content-Type")`。三种写法对比实测：
> `""` → 422、不传 headers → 201、`delete` → 201。已写进 `api/client.ts` 的 `send()`。

### 新增测试（222 → **307**，+85）

| 文件 | 条数 | 覆盖 |
| --- | --- | --- |
| `tests/test_attachment_rules.py` | 53（unit） | 六步校验的每一种绕过手法 + 类型兜底 12 条（含边界：正好 10 MB 放行、正好 5000 万像素放行、`image/svg+xml` 拒绝） |
| `tests/test_image_probe.py` | 9（unit） | 真解码器：合法图能解、五类损坏文件全拒、Pillow 的炸弹阈值必须高于本项目上限 |
| `tests/test_api_attachments.py` | 23（integration） | 9/9 验收表 + 类型兜底 4 条 + 软删除 + 未登录 401 + 上传不动预算（接缝） |

静态检查：`ruff` All checks passed / `mypy` 54 files 无问题 / 三条分层自查**各 0 处**
（顺带把新文件里一处"注释里出现 `import Pillow` 字样"改成中文措辞，
否则"应无输出"的自查会多一行噪音）。

**对真实服务也验过一遍**（不只是进程内测试客户端）：`requester` 登录 → 上传真 PDF → `201`
→ 下载回 35 字节、`content-type: application/pdf`、`content-disposition: attachment; … filename*=UTF-8''`
→ 改 id / 未登录 / 直连 `/uploads/` 全部 404 → 时间线第 4 条是 `ATTACHMENT_ADDED`（`IN_APPROVAL → IN_APPROVAL`）。
探针数据（1 条附件 + 1 条事件 + 磁盘文件）已按 id 精确删除，`uploads/` 复查 0 个文件。

### 还没做（M9 剩下的部分）

- 无。M9 的验收项已全部在接口层与页面层各验一遍（见上表与"页面验收"一节）。

### M9 收口核对

| 收口动作 | 状态 |
| --- | --- |
| 验收逐条勾选 | ✅ 9/9（接口层）+ 页面层 12 项新增（`m6-页面实测.mjs` 32/32） |
| 四问总结写进 `Development progress.md` | ✅ |
| 回归入口 | ✅ 退出码 0，11 步全绿，**307 passed** |
| 三条分层自查 | ✅ 各 0 处 |

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
