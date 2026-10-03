"""审批流与审批任务的表（M6）。

命名纪律同其他 records：ORM 映射类一律 `*Record`，与 `domain/` 的业务对象分开。

============================ 这两张表的分工 ============================
    approval_flows   一次审批实例。存**依据**（policy_snapshot）和**现在走到哪一级**
                     （current_step_order）。
    approval_tasks   这一级上的具体任务。一次提交会把**所有级**的任务都建出来。

============================ 三个不能省的列 ============================
1. **`approval_flows.current_step_order`** —— "现在轮到谁"的**唯一事实来源**。
   没有它，"某一级的任务已建出、但还没轮到"在库里无法表达：所有级都躺在 `PENDING` 里，
   高级别审批人会提前看到待办，甚至能把还没轮到的级别直接批掉。

2. **`approval_flows.policy_snapshot` + `policy_version`** —— 存"提交那一刻策略的原件"。
   没有它们，管理员改一次阈值，就再也说不清历史申请当时为什么只走两级：
   任务表只记录了**结果**，没记录**依据**。

3. **`approval_tasks.request_id`** —— 受控冗余（与 `flow_id` 同时写入）。
   待办列表是全系统最频繁的查询，有它就**一次 JOIN 查完**；没有它就得再过一层
   `approval_flows`，或者退化成 N+1。

============================ 一个刻意的不加 ============================
`approval_tasks.status` 里**没有"还没轮到"**（不加 `WAITING`）。见上面第 1 条：
同一件事存两处必然漂移。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import (
    FLOW_STATUSES,
    MODE_ALL,
    MODE_SINGLE,
    TASK_STATUSES,
)
from app.infrastructure.records.base import Base


class ApprovalFlowRecord(Base):
    """一次审批实例。"""

    __tablename__ = "approval_flows"
    __table_args__ = (
        # 一笔申请第一版只有一个审批流（撤回后重新提交会建新的？不会：撤回是终态）
        UniqueConstraint("request_id", name="uq_flow_request"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{item}'" for item in FLOW_STATUSES) + ")",
            name="ck_flow_status_known",
        ),
        CheckConstraint(
            "policy_version >= 1 AND current_step_order >= 0",
            name="ck_flow_numbers",
        ),
        Index("ix_flows_request", "request_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_requests.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RUNNING")
    #: 现在轮到哪一级（= ApprovalStep.order）。**唯一事实来源**，见模块文档。
    current_step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 提交那一刻的品类审批策略原件（深拷贝，不是品类 id）
    policy_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    #: 那份策略当时挂在品类的第几版
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ApprovalTaskRecord(Base):
    """一级上的审批任务。一次提交把**所有级**的任务都建出来。"""

    __tablename__ = "approval_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{item}'" for item in TASK_STATUSES) + ")",
            name="ck_task_status_known",
        ),
        CheckConstraint(
            f"mode IN ('{MODE_SINGLE}', '{MODE_ALL}')", name="ck_task_mode_known"
        ),
        # 待办列表主查询：按人 + 状态。全系统最频繁的查询，必须走索引
        Index("ix_tasks_assignee_status", "assignee_id", "status"),
        # 步骤完成判定：按流 + 级
        Index("ix_tasks_flow_step", "flow_id", "step_order"),
        # 审批链进度视图：按申请取全部任务
        Index("ix_tasks_request", "request_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    flow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("approval_flows.id", ondelete="CASCADE"), nullable=False
    )
    #: 受控冗余：与 flow_id 同时写入，专为"待办一次 JOIN 查完"服务
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_requests.id", ondelete="CASCADE"), nullable=False
    )
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default=MODE_SINGLE)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    #: NULL = 未认领（只有或签会这样）；会签在建任务时就预分配
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 乐观锁：两个人同时点"通过"只有一个成功
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
