"""询价与定标接口（M10 / 8.1）。

四条路由：

    GET  /api/v1/sourcing/queue                     待询价队列（状态 = SOURCING）
    POST /api/v1/requests/{id}/quotations           登记报价
    GET  /api/v1/requests/{id}/quotations           比价表（含"还差几家"的说明）
    POST /api/v1/requests/{id}/award                定标

============================ 比价表为什么返回"说明"而不是报错 ============================
`GET .../quotations` 是**只读视图**：采购专员需要看到"当前 2 家、还差 1 家"
才能决定下一步该干什么。把这条信息做成 422 的话，界面只能显示一个红色错误条，
而看不到已经登记的报价 —— 那等于把工作台变成报错页。

真正会拒绝的是**定标**（`POST .../award`）：那才是"能不能成交"的判定点。

============================ 为什么三条写接口都要求 PROCUREMENT ============================
业务规则第 3 条定稿：v1 **由同一个采购角色完成询价与定标**（不新增第二个角色，
靠"≥3 家 + 供应商合格 + 报价完整 + 记录成交单价"四条硬控制兜底）。
所以入口收在 `PROCUREMENT` 这一侧；比价表（读）不限角色，因为申请人/审批人也该
看得到自己那张单子比了几家（可见范围照旧由行级权限决定）。

============================ 定标要带幂等键 ============================
与提交 / 撤回同一类："重复执行会改变结果"的动作。定标已经用条件更新兜住了
并发（第二个人会拿到 409），但**网络超时后的重试**是另一回事：
用户不知道第一次到底成功了没有，重试会撞 409 —— 那个 409 是**正确**的，
可体验很差。带幂等键之后，同键重放会拿回第一次的响应。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.application.coordinators.sourcing import (
    SourcingCoordinator,
    build_sourcing_coordinator,
)
from app.boundaries.deps import CurrentUser, SessionDep, require_roles
from app.boundaries.http.requests import idempotency_key
from app.config import get_settings
from app.database import SessionFactory
from app.domain.permissions import ROLE_PROCUREMENT
from app.domain.sourcing_policy import MIN_QUOTATIONS

router = APIRouter(tags=["询价与定标"])


def sourcing_coordinator(session: SessionDep) -> SourcingCoordinator:
    """组装询价用例的依赖。

    装配本身在 `coordinators/sourcing.build_sourcing_coordinator` ——
    边界层只负责把**配置**与**会话工厂**喂进去，测试也共用同一份装配。
    """
    return build_sourcing_coordinator(
        session,
        # 幂等层要能在独立事务里提交占位行，所以它拿的是**会话工厂**
        # （与 `purchase_coordinator` 完全同一套接线，见那里的说明）。
        claim_session_factory=SessionFactory,
        threshold_fen=get_settings().sourcing_threshold_fen,
    )


# ------------------------------------------------------------------ 响应模型


class RequestSummaryOut(BaseModel):
    """队列与比价表里的申请摘要。**只带界面要用的字段**。"""

    id: uuid.UUID
    code: str
    title: str
    requester_id: uuid.UUID
    department_id: uuid.UUID
    amount_total_fen: int
    status: str
    version: int


class QuotationOut(BaseModel):
    id: uuid.UUID
    request_id: uuid.UUID
    supplier_id: uuid.UUID
    supplier_name: str
    amount_fen: int
    currency: str
    status: str
    valid_until: datetime | None
    attachment_id: uuid.UUID | None
    created_by: uuid.UUID
    created_at: datetime


class QueueOut(BaseModel):
    items: list[RequestSummaryOut]
    total: int


class CompareOut(BaseModel):
    """比价表。

    `problems` 是**给人看的说明**（"还差 1 家"），不是错误码：
    读比价表本身永远成功，能不能定标由它回答。
    """

    request: RequestSummaryOut
    quotations: list[QuotationOut]
    problems: list[str]
    threshold_fen: int
    min_quotations: int


class AwardOut(BaseModel):
    request: RequestSummaryOut
    quotation: QuotationOut
    deal_unit_price_fen: int


class QuotationIn(BaseModel):
    supplier_id: uuid.UUID
    #: 单位：分（界面上的"元"由 money.ts 换算）
    amount_fen: int
    valid_until: datetime | None = None
    attachment_id: uuid.UUID | None = None


class AwardIn(BaseModel):
    supplier_id: uuid.UUID
    version: int


def _summary(request: Any) -> RequestSummaryOut:
    return RequestSummaryOut(
        id=request.id,
        code=request.code,
        title=request.title,
        requester_id=request.requester_id,
        department_id=request.department_id,
        amount_total_fen=int(request.amount_total),
        status=request.status,
        version=request.version,
    )


def _quotation(row: Any, supplier_name: str) -> QuotationOut:
    return QuotationOut(
        id=row.id,
        request_id=row.request_id,
        supplier_id=row.supplier_id,
        supplier_name=supplier_name,
        amount_fen=int(row.amount),
        currency=row.currency,
        status=row.status,
        valid_until=row.valid_until,
        attachment_id=row.attachment_id,
        created_by=row.created_by,
        created_at=row.created_at,
    )


# ------------------------------------------------------------------ 队列


@router.get(
    "/api/v1/sourcing/queue",
    response_model=QueueOut,
    summary="待询价队列（状态 = SOURCING）",
    dependencies=[Depends(require_roles(ROLE_PROCUREMENT))],
)
async def list_queue(
    user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> QueueOut:
    """审批通过、还没定标的单子 —— 采购专员的工作台。"""
    rows, total = await sourcing_coordinator(session).list_queue(
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
        limit=limit,
        offset=offset,
    )
    return QueueOut(items=[_summary(row) for row in rows], total=total)


# ------------------------------------------------------------------ 报价


@router.post(
    "/api/v1/requests/{request_id}/quotations",
    response_model=QuotationOut,
    status_code=201,
    summary="登记供应商报价",
    dependencies=[Depends(require_roles(ROLE_PROCUREMENT))],
    responses={
        409: {"description": "该供应商已经报过价，或申请不在询价中"},
        422: {"description": "供应商不合格 / 金额非法"},
    },
)
async def add_quotation(
    request_id: uuid.UUID,
    payload: QuotationIn,
    user: CurrentUser,
    session: SessionDep,
) -> QuotationOut:
    """登记一家报价。金额单位是**分**（与全项目一致）。"""
    record, supplier_name = await sourcing_coordinator(session).add_quotation(
        request_id=request_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
        supplier_id=payload.supplier_id,
        amount_fen=payload.amount_fen,
        valid_until=payload.valid_until,
        attachment_id=payload.attachment_id,
    )
    return _quotation(record, supplier_name)


@router.get(
    "/api/v1/requests/{request_id}/quotations",
    response_model=CompareOut,
    summary="比价表（含当前能否定标的说明）",
)
async def list_quotations(
    request_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> CompareOut:
    """比价表。**读永远成功** —— 能不能定标放在 `problems` 里说。"""
    request, rows, names, problems = await sourcing_coordinator(session).list_quotations(
        request_id=request_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
    )
    return CompareOut(
        request=_summary(request),
        quotations=[_quotation(row, names.get(str(row.id), str(row.supplier_id))) for row in rows],
        problems=problems,
        threshold_fen=get_settings().sourcing_threshold_fen,
        min_quotations=MIN_QUOTATIONS,
    )


# ------------------------------------------------------------------ 定标


@router.post(
    "/api/v1/requests/{request_id}/award",
    response_model=AwardOut,
    summary="定标（选中一家成交）",
    dependencies=[Depends(require_roles(ROLE_PROCUREMENT))],
    responses={
        409: {"description": "已被别人定标 / 版本过期 / 申请不在询价中"},
        422: {"description": "不满足定标条件（返回具体差在哪）"},
    },
)
async def award(
    request_id: uuid.UUID,
    payload: AwardIn,
    user: CurrentUser,
    session: SessionDep,
    key: Annotated[str, Depends(idempotency_key)],
) -> AwardOut:
    """定标。

    带 `Idempotency-Key`：这个动作改变了主单状态，网络超时后的重试
    应当拿回**第一次的结果**（同键同载荷重放），而不是撞一个 409 让用户
    不知道到底成没成。同键**不同供应商** = 改了主意 → 409。
    """
    payload_out = await sourcing_coordinator(session).award_idempotent(
        request_id=request_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
        supplier_id=payload.supplier_id,
        expected_version=payload.version,
        idempotency_key=key,
    )
    # 幂等层存的就是可直接序列化的 dict，这里直接校验成响应模型 ——
    # 首次执行与重放走**同一条**返回路径，所以两次的响应逐字节相同。
    return AwardOut.model_validate(payload_out)
