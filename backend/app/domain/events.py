"""事件类型词表（M5）。

这一层是纯 Python，**不 import 任何框架**。

============================ 词表要定死，理由和 scope 一样 ============================
`request_events.event_type` 是**字符串列**。如果各处随手写字符串，
查询时就得先猜"到底是 SUBMITTED 还是 REQUEST_SUBMITTED"，猜错不会报错，
只会查不到——时间线页面显示成空列表，看起来像"没有事件"。

所以：**只在事件真正产生的地方 import 这里的常量**，别处不许再写字面量。

============================ 命名规矩 ============================
    REQUEST_*   主单本身的动作（创建 / 提交 / 撤回 / 丢弃）
    ITEMS_*     明细被整体替换
    APPROVAL_*  审批相关（M6 起）
    BUDGET_*    预算相关（M7 起）
    ATTACHMENT_* 附件相关（M9 起）

**M5 只写它真正会用的那几个**：一次写全套词表，等于给后面的阶段留了一堆
"看起来存在、其实没有任何代码会产生它"的常量，读代码的人会被误导。
"""

from __future__ import annotations

REQUEST_CREATED = "REQUEST_CREATED"
REQUEST_UPDATED = "REQUEST_UPDATED"
REQUEST_SUBMITTED = "REQUEST_SUBMITTED"
REQUEST_WITHDRAWN = "REQUEST_WITHDRAWN"
REQUEST_DISCARDED = "REQUEST_DISCARDED"

# ---- 审批（M6）----
APPROVAL_STARTED = "APPROVAL_STARTED"
APPROVAL_CLAIMED = "APPROVAL_CLAIMED"
APPROVAL_APPROVED = "APPROVAL_APPROVED"
APPROVAL_REJECTED = "APPROVAL_REJECTED"
APPROVAL_COMPLETED = "APPROVAL_COMPLETED"

# ---- 附件（M9）----
#: 上传成功记一条。**不改变状态**（`from_status == to_status`），
#: 只是让时间线能回答"这份报价单是什么时候挂上来的"。
#: 注意：它和审批链视图是两套数据（见计划里"别混成一张表"那条）。
ATTACHMENT_ADDED = "ATTACHMENT_ADDED"

#: 删除附件（软删除）记一条。
#:
#: ============================ 为什么删除也要留事件 ============================
#: 附件是审计证据：**"曾经附过一份报价单，后来被删了"本身就是必须留痕的事实**。
#: 只把行标记成删除、不写事件的话，时间线上看不出任何变化 ——
#: 事后查"这份合同怎么没了"就永远查不出来。
#:
#: 与 `ATTACHMENT_ADDED` 一样，它**不改变主单状态**。
ATTACHMENT_REMOVED = "ATTACHMENT_REMOVED"

# ---- 询价与定标（M10 / 8.1）----
#: 登记一家报价。**不改变主单状态**（主单还在 SOURCING）—— 它是过程动作。
QUOTATION_ADDED = "QUOTATION_ADDED"

#: 定标。**这一条改变主单状态**（`SOURCING → AWARDED`）。
#: payload 里记下成交供应商与成交单价 —— 订单金额以它为准（ER 图第 136-142 行）。
QUOTATION_AWARDED = "QUOTATION_AWARDED"

# ---- 订单与收货（M10 / 8.2）----
#: 下单。**改变主单状态**（`AWARDED → ORDERED`）。
#: payload 里记下订单号、供应商与**订单金额** —— 它是三单匹配与预算结转的基准，
#: 与申请金额不一致是正常的（订单金额通常更小）。
ORDER_PLACED = "ORDER_PLACED"

#: 收货。**改变主单状态**（`ORDERED → RECEIVED`，且只有**收齐**才转）。
#: payload 里记下逐行实收数量：三单匹配要用它（订单级粒度下是各行之和）。
GOODS_RECEIVED = "GOODS_RECEIVED"

# ---- 发票与三单匹配（M10 / 8.3）----
#: 登记发票。**这一条改变主单状态**（`RECEIVED → INVOICED`）。
#: payload 里记下发票号、金额与**登记后的发票金额合计** ——
#: 三单匹配比的是"所有发票之和"，只记单张看不出整单的全貌。
INVOICE_ADDED = "INVOICE_ADDED"

#: 作废发票。**不改变主单状态**（主单留在 `INVOICED`：
#: "这张单子收过票"是历史事实，不会因为票作废而没发生过）。
#: 作废进时间线而不删行 —— 与 M9 的 `ATTACHMENT_REMOVED` 同一条口径：
#: "曾经登记过、后来作废了"本身就是必须留存的事实，否则事后查
#: "这张票怎么没了 / 谁作废的"永远查不出来。
INVOICE_VOIDED = "INVOICE_VOIDED"

# ---- 付款与归档（M10 / 8.4）----
#: 创建付款单（**不改变主单状态**：钱还没动，主单仍是 `INVOICED`）。
#: 它进时间线是因为"谁在什么时候准备付多少"本身是内控要看的事实。
PAYMENT_CREATED = "PAYMENT_CREATED"

#: 执行付款。**改变主单状态**（`INVOICED → PAID`）。
#: payload 里带**实付金额**与**释放的差额** —— M7 那条"差额必须显式释放"的规则
#: 在时间线上要看得见（流水里有 `SPEND`/`RELEASE`，事件里要有对应的两个数）。
PAYMENT_COMPLETED = "PAYMENT_COMPLETED"

#: 付款尝试失败（**保留痕迹、允许重新发起**）。
#: 与 `PAYMENT_COMPLETED` 分开两个事件类型，而不是共用一个加状态字段：
#: 时间线上"付成功了"和"付失败了"给人的判断完全不同，不该让读的人再去解析字段。
PAYMENT_FAILED = "PAYMENT_FAILED"

#: 归档。**改变主单状态**（`PAID → CLOSED`，终态）。
REQUEST_CLOSED = "REQUEST_CLOSED"

#: 全套事件类型。测试拿它做白名单断言：
#: 出现词表以外的事件 = 有人绕过常量直接写了字符串，时间线上会显示成英文码。
EVENT_TYPES: frozenset[str] = frozenset(
    {
        REQUEST_CREATED,
        REQUEST_UPDATED,
        REQUEST_SUBMITTED,
        REQUEST_WITHDRAWN,
        REQUEST_DISCARDED,
        APPROVAL_STARTED,
        APPROVAL_CLAIMED,
        APPROVAL_APPROVED,
        APPROVAL_REJECTED,
        APPROVAL_COMPLETED,
        ATTACHMENT_ADDED,
        ATTACHMENT_REMOVED,
        QUOTATION_ADDED,
        QUOTATION_AWARDED,
        ORDER_PLACED,
        GOODS_RECEIVED,
        INVOICE_ADDED,
        INVOICE_VOIDED,
        PAYMENT_CREATED,
        PAYMENT_COMPLETED,
        PAYMENT_FAILED,
        REQUEST_CLOSED,
    }
)
