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
    }
)
