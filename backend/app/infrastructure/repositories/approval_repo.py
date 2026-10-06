"""审批流与审批任务的数据访问（M6）。

纪律同其他仓库：**只 flush()，不 commit()** —— 事务边界在用例层。

============================ 这个仓库里两处"必须条件更新" ============================
1. **审批决定**：`status = PENDING` 与 `version` 一起进 `WHERE`，
   两个人同时点"通过"只有一个成功。
2. **推进当前级**：`current_step_order = <旧值>` 进 `WHERE`，
   否则两人同时通过同一级的两个会签任务，会把指针推进两次、**跳过一整级**。

外加一处"必须写进 WHERE"：**只有当前这一级的任务可以被处理**。
这一条不能只靠界面不显示按钮 —— 接口是可以被直接调用的。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import Select, and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    FLOW_APPROVED,
    FLOW_REJECTED,
    FLOW_RUNNING,
    MODE_ALL,
    TASK_APPROVED,
    TASK_CANCELLED,
    TASK_PENDING,
    TASK_REJECTED,
)
from app.domain.permissions import RequestScope
from app.infrastructure.records.approval import ApprovalFlowRecord, ApprovalTaskRecord
from app.infrastructure.records.catalog import PurchaseCategoryRecord
from app.infrastructure.records.purchase import PurchaseRequestRecord

# 部门范围条件的**唯一落点**在采购仓库里（`_scope_conditions`），这里直接复用。
# 为什么不各写一份：那是"权限必须写进 WHERE"的实现，抄一份就多一处会漂移的规则 ——
# 两处不一致时，其中一处必然是越权，而且不会报错。
from app.infrastructure.repositories.purchase_repo import _scope_conditions


class ApprovalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ 建流

    async def create_flow(
        self,
        *,
        request_id: uuid.UUID,
        current_step_order: int,
        policy_snapshot: dict,
        policy_version: int,
    ) -> ApprovalFlowRecord:
        """建审批流。`current_step_order` 初始化成**第一级的 order**。

        它从这一刻起就是"现在轮到谁"的唯一事实来源；后面每完成一级推进一次。
        """
        flow = ApprovalFlowRecord(
            request_id=request_id,
            status=FLOW_RUNNING,
            current_step_order=current_step_order,
            policy_snapshot=policy_snapshot,
            policy_version=policy_version,
        )
        self.session.add(flow)
        await self.session.flush()
        return flow

    async def add_tasks(
        self,
        *,
        flow_id: uuid.UUID,
        request_id: uuid.UUID,
        tasks: Iterable[dict[str, Any]],
    ) -> list[ApprovalTaskRecord]:
        """建任务。**一次把审批链上所有级的任务都建出来**。

        这样审批链进度能一眼看全（"已完成 / 当前 / 未开始"三态时间轴），
        而"现在轮到谁"由流的 `current_step_order` 单独表达 —— 见 `domain/enums.py`
        里"任务状态刻意没有 WAITING"的说明。

        `tasks` 每项：`step_order / role / mode / assignee_id`。
        `assignee_id` 只有会签会预分配；或签留 NULL（谁先认领谁审）。
        """
        records: list[ApprovalTaskRecord] = []
        for item in tasks:
            record = ApprovalTaskRecord(
                flow_id=flow_id,
                request_id=request_id,
                step_order=int(item["step_order"]),
                role=str(item["role"]),
                mode=str(item.get("mode", "SINGLE")),
                status=TASK_PENDING,
                assignee_id=item.get("assignee_id"),
            )
            self.session.add(record)
            records.append(record)
        await self.session.flush()
        return records

    # ------------------------------------------------------------------ 读

    async def get_flow(self, request_id: uuid.UUID) -> ApprovalFlowRecord | None:
        statement = select(ApprovalFlowRecord).where(
            ApprovalFlowRecord.request_id == request_id
        )
        return (await self.session.scalars(statement)).one_or_none()

    async def get_task(self, task_id: uuid.UUID) -> ApprovalTaskRecord | None:
        return await self.session.get(ApprovalTaskRecord, task_id)

    async def list_tasks(
        self, *, flow_id: uuid.UUID | None = None, request_id: uuid.UUID | None = None
    ) -> list[ApprovalTaskRecord]:
        statement = select(ApprovalTaskRecord).order_by(
            ApprovalTaskRecord.step_order, ApprovalTaskRecord.created_at
        )
        if flow_id is not None:
            statement = statement.where(ApprovalTaskRecord.flow_id == flow_id)
        if request_id is not None:
            statement = statement.where(ApprovalTaskRecord.request_id == request_id)
        return list((await self.session.scalars(statement)).all())

    async def step_tasks(
        self, *, flow_id: uuid.UUID, step_order: int
    ) -> list[ApprovalTaskRecord]:
        """某一级下的全部任务（读对象，给响应与进度视图用）。

        ⚠️ **判定"这一级是否完成"不要用它的 `status`**：`decide` 走的是 bulk UPDATE
        且 `synchronize_session=False`，会话里已加载的对象**不会**被刷新 ——
        刚通过的那条读出来还是 `PENDING`。判状态请用 `step_statuses`。
        """
        statement = (
            select(ApprovalTaskRecord)
            .where(
                ApprovalTaskRecord.flow_id == flow_id,
                ApprovalTaskRecord.step_order == step_order,
            )
            .order_by(ApprovalTaskRecord.created_at)
        )
        return list((await self.session.scalars(statement)).all())

    async def step_statuses(
        self, *, flow_id: uuid.UUID, step_order: int | None = None
    ) -> list[tuple[int, str]]:
        """**强制从库里读**状态：`(step_order, status)`。

        为什么不用 `step_tasks` 再读 `.status`：`decide` 走的是 bulk UPDATE 且
        `synchronize_session=False`，它**不会**刷新会话里已加载的对象。于是刚通过的那条
        在内存里仍是 `PENDING`，步骤完成判定会永远判"还没完"、`current_step_order`
        永远不推进 —— 而且**不报任何错**（实测踩过，与 M5 回读旧对象是同一类问题）。

        ⚠️ 还有一个更隐蔽的坑：**列查询（`select(Model.col)`）也会经过身份映射** ——
        行会映射回会话里已有的对象，读出来照样是旧值。
        必须显式加 `populate_existing=True` 才会用库里的值覆盖内存里的那份。
        （诊断记录：两个任务库里都是 APPROVED，接口却回 `step_completed: false`。）
        """
        statement = (
            select(ApprovalTaskRecord.step_order, ApprovalTaskRecord.status)
            .where(ApprovalTaskRecord.flow_id == flow_id)
            .execution_options(populate_existing=True)
        )
        if step_order is not None:
            statement = statement.where(ApprovalTaskRecord.step_order == step_order)
        rows = await self.session.execute(statement)
        return [(int(step), str(status)) for step, status in rows.all()]

    # ------------------------------------------------------------------ 待办

    async def list_todo(
        self,
        *,
        actor_id: uuid.UUID,
        my_roles: Sequence[str],
        scope: RequestScope,
        limit: int,
        offset: int,
    ) -> tuple[list[tuple[ApprovalTaskRecord, PurchaseRequestRecord, PurchaseCategoryRecord]], int]:
        """我的待办：**一次 JOIN 查完**，并**只取当前这一级**。

        三个条件都关键：

        - `step_order == flow.current_step_order`（规则 6）：少了它，第 4 级的财务
          在自己还没轮到时就拿到待办，而且点了"通过"真的会生效；
        - **部门范围**（2026-10-04 补）：`DEPT_MANAGER` / `DEPT_HEAD` 是**部门级角色**
          —— 建链时引擎把它们解析成"该部门及其子部门里的人"，可**办的时候**
          如果只校验"你有没有这个角色"，同一个角色名就会跨部门通行
          （行政部门的主管能看到、能批掉采购部那一级的任务）。
          任务表里只有 `role`，**不携带部门**，所以范围必须由**申请所属部门**推出来。
          这条此前是绿的，只因为开发库里恰好没有别的部门停在同一级。
        - 三个 JOIN：待办列表要显示金额、申请人、品类。写成"查出 20 条任务再逐条查申请"
          就是经典的 N+1（20 条任务 → 21 条查询）。

        `my_roles` 为空时只按 `assignee_id` 匹配：会签的任务建的时候就预分配了人，
        所以会签人拿得到；或签（NULL + 角色）则需要角色。

        ⚠️ 部门范围对**已预分配**（会签）的任务同样生效 —— 但那是安全的：
        会签的候选人本来就是建链时按部门范围挑出来的，两边一致。
        """
        candidates = [ApprovalTaskRecord.assignee_id == actor_id]
        if my_roles:
            candidates.append(
                and_(
                    ApprovalTaskRecord.assignee_id.is_(None),
                    ApprovalTaskRecord.role.in_(list(my_roles)),
                )
            )
        visibility = or_(*candidates)

        base = (
            select(ApprovalTaskRecord, PurchaseRequestRecord, PurchaseCategoryRecord)
            .join(
                ApprovalFlowRecord,
                ApprovalFlowRecord.id == ApprovalTaskRecord.flow_id,
            )
            .join(
                PurchaseRequestRecord,
                ApprovalTaskRecord.request_id == PurchaseRequestRecord.id,
            )
            .join(
                PurchaseCategoryRecord,
                PurchaseRequestRecord.category_id == PurchaseCategoryRecord.id,
            )
            .where(
                ApprovalTaskRecord.status == TASK_PENDING,
                # ★ 规则 6：只取"当前这一级"
                ApprovalTaskRecord.step_order == ApprovalFlowRecord.current_step_order,
                visibility,
                # ★ 部门范围：部门级角色只在本部门（含子部门）内可见
                *_scope_conditions(scope, actor_id=actor_id),
                # ★ 职责分离（2026-10-03）：提交人不能审自己的申请。
                # 或签（SINGLE）任务建的时候 `assignee_id` 是 NULL、只按角色可见，
                # 所以"建任务时剔除提交人"挡不住它 —— 必须在这里也排掉。
                PurchaseRequestRecord.requester_id != actor_id,
            )
        )

        total = (
            await self.session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar_one()

        statement: Select = base.order_by(
            ApprovalTaskRecord.created_at.desc(), ApprovalTaskRecord.id
        ).limit(limit).offset(offset)
        rows = [
            (task, request, category)
            for task, request, category in (await self.session.execute(statement)).all()
        ]
        return rows, int(total)

    # ------------------------------------------------------------------ 写

    async def decide(
        self,
        *,
        task_id: uuid.UUID,
        expected_version: int,
        current_step_order: int,
        status: str,
        actor_id: uuid.UUID,
        comment: str | None,
        request_id: uuid.UUID | None = None,
    ) -> bool:
        """通过 / 驳回：**条件更新**，四种条件一起进 `WHERE`。

        ```
        id = :task_id
        status = 'PENDING'                    -- 已被处理 → 0 行
        version = :expected_version           -- 版本过期 → 0 行（并发同点）
        step_order = :current_step_order      -- 还没轮到 / 已经过了 → 0 行（越级）
        ```

        第 4 条是"规则 6"在**决定**这条路径上的落点。少了它，越级审批只被界面挡着，
        而界面是拦不住 Postman 的。

        `request_id` 给了就再加一条**职责分离**守卫：`NOT EXISTS(该申请是 actor 提的)`。
        这条挡的是**或签**：或签任务没有预分配人，只要角色够就能看到一个 NULL 的任务，
        所以"建任务时剔除提交人"对它无效 —— 必须在决定时也挡一次。
        放进同一条 SQL 而不是"先查申请再判断"，是为了避免先查再写（并发下不可靠）。
        """
        conditions = [
            ApprovalTaskRecord.id == task_id,
            ApprovalTaskRecord.status == TASK_PENDING,
            ApprovalTaskRecord.version == expected_version,
            ApprovalTaskRecord.step_order == current_step_order,
        ]
        if request_id is not None:
            conditions.append(
                ~exists(
                    select(PurchaseRequestRecord.id).where(
                        PurchaseRequestRecord.id == request_id,
                        PurchaseRequestRecord.requester_id == actor_id,
                    )
                )
            )

        statement = (
            update(ApprovalTaskRecord)
            .where(*conditions)
            .values(
                status=status,
                decided_by=actor_id,
                decided_at=func.now(),
                comment=comment,
                version=ApprovalTaskRecord.version + 1,
            )
            .returning(ApprovalTaskRecord.id)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        updated = result.scalar_one_or_none() is not None
        if updated:
            await self._refresh_task(task_id)
        return updated

    async def claim(
        self,
        *,
        task_id: uuid.UUID,
        actor_id: uuid.UUID,
        expected_version: int,
        current_step_order: int,
        request_id: uuid.UUID | None = None,
    ) -> bool:
        """认领：`assignee_id` 从 NULL 置为当前用户。

        同样要求"属于当前级"与"仍是 NULL"（`assignee_id.is_(None)` 进 `WHERE`
        就是"谁先抢到算谁的"—— 第二个人 0 行受影响）。会签任务建的时候就预分配了人，
        所以走不到这里。

        `request_id` 与 `decide` 同理：加一条**职责分离**守卫，提交人抢不到自己的单。
        """
        conditions = [
            ApprovalTaskRecord.id == task_id,
            ApprovalTaskRecord.status == TASK_PENDING,
            ApprovalTaskRecord.version == expected_version,
            ApprovalTaskRecord.step_order == current_step_order,
            ApprovalTaskRecord.assignee_id.is_(None),
        ]
        if request_id is not None:
            conditions.append(
                ~exists(
                    select(PurchaseRequestRecord.id).where(
                        PurchaseRequestRecord.id == request_id,
                        PurchaseRequestRecord.requester_id == actor_id,
                    )
                )
            )

        statement = (
            update(ApprovalTaskRecord)
            .where(*conditions)
            .values(
                assignee_id=actor_id,
                version=ApprovalTaskRecord.version + 1,
            )
            .returning(ApprovalTaskRecord.id)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        updated = result.scalar_one_or_none() is not None
        if updated:
            await self._refresh_task(task_id)
        return updated

    async def _refresh_task(self, task_id: uuid.UUID) -> None:
        """让会话里那个任务对象跟上刚提交的 bulk UPDATE。

        为什么必须刷新：`update()` 走的是 bulk UPDATE 且 `synchronize_session=False`，
        它**不会**改内存里的对象；而 `expire_on_commit=False`（异步下必须这么设）
        也不会在提交时让它们过期。不刷新的话，接口把 `assignee_id = null` 返回给前端，
        而库里其实已经认领成功了 —— **接口说没成、实际成了**，最难查的一类 bug。
        """
        task = await self.session.get(ApprovalTaskRecord, task_id)
        if task is not None:
            await self.session.refresh(task)

    async def advance_step(
        self, *, flow_id: uuid.UUID, expected_step_order: int, next_step_order: int
    ) -> bool:
        """把"当前级"推进到下一级。**条件更新**，这是全项目最容易被写错的一处。

        写成"读出来、改掉、写回去"的话，两个人同时通过同一级的两个会签任务时：
        两边读到的都是旧值，两边都写"下一级" —— 指针被推进两次，**跳过一整级**，
        而那一级的审批人永远等不到自己的待办（也不会有人发现）。

        `WHERE current_step_order = <旧值>` 让第二次推进变成 0 行受影响，
        由调用方决定是"忽略"还是"报冲突"。
        """
        statement = (
            update(ApprovalFlowRecord)
            .where(
                ApprovalFlowRecord.id == flow_id,
                ApprovalFlowRecord.current_step_order == expected_step_order,
                ApprovalFlowRecord.status == FLOW_RUNNING,
            )
            .values(current_step_order=next_step_order)
            .returning(ApprovalFlowRecord.id)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        moved = result.scalar_one_or_none() is not None
        if moved:
            # 同上：不刷新的话，紧接着读 flow.current_step_order 还是旧值，
            # 于是"推进成功"这件事在响应里看不见。
            flow = await self.session.get(ApprovalFlowRecord, flow_id)
            if flow is not None:
                await self.session.refresh(flow)
        return moved

    async def lock_flow_for_update(self, flow_id: uuid.UUID) -> int | None:
        """把审批流那一行**锁住**并读回当前级。

        为什么必须锁：判"这一级是否完成"要看**别人的**任务状态，而每个请求一个会话，
        在默认隔离级别下**看不到别的事务还没提交的改动**。两个人同时通过同一级的
        两个会签任务时，A 看到 B 还是 `PENDING`、B 看到 A 还是 `PENDING`，
        于是两边都判"本步没完成" —— 谁都不推进指针，而库里其实已经全通过了
        （**指针永远停在那一级**，而且不报任何错。实测撞到过）。

        `SELECT ... FOR UPDATE` 让同一个审批流上的判定串行化：后到的那个事务会等前者提交，
        然后看到"这一级确实完成了"，于是由它来推进。不同申请之间互不影响（锁只在流这一行上）。
        """
        statement = (
            select(ApprovalFlowRecord)
            .where(ApprovalFlowRecord.id == flow_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        flow = (await self.session.scalars(statement)).one_or_none()
        return flow.current_step_order if flow is not None else None

    async def flow_step_order(self, flow_id: uuid.UUID) -> int | None:
        """**强制从库里读**"现在轮到哪一级"（不加锁的只读版本，给响应回读用）。

        与 `step_statuses` 同一个理由：bulk UPDATE 之后内存里的对象可能是旧值，
        而返回给前端的必须是库里的事实。`populate_existing=True` 是这里的关键。
        """
        statement = (
            select(ApprovalFlowRecord.current_step_order)
            .where(ApprovalFlowRecord.id == flow_id)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def complete_flow(
        self, *, flow_id: uuid.UUID, status: str = FLOW_APPROVED, expected_step_order: int
    ) -> bool:
        """结束审批流。同样条件更新：只有"当前级还是那一级"才允许收尾。"""
        if status not in {FLOW_APPROVED, FLOW_REJECTED}:
            raise ValueError(f"complete_flow 不接受的状态：{status}")
        statement = (
            update(ApprovalFlowRecord)
            .where(
                ApprovalFlowRecord.id == flow_id,
                ApprovalFlowRecord.status == FLOW_RUNNING,
                ApprovalFlowRecord.current_step_order == expected_step_order,
            )
            .values(status=status, completed_at=func.now())
            .returning(ApprovalFlowRecord.id)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        completed = result.scalar_one_or_none() is not None
        if completed:
            flow = await self.session.get(ApprovalFlowRecord, flow_id)
            if flow is not None:
                await self.session.refresh(flow)
        return completed


    async def cancel_pending_tasks(
        self, *, flow_id: uuid.UUID, except_task_id: uuid.UUID | None = None
    ) -> int:
        """把该流里还挂着的 `PENDING` 任务标成 `CANCELLED`（驳回时用）。

        驳回意味着这张单子已经结束，后面几级的任务不该继续挂在别人的待办里 ——
        "有一条永远不需要处理的待办"会让审批人以为系统坏了。

        刻意**保留被驳回的那一条**（`except_task_id`）：它是这次决定本身，
        状态是 `REJECTED` 而不是 `CANCELLED`，两者在时间线上含义不同。
        """
        statement = (
            update(ApprovalTaskRecord)
            .where(
                ApprovalTaskRecord.flow_id == flow_id,
                ApprovalTaskRecord.status == TASK_PENDING,
            )
            .values(
                status=TASK_CANCELLED,
                version=ApprovalTaskRecord.version + 1,
            )
            .returning(ApprovalTaskRecord.id)
            .execution_options(synchronize_session=False)
        )
        if except_task_id is not None:
            statement = statement.where(ApprovalTaskRecord.id != except_task_id)
        result = await self.session.execute(statement)
        # 用 RETURNING 数条数，而不是 `result.rowcount`：
        # 异步 Result 的类型标注里没有 rowcount（mypy 会直接报错），
        # 而且 RETURNING 还能顺带拿到被取消的是哪几条。M2 起一直沿用这个写法。
        return len(list(result.scalars().all()))


__all__ = ["ApprovalRepository", "MODE_ALL", "TASK_APPROVED", "TASK_REJECTED"]
