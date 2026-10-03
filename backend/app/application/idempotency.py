"""幂等（M-09，M5 首次接入）。

============================ 它解决的是什么问题 ============================
"网络超时"和"服务端失败"在客户端看来长得一样：都是没拿到响应。
于是客户端重试 —— 没有幂等机制时，重试就会**多建一条申请**或**重复提交一次**。

契约（AGENTS.md 第 4 条）：
    `(actor, scope, key, payload_hash)`
    同键同载荷 → 重放上次的响应
    同键不同载荷 → 409

============================ 并发才是真难点 ============================
只做"查不到就执行"的话，两个请求带**同一把 key 同时**到达时：
两边都查不到 → 两边都执行 → 幂等失效，而且失效得**悄无声息**。

所以流程是"先占位、再执行"：

    1. 独立事务插入占位行 (actor_id, scope, key, payload_hash, IN_PROGRESS) 并提交
       ├─ 抢到了        → 继续往下做业务
       └─ 撞唯一索引    → 说明已有同 key 请求，再查那一行：
            ├─ COMPLETED 且载荷相同 → 返回它存下的响应（这才是"重放"）
            ├─ 载荷不同            → 409
            └─ IN_PROGRESS         → 409（正在处理中，请稍后重试）
    2. 业务事务：执行 → 提交
    3. 记录事务：把响应与 COMPLETED 回填 → 提交

**占位行必须独立提交**：否则业务回滚会把它一起带走，第二个请求就再也看不到它。

============================ scope 必须用端点标识 ============================
scope 留空或随手写，同一把 key 会在"提交"和"撤回"之间**串味**：
第二次请求会拿到第一次的响应，而且看起来一切正常 —— 这类错误不会报错，
只会让某个操作凭空消失。所以 scope 一律由调用方按端点写死。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.errors import ConflictError
from app.infrastructure.records.purchase import IdempotencyRecord
from app.infrastructure.repositories.idempotency_repo import (
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    IdempotencyRepository,
)

T = TypeVar("T")

#: 占位行卡住多久算"过期"。两分钟足够任何一次正常请求走完。
STALE_CLAIM_SECONDS = 120


def payload_fingerprint(payload: Mapping[str, Any] | None) -> str:
    """把载荷变成稳定的哈希。

    `sort_keys=True` 不能省：字典顺序不同、内容相同也算**同一个载荷**，
    否则"同样的请求重试一次"会被误判成"改了内容"，直接给用户一个 409。
    """
    canonical = json.dumps(payload or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IdempotentResult:
    value: Any
    replayed: bool


class IdempotencyCoordinator:
    """包住"一次写操作"，让它可以被安全重试。

    依赖两个会话工厂，而不是两个现成的会话 —— 这是刻意的：
    独立事务意味着**必须自己开会话**，否则"提交占位行"会把调用方还没做完的事
    一起提交掉。把"自己管事务"这件事显式放在构造参数里，读代码的人一眼能看见。
    """

    def __init__(
        self,
        *,
        claim_session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        self._claim_factory = claim_session_factory
        self._session = session

    async def run(
        self,
        *,
        actor_id: uuid.UUID,
        scope: str,
        key: str,
        payload: Mapping[str, Any] | None,
        work: Callable[[], Awaitable[T]],
        dump: Callable[[T], Mapping[str, Any]],
    ) -> IdempotentResult:
        """执行 `work`，并按幂等契约决定是"真做"还是"重放"。"""
        fingerprint = payload_fingerprint(payload)

        # ---- 1. 占位（独立事务）----
        async with self._claim_factory() as claim_session:
            repo = IdempotencyRepository(claim_session)
            # 顺手清理卡住的占位行：上一次请求被 Ctrl+C 杀掉时留下的那种
            await repo.purge_stale_in_progress(older_than_seconds=STALE_CLAIM_SECONDS)
            claimed = await repo.try_claim(
                actor_id=actor_id, scope=scope, key=key, payload_hash=fingerprint
            )
            await claim_session.commit()
            if not claimed:
                await claim_session.rollback()
                replay = await repo.find(actor_id=actor_id, scope=scope, key=key)
                await claim_session.commit()
                if replay is not None:
                    return self._decide(replay, fingerprint)

        # ---- 2. 业务事务 ----
        try:
            value = await work()
        except Exception:
            # 业务失败要把占位行标成 FAILED，否则这把 key 会一直占用到过期为止
            await self._mark_failed(actor_id=actor_id, scope=scope, key=key)
            raise

        # ---- 3. 记录事务 ----
        await self._record(actor_id=actor_id, scope=scope, key=key, body=dump(value))
        return IdempotentResult(value=value, replayed=False)

    @staticmethod
    def _decide(record: Any, fingerprint: str) -> IdempotentResult:
        """拿到已存在的那一行之后怎么办。"""
        if record.payload_hash != fingerprint:
            raise ConflictError(
                "同一个 Idempotency-Key 被用于不同的请求内容，请换一把新的键"
            )
        if record.status == STATUS_IN_PROGRESS:
            raise ConflictError("该请求正在处理中，请稍后重试")
        if record.status == STATUS_COMPLETED:
            return IdempotentResult(value=record.response, replayed=True)
        raise ConflictError("幂等记录状态异常")  # pragma: no cover - 状态列有约束兜底

    async def _record(
        self,
        *,
        actor_id: uuid.UUID,
        scope: str,
        key: str,
        body: Mapping[str, Any],
    ) -> None:
        """把响应与 COMPLETED 回填，并提交。

        这一次提交必须在**业务提交之后**（业务已经落库了，记录没落上，
        最坏结果是这把 key 过期后可以再来一次；反过来则会丢掉业务结果）。
        """
        await self._session.commit()
        await IdempotencyRepository(self._session).complete(
            actor_id=actor_id, scope=scope, key=key, response=body
        )
        await self._session.commit()

    async def _mark_failed(
        self, *, actor_id: uuid.UUID, scope: str, key: str
    ) -> None:
        """业务失败：回滚业务事务，把**占位行删掉**。

        删而不是标记 FAILED：占位行的唯一作用是"挡住并发的同键请求"。
        业务已经失败了，继续占着这把 key 只会让用户的重试也拿到 409 ——
        那是最糟的体验：明明什么都没做成，却告诉他"重复提交"。
        （实施计划第 4 步允许"标记 FAILED 或直接删掉"，这里选后者，少一个状态分支。）

        用**独立的会话**去做：业务会话这时已经不可用了。
        """
        await self._session.rollback()
        async with self._claim_factory() as session:
            await session.execute(
                delete(IdempotencyRecord).where(
                    IdempotencyRecord.actor_id == actor_id,
                    IdempotencyRecord.scope == scope,
                    IdempotencyRecord.key == key,
                )
            )
            await session.commit()
