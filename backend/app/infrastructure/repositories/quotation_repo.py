"""供应商报价的数据访问（M10 / 8.1）。

纪律同其他仓库：**只 flush()，不 commit()** —— 事务边界在用例层。

============================ 这个仓库里两处"必须条件更新" ============================
1. **定标**（`award`）：`status = SUBMITTED` 与"该申请还没定过标"一起进 `WHERE`，
   两个人同时点定标只有一个成功。少了它，两边都会把各自的供应商写成 `SELECTED`，
   而主单只能有一个成交供应商 —— 结果是一条单子两个成交价，**而且不报错**。
2. **主单推进**（`SOURCING → AWARDED`）走采购仓库的 `transition_status`（带 version），
   与上面那次更新在**同一个事务**里。

============================ 惰性结算过期报价 ============================
`expire_overdue` 把"已过有效期却还写着 SUBMITTED"的行改成 `EXPIRED`。
判定只认时间（`valid_until < now`），状态列是这份判定的事后投影：
读比价表与定标前各结一次，保证界面上看到的家数与判定用的是同一份事实。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.sourcing_policy import (
    QUOTE_EXPIRED,
    QUOTE_NOT_SELECTED,
    QUOTE_SELECTED,
    QUOTE_SUBMITTED,
    QuotationFacts,
)
from app.infrastructure.records.sourcing import QuotationRecord


class QuotationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ 读

    async def list_for_request(self, request_id: uuid.UUID) -> list[QuotationRecord]:
        """比价表：某张申请的全部报价，**登记顺序**（先报的在前）。

        为什么按 `created_at` 正序而不是金额：比价表的读者要的是"这一轮有哪几家、
        各报多少"，顺序稳定比排序规则重要 —— 按金额排会让同价的几家次序随机漂移。
        """
        statement = (
            select(QuotationRecord)
            .where(QuotationRecord.request_id == request_id)
            .order_by(QuotationRecord.created_at, QuotationRecord.id)
        )
        return list((await self.session.scalars(statement)).all())

    async def get(
        self, quotation_id: uuid.UUID, *, populate_existing: bool = False
    ) -> QuotationRecord | None:
        """按 id 取一条报价。

        `populate_existing=True` 用于**刚做完 bulk UPDATE 之后立刻回读**的场景
        （定标把选中的那条改成 `SELECTED`）：条件更新用的是
        `synchronize_session=False`，会话里那份对象还是旧值，而它已经在身份映射里 ——
        普通查询会把它原样还回来，于是响应显示"选中的那家还是 SUBMITTED"。
        这个坑在本项目已经踩过三次（M5、M6、M10）。
        """
        if not populate_existing:
            return await self.session.get(QuotationRecord, quotation_id)
        statement = (
            select(QuotationRecord)
            .where(QuotationRecord.id == quotation_id)
            .execution_options(populate_existing=True)
        )
        return (await self.session.scalars(statement)).one_or_none()

    async def selected_of(self, request_id: uuid.UUID) -> QuotationRecord | None:
        """这张申请**已选中**的那一家（没有就返回 None）。"""
        statement = select(QuotationRecord).where(
            QuotationRecord.request_id == request_id,
            QuotationRecord.status == QUOTE_SELECTED,
        )
        return (await self.session.scalars(statement)).first()

    # ------------------------------------------------------------------ 写

    async def create(
        self,
        *,
        request_id: uuid.UUID,
        supplier_id: uuid.UUID,
        amount_fen: int,
        valid_until: datetime | None,
        attachment_id: uuid.UUID | None,
        created_by: uuid.UUID,
    ) -> QuotationRecord:
        """登记一家报价。

        撞 `UNIQUE(request_id, supplier_id)` 说明同一家报了两次 —— 由**调用方**
        转成 409（这里不 try/except：错误码是边界层的事，仓库层只负责让约束生效）。
        """
        record = QuotationRecord(
            request_id=request_id,
            supplier_id=supplier_id,
            amount=amount_fen,
            status=QUOTE_SUBMITTED,
            valid_until=valid_until,
            attachment_id=attachment_id,
            created_by=created_by,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def expire_overdue(self, request_id: uuid.UUID) -> int:
        """把该申请下"已过期却还写着 SUBMITTED"的报价改成 `EXPIRED`。返回改动行数。

        条件整条写进 `WHERE`（一条 SQL 改完，不先查再写）：
        只动 `SUBMITTED` 的行 —— `SELECTED` / `NOT_SELECTED` 是定标那一刻的历史结论，
        让它们随时间变成"过期"，事后看会像是那家自己失效了。
        """
        statement = (
            update(QuotationRecord)
            .where(
                QuotationRecord.request_id == request_id,
                QuotationRecord.status == QUOTE_SUBMITTED,
                QuotationRecord.valid_until.is_not(None),
                QuotationRecord.valid_until < datetime.now(UTC),
            )
            .values(status=QUOTE_EXPIRED)
            .returning(QuotationRecord.id)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        # 用 RETURNING 数条数，而不是 `result.rowcount`：异步 Result 的类型标注里
        # 没有 rowcount（mypy 会直接报错）。M2 起一直沿用这个写法，见
        # `approval_repo.cancel_pending_tasks` 的同一句注释。
        return len(list(result.scalars().all()))

    async def mark_awarded(self, quotation_id: uuid.UUID) -> bool:
        """把选中的那条报价标成 `SELECTED`（**条件更新**）。

        守卫：`status = SUBMITTED`。两个人同时定标时，只有一个人能把它从
        `SUBMITTED` 推走；另一个人受影响行数为 0，会拿到 409。
        """
        return await self._conditional_status(quotation_id, from_status=QUOTE_SUBMITTED)

    async def mark_not_selected(self, request_id: uuid.UUID, *, keep_id: uuid.UUID) -> int:
        """其余报价标成 `NOT_SELECTED`（含已过期的那些）。

        为什么连过期的也改：这一轮结束了，其余报价不该还能被算进下一轮的家数。
        **不动 `keep_id`**（选中的那条），也不动已经 `NOT_SELECTED` 的（幂等）。
        """
        statement = (
            update(QuotationRecord)
            .where(
                QuotationRecord.request_id == request_id,
                QuotationRecord.id != keep_id,
                QuotationRecord.status != QUOTE_NOT_SELECTED,
            )
            .values(status=QUOTE_NOT_SELECTED)
            .returning(QuotationRecord.id)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        return len(list(result.scalars().all()))

    async def _conditional_status(self, quotation_id: uuid.UUID, *, from_status: str) -> bool:
        statement = (
            update(QuotationRecord)
            .where(
                QuotationRecord.id == quotation_id,
                QuotationRecord.status == from_status,
            )
            .values(status=QUOTE_SELECTED)
            .returning(QuotationRecord.id)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none() is not None


def quotation_facts(
    records: list[QuotationRecord], *, usable: Mapping[uuid.UUID, bool]
) -> list[QuotationFacts]:
    """把 ORM 记录转成领域层的 `QuotationFacts` 列表（纯函数，不碰会话）。

    放在仓库模块里而不是领域层：**领域层不认识 ORM 记录**，
    转换是"适配"这件事的一部分（`domain/` 只吃自己的 dataclass）。

    `usable` 是"供应商是否合格"的查表结果（协调者查好传进来）：
    缺项按**不可用**处理（`False`）—— 兜底方向只能往严的一侧倒。
    """
    return [
        QuotationFacts(
            supplier_id=str(record.supplier_id),
            amount_fen=int(record.amount),
            status=record.status,
            valid_until=record.valid_until,
            supplier_usable=usable.get(record.supplier_id, False),
        )
        for record in records
    ]
