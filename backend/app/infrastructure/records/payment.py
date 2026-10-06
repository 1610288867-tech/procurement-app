"""付款表（M10 / 8.4）。

============================ 这一张表的约束是本阶段最要紧的东西 ============================
实施计划里有两处说法，看起来矛盾：

    2460 行：`FAILED` → 付款尝试失败（**保留痕迹，允许重新发起**）
    2463 行：一笔申请第一版只允许一笔付款：`UNIQUE(request_id)`

**普通 `UNIQUE(request_id)` 会让"允许重新发起"根本执行不了** ——
第一次失败之后再建付款单会撞唯一约束，而它表现为一个数据库冲突，
看起来像偶发故障。文档第 2484 行已经给出了正解（部分唯一索引），照它做：

    CREATE UNIQUE INDEX uq_payment_active ON payments (request_id)
        WHERE status <> 'FAILED';

于是不变量变成 **"同一张申请至多一张**有效**付款单"**（`PENDING` / `PAID`），
而失败的尝试可以累积多行 —— 那正是"保留痕迹"要的样子。

判据（写进 `PROGRESS.md`）：**这条约束防的是"同时存在两条"，还是"历史上出现过两条"？**
前者用部分唯一索引，后者才用普通唯一约束。

============================ 并发保护不靠那个索引 ============================
索引只是**数据库兜底**。主防线是 `UPDATE payments SET status='PAID'
WHERE id = ? AND status = 'PENDING'` 的 `rowcount != 1 → 409`（条件更新），
而且它必须在 `spend_budget` **之前** —— 先抢到付款权的那一方才去动预算，
抢输的一方在动钱之前就退出了（实施计划 2480 行）。

⚠️ 两者不是重复：索引挡的是"**建出**两张有效付款单"，
条件更新挡的是"**同一张付款单被付两次**"。两件事都得挡。

============================ `attempt_no` 为什么值得单独一列 ============================
同一张申请可能试了三次才付成功（`FAILED` 的行留着）。界面上要能显示
"这是第 2 次尝试"，否则财务看到三行 `FAILED`/`PENDING` 会以为系统重复建单了。
用 `MAX(attempt_no) + 1` 在**同一个事务**里算 —— 并发下可能与另一条
`FAILED` 行撞号，所以 `UNIQUE(request_id, attempt_no)` 兜底；
撞号时由用例层重试一次（比在这里写循环干净：重试是**用例**的语义）。

============================ `amount` 记的是**实付** ============================
= 有效发票金额之和（订单级粒度，业务规则第 6 条）。它**可能不等于订单金额** ——
容差之内就算匹配通过，差额由 M7 的"实付小于预留则显式释放"处理。
所以这一列是三单匹配的输出，不是输入端。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import PAYMENT_STATUSES
from app.infrastructure.records.base import Base


class PaymentRecord(Base):
    """付款单。一笔申请**至多一张有效**（部分唯一索引），失败的尝试留着。"""

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payment_amount_positive"),
        CheckConstraint("attempt_no > 0", name="ck_payment_attempt_positive"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in PAYMENT_STATUSES) + ")",
            name="ck_payment_status_known",
        ),
        #: ⚠️ **至多一张有效付款单**（`PENDING` / `PAID`）；
        #: `FAILED` 的不占位 —— 这样"失败后重新发起"才走得通。
        Index(
            "uq_payment_active",
            "request_id",
            unique=True,
            postgresql_where=text("status <> 'FAILED'"),
        ),
        UniqueConstraint("request_id", "attempt_no", name="uq_payment_attempt"),
        Index("ix_payments_request_created", "request_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # CASCADE：付款单是申请的从属数据（与报价、订单、发票同一处理）
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_requests.id", ondelete="CASCADE"), nullable=False
    )
    #: 实付金额（分）= 有效发票金额之和。见模块说明最后一段
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 同一张申请第几次尝试（`FAILED` 的行会累积）
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: 失败的**原因**（一句话）。成功时为 NULL
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # RESTRICT：谁发起的付款要留得住（内控：付款要有责任人）
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: 付款成功那一刻（`PENDING` / `FAILED` 时为 NULL）
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
