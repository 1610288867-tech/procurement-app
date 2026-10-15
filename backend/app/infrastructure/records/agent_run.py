"""Agent 运行记录（M12++）。

============================ 它存的是什么 ============================
一次 Agent 提问的**执行轨迹**：跑到第几步、每步调了什么工具、拿到什么结果。

**它不是 `request_events`，也不是 `chat_messages`**：

    `request_events`  单据的时间线（审计证据，只追加，给申请人看）
    `chat_messages`   会话里的问答消息（给用户看的历史）
    `agent_runs`      **一次 Agent 执行的内部状态**（给续跑与排障用）

混进前两者会让"这张单子经历过什么"里出现一堆工具调用记录，
也会让消息列表里多出机器人的中间步骤。

============================ 与"审计只追加"的关系 ============================
本表**允许更新**（`trace` 会随每步增长、`status` 会从 RUNNING 变成 FINISHED）——
这一点与"事件与审计只追加"（[AGENTS.md](../../../AGENTS.md) 第五章第 7 条）**不冲突**：

那条纪律管的是 **单据的审计证据**（"当时发生了什么"不能事后改）。
而这里存的是**一个正在进行的执行过程的状态**，它的本质是"可变的当前状态"，
不是"已发生的事实"。要留事实的话，落在 `chat_messages` 里的最终回答
与 `support_details` 里的轨迹快照才是审计面 —— 那两个仍然只追加。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.agent_run import RUN_STATUSES
from app.infrastructure.records.base import Base


class AgentRunRecord(Base):
    """一次 Agent 执行。"""

    __tablename__ = "agent_runs"
    __table_args__ = (
        # 状态必须在词表里。与 `chat_messages.role` 同一个写法：
        # 词表改了要**同时**改 Python 枚举与这条 CHECK（见 RISKS 第三节：
        # 新增枚举值必须同时改三处，而 Alembic autogenerate 看不见 CHECK 内容变化）。
        CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in sorted(RUN_STATUSES)) + ")",
            name="ck_agent_run_status_known",
        ),
        # 找"某个会话有哪些运行"（前端按会话列出、排障时按会话回看）
        Index("ix_agent_runs_session_created", "session_id", "created_at"),
        # 找"谁的运行" —— 可见范围过滤走这一条
        Index("ix_agent_runs_actor_status", "actor_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    #: 谁跑的。**续跑时要校验这个** —— 别人的运行不能被你接着跑。
    actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    #: 这次的问题原文。续跑时不重新问一遍，直接用记录里这个。
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 执行轨迹：`[{"round","kind","detail","call_id","tool","arguments"}]`。
    #: **每跑完一步就整列覆盖**（不是追加行）—— 一行就是一次运行的当前状态，
    #: 拆成多行会让"这次跑到哪了"要聚合才能答，而它每步都要问一次。
    trace: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    #: 停下来时写一句话（护栏触发的说明 / 异常类型）—— 排障时最想知道的那句。
    stop_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: 跑完的最终回答。**留成 NULL 表示"没跑完"** ——
    #: 与"跑完了但回答是空字符串"是两件不同的事（null ≠ ''，与全项目口径一致）。
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        # 由数据库在每次 UPDATE 时刷新：应用层忘了写时间戳时，
        # "这个 RUNNING 卡了多久"就永远算不出来，而它正是超时清理的判据。
        onupdate=func.now(),
    )
