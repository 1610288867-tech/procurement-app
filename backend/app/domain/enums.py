"""采购主流程的枚举（M5）。

这一层是纯 Python，**不 import 任何框架**。

============================ 为什么状态与事件类型要分开两个模块 ============================
状态（`RequestStatus`）回答"这张单子现在在哪"；
事件类型（`domain/events.py`）回答"它经历过什么"。

它们看起来成对出现，但**不是一回事**：一次状态变更会写一条事件，可事件里还有
"创建草稿"这种**不改变状态**的动作（草稿建出来时本来就是 DRAFT）。
把它们混成一个枚举，早晚会出现"用事件类型当状态用"的代码——
那样事件的只追加语义就保不住了（有人会想去"修正"事件）。

============================ 字符串值即数据库列值 ============================
这些常量的**字面值会直接落进数据库列**，所以：
  - 不许改字面值（改了要一起写迁移刷历史数据）；
  - 不许在别处另写一份字符串（用常量，别写 `"DRAFT"`）。
"""

from __future__ import annotations

from enum import StrEnum

# ============================ 付款状态（M10 / 8.4，实施计划 2455-2461） ============================
PAYMENT_PENDING = "PENDING"  # 刚创建，还没付
PAYMENT_PAID = "PAID"  # 已执行付款
#: 付款尝试失败（**保留痕迹，允许重新发起**）。
#: ⚠️ 这一档与 `uq_payment_active` 的部分唯一索引是配套的：
#: 索引的条件是 `status <> 'FAILED'`，所以失败之后还能再建付款单 ——
#: 普通 `UNIQUE(request_id)` 会让"允许重新发起"这条规则**根本执行不了**。
PAYMENT_FAILED = "FAILED"
PAYMENT_STATUSES: tuple[str, ...] = (PAYMENT_PENDING, PAYMENT_PAID, PAYMENT_FAILED)

# 采购申请主单状态（M5 四个 + M6 两个 + M10 履约链全部）
STATUS_DRAFT = "DRAFT"
STATUS_IN_APPROVAL = "IN_APPROVAL"
STATUS_DISCARDED = "DISCARDED"
STATUS_WITHDRAWN = "WITHDRAWN"
STATUS_SOURCING = "SOURCING"  # 审批通过，进入询价 / 定标（M6）
STATUS_REJECTED = "REJECTED"  # 任一级驳回（M6）
STATUS_AWARDED = "AWARDED"  # 已定标，等待下单（M10 / 8.1）
STATUS_ORDERED = "ORDERED"  # 已下单，等待收货（M10 / 8.2）
STATUS_RECEIVED = "RECEIVED"  # 已收货，等待发票（M10 / 8.2）
STATUS_INVOICED = "INVOICED"  # 已收到发票，等待付款（M10 / 8.3）
STATUS_PAID = "PAID"  # 已付款，等待归档（M10 / 8.4）
STATUS_CLOSED = "CLOSED"  # 已归档（终态）


class RequestStatus(StrEnum):
    """主单状态。

    用 `StrEnum` 而不是普通 `Enum`：它的成员**本身就是 str**，
    直接进 SQL 参数、直接做 JSON 字段都不用再 `.value` 一次，
    少一层转换就少一处写错的机会。
    """

    DRAFT = STATUS_DRAFT
    IN_APPROVAL = STATUS_IN_APPROVAL
    DISCARDED = STATUS_DISCARDED
    WITHDRAWN = STATUS_WITHDRAWN
    SOURCING = STATUS_SOURCING
    REJECTED = STATUS_REJECTED
    AWARDED = STATUS_AWARDED
    ORDERED = STATUS_ORDERED
    RECEIVED = STATUS_RECEIVED
    INVOICED = STATUS_INVOICED
    PAID = STATUS_PAID
    CLOSED = STATUS_CLOSED
    # ⚠️ 到此为止是**全部** 12 个状态里的 11 个正常态。
    # `PARTIALLY_RECEIVED` **永远不该加**：第一版只做整单收货，
    # 留一个走不到的状态会让"合法/非法转移"那批测试失去意义
    # （`domain/state_machine.py` 的文件头写了这条纪律）。


#: 审批任务状态（M6）。
#: **刻意没有"还没轮到"这一档** —— 那件事由 `approval_flows.current_step_order`
#: 表达（一个概念只存一处，两份状态必然漂移）。
TASK_PENDING = "PENDING"
TASK_APPROVED = "APPROVED"
TASK_REJECTED = "REJECTED"
TASK_SKIPPED = "SKIPPED"
TASK_CANCELLED = "CANCELLED"

TASK_STATUSES: tuple[str, ...] = (
    TASK_PENDING,
    TASK_APPROVED,
    TASK_REJECTED,
    TASK_SKIPPED,
    TASK_CANCELLED,
)

#: 审批流状态（M6）
FLOW_RUNNING = "RUNNING"
FLOW_APPROVED = "APPROVED"
FLOW_REJECTED = "REJECTED"
FLOW_CANCELLED = "CANCELLED"

FLOW_STATUSES: tuple[str, ...] = (FLOW_RUNNING, FLOW_APPROVED, FLOW_REJECTED, FLOW_CANCELLED)

#: 审批模式
MODE_SINGLE = "SINGLE"
MODE_ALL = "ALL"


#: 预算流水类型（M7）。三类合起来必须能还原预算的**每一次**变动：
#: `RESERVE`（提交时预留） / `RELEASE`（驳回、撤回，以及"预留 − 实付"的差额） / `SPEND`（付款结转）。
#:
#: 注意 `RELEASE` 有两种来源，这正是"差额必须显式释放"那条规则的落点：
#: 少了差额那一条，预留会永久挂在账上，而账面看不出任何异常。
TX_RESERVE = "RESERVE"
TX_RELEASE = "RELEASE"
TX_SPEND = "SPEND"

BUDGET_TX_KINDS: tuple[str, ...] = (TX_RESERVE, TX_RELEASE, TX_SPEND)


# 紧急程度。规则 DSL 里会用到（`{"eq": ["urgency", "URGENT"]}`），
# 所以它既是数据库列值，也是策略上下文里的取值。
URGENCY_NORMAL = "NORMAL"
URGENCY_URGENT = "URGENT"

URGENCIES: tuple[str, ...] = (URGENCY_NORMAL, URGENCY_URGENT)

DEFAULT_URGENCY = URGENCY_NORMAL


#: 附件用途（M9）。与 `docs/实施计划` 第 2 步的清单一致。
#: 它是**给人看的分类**，不参与任何校验判定 —— 上传时校验的是内容与大小，
#: 不是"这张发票该不该挂在这个环节"（那属于 M10 的履约链）。
ATTACH_QUOTATION = "QUOTATION"
ATTACH_CONTRACT = "CONTRACT"
ATTACH_INVOICE = "INVOICE"
ATTACH_RECEIPT = "RECEIPT"
ATTACH_OTHER = "OTHER"

ATTACHMENT_KINDS: tuple[str, ...] = (
    ATTACH_QUOTATION,
    ATTACH_CONTRACT,
    ATTACH_INVOICE,
    ATTACH_RECEIPT,
    ATTACH_OTHER,
)

DEFAULT_ATTACHMENT_KIND = ATTACH_OTHER
