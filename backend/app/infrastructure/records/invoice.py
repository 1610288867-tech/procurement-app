"""发票表（M10 / 8.3）。

============================ 与 ER 图的两处**有意的差异**（都写了原因） ============================
ER 图第 473 行给的列是：`id, request_id, supplier_id, invoice_no, amount, tax_amount,
issued_at, matched`。我按下面两处调整：

1. **不加 `matched`（是否已匹配）**：三单匹配**在"执行付款"那一刻跑**
   （实施计划 2433 行），而它返回的是**问题列表**，不是"匹配过 / 没匹配"这个布尔值。
   `matched` 是典型的"派生状态的缓存列" —— 报告口径那个坑（只读状态列做统计、
   而状态列只在有人结算时才更新）就是同一类问题的另一种样子。
   所以匹配结果**每次问接口现算**（`GET /requests/{id}/match`），不落库。
   8.4 执行付款时也一样：那一刻现算 —— 那正是"唯一能保证校验通过后没有再改单据的时机"。

2. **加 `attachment_id`（发票影像）**：实施计划 2405 行的接口签名里明确有"附件"
   （"发票号 + 金额 + 税额 + 供应商 + 附件"），而 ER 图那一行列了 `attachments` 的用途
   包含"发票"。两者合起来就是"发票要能挂附件"，所以这一列有真实的写入方。

============================ 发票号为什么不做全局唯一 ============================
`UNIQUE(request_id, invoice_no)`（**同一张申请内**不许重复），而不是全局唯一：
不同供应商的发票号体系互不相干（甚至可能都叫 `0001`），全局唯一会把正常业务挡在门外。
但"同一张申请里同一个号登记两次"几乎一定是重复录入 —— 那正是要拦的。

============================ 税额是**信息**不是**计算项** ============================
`tax_amount` 只是记录发票上的税额（对账时要看），**不参与三单匹配**：
匹配比的是"发票金额 vs 订单金额"（含税总额对含税总额）。
把它拿去做"金额 - 税额 = 不含税金额再比"会引入一套税率口径，
而订单金额那边并没有对应的不含税口径 —— 两边口径不一致，比出来的差异没法解释。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.settlement_policy import INVOICE_ACTIVE, INVOICE_STATUSES
from app.infrastructure.records.base import Base


class InvoiceRecord(Base):
    """供应商开具的发票。

    一笔申请可以有**多张**发票（匹配时金额求和）—— 这与"一笔申请只允许一张订单、
    一次收货"不同，是刻意的：现实里分批开票很常见，而拆单不常见。
    """

    __tablename__ = "invoices"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_invoice_amount_positive"),
        # 税额可以为 0（免税 / 普票），但不能为负
        CheckConstraint("tax_amount >= 0", name="ck_invoice_tax_non_negative"),
        # 税额不能超过总额（否则"价税合计"这个概念就自相矛盾了）
        CheckConstraint("tax_amount <= amount", name="ck_invoice_tax_not_over_amount"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in INVOICE_STATUSES) + ")",
            name="ck_invoice_status_known",
        ),
        # ⚠️ **部分唯一索引**（不是普通唯一约束）：只约束**有效**发票的号不重复。
        # 作废之后号可以重新登记 —— 否则"录错号 → 作废 → 按正确的号重录"
        # 这条最正常的纠错路径会被作废的那行一直占着号挡住。
        Index(
            "uq_invoice_active_no",
            "request_id",
            "invoice_no",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index("ix_invoices_request_created", "request_id", "created_at"),
        # 匹配要按"这张申请的有效发票"过滤 + 求和
        Index("ix_invoices_request_active", "request_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # CASCADE：发票是申请的从属数据（与报价、订单同一处理）
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_requests.id", ondelete="CASCADE"), nullable=False
    )
    # RESTRICT：开过票的供应商不许被删
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False
    )
    invoice_no: Mapped[str] = mapped_column(String(64), nullable=False)
    #: `ACTIVE`（参与匹配）/ `VOIDED`（作废：不参与求和，号可重新登记）。
    #: 作废是**状态变更**，不是删行 —— 删行会丢审计痕迹。
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=INVOICE_ACTIVE
    )
    # 单位：分（含税总额）。三单匹配比的就是它
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 单位：分。只记录、不参与匹配（见模块说明）
    tax_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: 开票日期（供应商开票那天），可以为空 —— 手上有票但日期一时看不清是常事
    issued_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: 发票影像。附件被删时置 NULL（票还在，只是影像没了）——
    #: 与报价单附件同一条处理（SET NULL），而不是 CASCADE 把发票一起删掉
    attachment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("attachments.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
