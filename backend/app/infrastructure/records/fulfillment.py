"""订单与收货表（M10 / 8.2）。

四张表一起放：它们是同一条链上前后相接的凭证，分开写反而不好对照。

    purchase_orders        采购订单（独立凭证）
    purchase_order_lines   订单明细（**申请明细的快照复制**）
    goods_receipts         收货单
    goods_receipt_lines    收货明细（逐行实收数量）

============================ 三条不许破的纪律 ============================
1. **订单明细是复制，不是指向**（ER 图第 134-135 行）：
   `item_id` 只是"从哪一行来的"的记录，**不是外键链**。
   订单一旦生成就是独立凭证 —— 加了外键之后，申请明细被替换（M5 的换明细是
   覆盖式替换）就会把历史订单一起带走，而订单可能已经付过款了。
   所以这里**刻意不加外键**，字段上写明"快照"。

2. **订单金额由后端按明细重算**：`amount` 不接受客户端传值，
   算法只有一处（`domain/fulfillment.order_amount_fen`）。
   它是三单匹配与预算结转的基准 —— **不是申请金额**。

3. **收货只做整单**：`goods_receipts.status` 只有 `RECEIVED` 一档，
   状态机里也没有 `PARTIALLY_RECEIVED`（留一个走不到的状态会让状态机测试失去意义）。
   没收齐就**不进 RECEIVED** —— 那条判定在 `domain/fulfillment.is_fully_received`。

============================ 收货明细为什么只需要 accepted_quantity ============================
ER 图写的是 `received_quantity, accepted_quantity` 两列（收了多少 / 验收合格多少）。
第一版**只留 `accepted_quantity`**，理由是：三单匹配用的是"验收合格的数量"
（业务规则第 6 条：`accepted_qty` 取 `SUM(goods_receipt_lines.accepted_quantity)`），
而"收了多少"这个中间量没有任何一处会读它 —— 留一列没人读的字段，
早晚会有人拿它去算点什么，于是出现两份"已收数量"。

这一条与计划里"砍掉部分收货"是同一个取舍：**不留走不到的分支**。
将来真要做"收货 vs 验收"分离时，再按 5.5 节 E 的步骤把列与语义一起加。
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
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import RequestStatus
from app.domain.fulfillment import RECEIPT_STATUSES
from app.infrastructure.records.base import Base

#: 订单状态：跟着**主单**走（`ORDERED` → `RECEIVED`）。
#: 用主单的状态字面值而不是另立一套：订单状态与主单状态在第一版是同一件事，
#: 两套词表必然漂移（"订单收到了但主单还在 ORDERED"这种组合不该存在）。
ORDER_STATUSES: tuple[str, ...] = (
    RequestStatus.ORDERED.value,
    RequestStatus.RECEIVED.value,
)


class PurchaseOrderRecord(Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in ORDER_STATUSES) + ")",
            name="ck_order_status_known",
        ),
        CheckConstraint("amount > 0", name="ck_order_amount_positive"),
        # 一笔申请第一版只允许一张订单：与"定标只选一家供应商"自洽。
        # 拆单（一笔申请分给多家供应商）进"暂时砍掉"清单 ——
        # 那要同时改定标（多家 SELECTED）、订单、以及三单匹配的粒度，三处绑定。
        UniqueConstraint("request_id", name="uq_order_request"),
        # 时间列叫 `ordered_at`（不是 `created_at`）—— 它是"下单时刻"，业务语义更准。
        # 索引跟着它走（实测撞过一次：写 `created_at` 会直接
        # `ConstraintColumnNotFoundError`，因为这张表根本没有那一列）。
        Index("ix_orders_request_ordered", "request_id", "ordered_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # CASCADE：订单是申请的从属数据（与报价、流水同一处理）
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_requests.id", ondelete="CASCADE"), nullable=False
    )
    # RESTRICT：定过标的供应商不许被删
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False
    )
    # 单号（人读的）。由后端生成，**不接受客户端传值**。
    order_no: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    # 单位：分。= Σ(明细行金额)，由后端重算
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    ordered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PurchaseOrderLineRecord(Base):
    """订单明细行（**快照**）。

    `item_id` **不是外键**：见模块说明第 1 条。
    它是"这行从申请的第几行来"的线索，用于对账时回看，不构成引用。
    """

    __tablename__ = "purchase_order_lines"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_line_quantity_positive"),
        CheckConstraint("unit_price > 0", name="ck_order_line_price_positive"),
        CheckConstraint("amount >= 0", name="ck_order_line_amount_non_negative"),
        # 同一张订单里行号不许重复（顺序是明细的一部分）
        UniqueConstraint("order_id", "line_no", name="uq_order_line_no"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False
    )
    #: 快照：它来自申请的第几行（**软引用**，刻意不加外键）
    item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    spec: Mapped[str | None] = mapped_column(String(500), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    # 单位：分。询价取定标成交单价，直采取申请单价
    unit_price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)


class GoodsReceiptRecord(Base):
    """收货单。

    第一版只有 `RECEIVED` 一档状态（整单收货）。它同时是"这张单子收过货了"
    的标记 —— `uq_receipt_request` 保证一笔申请只收一次。
    """

    __tablename__ = "goods_receipts"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in RECEIPT_STATUSES) + ")",
            name="ck_receipt_status_known",
        ),
        UniqueConstraint("request_id", name="uq_receipt_request"),
        Index("ix_receipts_request_received", "request_id", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_requests.id", ondelete="CASCADE"), nullable=False
    )
    # RESTRICT：收过货的订单不许被删（收货是它的下游凭证）
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="RESTRICT"), nullable=False
    )
    # RESTRICT：谁收的货要留得住（内控：采购、验收、记录三方分离）
    receiver_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class GoodsReceiptLineRecord(Base):
    """收货明细：逐行填**验收合格**的数量。

    只留 `accepted_quantity`（见模块说明最后一段）：三单匹配用的就是它，
    而"收了多少"这个中间量没有任何一处会读 —— 留一列没人读的字段，
    早晚有人拿它去算点什么，于是出现两份"已收数量"。
    """

    __tablename__ = "goods_receipt_lines"
    __table_args__ = (
        CheckConstraint("accepted_quantity >= 0", name="ck_receipt_line_quantity_non_negative"),
        UniqueConstraint("receipt_id", "order_line_id", name="uq_receipt_line"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    receipt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("goods_receipts.id", ondelete="CASCADE"), nullable=False
    )
    # RESTRICT：订单明细不许被删（收货是它的下游记录）
    order_line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_order_lines.id", ondelete="RESTRICT"), nullable=False
    )
    accepted_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
