"""事件流的数据访问（M5）。

**只追加**：这个仓库里不提供任何 update / delete 方法 ——
不是"我们约定不去调用"，而是**根本没有那个方法可调**。
事件流一旦可改就失去审计意义，所以从接口上先把它堵死。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.records.purchase import RequestEventRecord


class RequestEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def append(
        self,
        *,
        request_id: uuid.UUID,
        event_type: str,
        actor_id: uuid.UUID,
        from_status: str | None = None,
        to_status: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> RequestEventRecord:
        """追加一条事件。

        ============================ 顺序靠 seq，不靠时间戳 ============================
        时间线是给人看历史的，顺序不稳定是硬伤。两条事件在**同一个事务**里写入时，
        时间戳可能落在同一微秒上（早期用 `created_at` 排序就是这么翻车的：
        顺序退化到按 uuid4 的 `id` 比大小，实测撞到过 `REQUEST_SUBMITTED`
        与 `APPROVAL_STARTED` 颠倒）。

        所以排序键改为 `seq`：数据库侧的单调序列，谁先落盘谁的值小，
        与时钟精度、并发、UUID 都无关（迁移 `b7d3f0c41a26`）。
        **这里刻意不给它赋值** —— 手工指定会打乱这条保证，
        而且列上的 identity 也不需要应用侧参与。
        """
        record = RequestEventRecord(
            request_id=request_id,
            event_type=event_type,
            actor_id=actor_id,
            from_status=from_status,
            to_status=to_status,
            payload=dict(payload or {}),
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def list_for_request(self, request_id: uuid.UUID) -> list[RequestEventRecord]:
        """时间线读法：按写入顺序**正序**（先发生的在上面）。"""
        statement = (
            select(RequestEventRecord)
            .where(RequestEventRecord.request_id == request_id)
            .order_by(RequestEventRecord.seq)
        )
        return list((await self.session.scalars(statement)).all())
