"""事件流的数据访问（M5）。

**只追加**：这个仓库里不提供任何 update / delete 方法 ——
不是"我们约定不去调用"，而是**根本没有那个方法可调**。
事件流一旦可改就失去审计意义，所以从接口上先把它堵死。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
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

        ============================ 为什么时间戳由应用侧给 ============================
        `created_at` 的列默认值是 PostgreSQL 的 `now()`，而它返回的是**事务开始时间** ——
        同一个事务里写的多条事件会拿到**完全相同**的时间戳，于是"按时间正序"退化成
        按 UUID 随机比较，时间线顺序每次都可能不一样
        （实测撞到过：同一笔提交里的 `REQUEST_SUBMITTED` 与 `APPROVAL_STARTED` 顺序颠倒）。

        时间线是给人看历史的，顺序不稳定是硬伤。所以这里在应用侧取一次微秒级时间戳
        显式写进去：同一事务内先后调用必然递增，顺序与代码执行顺序一致。
        """
        record = RequestEventRecord(
            request_id=request_id,
            event_type=event_type,
            actor_id=actor_id,
            from_status=from_status,
            to_status=to_status,
            payload=dict(payload or {}),
            created_at=datetime.now(UTC),
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def list_for_request(self, request_id: uuid.UUID) -> list[RequestEventRecord]:
        """时间线读法：按时间**正序**（先发生的在上面）。"""
        statement = (
            select(RequestEventRecord)
            .where(RequestEventRecord.request_id == request_id)
            .order_by(RequestEventRecord.created_at, RequestEventRecord.id)
        )
        return list((await self.session.scalars(statement)).all())
