"""发票与三单匹配接口（M10 / 8.3）。

四条路由：

    POST /api/v1/requests/{id}/invoices     登记发票（发票号 + 金额 + 税额 + 供应商 + 附件）
    GET  /api/v1/requests/{id}/invoices     这张申请的发票列表
    GET  /api/v1/payments/queue             待付款队列（状态 = INVOICED）
    GET  /api/v1/requests/{id}/match        三单匹配预检结果（**只读，不改任何状态**）

============================ 为什么预检是只读的 ============================
文档里"匹配什么时候跑"原来有两处说法不一致（一处说创建付款单时校验、
一处说执行付款时触发）。实施计划 2425-2433 行把它定死了：

| 时机 | 做什么 |
| --- | --- |
| 创建付款单（`PENDING`，8.4） | 只校验"主单 = `RECEIVED`/`INVOICED`、且至少有一张发票" |
| **执行付款（`pay`，8.4）** | **在这里跑三单匹配**，通过才动预算 |
| 预检接口（本条） | 只读展示，给前端渲染对比页，**不做任何状态变更** |

**匹配跑在"执行付款"那一步**，因为那是唯一真正动钱的时刻，
也是唯一能保证"校验通过后没有再改单据"的时机。

============================ 角色口径 ============================
| 动作 | 角色 | 依据 |
| --- | --- | --- |
| 登记发票 | **`FINANCE`** | 发票是付款的依据（内控：付款须审核预算 / 合同 / 单据 / 审批程序）。让"要用它的人"登记，责任落在同一处 |
| 看发票 / 看匹配 | `FINANCE`、`PROCUREMENT` | 采购要跟进到票情况、财务要付款 —— 两边都得看得到 |
| 待付款队列 | **`FINANCE`** | 财务的工作台 |

不改申请状态的角色一律不限制到具体角色，只按**数据可见范围**判
（申请人 / 审批人也该看得到自己那张单子开了多少票）。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.application.coordinators.settlement import (
    SettlementCoordinator,
    build_settlement_coordinator,
    invoice_payload,
    payment_payload,
)
from app.boundaries.deps import CurrentUser, SessionDep, require_roles
from app.boundaries.http.requests import idempotency_key
from app.database import SessionFactory
from app.domain.permissions import ROLE_FINANCE, ROLE_PROCUREMENT

router = APIRouter(tags=["发票与三单匹配"])


def settlement_coordinator(session: SessionDep) -> SettlementCoordinator:
    """组装发票/匹配用例的依赖（装配本身在 `coordinators/settlement`）。"""
    return build_settlement_coordinator(session, claim_session_factory=SessionFactory)


# ------------------------------------------------------------------ 模型


class RequestSummaryOut(BaseModel):
    id: uuid.UUID
    code: str
    title: str
    amount_total_fen: int
    status: str
    version: int


class InvoiceOut(BaseModel):
    """发票。

    `amount_fen` 是**含税总额** —— 三单匹配比的就是它。
    `tax_amount_fen` 只记录、不参与匹配（见 `records/invoice.py` 的说明）。

    `status`：`ACTIVE`（参与匹配）/ `VOIDED`（作废：**不参与求和**，且号可重新登记）。
    列表里两者都会出现 —— 作废票必须让人看得见，否则用户会以为从没登记过。
    """

    id: uuid.UUID
    request_id: uuid.UUID
    supplier_id: uuid.UUID
    invoice_no: str
    status: str
    amount_fen: int
    tax_amount_fen: int
    issued_on: date | None
    attachment_id: uuid.UUID | None
    created_at: datetime


class InvoiceAddedOut(BaseModel):
    """登记发票的响应。

    带上 `invoice_total_fen` / `invoice_count`：前端登记完要立刻显示
    "这张单子一共开了多少票、合计多少" —— 而**匹配比的是合计**，
    只回单张的金额看不出全貌。
    """

    invoice: InvoiceOut
    invoice_total_fen: int
    invoice_count: int
    request_status: str
    request_version: int


class InvoiceVoidedOut(BaseModel):
    """作废发票的响应。

    与 `InvoiceAddedOut` 同一个形状（合计 + 张数）：作废之后界面要立刻显示
    "有效发票合计少了多少"，而合计是**匹配的入参** ——
    让调用方自己减一次，就是把"有效/作废"的判据抄到前端去。
    """

    invoice: InvoiceOut
    invoice_total_fen: int
    invoice_count: int
    request_status: str
    request_version: int


class VoidInvoiceIn(BaseModel):
    """作废发票。只收一个原因 —— 金额、号、状态都不接受客户端传值。"""

    reason: str | None = Field(default=None, max_length=200)


class AddInvoiceIn(BaseModel):
    """登记发票。

    ⚠️ **不接受客户端传"这不含税"之类的口径**：`amount_fen` 就是发票上的
    价税合计（含税总额），因为订单金额也是含税总额。
    两边口径一致，比出来的差异才解释得通。
    """

    supplier_id: uuid.UUID
    invoice_no: str = Field(min_length=1, max_length=64)
    #: 含税总额（分）
    amount_fen: int
    #: 税额（分）。只记录、不参与匹配
    tax_amount_fen: int = 0
    issued_on: date | None = None
    attachment_id: uuid.UUID | None = None
    version: int


class MatchIssueOut(BaseModel):
    """一条不匹配项。前端按 `code` 决定高亮哪一列，按 `expected`/`actual` 显示差异。"""

    code: str
    message: str
    expected: int | None = None
    actual: int | None = None


class MatchOrderOut(BaseModel):
    id: uuid.UUID
    order_no: str
    supplier_id: uuid.UUID
    amount_fen: int
    ordered_qty: int
    line_count: int


class MatchReceiptOut(BaseModel):
    accepted_qty: int
    receipt_exists: bool
    receipt_id: uuid.UUID | None = None


class MatchRequestOut(BaseModel):
    id: uuid.UUID
    code: str
    title: str
    status: str
    version: int
    #: 申请金额。**它不是匹配基准** —— 放在这里是为了让对比页能显示"申请 vs 订单"的差额
    #: （询价压价之后订单通常更小，差额由预算的显式释放处理）
    request_amount_fen: int


class MatchOut(BaseModel):
    """三单匹配预检结果（**只读**）。

    `passed` 是 `issues` 为空的便捷布尔值；**判定请以 `issues` 为准** ——
    问题列表能告诉你"哪里不对、差多少"，这是它存在的全部意义。
    """

    request: MatchRequestOut
    order: MatchOrderOut | None = None
    receipt: MatchReceiptOut
    invoices: list[InvoiceOut]
    invoice_total_fen: int
    invoice_count: int
    all_suppliers_match: bool
    passed: bool
    issues: list[MatchIssueOut]


class QueueOut(BaseModel):
    items: list[RequestSummaryOut]
    total: int


# ------------------------------------------------------------------ 8.4 付款与归档


class PaymentOut(BaseModel):
    """付款单。

    `amount_fen` 是**实付**（= 有效发票金额之和），可能不等于订单金额 ——
    容差之内就算匹配通过，差额由预算的"实付小于预留则显式释放"处理。

    `attempt_no`：同一张申请第几次尝试。`FAILED` 的行会留在列表里，
    所以界面上要能说"这是第 2 次尝试"，否则多行会让人以为系统重复建单了。
    """

    id: uuid.UUID
    request_id: uuid.UUID
    amount_fen: int
    status: str
    attempt_no: int
    failure_reason: str | None
    created_by: uuid.UUID
    created_at: datetime
    paid_at: datetime | None


class PaymentCreatedOut(BaseModel):
    """创建付款单 / 标记失败的响应（状态仍是 `INVOICED` —— 钱还没动）。

    两者**共用同一个形状**：界面上的"付款单卡片"两种情况要显示同样的东西
    （金额、状态、尝试号、有效发票合计），形状不一致就会逼前端写两套渲染。
    """

    payment: PaymentOut
    invoice_total_fen: int
    invoice_count: int
    request_status: str
    request_version: int


class PayIn(BaseModel):
    """执行付款。只收一个 `version`（乐观锁）—— 金额不接受客户端传值。"""

    version: int


class PayOut(BaseModel):
    """执行付款的响应。

    `amount_fen` / `released_fen` 显式回给界面：财务点完"付款"要立刻看到
    "付了多少、释放了多少"，而不是自己去算（那是两笔流水，**差一分都说明有 bug**）。
    """

    payment: PaymentOut
    request_status: str
    request_version: int
    amount_fen: int
    released_fen: int


class FailPaymentIn(BaseModel):
    reason: str = Field(min_length=1, max_length=200)


class CloseOut(BaseModel):
    request_id: uuid.UUID
    status: str
    version: int


def _summary(request: Any) -> RequestSummaryOut:
    return RequestSummaryOut(
        id=request.id,
        code=request.code,
        title=request.title,
        amount_total_fen=int(request.amount_total),
        status=request.status,
        version=request.version,
    )


# ------------------------------------------------------------------ 待付款队列


@router.get(
    "/api/v1/payments/queue",
    response_model=QueueOut,
    summary="待付款队列（状态 = INVOICED）",
    dependencies=[Depends(require_roles(ROLE_FINANCE))],
)
async def list_payment_queue(
    user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> QueueOut:
    """收过货、有发票、等着付的单子 —— 财务的工作台。

    ⚠️ `INVOICED` 的含义是"**可以进入付款流程**"，**不是**"付款条件已全部满足"：
    三单匹配还没跑（它跑在"执行付款"那一刻，8.4）。
    所以这个队列是**待办清单**，不是"可以付款的清单"。
    """
    rows, total = await settlement_coordinator(session).list_payment_queue(
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
        limit=limit,
        offset=offset,
    )
    return QueueOut(items=[_summary(row) for row in rows], total=total)


# ------------------------------------------------------------------ 发票


@router.post(
    "/api/v1/requests/{request_id}/invoices",
    response_model=InvoiceAddedOut,
    status_code=201,
    summary="登记发票（幂等）",
    dependencies=[Depends(require_roles(ROLE_FINANCE))],
    responses={
        409: {"description": "申请不在 RECEIVED / INVOICED，或发票号重复，或版本过期"},
        422: {"description": "供应商与订单不一致 / 金额或税额非法"},
    },
)
async def add_invoice(
    request_id: uuid.UUID,
    payload: AddInvoiceIn,
    user: CurrentUser,
    session: SessionDep,
    key: Annotated[str, Depends(idempotency_key)],
) -> InvoiceAddedOut:
    """登记一张发票。

    一笔申请可以登记**多张**发票（分批开票很常见），匹配时金额**求和**。
    发票供应商必须与订单供应商一致 —— 不对的票在入口就挡掉，
    而不是等付款那一刻才发现（那时已经隔了几天，经手人的上下文也没了）。
    """
    body = await settlement_coordinator(session).add_invoice_idempotent(
        request_id=request_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
        supplier_id=payload.supplier_id,
        invoice_no=payload.invoice_no,
        amount_fen=payload.amount_fen,
        tax_amount_fen=payload.tax_amount_fen,
        issued_on=payload.issued_on,
        attachment_id=payload.attachment_id,
        expected_version=payload.version,
        idempotency_key=key,
    )
    return InvoiceAddedOut.model_validate(body)


@router.get(
    "/api/v1/requests/{request_id}/invoices",
    response_model=list[InvoiceOut],
    summary="这张申请的发票列表",
)
async def list_invoices(
    request_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> list[InvoiceOut]:
    """发票列表（按登记时间正序）。**没登记过发票返回空数组**，不是 404。"""
    records = await settlement_coordinator(session).list_invoices(
        request_id=request_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
    )
    return [InvoiceOut.model_validate(invoice_payload(record)) for record in records]


@router.post(
    "/api/v1/invoices/{invoice_id}/void",
    response_model=InvoiceVoidedOut,
    summary="作废发票（不是删行：保留痕迹，且发票号可重新登记）",
    dependencies=[Depends(require_roles(ROLE_FINANCE))],
    responses={
        404: {"description": "发票不存在，或它所属的申请不在可见范围内"},
        409: {"description": "已经作废过，或这张单子已经付过款了"},
    },
)
async def void_invoice(
    invoice_id: uuid.UUID,
    payload: VoidInvoiceIn,
    user: CurrentUser,
    session: SessionDep,
) -> InvoiceVoidedOut:
    """作废一张发票。

    **作废而不是删除**：删行会丢掉"这张票曾经登记过、后来作废了、谁作废的"，
    而那是审计要看的事实。作废之后这张票**不再参与三单匹配的金额求和**，
    它的发票号也**可以重新登记**（按正确的号重录是最常见的纠错路径）。

    ⚠️ 付过款的单子**不能**作废发票：钱已经按它付过了，作废会让
    "已付金额 vs 有效发票合计"在账上对不上，而系统没有退款流程。
    """
    body = await settlement_coordinator(session).void_invoice(
        invoice_id=invoice_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
        reason=payload.reason,
    )
    return InvoiceVoidedOut.model_validate(body)


# ------------------------------------------------------------------ 三单匹配预检


@router.get(
    "/api/v1/requests/{request_id}/match",
    response_model=MatchOut,
    summary="三单匹配预检结果（只读）",
    # 财务与采购都能看（采购要跟进到票情况、财务要付款）。
    # 这是**读**接口的角色闸门；写接口（登记发票）另有一道。
    dependencies=[Depends(require_roles(ROLE_FINANCE, ROLE_PROCUREMENT))],
    responses={404: {"description": "申请不存在，或不在可见范围内"}},
)
async def match_precheck(
    request_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> MatchOut:
    """订单 / 收货 / 发票三列对比 + 问题列表。**不做任何状态变更。**

    真正的拦截点在"执行付款"（8.4）—— 那是唯一真正动钱、
    也是唯一能保证"校验通过后没有再改单据"的时机。
    """
    body = await settlement_coordinator(session).match_precheck(
        request_id=request_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
    )
    return MatchOut.model_validate(body)


# ------------------------------------------------------------------ 付款（8.4）


@router.post(
    "/api/v1/requests/{request_id}/payments",
    response_model=PaymentCreatedOut,
    status_code=201,
    summary="创建付款单（PENDING，幂等）",
    dependencies=[Depends(require_roles(ROLE_FINANCE))],
    responses={
        409: {"description": "申请不在 INVOICED，或已经有一张有效付款单"},
        422: {"description": "没有有效发票（可能都被作废了）"},
    },
)
async def create_payment(
    request_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    key: Annotated[str, Depends(idempotency_key)],
) -> PaymentCreatedOut:
    """创建付款单。

    **这里只校验"已收票 + 至少一张有效发票"**（实施计划 2429 行）——
    **不跑三单匹配**。匹配跑在"执行付款"那一刻：那是唯一真正动钱、
    也是唯一能保证"校验通过后没有再改单据"的时机。
    在这里跑一遍的话，用户看到"匹配通过"会以为可以付了，而中间发票可能被作废。
    """
    body = await settlement_coordinator(session).create_payment_idempotent(
        request_id=request_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
        idempotency_key=key,
    )
    return PaymentCreatedOut.model_validate(body)


@router.post(
    "/api/v1/payments/{payment_id}/pay",
    response_model=PayOut,
    summary="执行付款（**触发三单匹配**；这个系统里唯一真正动钱的操作）",
    dependencies=[Depends(require_roles(ROLE_FINANCE))],
    responses={
        409: {"description": "付款单已被处理 / 申请不在 INVOICED / 版本过期"},
        422: {"description": "三单匹配未通过（返回问题列表说明差异）"},
    },
)
async def pay(
    payment_id: uuid.UUID,
    payload: PayIn,
    user: CurrentUser,
    session: SessionDep,
    key: Annotated[str, Depends(idempotency_key)],
) -> PayOut:
    """执行付款。

    顺序（**一步不省，顺序也不许调**）：

        ① 主单 = INVOICED  ② 三单匹配  ③ 条件更新 `PENDING → PAID`（抢付款权）
        ④ 预算结算（`SPEND` 实付 + `RELEASE` 差额）  ⑤ 主单 → PAID  ⑥ 写事件

    **③ 必须在 ④ 之前**：先抢到付款权的一方才去动预算，抢输的一方在动钱之前就退出。
    两个财务同时点"付款"，只有一个成功，`amount_spent` 只增加一次。
    """
    body = await settlement_coordinator(session).pay_idempotent(
        payment_id=payment_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
        expected_version=payload.version,
        idempotency_key=key,
    )
    return PayOut.model_validate(body)


@router.post(
    "/api/v1/payments/{payment_id}/fail",
    response_model=PaymentCreatedOut,
    summary="标记付款失败（保留痕迹、允许重新发起）",
    dependencies=[Depends(require_roles(ROLE_FINANCE))],
    responses={409: {"description": "付款单不是 PENDING，不能标记失败"}},
)
async def fail_payment(
    payment_id: uuid.UUID,
    payload: FailPaymentIn,
    user: CurrentUser,
    session: SessionDep,
) -> PaymentCreatedOut:
    """把付款单标成 `FAILED`。

    用于**外部付款通道失败**（银行退回、账号不对），**不是**"三单匹配没过"——
    那种情况付款单不该被推进，而是让用户去改发票。

    失败之后这张单子不再占用"有效付款单"的位置（部分唯一索引的条件是
    `status <> 'FAILED'`），所以可以重新发起 —— 这正是那处设计要解决的问题。
    """
    record = await settlement_coordinator(session).mark_payment_failed(
        payment_id=payment_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
        reason=payload.reason,
    )
    return PaymentCreatedOut.model_validate(record)


@router.get(
    "/api/v1/requests/{request_id}/payments",
    response_model=list[PaymentOut],
    summary="这张申请的付款单（含失败的尝试）",
)
async def list_payments(
    request_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> list[PaymentOut]:
    """付款单列表（按尝试次序）。**没创建过返回空数组**，不是 404。"""
    records = await settlement_coordinator(session).list_payments(
        request_id=request_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
    )
    return [PaymentOut.model_validate(payment_payload(record)) for record in records]


@router.get(
    "/api/v1/payments/{payment_id}",
    response_model=PaymentOut,
    summary="付款单详情",
    responses={404: {"description": "付款单不存在，或它所属的申请不在可见范围内"}},
)
async def get_payment(
    payment_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> PaymentOut:
    record = await settlement_coordinator(session).get_payment(
        payment_id=payment_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
    )
    return PaymentOut.model_validate(payment_payload(record))


@router.post(
    "/api/v1/requests/{request_id}/close",
    response_model=CloseOut,
    summary="归档（PAID → CLOSED，终态）",
    dependencies=[Depends(require_roles(ROLE_FINANCE))],
    responses={409: {"description": "申请不在 PAID / 版本过期"}},
)
async def close_request(
    request_id: uuid.UUID,
    payload: PayIn,
    user: CurrentUser,
    session: SessionDep,
) -> CloseOut:
    """归档。

    归档之后这张单子不再出现在任何待办队列里（队列都按状态过滤），
    但仍然可查 —— 归档是"这件事办完了"，不是删除。

    **为什么不让"付完自动归档"**：付款之后可能还有发票原件归档、合同盖章这类
    线下动作。自动归档会把"钱付了"和"事办完了"混成同一个时刻。
    """
    body = await settlement_coordinator(session).close(
        request_id=request_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
        expected_version=payload.version,
    )
    return CloseOut.model_validate(body)
