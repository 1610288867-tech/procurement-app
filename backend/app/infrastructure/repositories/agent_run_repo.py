"""Agent 运行记录的数据访问（M12++）。

纪律同其他仓库：**只 flush()，不 commit()** —— 事务边界在用例层。

============================ 一个例外要说清 ============================
本仓库的 `save_trace` 会**显式 flush**，因为调用方（Agent 循环）需要在
**每次工具调用之后**把轨迹落盘 —— 那是"断点"的全部意义：
进程在下一步之前被杀，已跑完的步骤必须已经在库里。

只 flush 不 commit 的话，轨迹会留在**未提交的事务**里，
进程一挂就全没了 —— 那正是这个功能要解决的问题。
所以协调者那边**每步之后 commit 一次**（见 `KnowledgeCoordinator._run_agent`）。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.agent_run import (
    RUN_FAILED,
    RUN_FINISHED,
    RUN_INTERRUPTED,
    RUN_RUNNING,
    is_stale,
)
from app.infrastructure.records.agent_run import AgentRunRecord

#: 判定"卡住的 RUNNING"的阈值（秒）。取 600 = `MAX_TOTAL_SECONDS`（180）的 3.3 倍，
#: 故意宽松：宁可让残留多躺一会儿，也不要误判一个正在跑的请求。
STALE_RUNNING_SECONDS = 600.0


class AgentRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def start(
        self,
        *,
        session_id: uuid.UUID,
        actor_id: uuid.UUID,
        question: str,
    ) -> AgentRunRecord:
        """**新开**一次运行（id 由数据库默认生成）。

        ⚠️ 这里**不再接受 `run_id`**：第一版接受它并用它 INSERT，
        而续跑时那一行本来就在库里 → 主键冲突 → 被翻成 503（实测撞到）。
        "接着那一次跑"是 `get()` + `mark_running()`，不是"再用那个 id 建一行"。
        去掉这个参数，就没法把那两种语义写混。
        """
        record = AgentRunRecord(
            session_id=session_id,
            actor_id=actor_id,
            question=question,
            status=RUN_RUNNING,
            trace=[],
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def get(self, run_id: uuid.UUID) -> AgentRunRecord | None:
        return await self.session.get(AgentRunRecord, run_id)

    async def save_trace(
        self,
        *,
        run_id: uuid.UUID,
        trace: list[dict[str, Any]],
        status: str = RUN_RUNNING,
    ) -> None:
        """整列覆盖轨迹并落盘。**必须由调用方 commit** —— 见模块说明。"""
        statement = (
            update(AgentRunRecord)
            .where(AgentRunRecord.id == run_id)
            .values(trace=list(trace), status=status)
            .execution_options(synchronize_session=False)
        )
        await self.session.execute(statement)
        await self.session.flush()

    async def finish(
        self,
        *,
        run_id: uuid.UUID,
        status: str,
        answer: str | None = None,
        stop_reason: str | None = None,
    ) -> None:
        """结束一次运行。`answer` 留 None 表示没跑出回答（不是空回答）。

        ⚠️ **刻意不接受 `trace`**（第一版接受，结果踩了坑）：
        调用点手里那份"步骤"是**给界面看的简化形状**（`round/kind/detail/tool`），
        而 `trace` 要的是**能用来续跑的完整形状**（还要 `call_id` / `arguments`）。
        把前者写进 `trace` 会把续跑需要的字段抹掉 —— 实测撞到：
        一次运行结束后 `trace` 变成 2 条精简记录，`arguments` 全丢，
        于是"续跑"再也对不上模型这次的调用。

        **完整轨迹的唯一写入者是 `save_trace`**（每步一次，见 `_agent_run`）。
        收尾只改状态与回答，不碰轨迹。
        """
        values: dict[str, Any] = {"status": status, "stop_reason": stop_reason}
        if answer is not None:
            values["answer"] = answer
        statement = (
            update(AgentRunRecord)
            .where(AgentRunRecord.id == run_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        await self.session.execute(statement)
        await self.session.flush()

    async def mark_running(self, *, run_id: uuid.UUID) -> bool:
        """把一次中断的运行**重新标成 RUNNING**（开始续跑）。

        必须做这一步，否则续跑期间它仍是 `INTERRUPTED` ——
        另一个请求可以同时"续跑"同一次运行，两边都调模型、都执行工具。
        条件更新（只有 `INTERRUPTED` 能转回 `RUNNING`）让第二个请求拿到 0 行，
        于是它只能走"不能续跑"的分支。
        """
        statement = (
            update(AgentRunRecord)
            .where(
                AgentRunRecord.id == run_id,
                AgentRunRecord.status == RUN_INTERRUPTED,
            )
            .values(status=RUN_RUNNING)
            .returning(AgentRunRecord.id)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def mark_interrupted(self, *, run_id: uuid.UUID, reason: str) -> bool:
        """把一次运行标成"中断"（可续跑）。

        **条件更新**：只有 `RUNNING` 才能转 `INTERRUPTED`。
        两个进程同时想标同一次运行时，只有一个会成功 —— 另一个拿到 0 行，
        于是不会出现"两次都以为自己该续跑"。
        """
        statement = (
            update(AgentRunRecord)
            .where(AgentRunRecord.id == run_id, AgentRunRecord.status == RUN_RUNNING)
            .values(status=RUN_INTERRUPTED, stop_reason=reason)
            .returning(AgentRunRecord.id)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def sweep_stale_running(self, *, stale_after_seconds: float = STALE_RUNNING_SECONDS) -> int:
        """把卡住的 `RUNNING` 批量标成 `INTERRUPTED`，让它们可以被续跑。

        为什么必须有这一步：进程被杀时，"其实已经中断了"这件事**没人会写库**
        （没有 finally、没有信号处理）。所以只能由**后来者**按时间判：
        一个 `RUNNING` 躺了远超一次正常运行的时长，它只可能是残留。

        用 `updated_at` 而不是 `created_at` 算年龄：一次运行可能跑很久，
        但**每步都会刷新 updated_at**；用创建时间会把"跑得久"误判成"卡住了"。
        """
        rows = (
            await self.session.scalars(
                select(AgentRunRecord).where(AgentRunRecord.status == RUN_RUNNING)
            )
        ).all()
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        swept = 0
        for row in rows:
            # `updated_at` 由数据库的 onupdate 维护；某些驱动下刚插入的行可能为 None，
            # 那时退回 created_at（宁可晚一次，也不要漏掉）。
            reference = row.updated_at or row.created_at
            age = (now - reference).total_seconds()
            if is_stale(
                status=row.status, age_seconds=age, stale_after_seconds=stale_after_seconds
            ):
                row.status = RUN_INTERRUPTED
                row.stop_reason = "被判定为中断（超过阈值仍停在 RUNNING）"
                swept += 1
        if swept:
            await self.session.flush()
        return swept

    async def mark_failed(self, *, run_id: uuid.UUID, reason: str) -> None:
        await self.finish(run_id=run_id, status=RUN_FAILED, stop_reason=reason)

    @staticmethod
    def finished_status() -> str:
        return RUN_FINISHED
