"""订单与收货接口（M10 / 8.2）。

五条路由：

    POST /api/v1/requests/{id}/orders      由定标结果生成采购订单
    GET  /api/v1/requests/{id}/orders      按申请查订单（详情页用；没有订单不是错误）
    GET  /api/v1/orders/{id}               订单详情（订单 + 明细 + 收货单）
    GET  /api/v1/receipts/queue            待收货队列（状态 = ORDERED）
    POST /api/v1/requests/{id}/receipts    收货登记（逐行填实收数量）

============================ 角色口径 ============================
| 动作 | 角色 | 依据 |
| --- | --- | --- |
| 下单 | `PROCUREMENT` | 定标之后下单是采购的连续动作（业务规则第 3 条：v1 同一采购角色） |
| 看订单 | 登录 + 数据可见 | 申请人 / 审批人也该看得到自己那张单子买了什么、花了多少 |
| 登记收货 | **`RECEIVER`** | 内控：**采购、验收、记录三方分离**（`docs/采购内控对照.md`）—— 收货不能由采购自己做 |

============================ 下单与收货都要幂等键 ============================
两者都"重复执行会改变结果"（下单会生成订单号、收货会写收货单），
所以与提交 / 撤回 / 定标同一套幂等：同键同载荷 → 拿回第一次的响应；
同键改了载荷（例如改了实收数量）→ **409 改了主意**，而不是静默返回旧结果。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.application.coordinators.fulfillment import (
    FulfillmentCoordinator,
    build_fulfillment_coordinator,
)
from app.boundaries.deps import CurrentUser, SessionDep, require_roles
from app.boundaries.http.requests import idempotency_key
from app.database import SessionFactory
from app.domain.permissions import ROLE_PROCUREMENT, ROLE_RECEIVER

router = APIRouter(tags=["订单与收货"])


def fulfillment_coordinator(session: SessionDep) -> FulfillmentCoordinator:
    """组装订单/收货用例的依赖（装配本身在 `coordinators/fulfillment`）。"""
    return build_fulfillment_coordinator(
        session, claim_session_factory=SessionFactory
    )


# ------------------------------------------------------------------ 模型


class RequestSummaryOut(BaseModel):
    id: uuid.UUID
    code: str
    title: str
    amount_total_fen: int
    status: str
    version: int


class OrderLineOut(BaseModel):
    # `from_attributes`：订单/收货的**读接口**是把 ORM 记录直接喂给模型，
    # 而这两个模型刻意没有在协调者里再抄一份 dict（写接口才需要 dict —— 幂等层要序列化）。
    # `validation_alias`：列名不带单位后缀（`unit_price` / `amount`），
    # 响应字段带（`unit_price_fen` / `amount_fen`）——
    # **单位写进字段名**是刻意的：全项目的钱都是"分"，字段名自己说清比翻文档快。
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    order_id: uuid.UUID
    item_id: uuid.UUID | None
    line_no: int
    name: str
    spec: str | None
    quantity: int
    unit_price_fen: int = Field(validation_alias="unit_price")
    amount_fen: int = Field(validation_alias="amount")


class OrderOut(BaseModel):
    """订单（**读接口的模型，直接吃 ORM 记录**）。

    `amount_fen` 是从记录的 `amount` 列来的（列名不带单位后缀，响应字段带 ——
    单位写在字段名里是刻意的：全项目的钱都是"分"，字段名自己说清比看文档快）。
    用 `validation_alias` 做映射，而不是在边界层手抄一份 dict：
    多抄一份就多一处会漂移的地方，而漂移的表现是"某个字段忘了带上"。

    `request_status` / `request_version` 不在订单表上（它们是**申请**的字段），
    由父对象提供 —— 见 `_order_out`。
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    request_id: uuid.UUID
    order_no: str
    supplier_id: uuid.UUID
    amount_fen: int = Field(validation_alias="amount")
    status: str
    ordered_at: datetime
    request_status: str
    request_version: int


