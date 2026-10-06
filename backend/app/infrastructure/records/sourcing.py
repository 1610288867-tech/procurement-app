"""供应商报价表（M10 / 8.1）。

与采购主单、审批表分开放一个文件：它回答的是"这一轮询价，各家报了多少"，
关注点与状态机、金额账本都不同（虽然金额也是分）。

============================ 三条不许破的纪律 ============================
1. **`amount` 是整数分**（`BIGINT`），与全项目一致 —— 单位换算只允许出现在
   `frontend/src/lib/money.ts` 与后端的单一入口。
2. **同一家供应商在同一张申请上只能报一次**：`UNIQUE(request_id, supplier_id)`。
   少了它，"三家比价"可以靠同一家传三次凑出来 —— 而那条控制的全部意义
   就是"不同供应商之间的竞争"，凑数等于把控制架空。
3. **`valid_until` 为 NULL = 长期有效**（不是"已过期"）。
   `domain/sourcing_policy.is_expired` 认这个语义，测试钉住了它。

============================ 状态里为什么没有 DRAFT ============================
业务规则原文写的是 `DRAFT / SUBMITTED / EXPIRED / SELECTED`。本项目**砍掉 DRAFT**：
询价是采购专员在工作台上的连续动作，"先存个报价草稿"这一版没有使用场景，
而留一个永远走不到的状态，会让状态校验的测试失去意义
（同 `domain/state_machine.py` 与 `domain/enums.py` 的一贯做法）。

`EXPIRED` 是**唯一一个靠时间自己成立的状态**：判定只认 `valid_until < now`，
状态列由协调者在读比价表 / 定标时**惰性结算**（业务规则那条
"资质过期用查询时过滤、不引定时任务"的同源做法）。
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
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.sourcing_policy import QUOTATION_STATUSES
from app.infrastructure.records.base import Base


class QuotationRecord(Base):
    __tablename__ = "quotations"
    __table_args__ = (
        # 状态字面值由领域层提供：两处各写一份必然漂移
        CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in QUOTATION_STATUSES) + ")",
            name="ck_quotation_status_known",
        ),
        CheckConstraint("amount > 0", name="ck_quotation_amount_positive"),
        # 同一家在同一张申请上只能报一次（见文件头第 2 条）
        UniqueConstraint("request_id", "supplier_id", name="uq_quotation_request_supplier"),
        # 比价表主查询：按申请取全部报价
        Index("ix_quotations_request_created", "request_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # CASCADE：报价是申请的从属数据（同 request_events / attachments 的处理）
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_requests.id", ondelete="CASCADE"), nullable=False
    )
    # RESTRICT：报过价的供应商不许被删（否则"当时谁报了多少"就断了）
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False
    )
    # 单位：分
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 币种：第一版只做人民币，留着列是为了将来多币种时不用改口径
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CNY")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # NULL = 长期有效（不是"已过期"）
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 报价单附件（M9 的表）。**可空**：口头询价之后补录也应当允许。
    # SET NULL：附件被删（软删或运维清理）不该让整条报价消失 ——
    # 报价的金额与供应商才是这张表的主体，附件是佐证。
    attachment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("attachments.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
