# ER 图与数据模型（第一版 15 张表）

> M0 产物之一。领域设计第六节共 22 张表，**第一版只画/只建 15 张**：
> 砍掉知识库那 5 张（M10 才用）和 `budget_transactions`（M7 才用）。
>
> 画图工具用 draw.io 或纸笔都行，**关键是画清楚下面第三节那三条关系链**。

## 一、表清单（15 张，按阶段分组）

| 阶段 | 表 | 一句话说明 |
| --- | --- | --- |
| M0/M1 | `suppliers` | 供应商（M1 的贯通载体，只有 4 个字段） |
| M2 | `departments` | 部门树（含 `parent_id`） |
| M2 | `purchase_categories` | 采购品类，**承载动态表单与审批策略（两个 JSONB）** |
| M2 | `budgets` | 预算（部门 × 年度 × 品类，三个金额字段都是「分」） |
| M3 | `users` | 账号（含 `token_version`，冻结时 +1 让旧 token 失效） |
| M3 | `user_roles` | 用户 ↔ 角色（多对多，一个用户可多角色） |
| M5 | `purchase_requests` | 采购申请主单 |
| M5 | `purchase_items` | 申请明细行 |
| M5 | `request_events` | **只追加**的事件流（时间线直接读它） |
| M5 | `idempotency_records` | 幂等记录（含 `IN_PROGRESS` 占位） |
| M6 | `approval_flows` | 一次审批实例（含**策略快照**） |
| M6 | `approval_tasks` | 审批任务（或签/会签） |
| M8 | `attachments` | 附件（报价单/合同/发票/验收照片） |
| M9 | `purchase_orders` + `purchase_order_lines` | 采购订单与其明细 |
| M9 | `quotations` | 供应商报价 |
| M9 | `goods_receipts` + `goods_receipt_lines` | 收货单与其明细 |
| M9 | `invoices` / `payments` | 发票 / 付款单 |
| M3+ | `audit_logs` | 审计，**只追加** |

> 表数量按"逻辑表"算是 15 组（把 `*_lines` 与主表算作一组）。
> 到 M9 全部建完，加上 M7 的 `budget_transactions` 和 M10 的 5 张知识库表，合计 22 张。

## 二、关键字段与取值（写迁移时的依据）

### 组织与账号

```
departments     id, name, code, parent_id, active
users           id, username, password_hash, full_name, department_id, active, token_version
user_roles      user_id, role
```

- `users.token_version`：冻结账号时 `+1`，签发与校验都比对 → **旧 token 立刻失效，不用维护黑名单**
- 角色常量（8 个）：`REQUESTER / DEPT_MANAGER / DEPT_HEAD / PROCUREMENT / FINANCE / RECEIVER / GM / ADMIN`
  - **`GM` 由 `ADMIN` 兼任**（给同一个用户挂两个角色即可，`user_roles` 天生支持）

### 基础主数据

```
suppliers              id, name, code, qualification_expires_at, active, disabled_reason
purchase_categories    id, code, name, form_schema(JSONB), approval_policy(JSONB), version, active
budgets                id, department_id, category_id, year,
                       amount_total, amount_reserved, amount_spent      ← 全部 BIGINT「分」
```

- `budgets` 唯一约束：`(department_id, year, category_id)`
- `budgets` CHECK 约束：`amount_reserved >= 0 AND amount_spent >= 0
  AND amount_reserved + amount_spent <= amount_total` ← **兜底，应用层总有一条路径没想到**
- `purchase_categories.form_schema` / `approval_policy` 用 **JSONB**（不用 TEXT）

### 采购主流程

```
purchase_requests   id, code, requester_id, department_id, category_id, budget_id,
                    title, reason, amount_total, urgency, status, version,
                    form_data(JSONB), created_at, updated_at, submitted_at, closed_at
purchase_items      id, request_id, line_no, name, spec, quantity, unit_price, amount
request_events      id, request_id, event_type, from_status, to_status,
                    actor_id, payload(JSONB), created_at
idempotency_records id, actor_id, scope, key, payload_hash,
                    status(IN_PROGRESS/COMPLETED/FAILED), response(JSONB), created_at
```

- `amount_total` **由后端按明细行重算**，绝不信任前端传的值
- `idempotency_records` 唯一约束：`(actor_id, scope, key)`；`scope` 用**端点标识**
- `request_events` **只追加**，永不 UPDATE / DELETE

### 审批

```
approval_flows   id, request_id, status(RUNNING/APPROVED/REJECTED/CANCELLED),
                 policy_snapshot(JSONB), policy_version, started_at, completed_at
approval_tasks   id, flow_id, request_id, step_order, role, mode(SINGLE/ALL), status,
                 assignee_id, decided_by, decided_at, comment, version, delegated_from
```

