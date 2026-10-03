"""审批接口（M6）。

边界层只做三件事：校验输入、装配依赖、把记录转成响应模型。
业务规则在 `domain/`，事务边界在 `application/coordinators/`。

============================ 403 与 404 的分工（这一层最容易搞混） ============================
   任务不在**当前级**        → **403**（他看得见这条单子，只是还没轮到他）
   任务分配给了别人          → **403**
   角色不匹配                → **403**
   申请本身不在他的可见范围  → **404**（连"这东西存在"都不告诉他）

两种语义不能混：可见但无权是 403；不在可见范围才是 404。

============================ 待办列表一次 JOIN 查完 ============================
列表项要显示金额、申请人、品类，这些字段在申请与品类表上。
查询在仓库里一次 JOIN 带出（`ApprovalRepository.list_todo`），
**不要在边界层循环补查** —— 那是经典的 N+1。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from app.application.coordinators.approval import ApprovalCoordinator
from app.application.coordinators.purchase import PurchaseCoordinator
from app.application.idempotency import IdempotencyCoordinator
from app.application.ports import ApprovalTaskView
from app.boundaries.deps import CurrentUser, SessionDep
from app.database import SessionFactory
from app.infrastructure.repositories.approval_repo import ApprovalRepository
from app.infrastructure.repositories.catalog_repo import CatalogRepository
from app.infrastructure.repositories.event_repo import RequestEventRepository
from app.infrastructure.repositories.purchase_repo import PurchaseRepository
from app.infrastructure.repositories.user_repo import UserRepository

router = APIRouter(prefix="/api/v1/approvals", tags=["审批"])


# ------------------------------------------------------------------ 依赖装配


def approval_coordinator(session: SessionDep) -> ApprovalCoordinator:
    """组装一次审批用例所需的依赖。

    审批要**推进主单状态**（通过 → SOURCING、驳回 → REJECTED），
    而状态迁移由 `PurchaseRepository.transition_status` 做 —— 两边共用同一个 `session`，
    所以"任务状态 + 主单状态 + 事件"落在同一个事务里。
    """
    return ApprovalCoordinator(
        approvals=ApprovalRepository(session),
        requests=PurchaseRepository(session),
        events=RequestEventRepository(session),
        users=UserRepository(session),
        catalog=CatalogRepository(session),
        uow=session,
    )


# ------------------------------------------------------------------ 入参


class DecisionIn(BaseModel):
    """通过 / 驳回的入参。`version` 是任务的乐观锁版本。"""

    version: int = Field(ge=1)
    comment: str | None = Field(default=None, max_length=2000)


class RejectIn(BaseModel):
    """驳回：**意见必填** —— 它是申请人唯一能看到的理由。"""

    version: int = Field(ge=1)
    comment: str = Field(min_length=1, max_length=2000)


class ClaimIn(BaseModel):
    version: int = Field(ge=1)


# ------------------------------------------------------------------ 响应


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    request_id: uuid.UUID
    step_order: int
    role: str
    mode: str
    status: str
    assignee_id: uuid.UUID | None = None
    decided_by: uuid.UUID | None = None
    decided_at: datetime | None = None
    comment: str | None = None
    version: int
    created_at: datetime


class RequestSummaryOut(BaseModel):
    """待办列表里要显示的申请摘要（一次 JOIN 带出的那几个字段）。"""

    id: uuid.UUID
    code: str
    title: str
    requester_id: uuid.UUID
    category_id: uuid.UUID
    category_name: str
    amount_total_fen: int
    urgency: str
    status: str
    submitted_at: datetime | None = None


class TodoItemOut(BaseModel):
    task: TaskOut
    request: RequestSummaryOut


class TodoPageOut(BaseModel):
    items: list[TodoItemOut]
    total: int


class TaskDetailOut(BaseModel):
    task: TaskOut
    request: RequestSummaryOut
    #: 审批链全貌：让审批人看到"前面谁批过了、后面还有几级"
    steps: list[TaskOut]
    current_step_order: int
    #: 这条任务分配给谁（未认领时为 null）
    assignee_name: str | None = None


class FlowOut(BaseModel):
    request_id: uuid.UUID
    status: str
    current_step_order: int
    #: 提交那一刻的策略原件与它的版本 —— "为什么只走了两级"的依据就在这里
    policy_snapshot: dict
    policy_version: int
    started_at: datetime
    completed_at: datetime | None = None
    steps: list[TaskOut]


class DecisionOut(BaseModel):
    approved: bool
    flow_status: str
    request_status: str
    step_completed: bool | None = None
    current_step_order: int | None = None
    comment: str | None = None


# ------------------------------------------------------------------ 序列化


def _summary(request: Any, category: Any) -> RequestSummaryOut:
    return RequestSummaryOut(
        id=request.id,
        code=request.code,
        title=request.title,
        requester_id=request.requester_id,
        category_id=request.category_id,
        category_name=category.name,
        amount_total_fen=request.amount_total,
        urgency=request.urgency,
        status=request.status,
        submitted_at=request.submitted_at,
    )


# ------------------------------------------------------------------ 接口


@router.get(
    "/tasks",
    response_model=TodoPageOut,
    summary="我的待办（只列当前这一级）",
)
async def list_tasks(
    user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TodoPageOut:
    """待办。

    **只列当前这一级**：这张单子走到第几级由 `approval_flows.current_step_order` 决定，
    还没轮到的高级别不会出现在这里。少了这个条件，第 4 级的财务会在第 1 级还没批完时
    就看到待办，而且点了"通过"真的会生效。
    """
    rows, total = await approval_coordinator(session).list_todo(
        actor_id=user.id, roles=user.roles, limit=limit, offset=offset
    )
    return TodoPageOut(
        items=[
            TodoItemOut(task=TaskOut.model_validate(task), request=_summary(request, category))
            for task, request, category in rows
        ],
        total=total,
    )


@router.get(
    "/tasks/{task_id}",
    response_model=TaskDetailOut,
    summary="任务详情（含申请与审批链全貌）",
    responses={403: {"description": "不是当前级，或不是分配给您"}, 404: {"description": "任务不存在"}},
)
async def get_task(
    task_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> TaskDetailOut:
    task, request, category, steps = await approval_coordinator(session).get_task_detail(
        task_id=task_id, actor_id=user.id, roles=user.roles
    )
    assignee_name = None
    if task.assignee_id is not None:
        assignee = await UserRepository(session).find_by_id(task.assignee_id)
        assignee_name = assignee.full_name if assignee is not None else None
    flow, _ = await approval_coordinator(session).list_flow(request_id=task.request_id)
    return TaskDetailOut(
        task=TaskOut.model_validate(task),
        request=_summary(request, category),
        steps=[TaskOut.model_validate(item) for item in steps],
        current_step_order=flow.current_step_order,
        assignee_name=assignee_name,
    )


@router.post(
    "/tasks/{task_id}/claim",
    response_model=TaskOut,
    summary="认领（抢单，仅或签）",
    responses={
        403: {"description": "不是当前级"},
        409: {"description": "已被他人认领 / 会签不能认领 / 版本过期"},
    },
)
async def claim_task(
    task_id: uuid.UUID, payload: ClaimIn, user: CurrentUser, session: SessionDep
) -> TaskOut:
    task = await approval_coordinator(session).claim(
        task_id=task_id,
        actor_id=user.id,
        roles=user.roles,
        expected_version=payload.version,
    )
    return TaskOut.model_validate(task)


@router.post(
    "/tasks/{task_id}/approve",
    response_model=DecisionOut,
    summary="通过",
    responses={403: {"description": "不是当前级"}, 409: {"description": "已被处理 / 版本过期"}},
)
async def approve_task(
    task_id: uuid.UUID, payload: DecisionIn, user: CurrentUser, session: SessionDep
) -> DecisionOut:
    """通过。

    某级完成后：会签要等**全部**任务通过，或签任一通过即可；
    该级完成则把 `current_step_order` 推进到下一级（**条件更新**，防止跳过一整级）；
    所有级都完成则主单推进到 `SOURCING` 并写 `APPROVAL_COMPLETED` 事件。
    """
    outcome = await approval_coordinator(session).approve(
        task_id=task_id,
        actor_id=user.id,
        roles=user.roles,
        expected_version=payload.version,
        comment=payload.comment,
    )
    return DecisionOut(**outcome)


@router.post(
    "/tasks/{task_id}/reject",
    response_model=DecisionOut,
    summary="驳回（意见必填）",
    responses={403: {"description": "不是当前级"}, 409: {"description": "已被处理 / 版本过期"}},
)
async def reject_task(
    task_id: uuid.UUID, payload: RejectIn, user: CurrentUser, session: SessionDep
) -> DecisionOut:
    """驳回：终止式。主单变 `REJECTED`，其余未处理的任务一并取消。"""
    outcome = await approval_coordinator(session).reject(
        task_id=task_id,
        actor_id=user.id,
        roles=user.roles,
        expected_version=payload.version,
        comment=payload.comment,
    )
    return DecisionOut(**outcome)


@router.get(
    "/requests/{request_id}/flow",
    response_model=FlowOut,
    summary="审批链进度（申请人也能看）",
    responses={404: {"description": "申请不可见，或还没有审批流"}},
)
async def get_flow(
    request_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> FlowOut:
    """审批链进度。

    先按**申请的可见范围**校验一次（看不见就 404），再读审批流 ——
    申请人因此也能看到"自己卡在谁那里"。
    """
    # 复用采购的可见范围校验：范围外直接 404
    purchase = PurchaseCoordinator(
        requests=PurchaseRepository(session),
        events=RequestEventRepository(session),
        catalog=CatalogRepository(session),
        idempotency=IdempotencyCoordinator(
            claim_session_factory=SessionFactory, session=session
        ),
        uow=session,
    )
    await purchase.get_request_detail(
        request_id=request_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
    )

    flow, steps = await approval_coordinator(session).list_flow(request_id=request_id)
    return FlowOut(
        request_id=flow.request_id,
        status=flow.status,
        current_step_order=flow.current_step_order,
        policy_snapshot=flow.policy_snapshot,
        policy_version=flow.policy_version,
        started_at=flow.started_at,
        completed_at=flow.completed_at,
        steps=[TaskOut.model_validate(task) for task in steps],
    )


__all__ = ["approval_coordinator", "router", "ApprovalTaskView"]
