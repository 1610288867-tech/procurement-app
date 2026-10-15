# API · 全部 HTTP 接口与认证要求

> **用途**：查"这个功能有哪些接口、要不要登录、什么角色"时读这一份。
> 它是**接口清单的唯一权威**；[README.md](../../README.md) 只留摘要与链接。
>
> 交互式文档：后端跑起来后 <http://127.0.0.1:8000/docs>（OpenAPI 自动生成）
>
> **实测规模**：**59 条路径 / 70 个操作**（`app.openapi()["paths"]`，2026-10-14）
>
> 最后核实：2026-10-14

## 一、通用约定

| 项 | 约定 |
| --- | --- |
| 前缀 | 业务接口全在 `/api/v1/`；健康检查在 `/health/` |
| 认证 | **JWT 双令牌放 httpOnly Cookie**（access 15 分钟 / refresh 7 天，`Path` 限定到 `/api/v1/auth/refresh`） |
| 幂等 | 写操作统一带 `Idempotency-Key`；**同键同载荷重放**，**同键不同载荷 → 409** |
| 乐观锁 | 并发编辑以响应里的 `version` 为准，冲突返回 **409** |
| 错误体 | 固定形状（见下） |
| 权限 | 两层：接口级 `require_roles`（401/403）+ 数据级范围写进 `WHERE`（404）。详见 [../domain/权限矩阵.md](../domain/权限矩阵.md) |

```json
{ "error": { "code": "...", "message": "...", "request_id": "..." } }
```

`request_id` **同时进响应头和错误体** —— 出错时搜这 12 个字符就能还原整条请求链路。

## 二、健康检查与认证

| 分组 | 说明 | 认证 | 状态 |
| --- | --- | --- | --- |
| `GET /health/live`、`/health/ready`、`/health/db` | 存活 / 就绪 / 数据库版本 | 否 | ✅ M0 |
| `POST /api/v1/auth/login` | 登录（下发 httpOnly Cookie） | 否 | ✅ M3 |
| `POST /api/v1/auth/refresh`、`/logout`、`GET /me` | 刷新、登出、当前账号 | 登录 | ✅ M3 |

## 三、主数据（只读）

| 接口 | 说明 | 认证 | 状态 |
| --- | --- | --- | --- |
| `GET /api/v1/suppliers` | 合格供应商（只读；过滤**写死**，不含停用与资质过期） | **登录** | ✅ M1（2026-10-03 收口） |
| `GET /api/v1/departments`、`/categories`、`/categories/{id}`、`/budgets/mine` | 主数据（只读；第三个**含整份审批策略**，第四个的部门**只从登录态推导**） | **登录** | ✅ M2（2026-10-03 收口） |
| `/api/v1/admin/departments`、`/categories`、`/suppliers`、`/budgets` | 主数据写入与配置 | **`ADMIN`** | ✅ M3 |

## 四、采购申请与审批

| 接口 | 说明 | 认证 | 状态 |
| --- | --- | --- | --- |
| `/api/v1/requests/*` | 建草稿 / 列表 / 详情 / 改草稿 / 存明细 / 提交 / 撤回 / 丢弃 / 时间线 | 登录 + **有部门** | ✅ M5 |
| `GET /api/v1/approvals/tasks`、`/tasks/{id}` | 我的待办、任务详情（**只含当前级**，且**不含自己提的单**） | 登录 | ✅ M6 |
| `POST /api/v1/approvals/tasks/{id}/claim`、`/approve`、`/reject` | 认领（或签）、通过、驳回 | 登录 + 是当前级候选人 | ✅ M6 |
| `GET /api/v1/approvals/requests/{id}/flow` | 审批链进度（谁已批、现在轮到谁） | 登录 + 数据可见 | ✅ M6 |
| `POST /api/v1/approvals/tasks/{id}/delegate` | 委托他人 | — | ❌ **不做**（2026-10-03 定稿，见业务规则第 9 条） |

## 五、附件

| 接口 | 说明 | 认证 | 状态 |
| --- | --- | --- | --- |
| `POST` / `GET /api/v1/requests/{id}/attachments` | 上传附件（PDF / JPEG / PNG，≤ 10 MB，**六步校验**；带上传进度）、某张申请的附件列表 | 登录 + 数据可见 | ✅ M9 |
| `GET /api/v1/attachments/{id}/download` | 鉴权下载（强制 `attachment` + `nosniff`；**越权 403**、不存在 404） | 登录 + 数据可见 | ✅ M9 |
| `DELETE /api/v1/attachments/{id}` | 删除附件（**软删除**：只标记，磁盘文件留着；时间线留一条 `ATTACHMENT_REMOVED`） | 登录 + 数据可见 | ✅ M9 |

## 六、履约链（M10，8.1–8.4）