class ReceiptOut(BaseModel):
    """收货单（读接口的模型）。

    `fully_received` **不是列**，是"每一行都收齐"的判定结果
    （`domain/fulfillment.is_fully_received`）—— 它在协调者里算。
    缺省 `False`：拿不到判定时宁可显示"没收齐"（保守方向），
    也不要默认成"已收齐"而让下游以为可以付款了。
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    request_id: uuid.UUID
    order_id: uuid.UUID
    receiver_id: uuid.UUID
    status: str
    note: str | None
    received_at: datetime
    fully_received: bool = False


class OrderDetailOut(BaseModel):
    """订单详情。`receipt` 为 null 表示**还没收货**（不是错误）。"""

    order: OrderOut
    lines: list[OrderLineOut]
    receipt: ReceiptOut | None = None


class PlaceOrderOut(BaseModel):
    order: OrderOut
    lines: list[OrderLineOut]


class ReceiveIn(BaseModel):
    """收货登记。

    `accepted` 的键是**订单明细 id**（字符串）→ 实收数量。
    **没出现的行按 0 处理**（整单收货要求每一行都有个结论）。
    """

    accepted: dict[str, int]
    note: str | None = None
    version: int


class ReceiveOut(BaseModel):
    order: OrderOut
    receipt: ReceiptOut


class QueueOut(BaseModel):
    items: list[RequestSummaryOut]
    total: int


def _summary(request: Any) -> RequestSummaryOut:
    return RequestSummaryOut(
        id=request.id,
        code=request.code,
        title=request.title,
        amount_total_fen=int(request.amount_total),
        status=request.status,
        version=request.version,
    )


def _order_out(order: Any, request: Any) -> OrderOut:
    """订单 → 响应。**两个父对象**：订单自己的列 + 申请的状态与版本。

    申请的版本是乐观锁要用的（下一次写操作用它），状态是给人看的
    （"这张单子进到哪一步了"）—— 都在响应里一次给全，省掉前端一次往返。
    """
    return OrderOut(
        id=order.id,
        request_id=order.request_id,
        order_no=order.order_no,
        supplier_id=order.supplier_id,
        amount_fen=int(order.amount),
        status=order.status,
        ordered_at=order.ordered_at,
        request_status=request.status,
        request_version=request.version,
    )


# ------------------------------------------------------------------ 待收货队列


@router.get(
    "/api/v1/receipts/queue",
    response_model=QueueOut,
    summary="待收货队列（状态 = ORDERED）",
    dependencies=[Depends(require_roles(ROLE_RECEIVER, ROLE_PROCUREMENT))],
)
async def list_receipt_queue(
    user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> QueueOut:
    """已下单、等着验收的单子。

    收货人看得到（这是他的工作台），采购也看得到（他要跟进到货）——
    两个角色都给，是因为"到货了没有"是双方共同关心的事实。
    """
    rows, total = await fulfillment_coordinator(session).list_receipt_queue(
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
        limit=limit,
        offset=offset,
    )
    return QueueOut(items=[_summary(row) for row in rows], total=total)


# ------------------------------------------------------------------ 订单


class PlaceOrderIn(BaseModel):
    """下单：只需要申请当前的 version（乐观锁）。

    金额、供应商、明细**都不接受客户端传值** —— 它们全部来自定标结果与申请明细，
    由后端算。接受总额就等于把"订单金额"这个基准交给客户端。
    """

    version: int


@router.post(
    "/api/v1/requests/{request_id}/orders",
    response_model=PlaceOrderOut,
    status_code=201,
    summary="由定标结果生成采购订单（幂等）",
    dependencies=[Depends(require_roles(ROLE_PROCUREMENT))],
    responses={
        409: {"description": "申请不在 AWARDED / 已生成过订单 / 版本过期"},
        422: {"description": "明细非法（数量或单价非正、没有明细）"},
    },
)
async def place_order(
    request_id: uuid.UUID,
    payload: PlaceOrderIn,
    user: CurrentUser,
    session: SessionDep,
    key: Annotated[str, Depends(idempotency_key)],
) -> PlaceOrderOut:
    """下单。

    明细行单价取**定标成交单价**（询价场景），所以订单金额通常小于申请金额 ——
    这是口径，不是 bug（差额由预算的"实付小于预留则显式释放"处理）。
    """
    body = await fulfillment_coordinator(session).place_order_idempotent(
        request_id=request_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
        expected_version=payload.version,
        idempotency_key=key,
    )
    return PlaceOrderOut.model_validate(body)


@router.get(
    "/api/v1/requests/{request_id}/orders",
    response_model=OrderDetailOut | None,
    summary="按申请查订单（没有订单返回 null，不是 404）",
)
async def get_order_by_request(
    request_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Any:
    """按申请查订单。

    **没有订单返回 `null`**：详情页要能一屏显示"申请 + 订单（若有）+ 收货（若有）"，
    让"还没下单"变成一个正常的空值，而不是一个要特判的错误码。
    """
    body = await fulfillment_coordinator(session).order_of_request(
        request_id=request_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
    )
    if body is None:
        return None
    return OrderDetailOut.model_validate(body)


@router.get(
    "/api/v1/orders/{order_id}",
    response_model=OrderDetailOut,
    summary="订单详情（订单 + 明细 + 收货单）",
    responses={404: {"description": "订单不存在，或它所属的申请不在可见范围内"}},
)
async def get_order(
    order_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> OrderDetailOut:
    body = await fulfillment_coordinator(session).get_order(
        order_id=order_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
    )
    return OrderDetailOut.model_validate(body)


# ------------------------------------------------------------------ 收货


@router.post(
    "/api/v1/requests/{request_id}/receipts",
    response_model=ReceiveOut,
    status_code=201,
    summary="收货登记（逐行填实收数量；整单收货）",
    dependencies=[Depends(require_roles(ROLE_RECEIVER))],
    responses={
        409: {"description": "申请不在 ORDERED / 已登记过收货 / 版本过期"},
        422: {"description": "超收、负数、或明细不属于这张订单（一次列出全部问题）"},
    },
)
async def receive_goods(
    request_id: uuid.UUID,
    payload: ReceiveIn,
    user: CurrentUser,
    session: SessionDep,
    key: Annotated[str, Depends(idempotency_key)],
) -> ReceiveOut:
    """收货登记。

    **收货人角色**（`RECEIVER`）：内控要求采购、验收、记录三方分离 ——
    收货不能由采购自己做。

    收齐（每一行都收满）才把主单推到 `RECEIVED`；没收齐就停在 `ORDERED`
    （第一版没有"部分收货"这个状态）。
    """
    body = await fulfillment_coordinator(session).receive_goods_idempotent(
        request_id=request_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
        accepted=payload.accepted,
        note=payload.note,
        expected_version=payload.version,
        idempotency_key=key,
    )
    return ReceiveOut.model_validate(body)
