"""付款的数据访问（M10 / 8.4）。

纪律同其他仓库：**只 flush()，不 commit()** —— 事务边界在用例层。

============================ 这个仓库里两处条件更新 ============================
1. **`mark_paid`**（`PENDING → PAID`）：`WHERE id = ? AND status = 'PENDING'`
   + `RETURNING`。两个人同时点"付款"只有一个成功 ——
   与 M6 的审批决定、M7 的预算预留是**完全同一个套路**。
   它是"动钱"这道关的主防线，且必须在 `spend_budget` 之前调用
   （先抢到付款权的一方去动预算，抢输的一方在动钱之前就退出了）。

2. **`mark_failed`**（`PENDING → FAILED`）：同样条件更新。
   失败要留痕迹（实施计划 2460 行），所以不删行、只改状态。

============================ `uq_payment_active` 只是兜底 ============================
**部分唯一索引**（`WHERE status <> 'FAILED'`）挡的是"建出两张有效付款单"，
条件更新挡的是"同一张付款单被付两次"。两件事都得挡，不是重复。

`attempt_no` 用 `MAX + 1` 在**同一事务**里算：并发下两条新单可能算出同一个号，
所以 `uq_payment_attempt` 兜底；撞号由用例层**重试一次**（重试是用例的语义，
不是仓库的 —— 仓库里写循环会掩盖"到底谁在重试"）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import PAYMENT_FAILED, PAYMENT_PAID, PAYMENT_PENDING
from app.infrastructure.records.payment import PaymentRecord


class PaymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(
        self, payment_id: uuid.UUID, *, populate_existing: bool = False
    ) -> PaymentRecord | None:
        """按 id 取一张付款单。

        `populate_existing=True` 用于**刚做完 bulk UPDATE 之后立刻回读**
        （`mark_paid` 就是）：条件更新用 `synchronize_session=False`，
        会话里那份对象还是 `PENDING`，而它在身份映射里 ——
        普通查询会把它原样还回来，于是**响应里显示"没付成功"**。
        这个坑在本项目已踩过四次（M5 / M6 / 8.1 / 8.2），8.3 的作废是第五次，
        所以这里的开关是**默认就位**的。
        """
        if not populate_existing:
            return await self.session.get(PaymentRecord, payment_id)
        statement = (
            select(PaymentRecord)
            .where(PaymentRecord.id == payment_id)
            .execution_options(populate_existing=True)
        )
        return (await self.session.scalars(statement)).one_or_none()

    async def list_for_request(self, request_id: uuid.UUID) -> list[PaymentRecord]:
        """这张申请的**全部**付款单（含 `FAILED` 的尝试），按尝试次序。

        含失败的是刻意的：界面上要能说"这是第 2 次尝试"——
        只显示成功那张，会让"付了三次才成功"看起来像"只付过一次"。
        """
        statement = (
            select(PaymentRecord)
            .where(PaymentRecord.request_id == request_id)
            .order_by(PaymentRecord.attempt_no)
        )
        return list((await self.session.scalars(statement)).all())

    async def active_of(
        self, request_id: uuid.UUID, *, populate_existing: bool = False
    ) -> PaymentRecord | None:
        """这张申请**有效**的付款单（`PENDING` / `PAID`），没有就 None。

        与 `uq_payment_active` 的部分索引条件是同一句 ——
        写成两处的话，索引挡得住重复、查询却可能查到 `FAILED` 那行。
        """
        statement = select(PaymentRecord).where(
            PaymentRecord.request_id == request_id,
            PaymentRecord.status != PAYMENT_FAILED,
        )
        if populate_existing:
            statement = statement.execution_options(populate_existing=True)
        return (await self.session.scalars(statement)).one_or_none()

    async def next_attempt_no(self, request_id: uuid.UUID) -> int:
        """下一次尝试的序号 = `MAX(attempt_no) + 1`（没有历史尝试时是 1）。"""
        statement = select(
            func.coalesce(func.max(PaymentRecord.attempt_no), 0)
        ).where(PaymentRecord.request_id == request_id)
        return int((await self.session.scalars(statement)).one()) + 1

    async def create(
        self,
        *,
        request_id: uuid.UUID,
        amount_fen: int,
        created_by: uuid.UUID,
        attempt_no: int,
    ) -> PaymentRecord:
        """创建付款单（`PENDING`）。

        撞 `uq_payment_active`（已经有一张有效付款单）或
        `uq_payment_attempt`（尝试号撞了）时**让异常冒出去**：
        用例层分别翻成 409 与"重算一次序号"，而不是在这里静默处理 ——
        两种情况的原因完全不同，混在一起会让日志失去信息。
        """
        record = PaymentRecord(
            request_id=request_id,
            amount=amount_fen,
            status=PAYMENT_PENDING,
            attempt_no=attempt_no,
            created_by=created_by,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def mark_paid(self, payment_id: uuid.UUID) -> bool:
        """`PENDING → PAID`（**条件更新**：付款权只发一次）。

        返回 `False` = 这张付款单已经被处理过（别人先付了、或它已经是 `FAILED`）。
        调用方据此回 409，而且**在动预算之前就退出**。
        """
        statement = (
            update(PaymentRecord)
            .where(
                PaymentRecord.id == payment_id,
                PaymentRecord.status == PAYMENT_PENDING,
            )
            .values(status=PAYMENT_PAID, paid_at=datetime.now(UTC))
            .returning(PaymentRecord.id)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def mark_failed(self, payment_id: uuid.UUID, *, reason: str) -> bool:
        """`PENDING → FAILED`（保留痕迹，允许重新发起）。

        失败**不删行**：删掉之后"试过一次但失败了"这件事就没地方看了，
        而它恰恰是财务最需要知道的（为什么这张单子还没付出去）。
        """
        statement = (
            update(PaymentRecord)
            .where(
                PaymentRecord.id == payment_id,
                PaymentRecord.status == PAYMENT_PENDING,
            )
            .values(status=PAYMENT_FAILED, failure_reason=reason)
            .returning(PaymentRecord.id)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none() is not None
