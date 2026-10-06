"""发票与三单匹配的用例（M10 / 8.3）。

============================ 两个用例，两种性质 ============================
**登记发票**（`add_invoice`）是**写**：

    ① 申请在 `RECEIVED`（还没收货就不能收票 —— 状态机也只允许 `RECEIVED → INVOICED`）
    ② 发票供应商 = 订单供应商（收一张别家的票没有意义，登记下来只会让匹配必然失败）
    ③ 发票号在**同一张申请内**不重复（重复录入要让人看见，不能静默换号）
    ④ 条件更新推主单 `RECEIVED → INVOICED` + 写事件 + 提交

**三单匹配预检**（`match_precheck`）是**纯读**：

    它**不做任何状态变更**（实施计划 2431 行明确写了）。
    真正的拦截点在"执行付款"那一刻（8.4）—— 那是唯一真正动钱、
    也是唯一能保证"校验通过后没有再改单据"的时机。
    预检只是给前端渲染三列对比页用的。

============================ 为什么发票可以做多张 ============================
一笔申请只允许**一张**订单、**一次**收货，但发票可以**多张**：
现实里分批开票很常见，而拆单不常见。所以匹配时发票金额**求和**
（订单级粒度，实施计划 2420 行）。

这带来一个容易写错的地方：**多张发票的供应商一致性要"全部一致"**，
不是"看第一张"（领域设计 1372 行专门点了名）。判定在
`domain/settlement_policy.document_supplier_consistent` 里，只有一处实现。

============================ 为什么登记发票时就把"供应商不对"挡掉 ============================
三单匹配里本来就有这条规则，为什么还要在登记时挡一次？

    因为登记时挡掉**能给出更好的错误**（"这张票的供应商不是订单供应商"），
    而留到付款时才炸，用户已经以为票登记好了 —— 到付款那一刻才发现对不上，
    中间隔了几天，`created_by` 那个人的上下文也没了。

挡在入口 + 匹配时再判一次，两层不冲突：入口是为了体验，匹配是为了**动钱之前**必然检查。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.application.idempotency import IdempotencyCoordinator
from app.application.ports import UnitOfWork
from app.domain.enums import RequestStatus
from app.domain.events import (
    INVOICE_ADDED,
    INVOICE_VOIDED,
    PAYMENT_COMPLETED,
    PAYMENT_CREATED,
    PAYMENT_FAILED,
    REQUEST_CLOSED,
)
from app.domain.permissions import needs_department_subtree, resolve_request_scope
from app.domain.settlement_policy import (
    InvoiceError,
    can_void_invoice,
    document_supplier_consistent,
    precheck_three_way,
)
from app.errors import BusinessError, ConflictError, NotFoundError
from app.infrastructure.repositories.fulfillment_repo import FulfillmentRepository
from app.infrastructure.repositories.invoice_repo import InvoiceRepository

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

#: 幂等 scope：**端点标识**，写死在这里（与提交 / 撤回 / 定标 / 下单同一条约定）。
SCOPE_ADD_INVOICE = "POST:/api/v1/requests/{id}/invoices"
#: 8.4：创建付款单与执行付款各自的 scope。
#: **两者必须分开**：同一把 key 用在"建单"和"付款"上会串味 ——
#: 付款请求会拿到建单的响应体（形状还不同），而看起来一切正常。
SCOPE_CREATE_PAYMENT = "POST:/api/v1/requests/{id}/payments"
SCOPE_PAY = "POST:/api/v1/payments/{id}/pay"


def build_settlement_coordinator(
    session: Any, *, claim_session_factory: Any
) -> SettlementCoordinator:
    """按一次请求的会话装配发票 / 匹配 / 付款用例的依赖。

    与 `build_sourcing_coordinator` / `build_fulfillment_coordinator` 同一个理由：
    装配只写一份，边界层与测试共用。**没有配置参数** —— 容差是业务常量
    （`domain/settlement_policy`），不是制度参数，所以不需要配置项
    （与比价线不同：那条线各公司差别很大，2% 与 50 元是行业惯例）。
    """
    from app.infrastructure.repositories.budget_repo import BudgetRepository
    from app.infrastructure.repositories.catalog_repo import CatalogRepository
    from app.infrastructure.repositories.event_repo import RequestEventRepository
    from app.infrastructure.repositories.payment_repo import PaymentRepository
    from app.infrastructure.repositories.purchase_repo import PurchaseRepository
    from app.infrastructure.repositories.quotation_repo import QuotationRepository

    return SettlementCoordinator(
        invoices=InvoiceRepository(session),
        payments=PaymentRepository(session),
        budgets=BudgetRepository(session),
        fulfillment=FulfillmentRepository(session),
        quotations=QuotationRepository(session),
        requests=PurchaseRepository(session),
        catalog=CatalogRepository(session),
        events=RequestEventRepository(session),
        idempotency=IdempotencyCoordinator(
            claim_session_factory=claim_session_factory, session=session
        ),
        uow=session,
    )


class SettlementCoordinator:
    def __init__(
        self,
        *,
        invoices: InvoiceRepository,
        payments: Any,  # PaymentRepository（8.4）
        budgets: Any,  # BudgetRepository：settle() 里那条"差额必须显式释放"
        fulfillment: FulfillmentRepository,
        quotations: Any,  # QuotationRepositoryPort：只为"有没有定标"这类一致性检查留位置
        requests: Any,  # PurchaseRepositoryPort
        catalog: Any,  # CatalogRepositoryPort：算部门树
        events: Any,  # RequestEventRepositoryPort
        idempotency: IdempotencyCoordinator,
        uow: UnitOfWork,
    ) -> None:
        self._payments = payments
        self._budgets = budgets
        self._invoices = invoices
        self._fulfillment = fulfillment
        self._quotations = quotations
        self._requests = requests
        self._catalog = catalog
        self._events = events
        self._idempotency = idempotency
        self._uow = uow

    # ------------------------------------------------------------------ 可见范围

    async def _scope_for(
        self, *, actor_id: uuid.UUID, roles: Sequence[str], department_id: uuid.UUID | None
    ) -> Any:
        """与询价、订单、收货**同一套**可见范围判定（判据只有一处实现）。"""
        from app.domain.org import descendant_ids, parent_map

        if department_id is None or not needs_department_subtree(
            roles, department_id=department_id
        ):
            return resolve_request_scope(
                roles, actor_id=actor_id, department_id=department_id
            )
        departments = await self._catalog.list_departments(only_active=False)
        parents = parent_map((row.id, row.parent_id) for row in departments)
        return resolve_request_scope(
            roles,
            actor_id=actor_id,
            department_id=department_id,
            sub_department_ids=descendant_ids(department_id, parents),
        )

    async def _require_visible_request(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
    ) -> Any:
        request = await self._requests.get_request(
            request_id,
            scope=await self._scope_for(
                actor_id=actor_id, roles=roles, department_id=department_id
            ),
            actor_id=actor_id,
        )
        if request is None:
            raise NotFoundError(f"采购申请不存在：{request_id}")
        return request

    # ------------------------------------------------------------------ 待付款队列

    async def list_payment_queue(
        self,
        *,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> tuple[Sequence[Any], int]:
        """待付款队列：状态为 `INVOICED` 的单子（收过货、有发票、等着付）。

        `INVOICED` 的含义是"**可以进入付款流程**"，不是"付款条件已全部满足" ——
        三单匹配还没跑（它跑在"执行付款"那一刻，8.4）。
        所以这个队列是"待办清单"，不是"可以付款的清单"。
        """
        return await self._requests.list_requests(
            scope=await self._scope_for(
                actor_id=actor_id, roles=roles, department_id=department_id
            ),
            actor_id=actor_id,
            status=RequestStatus.INVOICED.value,
            limit=max(1, min(limit, MAX_PAGE_SIZE)),
            offset=max(0, offset),
        )

    # ------------------------------------------------------------------ 登记发票

    async def list_invoices(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
    ) -> list[Any]:
        """这张申请的发票列表（按登记时间正序）。"""
        await self._require_visible_request(
            request_id=request_id,
            actor_id=actor_id,
            roles=roles,
            department_id=department_id,
        )
        return await self._invoices.list_for_request(request_id)

    async def add_invoice_idempotent(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
        supplier_id: uuid.UUID,
        invoice_no: str,
        amount_fen: int,
        tax_amount_fen: int,
        issued_on: date | None,
        attachment_id: uuid.UUID | None,
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """带幂等的登记发票（边界层走这个）。

        载荷哈希含**发票号与金额**：同键但换了票 = 改了主意 → 409，
        而不是静默拿回第一张票的结果（用户会以为第二张也登记上了）。
        """

        async def work() -> dict[str, Any]:
            return await self.add_invoice(
                request_id=request_id,
                actor_id=actor_id,
                roles=roles,
                department_id=department_id,
                supplier_id=supplier_id,
                invoice_no=invoice_no,
                amount_fen=amount_fen,
                tax_amount_fen=tax_amount_fen,
                issued_on=issued_on,
                attachment_id=attachment_id,
                expected_version=expected_version,
            )

        result = await self._idempotency.run(
            actor_id=actor_id,
            scope=SCOPE_ADD_INVOICE,
            key=idempotency_key,
            payload={
                "request_id": str(request_id),
                "invoice_no": invoice_no,
                "amount_fen": int(amount_fen),
            },
            work=work,
            dump=lambda value: value,
        )
        return result.value

    async def add_invoice(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
        supplier_id: uuid.UUID,
        invoice_no: str,
        amount_fen: int,
        tax_amount_fen: int,
        issued_on: date | None,
        attachment_id: uuid.UUID | None,
        expected_version: int,
    ) -> dict[str, Any]:
        """登记一张发票。"""
        request = await self._require_visible_request(
            request_id=request_id,
            actor_id=actor_id,
            roles=roles,
            department_id=department_id,
        )
        if request.status not in (
            # `RECEIVED`：第一次登记发票（会把它推到 INVOICED）
            RequestStatus.RECEIVED.value,
            # `INVOICED`：再登记第二、第三张（状态已经是 INVOICED，不重复推进）
            # —— 发票可以多张，所以这个状态必须也放行，否则"分批开票"只能用一次
            RequestStatus.INVOICED.value,
        ):
            raise ConflictError(
                f"这张单子现在是 {request.status}，不能登记发票"
                "（只有已收货（RECEIVED）及之后的状态可以；"
                "没收货就收票，等于跳过了验收这一环）"
            )

        order = await self._fulfillment.order_of(request_id)
        if order is None:  # pragma: no cover - 状态机保证 RECEIVED 一定有订单
            raise ConflictError("这张单子标着已收货，但找不到订单 —— 数据不一致，请人工核对")

        # ② 发票供应商必须**就是订单供应商**。放在登记时挡，是为了给一句人话；
        #    三单匹配里还会再判一次（那是动钱之前必然要过的关）。
        if supplier_id != order.supplier_id:
            raise BusinessError(
                "发票上的供应商与订单供应商不一致，不能登记"
                "（这张订单的供应商是另一位，请核对发票是否开错）",
                status_code=422,
                code="INVOICE_SUPPLIER_MISMATCH",
            )

        if amount_fen <= 0:
            raise BusinessError(
                "发票金额必须大于 0", status_code=422, code="INVOICE_AMOUNT_INVALID"
            )
        if tax_amount_fen < 0 or tax_amount_fen > amount_fen:
            # 税额大于总额意味着"价税合计"自相矛盾（数据库 CHECK 也拦，
            # 但这里给一句人话；两层不冲突：一层为了体验，一层为了不可能绕过）
            raise BusinessError(
                f"税额 {tax_amount_fen} 必须落在 0 到发票金额 {amount_fen} 之间",
                status_code=422,
                code="INVOICE_TAX_INVALID",
            )

        try:
            record = await self._invoices.create(
                request_id=request_id,
                supplier_id=supplier_id,
                invoice_no=invoice_no,
                amount_fen=amount_fen,
                tax_amount_fen=tax_amount_fen,
                issued_on=issued_on,
                attachment_id=attachment_id,
                created_by=actor_id,
            )
        except IntegrityError as exc:
            # `UNIQUE(request_id, invoice_no)`：同一张申请里这个号已经登记过了
            raise ConflictError(
                f"这张申请里已经登记过发票号 {invoice_no} 了"
                "（同一张申请内发票号不许重复；不同供应商的号各自独立）"
            ) from exc

        total_fen, count = await self._invoices.totals_of(request_id)

        # ④ 主单推进：**只在第一次登记发票时**推（RECEIVED → INVOICED）。
        #    第二张票再推一次会撞状态校验（那时已经不是 RECEIVED），
        #    所以这里必须判"是不是第一次"，而不是无脑调 transition_status。
        moved = False
        if request.status == RequestStatus.RECEIVED.value:
            moved = await self._requests.transition_status(
                request_id=request_id,
                expected_version=expected_version,
                from_status=RequestStatus.RECEIVED.value,
                to_status=RequestStatus.INVOICED.value,
                extra={},
            )
            if not moved:
                raise ConflictError("版本已过期，或状态已被其他人改变，请刷新后重试")

        await self._events.append(
            request_id=request_id,
            event_type=INVOICE_ADDED,
            actor_id=actor_id,
            from_status=request.status,
            # 第二次之后登记发票时主单已经是 INVOICED（没推进），
            # 所以 to_status 就是它当前的值 —— 时间线上这一条显示成"状态未变"，
            # 而 payload 里的 invoice_count 说明"又加了一张票"。
            to_status=RequestStatus.INVOICED.value,
            payload={
                "invoice_id": str(record.id),
                "invoice_no": record.invoice_no,
                "supplier_id": str(record.supplier_id),
                "amount_fen": int(record.amount),
                "tax_amount_fen": int(record.tax_amount),
                # 整单口径：匹配比的是"所有发票之和"，只记单张看不出全貌
                "invoice_total_fen": total_fen,
                "invoice_count": count,
                "attachment_id": str(attachment_id) if attachment_id else None,
            },
        )
        await self._uow.commit()

        reloaded = await self._requests.get_request(
            request_id,
            scope=await self._scope_for(
                actor_id=actor_id, roles=roles, department_id=department_id
            ),
            actor_id=actor_id,
            populate_existing=True,
        )
        return {
            "invoice": invoice_payload(record),
            "invoice_total_fen": total_fen,
            "invoice_count": count,
            "request_status": reloaded.status,
            "request_version": reloaded.version,
        }

    # ------------------------------------------------------------------ 作废发票

    async def void_invoice(
        self,
        *,
        invoice_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
        reason: str | None,
    ) -> dict[str, Any]:
        """作废一张发票。

        ============================ 作废是**状态变更**，不是删行 ============================
        为什么保留行：
            ① 审计 —— "这张票曾经登记过、后来作废了、谁作废的"必须查得出来
               （与 M9 的附件删除同一条口径，那也进时间线）；
            ② 发票号可以重新登记（部分唯一索引只约束**有效**发票），
               所以作废之后能按正确的号重录一遍 —— 这是最正常的纠错路径。

        ============================ 三条拒绝 ============================
            重复作废     → 409（不是"成功"：痕迹只有一条，不能让人以为做了两次）
            主单已过付款  → 409（钱已经按这张票付过；作废会让
                           "已付金额 vs 有效发票合计"在账上对不上，而系统没有退款流程）
            可见范围外   → 404（发票继承它所属申请的可见范围）

        ⚠️ **不改主单状态**：主单留在 `INVOICED`。"这张单子收过票"是历史事实，
        不会因为票被作废而没发生过 —— 把状态退回去，会让"付过款的单子"
        与"收过票但票已作废的单子"在状态上长得一样。
        """
        invoice = await self._invoices.get(invoice_id)
        if invoice is None:
            raise NotFoundError(f"发票不存在：{invoice_id}")

        request = await self._require_visible_request(
            request_id=invoice.request_id,
            actor_id=actor_id,
            roles=roles,
            department_id=department_id,
        )

        # 判定在领域层（`can_void_invoice`），这里只把它的结论翻译成 HTTP 语义
        try:
            can_void_invoice(status=invoice.status, request_status=request.status)
        except InvoiceError as exc:
            raise ConflictError(str(exc)) from exc

        if not await self._invoices.mark_voided(invoice_id):
            # 条件更新抢输了：另一个人刚刚作废了它（或它已不是 ACTIVE）
            raise ConflictError("这张发票刚刚已被作废，请刷新后重试")

        voided = await self._invoices.get(invoice_id, populate_existing=True)
        assert voided is not None  # 刚更新过

        # 作废之后重算合计（仍然是"有效发票之和"）—— 响应里带上，
        # 界面就能立刻显示"作废后合计少了多少"，不必再查一次
        total_fen, count = await self._invoices.totals_of(request.id)

        await self._events.append(
            request_id=request.id,
            event_type=INVOICE_VOIDED,
            actor_id=actor_id,
            # 状态没变：from == to（时间线上这一条显示成"状态未变"，
            # 靠 payload 里的发票号与原因说明发生了什么）
            from_status=request.status,
            to_status=request.status,
            payload={
                "invoice_id": str(voided.id),
                "invoice_no": voided.invoice_no,
                "amount_fen": int(voided.amount),
                "reason": reason,
                # 作废后的**有效**发票合计与张数
                "invoice_total_fen": total_fen,
                "invoice_count": count,
            },
        )
        await self._uow.commit()
        return {
            "invoice": invoice_payload(voided),
            "invoice_total_fen": total_fen,
            "invoice_count": count,
            "request_status": request.status,
            "request_version": request.version,
        }

    # ------------------------------------------------------------------ 三单匹配预检

    async def match_precheck(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
    ) -> dict[str, Any]:
        """三单匹配预检：**只读**，返回三列数据 + 问题列表。

        只读是刻意的（实施计划 2431 行）：它给前端渲染对比页，
        **不做任何状态变更**，也不把"匹配过"记到任何列上。
        真拦截在"执行付款"（8.4）—— 那是唯一真正动钱的时刻。
        """
        request = await self._require_visible_request(
            request_id=request_id,
            actor_id=actor_id,
            roles=roles,
            department_id=department_id,
        )

        order = await self._fulfillment.order_of(request_id)
        ordered_qty: int | None = None
        ordered_amount_fen: int | None = None
        order_supplier_id: str | None = None
        order_line_count = 0
        if order is not None:
            ordered_qty = await self._fulfillment.ordered_quantity_of(order.id)
            ordered_amount_fen = int(order.amount)
            order_supplier_id = str(order.supplier_id)
            order_line_count = len(await self._fulfillment.order_lines(order.id))

        accepted_qty = await self._fulfillment.accepted_quantity_of(request_id)
        receipt = await self._fulfillment.receipt_of(request_id)
        invoices = await self._invoices.list_for_request(request_id)
        invoice_amount_fen, invoice_count = await self._invoices.totals_of(request_id)
        supplier_ids = await self._invoices.supplier_ids_of(request_id)

        # 多张发票的一致性：**全部一致**才算一致（不是看第一张）
        all_suppliers_match = document_supplier_consistent(
            order_supplier_id=order_supplier_id or "",
            supplier_ids=supplier_ids,
        )

        issues = precheck_three_way(
            ordered_qty=ordered_qty,
            ordered_amount_fen=ordered_amount_fen,
            order_supplier_id=order_supplier_id,
            accepted_qty=accepted_qty,
            invoice_amount_fen=invoice_amount_fen,
            invoice_count=invoice_count,
            all_suppliers_match=all_suppliers_match,
        )
        return {
            "request": {
                "id": str(request.id),
                "code": request.code,
                "title": request.title,
                "status": request.status,
                "version": request.version,
                "request_amount_fen": int(request.amount_total),
            },
            "order": (
                {
                    "id": str(order.id),
                    "order_no": order.order_no,
                    "supplier_id": str(order.supplier_id),
                    "amount_fen": int(order.amount),
                    "ordered_qty": ordered_qty,
                    "line_count": order_line_count,
                }
                if order is not None
                else None
            ),
            "receipt": {
                # `accepted_qty` 是**所有收货明细求和**（订单级粒度）
                "accepted_qty": accepted_qty,
                # 有没有收货单：`accepted_qty` 为 0 可能是"没有收货单"，
                # 也可能是"收过货但一件都没合格"—— 界面上这两者要能分开看
                "receipt_exists": receipt is not None,
                "receipt_id": str(receipt.id) if receipt is not None else None,
            },
            "invoices": [invoice_payload(record) for record in invoices],
            "invoice_total_fen": invoice_amount_fen,
            "invoice_count": invoice_count,
            "all_suppliers_match": all_suppliers_match,
            "passed": not issues,
            "issues": [
                {
                    "code": issue.code,
                    "message": issue.message,
                    "expected": issue.expected,
                    "actual": issue.actual,
                }
                for issue in issues
            ],
        }

    # ================================================================ 付款与归档（8.4）

    async def create_payment_idempotent(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """带幂等的创建付款单。"""

        async def work() -> dict[str, Any]:
            return await self.create_payment(
                request_id=request_id,
                actor_id=actor_id,
                roles=roles,
                department_id=department_id,
            )

        result = await self._idempotency.run(
            actor_id=actor_id,
            scope=SCOPE_CREATE_PAYMENT,
            key=idempotency_key,
            payload={"request_id": str(request_id)},
            work=work,
            dump=lambda value: value,
        )
        return result.value

    async def create_payment(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
    ) -> dict[str, Any]:
        """创建付款单（`PENDING`）。

        ============================ 这里**只**做两件事 ============================
        实施计划 2429 行：创建付款单时**只校验"主单已收票（INVOICED）、且至少有一张有效发票"**。

        **不在这里跑三单匹配**：匹配跑在"执行付款"那一刻（2433 行）——
        那是唯一真正动钱、也是唯一能保证"校验通过后没有再改单据"的时机。
        在这里跑一遍的话，用户看到"匹配通过"就会以为可以付了，
        而中间发票可能被作废、金额可能被改。

        金额取**有效发票之和**（订单级粒度）：它可能不等于订单金额 ——
        容差之内就算匹配通过，差额由 M7 的"实付小于预留则显式释放"处理。
        """
        request = await self._require_visible_request(
            request_id=request_id,
            actor_id=actor_id,
            roles=roles,
            department_id=department_id,
        )
        if request.status != RequestStatus.INVOICED.value:
            raise ConflictError(
                f"这张单子现在是 {request.status}，不能创建付款单"
                "（只有已收票（INVOICED）的申请可以；没收货、没收票都不能付）"
            )

        amount_fen, invoice_count = await self._invoices.totals_of(request_id)
        if invoice_count == 0:
            # 状态是 INVOICED 却一张有效发票都没有 = 票全被作废了。
            # 这里给一句人话，而不是让付款单带着 0 元建出来（数据库 CHECK 也会拦，
            # 但那时用户看到的是一句数据库错误）。
            raise BusinessError(
                "这张申请没有有效发票（已登记的可能都被作废了），不能创建付款单",
                status_code=422,
                code="NO_ACTIVE_INVOICE",
            )

        existing = await self._payments.active_of(request_id)
        if existing is not None:
            # 已经有一张有效付款单（`uq_payment_active` 也会拦，但这里给一句人话）
            raise ConflictError(
                f"这张申请已经有一张付款单了（{existing.status}），不能重复创建"
                "（付款失败的尝试会保留痕迹，重新发起时请走同一张单子或先由财务处理）"
            )

        created = await self._create_payment_with_retry(
            request_id=request_id, amount_fen=amount_fen, actor_id=actor_id
        )

        await self._events.append(
            request_id=request_id,
            event_type=PAYMENT_CREATED,
            actor_id=actor_id,
            # 状态不变：钱还没动，主单仍是 INVOICED
            from_status=request.status,
            to_status=request.status,
            payload={
                "payment_id": str(created.id),
                "amount_fen": amount_fen,
                "invoice_count": invoice_count,
                "attempt_no": created.attempt_no,
            },
        )
        await self._uow.commit()
        return {
            "payment": payment_payload(created),
            "invoice_total_fen": amount_fen,
            "invoice_count": invoice_count,
            "request_status": request.status,
            "request_version": request.version,
        }

    async def _create_payment_with_retry(
        self, *, request_id: uuid.UUID, amount_fen: int, actor_id: uuid.UUID
    ) -> Any:
        """创建付款单，尝试号撞了就重算一次。

        `attempt_no = MAX + 1` 在并发下两条新单可能算出同一个号，
        `uq_payment_attempt` 会挡住第二条。**只重试一次**：
        两次都撞说明有别的东西不对，那时该让人看见错误，而不是无限重算。
        """
        for attempt in range(2):
            attempt_no = await self._payments.next_attempt_no(request_id)
            try:
                return await self._payments.create(
                    request_id=request_id,
                    amount_fen=amount_fen,
                    created_by=actor_id,
                    attempt_no=attempt_no,
                )
            except IntegrityError as exc:
                await self._uow.rollback()
                if attempt == 1:
                    raise ConflictError(
                        "这张申请已经有一张有效付款单，或尝试序号冲突，请刷新后重试"
                    ) from exc
        raise AssertionError("unreachable")  # pragma: no cover

    async def pay_idempotent(
        self,
        *,
        payment_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
        idempotency_key: str,
        expected_version: int,
    ) -> dict[str, Any]:
        """带幂等的执行付款。"""

        async def work() -> dict[str, Any]:
            return await self.pay(
                payment_id=payment_id,
                actor_id=actor_id,
                roles=roles,
                department_id=department_id,
                expected_version=expected_version,
            )

        result = await self._idempotency.run(
            actor_id=actor_id,
            scope=SCOPE_PAY,
            key=idempotency_key,
            payload={"payment_id": str(payment_id), "version": expected_version},
            work=work,
            dump=lambda value: value,
        )
        return result.value

    async def pay(
        self,
        *,
        payment_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
        expected_version: int,
    ) -> dict[str, Any]:
        """执行付款：**这个系统里唯一真正动钱的操作**。

        实施计划 2467-2476 行的八步，一步不省，**顺序也不许调**：

            ① 主单 = INVOICED
            ② 三单匹配（有问题 → 422 + 问题列表）
            ③ **条件更新 `PENDING → PAID`，rowcount != 1 → 409**
            ④ settle：写 `SPEND(实付)` 与 `RELEASE(预留 − 实付)` 两笔流水
            ⑤ 主单 → PAID（条件更新）
            ⑥ 写事件（含实付与释放的差额）
            ⑦ commit

        **第 ③ 步为什么必须在第 ④ 步之前**：先抢到付款权的那一方才去动预算，
        抢输的一方在动钱之前就退出了。反过来的话，两个财务同时点"付款"
        会**各动一次预算**（`amount_spent` 加两次），而这是唯一真正动钱的地方。

        **第 ④ 步为什么是 `settle` 而不是 `spend`**：M7 那条规则是
        "实付小于预留则**显式释放**差额" —— 少了 RELEASE，差额会永久挂在
        `amount_reserved` 上：账面正常、日志不报，可用额度却虚低。
        `settle` 按实付与预留算出两条流水（金额换算在纯函数里）。
        """
        payment = await self._payments.get(payment_id)
        if payment is None:
            raise NotFoundError(f"付款单不存在：{payment_id}")

        request = await self._require_visible_request(
            request_id=payment.request_id,
            actor_id=actor_id,
            roles=roles,
            department_id=department_id,
        )
        if request.status != RequestStatus.INVOICED.value:
            raise ConflictError(
                f"这张单子现在是 {request.status}，不能付款"
                "（只有已收票（INVOICED）的申请可以付款）"
            )

        # ② 三单匹配 —— **动钱之前的最后一道关**。
        #    不能依赖"登记发票时校验过"：中间发票可能被作废、金额可能被改。
        precheck = await self.match_precheck(
            request_id=payment.request_id,
            actor_id=actor_id,
            roles=roles,
            department_id=department_id,
        )
        if not precheck["passed"]:
            raise BusinessError(
                "三单匹配未通过：" + "；".join(issue["message"] for issue in precheck["issues"]),
                status_code=422,
                code="MATCH_FAILED",
            )

        # ③ 抢付款权（**在动预算之前**）
        if not await self._payments.mark_paid(payment_id):
            raise ConflictError("这张付款单已被处理（可能刚刚已付款），请刷新后重试")

        # ④ 预算结算：SPEND(实付) + RELEASE(差额)
        actual_fen = int(payment.amount)
        budget_id = request.budget_id
        released_fen = 0
        if budget_id is None:
            # M7 之前提交的历史单据：当时没有预留，也就没有可结转/可释放的额度。
            # **不报错**：把它当成"这笔钱不走预算"处理（与 M7 的 `_release_all` 同一口径）。
            actual_fen = int(payment.amount)
        else:
            plan = await self._budgets.settle(
                budget_id=budget_id, request_id=payment.request_id, actual_fen=actual_fen
            )
            released_fen = int(plan.release_fen)

        # ⑤ 主单推进（条件更新，带 version）
        moved = await self._requests.transition_status(
            request_id=payment.request_id,
            expected_version=expected_version,
            from_status=RequestStatus.INVOICED.value,
            to_status=RequestStatus.PAID.value,
            extra={},
        )
        if not moved:
            raise ConflictError("版本已过期，或状态已被其他人改变，请刷新后重试")

        await self._events.append(
            request_id=payment.request_id,
            event_type=PAYMENT_COMPLETED,
            actor_id=actor_id,
            from_status=RequestStatus.INVOICED.value,
            to_status=RequestStatus.PAID.value,
            payload={
                "payment_id": str(payment.id),
                # 实付与释放的差额都要记：流水里是两笔，事件里要有对应的两个数，
                # 否则事后对不上账时无法从时间线还原"当时释放了多少"
                "amount_fen": actual_fen,
                "released_fen": released_fen,
                "invoice_total_fen": precheck["invoice_total_fen"],
                "order_amount_fen": (
                    precheck["order"]["amount_fen"] if precheck["order"] else None
                ),
                "attempt_no": payment.attempt_no,
            },
        )
        await self._uow.commit()

        reloaded_payment = await self._payments.get(payment_id, populate_existing=True)
        assert reloaded_payment is not None  # 刚更新过
        reloaded_request = await self._requests.get_request(
            payment.request_id,
            scope=await self._scope_for(
                actor_id=actor_id, roles=roles, department_id=department_id
            ),
            actor_id=actor_id,
            populate_existing=True,
        )
        return {
            "payment": payment_payload(reloaded_payment),
            "request_status": reloaded_request.status,
            "request_version": reloaded_request.version,
            # 把两个数显式回给界面：财务点完"付款"要立刻看到
            # "付了多少、释放了多少"，而不是自己去算
            "amount_fen": actual_fen,
            "released_fen": released_fen,
        }

    async def mark_payment_failed(
        self,
        *,
        payment_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
        reason: str,
    ) -> dict[str, Any]:
        """把付款单标成 `FAILED`（**保留痕迹、允许重新发起**）。

        什么时候用它：外部付款通道失败（银行退回、账号不对）——
        **不是**"三单匹配没过"（那种情况付款单根本不该被创建/推进，
        而是让用户改发票）。所以这个用例**不**检查三单匹配。

        失败之后不再占用 `uq_payment_active`（索引条件是 `status <> 'FAILED'`），
        于是可以重新发起 —— 这正是那处设计要解决的问题。
        """
        payment = await self._payments.get(payment_id)
        if payment is None:
            raise NotFoundError(f"付款单不存在：{payment_id}")
        request = await self._require_visible_request(
            request_id=payment.request_id,
            actor_id=actor_id,
            roles=roles,
            department_id=department_id,
        )

        if not await self._payments.mark_failed(payment_id, reason=reason):
            raise ConflictError("这张付款单不是 PENDING 状态，不能标记为失败")

        await self._events.append(
            request_id=payment.request_id,
            event_type=PAYMENT_FAILED,
            actor_id=actor_id,
            # 状态不变：主单仍停在 INVOICED（还等着付）
            from_status=request.status,
            to_status=request.status,
            payload={
                "payment_id": str(payment.id),
                "amount_fen": int(payment.amount),
                "reason": reason,
                "attempt_no": payment.attempt_no,
            },
        )
        await self._uow.commit()
        failed = await self._payments.get(payment_id, populate_existing=True)
        assert failed is not None  # 刚更新过
        # 与 `create_payment` **同一个响应形状**：界面上的"付款单卡片"
        # 两种情况都要显示同样的东西（金额、状态、尝试号、有效发票合计），
        # 形状不一致就会逼前端写两套渲染。
        total_fen, count = await self._invoices.totals_of(payment.request_id)
        return {
            "payment": payment_payload(failed),
            "invoice_total_fen": total_fen,
            "invoice_count": count,
            "request_status": request.status,
            "request_version": request.version,
        }

    # ------------------------------------------------------------------ 归档

    async def close(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
        expected_version: int,
    ) -> dict[str, Any]:
        """归档（`PAID → CLOSED`，**终态**）。

        归档之后这张单子不再出现在任何待办队列里（所有队列按状态过滤），
        但它仍然是可查的 —— 归档是"这件事办完了"，不是删除。

        为什么要有这一步而不是"付完就自动归档"：
        付款之后可能还有发票原件归档、合同盖章这类线下动作，
        自动归档会把"钱付了"和"事办完了"混成同一个时刻。
        """
        request = await self._require_visible_request(
            request_id=request_id,
            actor_id=actor_id,
            roles=roles,
            department_id=department_id,
        )
        if request.status != RequestStatus.PAID.value:
            raise ConflictError(
                f"这张单子现在是 {request.status}，不能归档（只有已付款（PAID）的申请可以）"
            )

        moved = await self._requests.transition_status(
            request_id=request_id,
            expected_version=expected_version,
            from_status=RequestStatus.PAID.value,
            to_status=RequestStatus.CLOSED.value,
            extra={},
        )
        if not moved:
            raise ConflictError("版本已过期，或状态已被其他人改变，请刷新后重试")

        await self._events.append(
            request_id=request_id,
            event_type=REQUEST_CLOSED,
            actor_id=actor_id,
            from_status=RequestStatus.PAID.value,
            to_status=RequestStatus.CLOSED.value,
            payload={},
        )
        await self._uow.commit()

        reloaded = await self._requests.get_request(
            request_id,
            scope=await self._scope_for(
                actor_id=actor_id, roles=roles, department_id=department_id
            ),
            actor_id=actor_id,
            populate_existing=True,
        )
        return {
            "request_id": str(reloaded.id),
            "status": reloaded.status,
            "version": reloaded.version,
        }

    # ------------------------------------------------------------------ 读付款单

    async def list_payments(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
    ) -> list[Any]:
        """这张申请的**全部**付款单（含 `FAILED` 的尝试）。

        含失败的：界面要能说"这是第 2 次尝试" ——
        只显示成功那张，会让"付了三次才成功"看起来像"只付过一次"。
        """
        await self._require_visible_request(
            request_id=request_id,
            actor_id=actor_id,
            roles=roles,
            department_id=department_id,
        )
        return await self._payments.list_for_request(request_id)

    async def get_payment(
        self,
        *,
        payment_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
    ) -> Any:
        payment = await self._payments.get(payment_id)
        if payment is None:
            raise NotFoundError(f"付款单不存在：{payment_id}")
        await self._require_visible_request(
            request_id=payment.request_id,
            actor_id=actor_id,
            roles=roles,
            department_id=department_id,
        )
        return payment


# ------------------------------------------------------------------ 响应体构造


def invoice_payload(record: Any) -> dict[str, Any]:
    """发票响应体。

    ⚠️ `amount_fen` / `tax_amount_fen` 是从记录的 `amount` / `tax_amount` 来的
    （列名不带单位后缀，响应字段带）。**只用这一个函数**生成发票的响应体，
    读接口与写接口都走它 —— 两个地方各拼一份就会出现
    "写接口说发票总额是 A、读接口说 B"这种两个说法（8.2 的
    `fully_received` 就是那么错的）。

    名字**不带下划线**：它要被边界层复用（读接口的列表也要用它）。
    私有名 + 跨层 import 是"看起来封装、实际还是耦合"的典型 ——
    要么公开它、要么让调用方自己拼一份（那才是真的会漂移）。
    """
    return {
        "id": str(record.id),
        "request_id": str(record.request_id),
        "supplier_id": str(record.supplier_id),
        "invoice_no": record.invoice_no,
        # ⚠️ `status` 必须在里面：列表**默认连作废的一起返回**（那是必须让人看见的事实），
        # 所以"哪张作废了"只能靠这个字段区分。少了它，界面上作废票与有效票长得一样 ——
        # 用户会以为作废没生效，或者以为那张票还能拿去付款。
        "status": record.status,
        "amount_fen": int(record.amount),
        "tax_amount_fen": int(record.tax_amount),
        "issued_on": record.issued_on.isoformat() if record.issued_on else None,
        "attachment_id": str(record.attachment_id) if record.attachment_id else None,
        "created_at": record.created_at.isoformat(),
    }


def payment_payload(record: Any) -> dict[str, Any]:
    """付款单响应体（读、写两条路共用 —— 与 `invoice_payload` 同一个理由）。

    ⚠️ `amount_fen` 是从记录的 `amount` 列来的；`paid_at` 只有付成功才有值。
    `attempt_no` 必须在里面：界面上要能说"这是第 2 次尝试"，
    否则多行 `FAILED`/`PENDING` 会让人以为系统重复建单了。
    """
    return {
        "id": str(record.id),
        "request_id": str(record.request_id),
        # 实付金额（分）= 有效发票金额之和
        "amount_fen": int(record.amount),
        "status": record.status,
        "attempt_no": int(record.attempt_no),
        "failure_reason": record.failure_reason,
        "created_by": str(record.created_by),
        "created_at": record.created_at.isoformat(),
        "paid_at": record.paid_at.isoformat() if record.paid_at else None,
    }


__all__ = [
    "SCOPE_ADD_INVOICE",
    "SCOPE_CREATE_PAYMENT",
    "SCOPE_PAY",
    "SettlementCoordinator",
    "build_settlement_coordinator",
    "invoice_payload",
    "payment_payload",
]