- **`policy_snapshot` 不能省**：存"提交那一刻的 `approval_policy` 原件"。
  没有它，管理员改一次阈值，你就再也说不清历史申请当时为什么只走两级
- `approval_tasks.request_id` 是**受控冗余**（与 `flow_id` 同时写入），
  专为"待办一次 JOIN 查完"服务——**待办是全系统最频繁的查询**
- `approval_tasks.status`：`PENDING / APPROVED / REJECTED / SKIPPED / CANCELLED / DELEGATED`

### 履约与结算（M9）

```
quotations            id, request_id, supplier_id, amount, currency, valid_until, status, attachment_id
purchase_orders       id, request_id, supplier_id, amount, status, order_no, ordered_at
purchase_order_lines  id, order_id, item_id, quantity, unit_price, amount
goods_receipts        id, request_id, order_id, receiver_id, received_at, status, note
goods_receipt_lines   id, receipt_id, order_line_id, received_quantity, accepted_quantity
invoices              id, request_id, supplier_id, invoice_no, amount, tax_amount, issued_at, matched
payments              id, request_id, amount, status(PENDING/PAID/FAILED), paid_at, method, reference
audit_logs            id, actor_id, action, entity_type, entity_id, before(JSONB), after(JSONB), created_at
```

- `purchase_order_lines.item_id` 是**快照复制**，不是指向申请明细的外键链路——
  **订单一旦生成就是独立凭证**，申请后续不能改它
- `payments` 第一版：`UNIQUE(request_id)`，**一笔申请只允许一笔付款**（不做部分付款）
- 收货只做**整单收货**，`ORDERED → RECEIVED`，
  **第一版不在状态机里写 `PARTIALLY_RECEIVED`**（留一个走不到的状态会让状态机测试失去意义）

## 三、三条必须画清楚的关系链

这是 M0 第 5 步的重点，画图时把这三条画对就够了：

### 链一：申请 → 明细（一对多）

```
purchase_requests 1 ──── n purchase_items
        │
        └─ amount_total = Σ(items.amount)      ← 后端重算，不信任前端
```

### 链二：申请 → 审批流 → 审批任务（一对多再一对多）

```
purchase_requests 1 ──── n approval_flows 1 ──── n approval_tasks
                                    │
                                    └─ policy_snapshot（这一层是"依据"）
        │
        └─ status 由"审批完成事件"驱动推进：IN_APPROVAL → SOURCING
           ★ 审批模块不直接改主单字段，两层通过事件解耦
```

### 链三：履约链（链式，每一步都以上一步为前置）

```
purchase_requests
   │  SOURCING
   ├──> quotations（≥3 家）──> 定标 AWARDED
   │                              │
   │                              └──> purchase_orders + lines ──> ORDERED
   │                                        │
   │                                        └──> goods_receipts + lines ──> RECEIVED
   │                                                  │
   │                                                  └──> invoices ──> INVOICED
   │                                                            │
   │                                                            └──> payments ──> PAID ──> CLOSED
   │
   └── budgets.amount_reserved 全程跟随：
       提交时 RESERVE → 驳回/撤回 RELEASE → 付款时 SPEND(实付) + RELEASE(差额)
```

## 四、索引（第一天就加）

```sql
purchase_requests (requester_id, status)
purchase_requests (department_id, category_id, status, created_at)
approval_tasks (assignee_id, status)          -- 待办列表主查询，最关键
approval_tasks (flow_id, step_order)
approval_tasks (request_id)
request_events (request_id, created_at)
audit_logs (entity_type, entity_id, created_at)
idempotency_records UNIQUE (actor_id, scope, key)
budget_transactions UNIQUE (request_id, kind)  -- M7 起，防重复预留
budgets UNIQUE (department_id, year, category_id)
suppliers (code)
```

`approval_tasks (assignee_id, status)` 这个索引很关键——"我的待办"是全系统访问最频繁的查询。

## 五、主单状态机（13 个状态）

```
DRAFT ──> IN_APPROVAL ──> SOURCING ──> AWARDED ──> ORDERED ──> RECEIVED
                                                                    │
                                                                    v
                                                          INVOICED ──> PAID ──> CLOSED

分支终态：REJECTED（驳回） / WITHDRAWN（撤回） / DISCARDED（丢弃草稿）
```

**两条业务规则长在状态机里**：

1. **`ORDERED` 之后没有 `WITHDRAWN` 出口** —— 货已经下了，不能随便撤
2. **`RECEIVED` 之前不允许创建付款单** —— "收货才能付款"靠状态机强制，不靠前端按钮

第一版**不含** `PARTIALLY_RECEIVED`（只做整单收货）。要加的时候按实施计划 5.5 节 E 的四步一起加状态和转移。
