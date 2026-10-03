"""审批用例（M6）：建链、待办、认领、通过、驳回、进度。

事务边界在这一层（仓库只 `flush()`），业务判断在 `domain/`。

============================ 这个文件里最要紧的三件事 ============================
**1. 建链时就把"没人能审"挡在门外。**
   解析出 0 个候选人 → 422，并说清是哪一级缺人。
   这一条比什么都重要：它把"申请提交成功后永久卡死、没有任何提示"
   变成"提交时就报错、立刻能修"。

**2. "轮到谁"只有一个事实来源**：`approval_flows.current_step_order`。
   待办查询要按它过滤，决定接口也要按它校验（不匹配 → 403）。
   任务状态里**没有**"未轮到"这一档 —— 同一件事存两处必然漂移。

**3. 推进指针必须条件更新。**
   两人同时通过同一级的两个会签任务时，"先读再写"会把指针推两格、**跳过一整级**，
   而那一级的审批人永远等不到待办，也不会有人发现。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from app.application.ports import (
    ApprovalRepositoryPort,
    ApprovalTaskView,
    CatalogRepositoryPort,
    PurchaseRepositoryPort,
    RequestEventRepositoryPort,
    UnitOfWork,
    UserDirectoryPort,
)
from app.domain.approval_engine import (
    ApprovalStep,
    exclude_requester,
    resolve_approval_chain,
)
from app.domain.enums import (
    FLOW_APPROVED,
    FLOW_REJECTED,
    MODE_ALL,
    MODE_SINGLE,
    TASK_APPROVED,
    TASK_PENDING,
    TASK_REJECTED,
    RequestStatus,
)
from app.domain.events import APPROVAL_COMPLETED, APPROVAL_REJECTED
from app.domain.org import descendant_ids, parent_map
from app.domain.permissions import RequestScope
from app.errors import BusinessError, ConflictError, NotFoundError, PermissionDeniedError

#: 需要"按申请人部门（含子部门）"解析候选人的角色。
#: 其余角色（采购 / 财务 / GM）职责本身就跨部门，全局找人。
_DEPARTMENT_SCOPED_ROLES = frozenset({"DEPT_MANAGER", "DEPT_HEAD"})

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class ApprovalCoordinator:
    def __init__(
        self,
        *,
        approvals: ApprovalRepositoryPort,
        requests: PurchaseRepositoryPort,
        events: RequestEventRepositoryPort,
        users: UserDirectoryPort,
        catalog: CatalogRepositoryPort,
        uow: UnitOfWork,
    ) -> None:
        self._approvals = approvals
        self._requests = requests
        self._events = events
        self._users = users
        self._catalog = catalog
        self._uow = uow

    # ================================================================ 建链（提交时）

    async def build_chain(
        self,
        *,
        request_id: uuid.UUID,
        requester_id: uuid.UUID,
        department_id: uuid.UUID,
        category_id: uuid.UUID,
        amount_fen: int,
        urgency: str,
        policy: Mapping[str, Any],
        policy_version: int,
        actor_id: uuid.UUID,
    ) -> tuple[Any, Sequence[ApprovalTaskView]]:
        """按策略算出审批链，建 1 条流 + N 条任务，并写事件。

        **不 commit** —— 它必须和"主单状态推进到 IN_APPROVAL"在同一个事务里
        （见 `PurchaseCoordinator.submit`）。任何一步失败都要整体回滚，
        否则会出现"状态已进审批中，但审批链没建出来"这种卡死的单子。
        """
        context = {
            "amount_fen": amount_fen,
            "urgency": urgency,
            "framework_contract": False,  # 第一版没有框架协议字段，见策略里的开关
            "department_id": str(department_id),
            "category_id": str(category_id),
            "requester_id": str(requester_id),
        }
        chain = resolve_approval_chain(policy, context)
        if not chain:
            raise BusinessError(
                "这份审批策略算出的审批链为空（没有任何一级需要审批）",
                status_code=422,
                code="EMPTY_APPROVAL_CHAIN",
            )

        # ---- 先解析每一级的候选人，解析不出来就整体拒绝（规则 3 + SoD）----
        planned = await self._plan_tasks(
            chain=chain, department_id=department_id, requester_id=requester_id
        )

        flow = await self._approvals.create_flow(
            request_id=request_id,
            # 建流时把"当前级"设成第一级 —— 从这一刻起它就是唯一事实来源
            current_step_order=chain[0].order,
            policy_snapshot=dict(policy),
            policy_version=policy_version,
        )
        tasks = await self._approvals.add_tasks(
            flow_id=flow.id, request_id=request_id, tasks=planned
        )
        await self._events.append(
            request_id=request_id,
            event_type="APPROVAL_STARTED",
            actor_id=actor_id,
            # 语义是"审批链已生成"，**不是**状态变更 ——
            # `DRAFT → IN_APPROVAL` 那一条由提交自己写（先写，再建链，见 PurchaseCoordinator）。
            # 这里再写一遍同样的迁移会让时间线出现两条一模一样的"提交"。
            from_status=RequestStatus.IN_APPROVAL.value,
            to_status=RequestStatus.IN_APPROVAL.value,
            payload={
                "steps": [step.order for step in chain],
                "roles": [step.role for step in chain],
                "tasks": len(planned),
            },
        )
        return flow, tasks

    async def _plan_tasks(
        self,
        *,
        chain: Sequence[ApprovalStep],
        department_id: uuid.UUID,
        requester_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        """为每一级解析候选人，并把"要建几条任务、分给谁"先算出来。

        | mode | 建几条 | assignee_id |
        | `SINGLE`（或签） | **1 条** | NULL —— 谁先认领谁审 |
        | `ALL`（会签） | **N 条**（N = 合格人数） | 建的时候就预分配 |

        会签不能抢单："所有人都要通过"和"谁抢到算谁的"在语义上互斥。

        ============================ 两步过滤，顺序不能换 ============================
        1. **先按角色解析候选人**（`DEPT_*` 限申请人部门含子部门，其余全局）；
        2. **再把提交人自己剔除**（职责分离：请购与审批必须分离）。

        第 2 步**必须在"有没有人"这个判断之前做**：否则部门只有 1 个主管时，
        他自提的申请会生成一条"他自己审自己"的任务，而且看着完全正常。
        """
        planned: list[dict[str, Any]] = []
        department_scope = await self._department_scope(department_id)

        for step in chain:
            if step.role in _DEPARTMENT_SCOPED_ROLES:
                everyone = await self._users.find_by_role(
                    step.role, department_ids=department_scope
                )
            else:
                everyone = await self._users.find_by_role(step.role)

            # ★ 职责分离：提交人不得审批自己的申请（Oracle 的 skip 语义）
            candidates = exclude_requester(everyone, requester_id)

            where = (
                "申请人所在部门（含子部门）"
                if step.role in _DEPARTMENT_SCOPED_ROLES
                else "全公司"
            )
            if not candidates:
                # 分两种情况报错，因为**用户该怎么做**完全不同：
                if everyone:
                    # 有人，但唯一的那个人就是提交人自己 → 让他知道是"回避"规则拦的，
                    # 而不是"没人配这个角色"。
                    raise BusinessError(
                        f"审批链第 order={step.order} 级（{step.role}）在{where}范围里"
                        "只有申请人自己符合条件 —— 申请人不能审批自己的申请，"
                        "请由他人提交，或先为该角色增加其他审批人",
                        status_code=422,
                        code="SELF_APPROVAL_NOT_ALLOWED",
                    )
                # 规则 3：这一级压根没人可审 → 直接 422，说清是哪一级。
                # 生成"没人能审"的任务 = 单据永久卡死且无人知情。
                raise BusinessError(
                    f"审批链第 order={step.order} 级（{step.role}）在{where}范围里"
                    "没有任何可用审批人，申请无法提交",
                    status_code=422,
                    code="NO_APPROVER_FOR_STEP",
                )

            if step.mode == MODE_ALL:
                for candidate in candidates:
                    planned.append(
                        {
                            "step_order": step.order,
                            "role": step.role,
                            "mode": MODE_ALL,
                            "assignee_id": candidate.id,
                        }
                    )
            else:
                planned.append(
                    {
                        "step_order": step.order,
                        "role": step.role,
                        "mode": MODE_SINGLE,
                        "assignee_id": None,
                    }
                )
        return planned

    async def _department_scope(self, department_id: uuid.UUID) -> set[uuid.UUID]:
        """申请人所在部门 + 全部子部门。

        子部门必须带上：后勤组挂在行政部下，行政部主管当然要能审后勤组的单子。
        少算了不会报错，只会让子部门的申请**没人看得到**。
        """
        departments = await self._catalog.list_departments(only_active=False)
        parents = parent_map((row.id, row.parent_id) for row in departments)
        return {department_id, *descendant_ids(department_id, parents)}

    # ================================================================ 待办

    async def list_todo(
        self,
        *,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> tuple[list[tuple[ApprovalTaskView, Any, Any]], int]:
        """我的待办：**只列当前这一级**，且一次 JOIN 查完。"""
        page_size = max(1, min(limit, MAX_PAGE_SIZE))
        rows, total = await self._approvals.list_todo(
            actor_id=actor_id,
            my_roles=list(roles),
            limit=page_size,
            offset=max(0, offset),
        )
        return list(rows), total

    async def get_task_detail(
        self, *, task_id: uuid.UUID, actor_id: uuid.UUID, roles: Sequence[str]
    ) -> tuple[ApprovalTaskView, Any, Any, Sequence[Any]]:
        """任务详情：任务 + 申请 + 品类 + 审批链全貌。

        非当前级 → 403：任务对他**可见**（他有这个角色），只是还没轮到他。
        连角色都不匹配 → 403 同样成立（"这条不该你审"）。
        """
        task = await self._approvals.get_task(task_id)
        if task is None:
            raise NotFoundError(f"审批任务不存在：{task_id}")

        flow = await self._approvals.get_flow(task.request_id)
        if flow is None:  # pragma: no cover - 任务有外键指向流，理论上到不了
            raise NotFoundError("审批流不存在")
        if task.step_order != flow.current_step_order:
            raise PermissionDeniedError(
                f"这张单子现在在第 {flow.current_step_order} 级，还没轮到第 {task.step_order} 级"
            )

        request = await self._requests.get_request(
            task.request_id, scope=await self._any_scope(), actor_id=actor_id
        )
        if request is None:  # pragma: no cover
            raise NotFoundError("申请不存在")
        await self._assert_can_act(
            task=task,
            actor_id=actor_id,
            roles=roles,
            requester_id=request.requester_id,
        )
        category = await self._catalog.get_category(request.category_id)
        tasks = await self._approvals.list_tasks(flow_id=flow.id)
        return task, request, category, tasks

    async def list_flow(self, *, request_id: uuid.UUID) -> tuple[Any, Sequence[Any]]:
        """审批链进度（申请人与审批人都能看）：流 + 全部任务。

        一眼能看出"卡在谁那里"，所以它不限制"只能看自己的"——
        调用方（边界层）已经先把申请的可见范围校验过了。
        """
        flow = await self._approvals.get_flow(request_id)
        if flow is None:
            raise NotFoundError("这张申请还没有审批流（可能还没提交）")
        tasks = await self._approvals.list_tasks(flow_id=flow.id)
        return flow, tasks

    # ================================================================ 决定

    async def claim(
        self,
        *,
        task_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        expected_version: int,
    ) -> ApprovalTaskView:
        """认领：`assignee_id` 从 NULL 置为当前用户（抢单）。

        只有未认领的**或签**任务能被认领：会签在建任务时就预分配了人，
        "所有人都要通过"与"谁抢到算谁的"语义互斥。
        """
        task, flow = await self._load_actionable(
            task_id=task_id, actor_id=actor_id, roles=roles
        )
        if task.assignee_id is not None:
            raise ConflictError("该任务已经被认领")
        if task.mode == MODE_ALL:
            raise ConflictError("会签任务在建单时就已指定审批人，不能认领")

        claimed = await self._approvals.claim(
            task_id=task_id,
            actor_id=actor_id,
            expected_version=expected_version,
            current_step_order=flow.current_step_order,
            # 职责分离：提交人抢不到自己的单（见 `_assert_can_act` 的说明）
            request_id=task.request_id,
        )
        if not claimed:
            raise ConflictError("该任务已被他人认领，或版本已过期，请刷新")
        await self._events.append(
            request_id=task.request_id,
            event_type="APPROVAL_CLAIMED",
            actor_id=actor_id,
            from_status=RequestStatus.IN_APPROVAL.value,
            to_status=RequestStatus.IN_APPROVAL.value,
            payload={"task_id": str(task_id), "step_order": task.step_order},
        )
        await self._uow.commit()
        reloaded = await self._approvals.get_task(task_id)
        if reloaded is None:  # pragma: no cover
            raise NotFoundError("审批任务不存在")
        return reloaded

    async def approve(
        self,
        *,
        task_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        expected_version: int,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """通过：条件更新 → 判该级是否完成 → 完成则推进"当前级"或收尾整个流程。"""
        task, flow = await self._load_actionable(
            task_id=task_id, actor_id=actor_id, roles=roles
        )
        decided = await self._approvals.decide(
            task_id=task_id,
            expected_version=expected_version,
            current_step_order=flow.current_step_order,
            status=TASK_APPROVED,
            actor_id=actor_id,
            comment=(comment or "").strip() or None,
            # 职责分离：提交人不能批自己的申请（或签任务没有预分配人，必须在这里挡）
            request_id=task.request_id,
        )
        if not decided:
            raise ConflictError("该任务已被处理，或还没轮到这一级，请刷新后重试")

        await self._events.append(
            request_id=task.request_id,
            event_type="APPROVAL_APPROVED",
            actor_id=actor_id,
            from_status=RequestStatus.IN_APPROVAL.value,
            to_status=RequestStatus.IN_APPROVAL.value,
            payload={
                "task_id": str(task_id),
                "step_order": task.step_order,
                "role": task.role,
            },
        )

        outcome = await self._after_decision(
            flow=flow, step_order=task.step_order, actor_id=actor_id
        )
        await self._uow.commit()
        return outcome

    async def reject(
        self,
        *,
        task_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        expected_version: int,
        comment: str,
    ) -> dict[str, Any]:
        """驳回：意见必填（它是申请人唯一能看到的理由），终止式。

        第一版只做**终止式**：任一级驳回，整张申请变 `REJECTED`。
        "回退式 / 补充式"进暂缓清单（见领域设计 4.5）。
        """
        reason = (comment or "").strip()
        if not reason:
            raise BusinessError("驳回必须填写意见", status_code=422, code="COMMENT_REQUIRED")

        task, flow = await self._load_actionable(
            task_id=task_id, actor_id=actor_id, roles=roles
        )
        decided = await self._approvals.decide(
            task_id=task_id,
            expected_version=expected_version,
            current_step_order=flow.current_step_order,
            status=TASK_REJECTED,
            actor_id=actor_id,
            comment=reason,
            request_id=task.request_id,
        )
        if not decided:
            raise ConflictError("该任务已被处理，或还没轮到这一级，请刷新后重试")

        # 后面的任务全部取消：单子已经结束了，不该再挂在别人待办里
        await self._approvals.cancel_pending_tasks(
            flow_id=flow.id, except_task_id=task_id
        )
        await self._approvals.complete_flow(
            flow_id=flow.id,
            status=FLOW_REJECTED,
            expected_step_order=flow.current_step_order,
        )
        await self._push_request_status(
            request_id=task.request_id,
            actor_id=actor_id,
            target=RequestStatus.REJECTED,
            event_type=APPROVAL_REJECTED,
            payload={"step_order": task.step_order, "role": task.role, "comment": reason},
        )
        await self._uow.commit()
        return {
            "approved": False,
            "flow_status": FLOW_REJECTED,
            "request_status": RequestStatus.REJECTED.value,
            "comment": reason,
        }

    # ================================================================ 内部

    async def _load_actionable(
        self, *, task_id: uuid.UUID, actor_id: uuid.UUID, roles: Sequence[str]
    ) -> tuple[ApprovalTaskView, Any]:
        """把"这个任务现在能不能由这个人处理"一次查清。

        三种拒绝要分清：
          - 任务不存在 → 404
          - 不属于当前级 → **403**（可见，但还没轮到）
          - 不属于他（角色不符 / 或签被别人认领了）→ **403**
        """
        task = await self._approvals.get_task(task_id)
        if task is None:
            raise NotFoundError(f"审批任务不存在：{task_id}")
        flow = await self._approvals.get_flow(task.request_id)
        if flow is None:  # pragma: no cover
            raise NotFoundError("审批流不存在")

        if task.step_order != flow.current_step_order:
            raise PermissionDeniedError(
                f"这张单子现在在第 {flow.current_step_order} 级，还没轮到第 {task.step_order} 级"
            )
        if task.status != TASK_PENDING:
            raise ConflictError(f"该任务已经处理过了（{task.status}）")

        await self._assert_can_act(
            task=task,
            actor_id=actor_id,
            roles=roles,
            requester_id=await self._requester_of(task.request_id, actor_id),
        )
        return task, flow

    @staticmethod
    async def _assert_can_act(
        *,
        task: ApprovalTaskView,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        requester_id: uuid.UUID,
    ) -> None:
        """是不是"该他审"。

        - **提交人本人 → 403**（职责分离：请购与审批必须分离。或签任务没有预分配人，
          只按角色可见，所以"建任务时剔除提交人"挡不住它，必须在这里也挡一次）；
        - 任务已预分配（会签）→ 必须是那个人；
        - 未认领（或签）→ 必须持有该角色；
        - 已被别人认领 → 只有认领人本人能处理。
        """
        if actor_id == requester_id:
            raise PermissionDeniedError("申请人不能审批自己的申请（职责分离）")
        if task.assignee_id is not None:
            if task.assignee_id != actor_id:
                raise PermissionDeniedError("这条审批任务不是分配给您的")
            return
        if task.role not in set(roles):
            raise PermissionDeniedError(f"这条审批任务需要 {task.role} 角色")

    async def _requester_of(self, request_id: uuid.UUID, actor_id: uuid.UUID) -> uuid.UUID:
        """这条申请的提交人是谁（职责分离判定要用）。"""
        request = await self._requests.get_request(
            request_id, scope=await self._any_scope(), actor_id=actor_id
        )
        if request is None:  # pragma: no cover - 任务有外键指向申请
            raise NotFoundError("申请不存在")
        return request.requester_id

    async def _after_decision(
        self, *, flow: Any, step_order: int, actor_id: uuid.UUID
    ) -> dict[str, Any]:
        """一次通过之后：判该级是否完成；完成则推进指针或收尾。

        | mode | 该级完成条件 |
        | `SINGLE` | 该级下**任意一个**任务已通过 |
        | `ALL` | 该级下**全部**任务都已通过 |

        ⚠️ 判定用的是 `step_statuses`（**强制从库里读**），不是对象上的 `.status`。
        `decide` 是 bulk UPDATE 且 `synchronize_session=False`，会话里已加载的对象
        不会被刷新 —— 读对象的话，"刚通过的那条"仍是 `PENDING`，于是这一步永远判"还没完"、
        指针永不推进，而且**不报任何错**（实测踩过，与 M5 回读旧对象是同一类问题）。

        ⚠️ **还要先在流那一行取锁**（`lock_flow_for_update`）。原因：
        "这一级是否完成"依赖**别人的**任务状态，而每个请求一个会话，默认隔离级别下
        看不到别的事务尚未提交的改动 —— 两个人同时通过同一级的两个会签任务时，
        A 看到 B 是 PENDING、B 看到 A 是 PENDING，两边都判"没完成"，谁都不推进。
        取锁后同一条流上的判定串行化，后到的那个负责推进。
        """
        # 锁住这条流：从这一刻起，"判定 + 推进"是一个串行的临界区
        locked_step = await self._approvals.lock_flow_for_update(flow.id)
        if locked_step is None:  # pragma: no cover - 流一定存在
            raise NotFoundError("审批流不存在")

        statuses = await self._approvals.step_statuses(
            flow_id=flow.id, step_order=step_order
        )
        if any(status == TASK_PENDING for _, status in statuses):
            # 这一级还没完（会签里有人没点），什么都不做
            return {
                "approved": True,
                "flow_status": flow.status,
                "request_status": RequestStatus.IN_APPROVAL.value,
                "step_completed": False,
            }

        # 该级完成 → 找下一个还有任务的级
        next_step = await self._next_step_order(flow=flow, after=step_order)
        if next_step is None:
            # 所有级都完成了 → 收尾
            await self._approvals.complete_flow(
                flow_id=flow.id,
                status=FLOW_APPROVED,
                # 用**加锁读回来的**当前级做条件更新（不是内存里那份可能过期的）
                expected_step_order=locked_step,
            )
            await self._push_request_status(
                request_id=flow.request_id,
                actor_id=actor_id,
                target=RequestStatus.SOURCING,
                event_type=APPROVAL_COMPLETED,
                payload={"steps": sorted({step for step, _ in statuses})},
            )
            return {
                "approved": True,
                "flow_status": FLOW_APPROVED,
                "request_status": RequestStatus.SOURCING.value,
                "step_completed": True,
            }

        # 推进"当前级"：**条件更新**（在锁的保护下，这里应当总能成功）
        moved = await self._approvals.advance_step(
            flow_id=flow.id,
            expected_step_order=locked_step,
            next_step_order=next_step,
        )
        return {
            "approved": True,
            "flow_status": flow.status,
            "request_status": RequestStatus.IN_APPROVAL.value,
            "step_completed": True,
            "current_step_order": next_step if moved else locked_step,
            "advanced": moved,
        }

    async def _next_step_order(self, *, flow: Any, after: int) -> int | None:
        """下一个还有任务、且尚未全部结束的级。

        用**任务表**反推而不是用策略快照：快照是"当时的依据"，
        而真正要走的级是建任务时定下来的那几级。
        同样只读状态列（原因见 `_after_decision` 的说明）。
        """
        statuses = await self._approvals.step_statuses(flow_id=flow.id)
        candidates = sorted({step for step, _ in statuses if step > after})
        for step in candidates:
            if any(
                status == TASK_PENDING
                for current, status in statuses
                if current == step
            ):
                return step
        return None

    async def _push_request_status(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        target: RequestStatus,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        """由审批模块推进主单状态，并写事件。

        边界很清楚：**审批模块不直接改主单的其它字段**，只推进状态 + 写事件。
        将来加"补充式驳回"这类流程时，两层不会互相纠缠。

        条件更新用"当前状态必须是 IN_APPROVAL" + 主单自己的 version：
        审批通过和申请人撤回可能同时发生，谁先到谁生效，另一个 0 行受影响。
        """
        request = await self._requests.get_request(
            request_id, scope=await self._any_scope(), actor_id=actor_id
        )
        if request is None:  # pragma: no cover
            raise NotFoundError("申请不存在")
        if request.status != RequestStatus.IN_APPROVAL.value:
            raise ConflictError(
                f"申请当前状态是 {request.status}，不能推进到 {target.value}"
            )

        moved = await self._requests.transition_status(
            request_id=request_id,
            expected_version=request.version,
            from_status=RequestStatus.IN_APPROVAL.value,
            to_status=target.value,
            extra={"closed_at": datetime.now(UTC)},
        )
        if not moved:
            raise ConflictError("申请状态已被其他人改变，请刷新后重试")

        await self._events.append(
            request_id=request_id,
            event_type=event_type,
            actor_id=actor_id,
            from_status=RequestStatus.IN_APPROVAL.value,
            to_status=target.value,
            payload=dict(payload),
        )

    @staticmethod
    async def _any_scope() -> RequestScope:
        """审批模块读申请时用"全公司"范围。

        为什么可以这样：能走到这一层的调用者，要么是**该级的审批人**
        （`_assert_can_act` 已校验角色/指派），要么是**申请人本人**
        （进度查询由边界层按申请可见范围校验）。
        审批动作的可见性由**任务**决定，而不是由申请的行级范围决定 ——
        采购专员本来就该看得到全公司的单子。
        """
        return RequestScope(sees_all=True)
