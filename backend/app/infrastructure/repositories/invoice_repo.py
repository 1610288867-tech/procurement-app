"""发票的数据访问（M10 / 8.3，含作废）。

纪律同其他仓库：**只 flush()，不 commit()** —— 事务边界在用例层。

============================ 这个仓库里唯一一处"求和" ============================
`totals_of` 是**订单级粒度**的落点（实施计划 2416-2421）：

    invoice_amount_fen = SUM(invoices.amount)      ← **有效（未作废）**发票求和
    invoice_count      = COUNT(*)                  ← 有效发票的张数

============================ ⚠️ 作废之后，"有效"这件事必须只有一处判据 ============================
作废的发票**不参与任何金额计算**。这件事最容易错的地方不是"忘了过滤"，
而是**过滤条件散在四五个查询里**，早晚漏一处 —— 而漏掉的那一处
会拿着包含作废票的合计去付款，**不报错、只是把钱付多了**。

所以：本模块里凡是要"金额 / 张数 / 张数是否为零"的地方，**一律走
`_active_only(...)` 这一个条件**；需要给界面看全部（含作废）的地方显式传
`include_voided=True`，让"我这次要的是全部"变成一句写在调用处的话，
而不是一个容易被抄漏的默认值。

`accepted_qty`（收货求和）的求和方在 `fulfillment_repo` ——
两处求和各自只有一个实现，且都由协调者喂给同一份纯函数
（`domain/settlement_policy.match_three_way`）。规则不在这里，这里只把行加起来。
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.settlement_policy import INVOICE_ACTIVE, INVOICE_VOIDED
from app.infrastructure.records.invoice import InvoiceRecord


def _active_only(statement: Any) -> Any:
    """只算**有效**发票。所有金额相关的查询都必须过这里。"""
    return statement.where(InvoiceRecord.status == INVOICE_ACTIVE)


class InvoiceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for_request(
        self, request_id: uuid.UUID, *, include_voided: bool = True
    ) -> list[InvoiceRecord]:
        """这张申请的发票，按登记时间正序（先登记的在前）。

        默认**包含已作废的**：界面上"这张票作废了"是一个必须让人看见的事实 ——
        悄悄不显示，用户会以为从没登记过，然后又去登记一次（这次会成功，
        因为号已经被作废释放了），于是账上出现两张一模一样的票。
        金额计算走 `totals_of`，那边只算有效的。
        """
        statement = select(InvoiceRecord).where(
            InvoiceRecord.request_id == request_id
        )
        if not include_voided:
            statement = _active_only(statement)
        statement = statement.order_by(InvoiceRecord.created_at)
        return list((await self.session.scalars(statement)).all())

    async def get(
        self, invoice_id: uuid.UUID, *, populate_existing: bool = False
    ) -> InvoiceRecord | None:
        """按 id 取一张发票。

        `populate_existing=True` 用于**刚做完 bulk UPDATE 之后立刻回读**
        （作废就是）：条件更新用的是 `synchronize_session=False`，
        会话里那份对象还是 `ACTIVE`，而它在身份映射里 ——
        普通查询会把它原样还回来，于是**响应里显示"没作废成功"**。
        这个坑在本项目已经踩过四次（M5 / M6 / 8.1 / 8.2），所以这里直接带上开关。
        """
        if not populate_existing:
            return await self.session.get(InvoiceRecord, invoice_id)
        statement = (
            select(InvoiceRecord)
            .where(InvoiceRecord.id == invoice_id)
            .execution_options(populate_existing=True)
        )
        return (await self.session.scalars(statement)).one_or_none()

    async def totals_of(self, request_id: uuid.UUID) -> tuple[int, int]:
        """返回 `(有效发票金额之和, 有效发票张数)`。

        **用 SQL 求和而不是把行拉回来在 Python 里加**：发票是要长期累积的凭证，
        一笔申请几十张票在真实场景里很正常，而这里只要两个数。
        （`COALESCE` 让"一张有效发票都没有"返回 0 而不是 None。）

        ⚠️ 只算 `ACTIVE`：作废的票不参与三单匹配。这是"作废"这个功能**唯一的作用**，
        漏了这个过滤，作废就等于没做。
        """
        statement = _active_only(
            select(
                func.coalesce(func.sum(InvoiceRecord.amount), 0),
                func.count(InvoiceRecord.id),
            ).where(InvoiceRecord.request_id == request_id)
        )
        amount, count = (await self.session.execute(statement)).one()
        return int(amount), int(count)

    async def supplier_ids_of(self, request_id: uuid.UUID) -> list[str]:
        """这张申请**所有有效发票**的供应商 id。

        同样只取有效的：作废的票不该再影响"供应商一致性"的判定 ——
        否则作废掉那张开错供应商的票之后，匹配仍然会报 `SUPPLIER_MISMATCH`，
        而用户明明已经把错票作废了（他会以为作废没生效）。

        单独一个方法是为了让"全部一致"这件事在调用处一眼可见：
        拿 `list[0]` 去比是这里最容易犯的错（领域设计 1372 行点过名）。
        """
        statement = _active_only(
            select(InvoiceRecord.supplier_id).where(
                InvoiceRecord.request_id == request_id
            )
        )
        return [str(value) for value in (await self.session.scalars(statement)).all()]

    async def create(
        self,
        *,
        request_id: uuid.UUID,
        supplier_id: uuid.UUID,
        invoice_no: str,
        amount_fen: int,
        tax_amount_fen: int,
        issued_on: date | None,
        attachment_id: uuid.UUID | None,
        created_by: uuid.UUID,
    ) -> InvoiceRecord:
        """登记一张发票（状态 `ACTIVE`）。

        `invoice_no` 撞部分唯一索引 `uq_invoice_active_no` 时**让异常冒出去**：
        用例层把它转成明确的 409（"这张申请里已经登记过这个发票号"）——
        而不是在这里静默换一个号（那等于把重复录入放进来）。
        """
        record = InvoiceRecord(
            request_id=request_id,
            supplier_id=supplier_id,
            invoice_no=invoice_no,
            status=INVOICE_ACTIVE,
            amount=amount_fen,
            tax_amount=tax_amount_fen,
            issued_on=issued_on,
            attachment_id=attachment_id,
            created_by=created_by,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def mark_voided(self, invoice_id: uuid.UUID) -> bool:
        """作废（**条件更新**：只从 `ACTIVE` 转，且只转一次）。

        ⚠️ 条件更新而不是"先查再写"：两个人同时点作废时，
        先查再写会让两边都读到 `ACTIVE`、两边都写成功，
        于是**同一次作废写了两条时间线事件**（痕迹重复，而账只该变一次）。
        用 `WHERE status = 'ACTIVE'` + `RETURNING` 判定胜负，
        与全项目的"并发场景绝不先查再写"是同一条纪律。
        """
        statement = (
            update(InvoiceRecord)
            .where(
                InvoiceRecord.id == invoice_id,
                InvoiceRecord.status == INVOICE_ACTIVE,
            )
            .values(status=INVOICE_VOIDED)
            .returning(InvoiceRecord.id)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        # 用 RETURNING 数条数（异步 Result 没有 rowcount，mypy 会直接报错）
        return result.scalar_one_or_none() is not None
