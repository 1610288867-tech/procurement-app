"""订单与收货的数据访问（M10 / 8.2）。

纪律同其他仓库：**只 flush()，不 commit()** —— 事务边界在用例层。

============================ 这个仓库里三处"必须条件更新" ============================
1. **下单**：`status = AWARDED` 一起进 WHERE（`transition_status` 已经带 version），
   两个人同时点下单只有一个人成功；
2. **订单转"已收货"**：`status = ORDERED` 进 WHERE —— 否则两个人同时提交收货，
   会写出两张收货单（`UNIQUE(request_id)` 会挡住第二张，但那是一个数据库错误，
   不是一句人话）；
3. **收货单唯一**：`UNIQUE(request_id)` 兜底，与上面那道是有意重复的两层。

============================ 订单明细是快照 ============================
`item_id` **不是外键**（见 `records/fulfillment.py` 的说明）：订单一旦生成就是
独立凭证，申请明细被替换也不该带走它。所以这里对 `purchase_order_lines` 的
写操作只有"整批插入"一种，没有任何 update —— 改订单金额的唯一途径是重新下单。
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import RequestStatus
from app.domain.fulfillment import RECEIPT_RECEIVED, OrderLineDraft, SourceLine
from app.infrastructure.records.fulfillment import (
    GoodsReceiptLineRecord,
    GoodsReceiptRecord,
    PurchaseOrderLineRecord,
    PurchaseOrderRecord,
)


def _new_order_no() -> str:
    """订单号：`PO-<年>-<8 位十六进制>`。

    与采购申请的单号同一个形状（`PR-2026-XXXXXXXX`），**由后端生成**——
    接受客户端传单号就等于让两笔订单可能撞同一个号，而单号是对账时人读的锚点。
    """
    return f"PO-{datetime.now(UTC).year}-{secrets.token_hex(4).upper()}"


class FulfillmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ 订单

    async def get_order(self, order_id: uuid.UUID) -> PurchaseOrderRecord | None:
        return await self.session.get(PurchaseOrderRecord, order_id)

    async def order_of(
        self, request_id: uuid.UUID, *, populate_existing: bool = False
    ) -> PurchaseOrderRecord | None:
        """这张申请的订单（没有就 None）。

        `populate_existing=True` 用于**刚做完 bulk UPDATE 之后立刻回读**：
        订单状态是 bulk UPDATE 改的，会话里那份对象仍是旧值（本项目踩过三次）。
        """
        statement = select(PurchaseOrderRecord).where(
            PurchaseOrderRecord.request_id == request_id
        )
        if populate_existing:
            statement = statement.execution_options(populate_existing=True)
        return (await self.session.scalars(statement)).one_or_none()

    async def order_lines(self, order_id: uuid.UUID) -> list[PurchaseOrderLineRecord]:
        """订单明细，按行号正序。"""
        statement = (
            select(PurchaseOrderLineRecord)
            .where(PurchaseOrderLineRecord.order_id == order_id)
            .order_by(PurchaseOrderLineRecord.line_no)
        )
        return list((await self.session.scalars(statement)).all())

    async def create_order(
        self,
        *,
        request_id: uuid.UUID,
        supplier_id: uuid.UUID,
        lines: Sequence[OrderLineDraft],
        amount_fen: int,
        created_by: uuid.UUID,
    ) -> PurchaseOrderRecord:
        """下单：一张订单 + 它的明细（**整批插入**）。

        `order_no` 在这里生成；撞 `UNIQUE(order_no)` 的概率是 1/43 亿，
        真撞上就让数据库报错（而不是在这里写一段重试逻辑 —— 那会掩盖真正的问题）。
        """
        order = PurchaseOrderRecord(
            request_id=request_id,
            supplier_id=supplier_id,
            order_no=_new_order_no(),
            amount=amount_fen,
            status=RequestStatus.ORDERED.value,
            created_by=created_by,
        )
        self.session.add(order)
        await self.session.flush()

        for index, line in enumerate(lines, start=1):
            self.session.add(
                PurchaseOrderLineRecord(
                    order_id=order.id,
                    # 快照：只记"来自申请的第几行"，**不是外键**
                    item_id=uuid.UUID(line.item_id),
                    line_no=index,
                    name=line.name,
                    spec=line.spec,
                    quantity=line.quantity,
                    unit_price=line.unit_price_fen,
                    amount=line.amount_fen,
                )
            )
        await self.session.flush()
        return order

    async def mark_order_received(self, order_id: uuid.UUID) -> bool:
        """订单转 `RECEIVED`（条件更新：只从 `ORDERED` 转，且只转一次）。"""
        statement = (
            update(PurchaseOrderRecord)
            .where(
                PurchaseOrderRecord.id == order_id,
                PurchaseOrderRecord.status == RequestStatus.ORDERED.value,
            )
            .values(status=RequestStatus.RECEIVED.value)
            .returning(PurchaseOrderRecord.id)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none() is not None

    # ------------------------------------------------------------------ 收货

    async def receipt_of(self, request_id: uuid.UUID) -> GoodsReceiptRecord | None:
        statement = select(GoodsReceiptRecord).where(
            GoodsReceiptRecord.request_id == request_id
        )
        return (await self.session.scalars(statement)).one_or_none()

    async def receipt_lines(self, receipt_id: uuid.UUID) -> list[GoodsReceiptLineRecord]:
        statement = select(GoodsReceiptLineRecord).where(
            GoodsReceiptLineRecord.receipt_id == receipt_id
        )
        return list((await self.session.scalars(statement)).all())

    # ------------------------------------------------------------------ 三单匹配用的求和（M10 / 8.3）

    async def accepted_quantity_of(self, request_id: uuid.UUID) -> int:
        """这张申请**验收合格数量之和**（订单级粒度，实施计划 2419 行）。

        `accepted_qty = SUM(goods_receipt_lines.accepted_quantity)`（**所有收货单求和**）。
        第一版一笔申请只有一张收货单（`UNIQUE(request_id)`），但求和写法现在就对 ——
        将来放开多次收货时，这里不用改，改的只是那条唯一约束。

        没有收货单时返回 **0**（不是 None）：调用方（三单匹配）要把它当数字比。
        用 SQL 求和而不是拉行回来加：它是"订单级"口径，明细行数会随业务增长。
        `COALESCE` 与 `OUTER JOIN` 一起保证"一张收货单都没有"时返回 0。
        """
        statement = (
            select(func.coalesce(func.sum(GoodsReceiptLineRecord.accepted_quantity), 0))
            .select_from(GoodsReceiptRecord)
            .outerjoin(
                GoodsReceiptLineRecord,
                GoodsReceiptLineRecord.receipt_id == GoodsReceiptRecord.id,
            )
            .where(GoodsReceiptRecord.request_id == request_id)
        )
        return int((await self.session.scalars(statement)).one())

    async def ordered_quantity_of(self, order_id: uuid.UUID) -> int:
        """这张订单**数量之和**（订单级粒度，实施计划 2416 行）。

        `ordered_qty = SUM(purchase_order_lines.quantity)`。
        """
        statement = select(
            func.coalesce(func.sum(PurchaseOrderLineRecord.quantity), 0)
        ).where(PurchaseOrderLineRecord.order_id == order_id)
        return int((await self.session.scalars(statement)).one())

    async def create_receipt(
        self,
        *,
        request_id: uuid.UUID,
        order_id: uuid.UUID,
        receiver_id: uuid.UUID,
        plan: Sequence[tuple[uuid.UUID, int]],
        note: str | None,
    ) -> GoodsReceiptRecord:
        """登记收货单 + 逐行实收数量。

        `plan` 是 `domain/fulfillment.receipt_plan` 算出来的
        `[(订单明细 id, 实收数量), ...]` —— **判定全在纯函数里**，
        这里只负责落库。
        """
        receipt = GoodsReceiptRecord(
            request_id=request_id,
            order_id=order_id,
            receiver_id=receiver_id,
            status=RECEIPT_RECEIVED,
            note=note,
        )
        self.session.add(receipt)
        await self.session.flush()

        for order_line_id, quantity in plan:
            self.session.add(
                GoodsReceiptLineRecord(
                    receipt_id=receipt.id,
                    order_line_id=order_line_id,
                    accepted_quantity=quantity,
                )
            )
        await self.session.flush()
        return receipt


def order_source_lines(items: Sequence[Any]) -> list[SourceLine]:
    """把申请明细记录转成领域层的 `SourceLine`（**适配**，不是业务逻辑）。

    放在仓库模块里与 `quotation_facts` 同一个理由：`domain/` 不认识 ORM 记录，
    而"从记录里挑哪几个字段"属于适配。
    """
    return [
        SourceLine(
            item_id=str(item.id),
            name=item.name,
            spec=item.spec,
            quantity=int(item.quantity),
            unit_price_fen=int(item.unit_price),
        )
        for item in items
    ]
