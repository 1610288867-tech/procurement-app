"""幂等记录的数据访问（M5）。

纪律同其他仓库：**只 flush()，不 commit()** —— 这里只负责读写这一行，
"什么时候提交"由幂等协调者决定（它必须让占位行**先于业务事务**提交）。

============================ 为什么占位行要独立提交 ============================
两个请求带着**同一个 key 同时**到达时，若只做"查不到就执行"：
两边都查不到 → 两边都执行 → 幂等失效，且**悄无声息**。

正确做法是先抢一个占位行（`IN_PROGRESS`）：
撞唯一索引的那一方就知道"有人正在处理同一把 key"，直接 409。
而占位行必须**用自己的事务提交**，否则业务回滚会把它一起带走，
第二个请求就再也查不到它，又回到了"两边都执行"。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.records.purchase import IdempotencyRecord

STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"


class IdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def find(
        self, *, actor_id: uuid.UUID, scope: str, key: str
    ) -> IdempotencyRecord | None:
        statement = select(IdempotencyRecord).where(
            IdempotencyRecord.actor_id == actor_id,
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.key == key,
        )
        return (await self.session.scalars(statement)).one_or_none()

    async def try_claim(
        self, *, actor_id: uuid.UUID, scope: str, key: str, payload_hash: str
    ) -> bool:
        """抢占位行。抢到返回 True，撞唯一索引返回 False。

        撞索引之后**必须回滚**：PostgreSQL 的事务一旦报错就进入 aborted 状态，
        后续任何语句都会失败 —— 而我们要接着查那一行，所以得先让事务可用。
        """
        record = IdempotencyRecord(
            actor_id=actor_id,
            scope=scope,
            key=key,
            payload_hash=payload_hash,
            status=STATUS_IN_PROGRESS,
        )
        self.session.add(record)
        try:
            await self.session.flush()
        except IntegrityError:
            await self.session.rollback()
            return False
        return True

    async def complete(
        self,
        *,
        actor_id: uuid.UUID,
        scope: str,
        key: str,
        response: Mapping[str, Any],
    ) -> None:
        """回填响应并标记完成。**不 commit** —— 与业务写在同一个事务里。"""
        statement = (
            update(IdempotencyRecord)
            .where(
                IdempotencyRecord.actor_id == actor_id,
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.key == key,
            )
            .values(status=STATUS_COMPLETED, response=dict(response))
            .execution_options(synchronize_session=False)
        )
        await self.session.execute(statement)

    async def purge_stale_in_progress(self, *, older_than_seconds: int = 120) -> int:
        """清掉卡住的占位行。

        进程在"占位之后、完成之前"被杀（Ctrl+C、崩溃），那一行会永远停在
        `IN_PROGRESS`，于是这把 key **永远**拿到 409 —— 用户重试也没用。

        这一版的处理是"过期即删"（而不是标记 FAILED）：删掉之后客户端可以原样重试，
        语义与实施计划第 4 步的"业务失败 → 标记 FAILED（或直接删掉），让客户端可以重试"一致，
        而少一个状态就少一处要维护的分支。

        注意阈值只有 120 秒：真有人处理两分钟以上，也是**慢**而不是"正在处理"，
        让他重试一次比让他一直 409 更好。
        """
        cutoff = datetime.now(UTC).timestamp() - older_than_seconds
        statement = (
            delete(IdempotencyRecord)
            .where(
                IdempotencyRecord.status == STATUS_IN_PROGRESS,
                IdempotencyRecord.created_at < datetime.fromtimestamp(cutoff, tz=UTC),
            )
            .returning(IdempotencyRecord.id)
        )
        result = await self.session.execute(statement)
        return len(list(result.scalars().all()))
