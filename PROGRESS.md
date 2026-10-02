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
| M5 | 采购申请 | ⬜ | |
| M6 | 审批引擎 ★ | ⬜ | |
| M7 | 预算并发预留 ★ | ⬜ | |
| M8 | 附件与时间线 | ⬜ | |
| M9 | 履约链 | ⬜ | |
| M10 | AI 制度助手 | ⬜ | |
| M11 | 桌面壳与移动端 | ⬜ | |

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
- [x] `npx tsc --noEmit` / `npx vitest run`（14 passed）/ `npm run build`（gzip 74.81 KB）全绿
- [x] 既有交互不回归：登录跳回原页面、HttpOnly 生效、无兜底策略 422 原文、预算「元⇄分」、越权提示页

> 本阶段**没有改任何后端接口**，也没有新增业务功能。
> 后端那一轮的三处改动（`budgets` CHECK 约束、供应商资质有效期过滤、自检 C3 竞态）
> 属于独立的一次修复，已在后端集成测试里覆盖（unit 50 + integration 41 = **91 passed**）。

## M5 · 采购申请

- [ ] 能建草稿、改草稿、加三行明细，`amount_total` 由后端算出且正确
- [ ] 旧 `version` 提交修改 → **409**
- [ ] 同一 `Idempotency-Key` 提交两次，只产生一条申请
- [ ] 同键但改成不同金额 → **409**
- [ ] **同键并发提交（`asyncio.gather`），只产生一条申请**
- [ ] 提交后状态变 `IN_APPROVAL`，时间线上出现两条事件
- [ ] 已提交的申请不能再 `PATCH`（409）

## M6 · 审批引擎 ★

- [ ] **500 / 5000 / 50000 / 200000 元 → 审批任务数 1 / 2 / 4 / 5**
- [ ] 500 元的申请只有部门主管能看到待办
- [ ] 两人同时通过同一任务，只有一个成功，另一个 409
- [ ] 驳回后主单变 `REJECTED`，时间线能看到驳回意见
- [ ] **待办列表一次 JOIN 查完**（`echo=True` 数 SELECT 条数）
- [ ] 全部通过后主单变 `SOURCING`，时间线有 `APPROVAL_COMPLETED`
- [ ] 改品类策略（不改代码）审批链跟着变
- [ ] **改完策略后，历史申请仍能复现它当时的审批链**（`policy_snapshot`）
- [ ] **把某部门 `DEPT_HEAD` 全删掉再提交 → 422 并说明哪一级缺人**
- [ ] **`ALL` 会签：3 人 → 3 条任务、都预分配 `assignee_id`、全部通过才算完成**
- [ ] **`SINGLE` 或签：3 人 → 只生成 1 条任务，任一人认领通过即完成**
- [ ] 没有兜底的策略 → 422；有步骤永不被触发 → 422

## M7 · 预算并发预留 ★

- [ ] 总额 1000 元时并发提交两笔 800 元，只有一笔成功，另一笔 422
- [ ] 驳回后可用额度恢复
- [ ] `budget_transactions` 能看到 RESERVE / RELEASE / SPEND 三类流水
- [ ] 同一申请重放不产生第二条 `RESERVE`
- [ ] **申请 1000 元、实付 900 元后：`amount_spent` +900、该申请 `amount_reserved` 归零、`amount_total` 不变**
- [ ] **该申请三类流水相加（RESERVE − RELEASE − SPEND）= 0**
- [ ] 并发测试连跑 20 次稳定

## M8 · 附件与时间线

- [ ] 上传 PDF 成功，返回附件 id
- [ ] 上传 11 MB → 422
- [ ] 上传改名成 `.pdf` 的 exe → 422
- [ ] **文件头像 PNG、内容非图片 → 422（解码失败）**
- [ ] **超大尺寸 PNG（如 20000×20000）→ 422（压缩炸弹防护）**
- [ ] 越权下载附件 → **403**
- [ ] 直接访问 `http://127.0.0.1:8000/uploads/<文件名>` 拿不到文件
- [ ] 下载响应带 `Content-Disposition: attachment`
- [ ] 时间线按时间正序展示全部事件

## M9 · 履约链

- [ ] 全链路走通：申请 → 审批 → 询价 → 定标 → 订单 → 收货 → 发票 → 付款 → 归档
- [ ] 发票超容差 → 付款 422 并列出具体差异
- [ ] 未收货就创建付款单 → 422
- [ ] **两人同时付款，只有一个成功；`amount_spent` 只增加一次**
- [ ] **预留 1000 实付 900：流水里有 `SPEND 900` 和 `RELEASE 100`**
- [ ] **登记两张发票，匹配用的是两张之和（订单级粒度）**
- [ ] 三单匹配对比页正确显示三列和差异高亮

## M10 · AI 制度助手

- [ ] 上传制度文档，`knowledge_chunks` 里能看到切分结果
- [ ] 问文档里有的问题，回答带出处
- [ ] **问文档里没有的问题，明确说"未找到相关规定"，不编造**
- [ ] 流式回答逐字出现
- [ ] `meta` 事件里有来源列表
- [ ] 检索为空时日志里能看到"未调用模型"
- [ ] **引用不存在编号的回答能被 `check_citation_integrity` 发现**
- [ ] **上传同一制度第 2 版，检索只命中第 2 版**

## M11 · 桌面壳与移动端

- [ ] 双击 `.exe` 能打开，窗口无浏览器地址栏
- [ ] APK 装到手机上能登录、能看到待办
- [ ] 桌面端和移动端用**同一份 `frontend/src`**，没有分叉出第二套代码

---

## 三条"静默失败"约束（每阶段都要回头看）

违反**不会报错，只会算错**，所以必须靠验收项主动去撞：

| 约束 | 怎么撞它 |
| --- | --- |
| **审批链不许为空** | M2 提交无兜底策略 → 422 |
| **预留与实付差额必须显式释放** | M7/M9 实付小于预留时，断言 `reserved` 归零 |
| **权限范围必须写进 `WHERE`** | M3 换 id 取别人的申请 → 404 |

## 阶段结束时的两件事

1. **本文件**：把该阶段的验收项逐条勾掉，并更新上方「进度总览」的状态与完成日期。
2. **[Development progress.md](Development%20progress.md)**：回答四问——
   学到了什么 / 卡在哪 / 有没有留坑 / 为什么用这套技术栈。**回答要精炼简洁。**

验收没过就不进下一阶段；四问没写就不算收尾。
