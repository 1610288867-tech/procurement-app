"""采购申请接口（M5）。

边界层只做三件事：校验输入、装配依赖、把记录转成响应模型。
业务规则在 `domain/`，事务边界在 `application/coordinators/`。

============================ 金额字段一律带 _fen ============================
响应里叫 `amount_total_fen` / `unit_price_fen` / `amount_fen` ——
名字里带单位，调用方就没机会把「分」当成「元」（差 100 倍，且不会报错）。
界面上的「元」只在 `frontend/src/lib/money.ts` 里换算。

============================ 幂等键是必填头 ============================
提交与撤回必须带 `Idempotency-Key`：它们是"重复执行会改变结果"的动作。
顺带白捡一道 CSRF 防线 —— 浏览器不会跨站自动带上自定义头。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.application.coordinators.purchase import PurchaseCoordinator
from app.application.idempotency import IdempotencyCoordinator
from app.application.ports import PurchaseRequestView
from app.boundaries.deps import CurrentUser, SessionDep
from app.boundaries.http.approvals import approval_coordinator
from app.database import SessionFactory
from app.errors import BusinessError
from app.infrastructure.repositories.catalog_repo import CatalogRepository
from app.infrastructure.repositories.event_repo import RequestEventRepository
from app.infrastructure.repositories.purchase_repo import PurchaseRepository

router = APIRouter(prefix="/api/v1/requests", tags=["采购申请"])

# 幂等键的长度上限。超长的键既没有意义，也会让唯一索引变得很宽。
MAX_IDEMPOTENCY_KEY_LENGTH = 128


# ------------------------------------------------------------------ 依赖装配


def purchase_coordinator(session: SessionDep) -> PurchaseCoordinator:
    """组装一次用例所需的全部依赖。

    - `IdempotencyCoordinator` 拿的是**会话工厂 + 当前会话**：
      它需要在独立事务里提交占位行，所以必须能自己开会话。
    - `ApprovalCoordinator`（M6）是**提交时**用的：提交这个事务里要按品类策略
      建审批流与全部任务。两边共用同一个 `session`，所以"状态 + 事件 + 审批链"
      落在同一个事务里 —— 建链失败（例如某级缺人）时状态也会一起回滚。
    """
    return PurchaseCoordinator(
        requests=PurchaseRepository(session),
        events=RequestEventRepository(session),
        catalog=CatalogRepository(session),
        idempotency=IdempotencyCoordinator(
            claim_session_factory=SessionFactory, session=session
        ),
        uow=session,
        approval=approval_coordinator(session),
    )


async def idempotency_key(
    x_idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key")
    ] = None,
) -> str:
    """取幂等键。缺失或超长一律 422，且提示里说清该带什么头。

    这里**不做"没有键就放行"**：那样这个接口就只是"支持"幂等，
    而不是"要求"幂等，客户端重试时该重复的照样重复。
    """
    if not x_idempotency_key:
        raise BusinessError(
            "缺少 Idempotency-Key 请求头：提交/撤回必须带一把客户端生成的唯一键",
            status_code=422,
            code="IDEMPOTENCY_KEY_REQUIRED",
        )
    if len(x_idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise BusinessError(
            f"Idempotency-Key 过长（上限 {MAX_IDEMPOTENCY_KEY_LENGTH} 字符）",
            status_code=422,
            code="IDEMPOTENCY_KEY_TOO_LONG",
        )
    return x_idempotency_key


IdempotencyKey = Annotated[str, Depends(idempotency_key)]


# ------------------------------------------------------------------ 入参


class RequestCreateIn(BaseModel):
    category_id: uuid.UUID
    title: str = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=2000)
    urgency: Literal["NORMAL", "URGENT"] = "NORMAL"
    form_data: dict[str, Any] = Field(default_factory=dict)


class RequestPatchIn(BaseModel):
    """改草稿。

    **刻意没有 `amount_total` 这个字段**：金额是后端按明细算出来的派生值，
    这个接口根本不接受它 —— 那就没有"信任客户端金额"的机会。
    `version` 必填，用来做乐观锁。
    """

    version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=2000)
    urgency: Literal["NORMAL", "URGENT"] | None = None
    category_id: uuid.UUID | None = None
    form_data: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_derived_fields(cls, data: Any) -> Any:
        """显式拒绝"算出来的字段"。

        只靠"模型里没有这个字段"是不够的：Pydantic 默认**忽略**多余字段，
        于是 `{"version":1,"amount_total":1}` 会被静默当成"什么都没改"，
        报一句"没有需要修改的字段"—— 用户完全看不出自己碰了不该碰的东西。

        所以这里主动点名，把"哪些字段是派生的"直接告诉他。
        """
        if isinstance(data, dict) and "amount_total" in data:
            raise ValueError(
                "amount_total 是后端按明细算出来的派生值，不能直接修改；"
                "请改明细（PUT /items），金额会自动重算"
            )
        return data


class ItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    spec: str | None = Field(default=None, max_length=500)
    quantity: int = Field(gt=0)
    # 名字带 _fen：金额一律整数「分」。写成浮点会被 Pydantic 直接拦下（int 不接受 float）
    unit_price_fen: int = Field(gt=0)


class ItemsReplaceIn(BaseModel):
    version: int = Field(ge=1)
    items: list[ItemIn] = Field(default_factory=list)


class VersionIn(BaseModel):
    version: int = Field(ge=1)


# ------------------------------------------------------------------ 响应


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line_no: int
    name: str
    spec: str | None = None
    quantity: int
    unit_price_fen: int = Field(validation_alias="unit_price")
    amount_fen: int = Field(validation_alias="amount")


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_type: str
    from_status: str | None = None
    to_status: str | None = None
    actor_id: uuid.UUID
    payload: dict = Field(default_factory=dict)
    created_at: datetime


class RequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    requester_id: uuid.UUID
    department_id: uuid.UUID
    category_id: uuid.UUID
    title: str
    reason: str | None = None
    amount_total_fen: int = Field(validation_alias="amount_total")
    urgency: str
    status: str
    version: int
    form_data: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None = None
    closed_at: datetime | None = None


class RequestDetailOut(BaseModel):
    request: RequestOut
    items: list[ItemOut]
    timeline: list[EventOut]


class RequestPageOut(BaseModel):
    items: list[RequestOut]
    total: int


class TimelineOut(BaseModel):
    request_id: uuid.UUID
    events: list[EventOut]


# ------------------------------------------------------------------ 序列化


def _request_out(record: Any) -> RequestOut:
    """兼容两种来源：ORM 记录与幂等重放回来的 dict。

    重放走的是"上次存下来的响应体"，它是 dict 而不是 ORM 对象 ——
    如果这里只认 ORM，重放请求会 500，而那正是幂等最该工作的时候。
    Pydantic 的 `from_attributes` 两种都认，所以这里不需要分支。
    """
    return RequestOut.model_validate(record)


def _detail_out(
    record: PurchaseRequestView,
    items: list[Any],
    timeline: list[Any],
) -> RequestDetailOut:
    return RequestDetailOut(
        request=_request_out(record),
        items=[ItemOut.model_validate(item) for item in items],
        timeline=[EventOut.model_validate(event) for event in timeline],
    )


# ------------------------------------------------------------------ 接口


@router.post(
    "",
    response_model=RequestOut,
    status_code=201,
    summary="创建草稿",
    responses={422: {"description": "部门缺失 / 品类不存在 / 标题为空"}},
)
async def create_request(
    payload: RequestCreateIn, user: CurrentUser, session: SessionDep
) -> RequestOut:
    """建草稿。

    部门取**登录用户所属部门**，不接受客户端指定 ——
    否则申请人可以挑一个"审批更松"的部门来提自己的单子。
    """
    record = await purchase_coordinator(session).create_draft(
        actor_id=user.id,
        department_id=user.department_id,
        category_id=payload.category_id,
        title=payload.title,
        reason=payload.reason,
        urgency=payload.urgency,
        form_data=payload.form_data,
    )
    return _request_out(record)


@router.get("", response_model=RequestPageOut, summary="申请列表（按状态筛选、分页）")
async def list_requests(
    user: CurrentUser,
    session: SessionDep,
    status: Annotated[
        str | None,
        Query(description="按状态筛选：DRAFT / IN_APPROVAL / DISCARDED / WITHDRAWN"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RequestPageOut:
    """列表。

    返回哪些行由**可见范围**决定（写进 WHERE）：
    REQUESTER 只看自己的，部门角色看本部门含子部门，采购/财务/GM/管理员看全公司。
    """
    rows, total = await purchase_coordinator(session).list_requests(
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return RequestPageOut(items=[_request_out(row) for row in rows], total=total)


@router.get(
    "/{request_id}",
    response_model=RequestDetailOut,
    summary="申请详情（含明细与时间线）",
    responses={404: {"description": "不存在，或不在当前用户的可见范围内"}},
)
async def get_request(
    request_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> RequestDetailOut:
    record, items, timeline = await purchase_coordinator(session).get_request_detail(
        request_id=request_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
    )
    return _detail_out(record, list(items), list(timeline))


@router.patch(
    "/{request_id}",
    response_model=RequestOut,
    summary="修改草稿（乐观锁）",
    responses={
        403: {"description": "不是自己的申请"},
        404: {"description": "不存在或不在可见范围内"},
        409: {"description": "版本过期，或已经不是草稿"},
    },
)
async def patch_request(
    request_id: uuid.UUID,
    payload: RequestPatchIn,
    user: CurrentUser,
    session: SessionDep,
) -> RequestOut:
    changes = payload.model_dump(exclude_unset=True, exclude={"version"})
    record = await purchase_coordinator(session).patch_draft(
        request_id=request_id,
        actor_id=user.id,
        department_id=user.department_id,
        roles=user.roles,
        expected_version=payload.version,
        changes=changes,
    )
    return _request_out(record)


@router.put(
    "/{request_id}/items",
    response_model=RequestOut,
    summary="覆盖式保存明细（金额由后端重算）",
    responses={
        404: {"description": "不存在或不在可见范围内"},
        409: {"description": "版本过期，或已经不是草稿"},
        422: {"description": "明细不合法（提示里带行号）"},
    },
)
async def replace_items(
    request_id: uuid.UUID,
    payload: ItemsReplaceIn,
    user: CurrentUser,
    session: SessionDep,
) -> RequestOut:
    record = await purchase_coordinator(session).replace_items(
        request_id=request_id,
        actor_id=user.id,
        department_id=user.department_id,
        roles=user.roles,
        expected_version=payload.version,
        raw_items=[item.model_dump() for item in payload.items],
    )
    return _request_out(record)


@router.post(
    "/{request_id}/submit",
    response_model=RequestOut,
    summary="提交（幂等）",
    responses={
        409: {"description": "版本过期 / 已在审批中 / 同键不同载荷"},
        422: {"description": "缺少幂等键，或申请不满足提交条件"},
    },
)
async def submit_request(
    request_id: uuid.UUID,
    payload: VersionIn,
    key: IdempotencyKey,
    user: CurrentUser,
    session: SessionDep,
) -> RequestOut:
    """提交。

    带 `Idempotency-Key`：同一个键重复提交只会真正执行一次，
    之后每次重放都返回**第一次的响应**（不会推进两次状态）。
    """
    result = await purchase_coordinator(session).submit(
        request_id=request_id,
        actor_id=user.id,
        department_id=user.department_id,
        roles=user.roles,
        expected_version=payload.version,
        idempotency_key=key,
    )
    return _request_out(result)


@router.post(
    "/{request_id}/withdraw",
    response_model=RequestOut,
    summary="撤回（幂等）",
    responses={409: {"description": "版本过期 / 状态不允许撤回 / 同键不同载荷"}},
)
async def withdraw_request(
    request_id: uuid.UUID,
    payload: VersionIn,
    key: IdempotencyKey,
    user: CurrentUser,
    session: SessionDep,
) -> RequestOut:
    result = await purchase_coordinator(session).withdraw(
        request_id=request_id,
        actor_id=user.id,
        department_id=user.department_id,
        roles=user.roles,
        expected_version=payload.version,
        idempotency_key=key,
    )
    return _request_out(result)


@router.post(
    "/{request_id}/discard",
    response_model=RequestOut,
    summary="丢弃草稿",
    responses={409: {"description": "版本过期，或已经不是草稿"}},
)
async def discard_request(
    request_id: uuid.UUID,
    payload: VersionIn,
    user: CurrentUser,
    session: SessionDep,
) -> RequestOut:
    record = await purchase_coordinator(session).discard(
        request_id=request_id,
        actor_id=user.id,
        department_id=user.department_id,
        roles=user.roles,
        expected_version=payload.version,
    )
    return _request_out(record)


@router.get(
    "/{request_id}/timeline",
    response_model=TimelineOut,
    summary="事件时间线（只追加，按时间正序）",
)
async def get_timeline(
    request_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> TimelineOut:
    record, _, timeline = await purchase_coordinator(session).get_request_detail(
        request_id=request_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
    )
    return TimelineOut(
        request_id=record.id,
        events=[EventOut.model_validate(event) for event in timeline],
    )