| 接口 | 说明 | 认证 | 状态 |
| --- | --- | --- | --- |
| `GET /api/v1/sourcing/queue` | 待询价队列（状态 = `SOURCING`） | **`PROCUREMENT`** | ✅ 8.1 |
| `POST` / `GET /api/v1/requests/{id}/quotations` | 登记报价（同一家只能报一次）、比价表（含"还差几家"） | 写：**`PROCUREMENT`**；读：登录 + 数据可见 | ✅ 8.1 |
| `POST /api/v1/requests/{id}/award` | 定标（条件更新抢权 + 幂等键；不满足条件 422 并列出差异） | **`PROCUREMENT`** | ✅ 8.1 |
| `POST` / `GET /api/v1/requests/{id}/orders` | 下单（幂等；金额由后端**按明细重算**）、按申请查订单（没有返回 `null`） | 写：**`PROCUREMENT`** | ✅ 8.2 |
| `GET /api/v1/orders/{id}` | 订单详情（订单 + 明细 + 收货单） | 登录 + 数据可见 | ✅ 8.2 |
| `GET /api/v1/receipts/queue` | 待收货队列（状态 = `ORDERED`） | **`RECEIVER`** / `PROCUREMENT` | ✅ 8.2 |
| `POST /api/v1/requests/{id}/receipts` | 收货登记（逐行实收；超收或没齐 → 422 并列出差异） | **`RECEIVER`**（内控：采购不能自己收货） | ✅ 8.2 |
| `POST` / `GET /api/v1/requests/{id}/invoices` | 登记发票（**可多张**，幂等；供应商须与订单一致）、发票列表（含已作废） | 写：**`FINANCE`**；读：登录 + 数据可见 | ✅ 8.3 |
| `POST /api/v1/invoices/{id}/void` | 作废发票（改状态、留痕迹；作废后**不参与金额求和**，号可重新登记） | **`FINANCE`** | ✅ 8.3 |
| `GET /api/v1/requests/{id}/match` | 三单匹配预检（三列对比 + 问题列表；**只读，不改任何状态**） | `FINANCE` / `PROCUREMENT` | ✅ 8.3 |
| `GET /api/v1/payments/queue` | 待付款队列（状态 = `INVOICED`） | **`FINANCE`** | ✅ 8.3 |
| `POST /api/v1/requests/{id}/payments` | 创建付款单（`PENDING`，幂等；只校验"已收票 + 有有效发票"，**不跑匹配**） | **`FINANCE`** | ✅ 8.4 |
| `POST /api/v1/payments/{id}/pay` | **执行付款**（触发三单匹配；条件更新抢付款权 → `SPEND` + `RELEASE` 两笔流水） | **`FINANCE`** | ✅ 8.4 |
| `POST /api/v1/payments/{id}/fail` | 标记付款失败（`FAILED` 保留痕迹，**允许重新发起**） | **`FINANCE`** | ✅ 8.4 |
| `GET /api/v1/payments/{id}`、`/api/v1/requests/{id}/payments` | 付款单详情 / 列表（含失败的尝试） | 登录 + 数据可见 | ✅ 8.4 |
| `POST /api/v1/requests/{id}/close` | 归档（`PAID → CLOSED`，终态） | **`FINANCE`** | ✅ 8.4 |

## 七、AI 制度助手（M11 / M12+）

| 接口 | 说明 | 认证 | 状态 |
| --- | --- | --- | --- |
| `POST /api/v1/chat`、`/api/v1/chat/stream` | 问制度（非流式 / **SSE 流式**） | 登录 | ✅ M11 |
| `POST /api/v1/chat/confirm-draft` | **确认 Agent 准备的采购申请草稿**（见下） | 登录 | ✅ M12+ |
| `GET /api/v1/chat/sessions`、`/sessions/{id}/messages` | 我的会话与历史消息（**只列自己的**） | 登录 | ✅ M11 |
| `POST` / `GET /api/v1/admin/knowledge-docs`、`/{id}/chunks` | 上传制度（切分 + 向量化）、列表、**看切分结果**（排障入口） | **`ADMIN`** | ✅ M11 |

### SSE 事件顺序（协议的一部分）

```
meta  → 会话 id、来源列表、是否调用模型、为什么
step  → Agent 执行步骤（只有走 Agent 时有；`tool_call` / `tool_result` / `guard`）
delta → 正文片段（可能很多条；未调用模型时只有一条）
done  → 结束（带落库后的消息 id、引用校验结果、token、成本、Agent 轮次与停止原因）
error → 出错（**不静默截断**）
```

`meta` **必须先发**：用户能在正文还没出来时就先看到引用来源，信任感强很多；
反过来（先出正文再补来源）用户读完一段断言才发现没有出处，那时的体感是"它在编"。

### `POST /api/v1/chat/confirm-draft` 为什么需要它

`create_purchase_draft` 是**写工具**，标 `mutating=True`：模型第一次调用**只校验、不落库**，
返回一个确认令牌；**人确认之后**才真正创建。理由 —— 草稿也不是无害的：
它占单号、出现在列表里、可能被同事当成真需求去审批。

| 要点 | 做法 |
| --- | --- |
| 请求体 | `{"confirmation_token": "...", "draft": {"title","category_id","reason"}}` |
| 令牌绑定 | `uuid5(actor + 标题 + 品类 + 备注)` —— **可被推算**，所以确认时必须**原样回传内容**并逐字比对；不匹配 → **404**（不告诉调用方"这份提案存在"） |
| 用谁的身份执行 | **确认者自己**（不是 Agent 的）；走**同一个用例** `create_draft` |
| 幂等键 | **令牌本身** —— 重复点确认返回 `replayed=true`，不会建第二张 |
| 未知字段 | 一律拒绝（`**kwargs` 是注入通道） |

⚠️ **前端还没有这个按钮**（后端端点已可用并有集成测试）—— 见 [RISKS.md](../../RISKS.md) 第一节第 7 项。

## 八、维护规则

- 新增/改动接口时更新本表；**接口清单只在这里有一份**
- 权限口径变化改 [../domain/权限矩阵.md](../domain/权限矩阵.md)（两处的分工是：
  本文件说"有哪些接口"，那份说"谁能用"）
