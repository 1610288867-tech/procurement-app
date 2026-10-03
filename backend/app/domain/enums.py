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

# 采购申请主单状态（M5 四个 + M6 两个；其余状态随 M7–M10 逐步加）
STATUS_DRAFT = "DRAFT"
STATUS_IN_APPROVAL = "IN_APPROVAL"
STATUS_DISCARDED = "DISCARDED"
STATUS_WITHDRAWN = "WITHDRAWN"
STATUS_SOURCING = "SOURCING"  # 审批通过，进入询价 / 定标（M6）
STATUS_REJECTED = "REJECTED"  # 任一级驳回（M6）


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


# 紧急程度。规则 DSL 里会用到（`{"eq": ["urgency", "URGENT"]}`），
# 所以它既是数据库列值，也是策略上下文里的取值。
URGENCY_NORMAL = "NORMAL"
URGENCY_URGENT = "URGENT"

URGENCIES: tuple[str, ...] = (URGENCY_NORMAL, URGENCY_URGENT)

DEFAULT_URGENCY = URGENCY_NORMAL
