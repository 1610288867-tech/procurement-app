"""订单与收货的用例（M10 / 8.2）。

============================ 三条主线的顺序都不能调 ============================
**下单**（`place_order`）：

    ① 申请状态 = `AWARDED`（没定标就不能下单）
    ② 取**选中的那条报价**当成交单价与供应商（`deal_unit_price_fen`）
    ③ 把申请明细**复制**成订单明细（`build_order_lines`，纯函数定金额）
    ④ 条件更新推主单 `AWARDED → ORDERED` + 写事件 + 提交

**收货**（`receive_goods`）：

    ① 申请状态 = `ORDERED` 且订单存在
    ② 纯函数算"整单收货计划"（超收/负数/别的订单的行 → 422，一次说完）
    ③ 条件更新把**订单**推 `RECEIVED`（抢权：两个人同时提交只有一个人成）
    ④ 落收货单 + 逐行实收数量
    ⑤ 收齐才推主单 `ORDERED → RECEIVED`；**没收齐就停在 ORDERED**
       —— 主单没有"部分收货"这个状态（第一版只做整单收货）

**`收齐` 的判据**在 `domain/fulfillment.is_fully_received`：写成"收了一行就算收到"
的话，仓库签一半货单子就进入待付款，而付款那边"未收货不能付款"的闸门会被绕过 ——
**不报错，只是账做错了**。

============================ 订单金额与申请金额不一致是正常的 ============================
询价场景取定标报价单价（通常更低），所以订单总额可能**小于**申请总额。
差额由 M7 的"实付小于预留则显式释放"处理 —— 这正是不变量，不是 bug。
所以这里**不做**"订单金额必须等于申请金额"这类校验：那会把正常业务挡在门外。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.application.idempotency import IdempotencyCoordinator
from app.application.ports import UnitOfWork
from app.domain.enums import RequestStatus
from app.domain.events import GOODS_RECEIVED, ORDER_PLACED
from app.domain.fulfillment import (
    SourcingOrderError,
    build_order_lines,
    is_fully_received,
    order_amount_fen,
    receipt_plan,
    receipt_problems,
)
from app.errors import BusinessError, ConflictError, NotFoundError
from app.infrastructure.repositories.catalog_repo import CatalogRepository
from app.infrastructure.repositories.event_repo import RequestEventRepository
from app.infrastructure.repositories.fulfillment_repo import (
    FulfillmentRepository,
    order_source_lines,
)
from app.infrastructure.repositories.purchase_repo import PurchaseRepository
from app.infrastructure.repositories.quotation_repo import QuotationRepository

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

#: 幂等 scope：**端点标识**，写死在这里（与提交 / 撤回 / 定标同一条约定）。
SCOPE_PLACE_ORDER = "POST:/api/v1/requests/{id}/orders"
SCOPE_RECEIVE = "POST:/api/v1/requests/{id}/receipts"


def build_fulfillment_coordinator(
    session: Any, *, claim_session_factory: Any
) -> FulfillmentCoordinator:
    """按一次请求的会话装配订单/收货用例的依赖。

    与 `build_sourcing_coordinator` 同一个理由：装配只写一份，
    边界层与测试共用 —— 测试要直接调协调者时不必再抄一遍依赖接线。
    这些用例**没有阈值之类的配置**，所以不需要额外参数（这本身就是好事：
    配置项越少，装配越不容易出错）。
    """
    return FulfillmentCoordinator(
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


class FulfillmentCoordinator:
    def __init__(
        self,
        *,
        fulfillment: FulfillmentRepository,
        quotations: Any,  # QuotationRepositoryPort：定标结果
        requests: Any,  # PurchaseRepositoryPort
        catalog: Any,  # CatalogRepositoryPort：只为算部门树
        events: Any,  # RequestEventRepositoryPort
        idempotency: IdempotencyCoordinator,
        uow: UnitOfWork,
    ) -> None:
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
        """与询价、申请列表、附件**同一套**可见范围判定。

        判据（`resolve_request_scope` + `needs_department_subtree`）与
        `PurchaseCoordinator` / `SourcingCoordinator` 用的是同一对函数：
        可见范围只允许有一处实现，抄一份就多一处会漂移的规则 ——
        而漂移的表现是"某个模块少了子部门"，不报错、只是查不到。
        """
        from app.domain.org import descendant_ids, parent_map
        from app.domain.permissions import needs_department_subtree, resolve_request_scope

        # `department_id is None` 单独判一次（既收窄类型，也表达"没有部门就没有本部门可算"）
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
        scope = await self._scope_for(
            actor_id=actor_id, roles=roles, department_id=department_id
        )
        request = await self._requests.get_request(
            request_id, scope=scope, actor_id=actor_id
        )
        if request is None:
            raise NotFoundError(f"采购申请不存在：{request_id}")
        return request

    # ------------------------------------------------------------------ 待收货队列

    async def list_receipt_queue(
        self,
        *,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> tuple[Sequence[Any], int]:
        """待收货队列：状态为 `ORDERED` 的单子（已下单、等着验收）。"""
        return await self._requests.list_requests(
            scope=await self._scope_for(
                actor_id=actor_id, roles=roles, department_id=department_id
            ),
            actor_id=actor_id,
            status=RequestStatus.ORDERED.value,
            limit=max(1, min(limit, MAX_PAGE_SIZE)),
            offset=max(0, offset),
        )

    # ------------------------------------------------------------------ 读订单

    async def get_order(
        self,
        *,
        order_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
    ) -> dict[str, Any]:
        """订单详情：订单 + 明细 + **收货单（若有）**。

        可见范围按**订单所属的申请**判：订单没有自己的权限模型，
        它继承申请的那一套（少一处会漂移的规则）。

        返回的是**可直接序列化的 dict**（与写接口同一个形状）——
        `receipt.fully_received` 与 `order.amount_fen` 都不是订单表上的列，
        让读接口自己去拼，就会出现"写接口说收齐了、读接口说没收齐"这种两个说法。
        """
        order = await self._fulfillment.get_order(order_id)
        if order is None:
            raise NotFoundError(f"采购订单不存在：{order_id}")
        request = await self._require_visible_request(
            request_id=order.request_id,
            actor_id=actor_id,
            roles=roles,
            department_id=department_id,
        )
        return await self._order_detail(order, request)

    async def order_of_request(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
    ) -> dict[str, Any] | None:
        """按申请查订单（详情页用）。**没有订单返回 `None`** —— 不是错误。

        "还没下单"是一个正常的空值：详情页要能一屏显示
        "申请 + 订单（若有）+ 收货（若有）"，而不是让前端特判一个错误码。
        """
        request = await self._require_visible_request(
            request_id=request_id,
            actor_id=actor_id,
            roles=roles,
            department_id=department_id,
        )
        order = await self._fulfillment.order_of(request_id)
        if order is None:
            return None
        return await self._order_detail(order, request)

    async def _order_detail(self, order: Any, request: Any) -> dict[str, Any]:
        """订单 + 明细 + 收货单（有就带上）。两个读接口共用，保证形状一致。"""
        lines = await self._fulfillment.order_lines(order.id)
        receipt = await self._fulfillment.receipt_of(order.request_id)
        return {
            "order": _order_payload(order, request),
            "lines": [_line_payload(line) for line in lines],
            "receipt": (
                _receipt_payload(
                    receipt,
                    # ⚠️ 判据是"**这张申请有收货单**"，不是"当前状态是不是 RECEIVED"。
                    #
                    # 拿状态判会随流程**倒退**：收齐之后主单是 RECEIVED（true），
                    # 但登记发票会把它推到 INVOICED、付款推到 PAID、归档推到 CLOSED ——
                    # 那时订单详情页会突然说"没收齐"，而仓库明明收齐了。
                    # 这个坑实测撞到过（8.3 的 `test_receipt_shows_up_in_the_order_detail`
                    # 只覆盖到 RECEIVED 那一刻，所以当时是绿的）。
                    #
                    # "有收货单"就等价于"收齐了"：第一版只做整单收货，
                    # 没收齐会被 422 挡住，所以收货单不可能代表一次部分收货。
                    fully_received=receipt is not None,
                )
                if receipt is not None
                else None
            ),
        }

    # ------------------------------------------------------------------ 下单

    async def place_order_idempotent(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """带幂等的下单（边界层走这个）。

        载荷哈希只含"这次请求的内容"：同一把键重放下单请求 → 拿回第一次的响应。
        这个动作**不是幂等的业务**（会生成订单号），所以必须靠幂等键兜住重试。
        """

        async def work() -> dict[str, Any]:
            return await self.place_order(
                request_id=request_id,
                actor_id=actor_id,
                roles=roles,
                department_id=department_id,
                expected_version=expected_version,
            )

        result = await self._idempotency.run(
            actor_id=actor_id,
            scope=SCOPE_PLACE_ORDER,
            key=idempotency_key,
            payload={"request_id": str(request_id), "version": expected_version},
            work=work,
            dump=lambda value: value,
        )
        return result.value

    async def place_order(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
        expected_version: int,
    ) -> dict[str, Any]:
        """下单：由定标结果生成采购订单。"""
        request = await self._require_visible_request(
            request_id=request_id,
            actor_id=actor_id,
            roles=roles,
            department_id=department_id,
        )
        if request.status != RequestStatus.AWARDED.value:
            raise ConflictError(
                f"这张单子现在是 {request.status}，不能下单"
                "（只有已定标（AWARDED）的申请可以生成订单）"
            )

        # ② 定标结果：供应商与成交单价都从**选中的那条报价**来
        awarded = await self._quotations.selected_of(request_id)
        if awarded is None:
            # 状态是 AWARDED 却没有选中的报价 = 数据不一致。宁可报出来也不要
            # "用申请金额下单"（那会让订单金额悄悄变回申请金额，三单匹配跟着错）。
            raise ConflictError(
                "这张单子标着已定标，但找不到选中的报价 —— 数据不一致，请人工核对"
            )

        # ③ 明细复制 + 金额重算（纯函数）
        items = await self._requests.list_items(request_id)
        try:
            drafts = build_order_lines(
                order_source_lines(list(items)),
                deal_unit_price_fen=int(awarded.amount),
            )
        except SourcingOrderError as exc:
            raise BusinessError(str(exc), status_code=422, code=exc.code) from exc
        amount_fen = order_amount_fen(drafts)

        try:
            order = await self._fulfillment.create_order(
                request_id=request_id,
                supplier_id=awarded.supplier_id,
                lines=drafts,
                amount_fen=amount_fen,
                created_by=actor_id,
            )
        except IntegrityError as exc:
            # `UNIQUE(request_id)`：这张申请已经有订单了。
            # 条件更新（下面那步）本来就会挡住，这里是**更早**的一道 ——
            # 早挡住的好处是不会先插一张订单再回滚。
            raise ConflictError("这张申请已经生成过订单了") from exc

        # ④ 主单推进（条件更新，带 version）
        moved = await self._requests.transition_status(
            request_id=request_id,
            expected_version=expected_version,
            from_status=RequestStatus.AWARDED.value,
            to_status=RequestStatus.ORDERED.value,
            extra={},
        )
        if not moved:
            raise ConflictError("版本已过期，或状态已被其他人改变，请刷新后重试")

        await self._events.append(
            request_id=request_id,
            event_type=ORDER_PLACED,
            actor_id=actor_id,
            from_status=RequestStatus.AWARDED.value,
            to_status=RequestStatus.ORDERED.value,
            payload={
                "order_id": str(order.id),
                "order_no": order.order_no,
                "supplier_id": str(order.supplier_id),
                # 订单金额是三单匹配与预算结转的基准 —— 与申请金额不一致是正常的
                "amount_fen": amount_fen,
                "request_amount_fen": int(request.amount_total),
                "deal_unit_price_fen": int(awarded.amount),
                "line_count": len(drafts),
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
        lines = await self._fulfillment.order_lines(order.id)
        return {
            "order": _order_payload(order, reloaded),
            "lines": [_line_payload(line) for line in lines],
        }

    # ------------------------------------------------------------------ 收货

    async def receive_goods_idempotent(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
        accepted: Mapping[str, int],
        note: str | None,
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """带幂等的收货登记（边界层走这个）。

        `accepted` 的键是**订单明细 id**（字符串）。用 id 而不是行号：
        行号是"第几行"的展示约定，id 才是稳定标识 ——
        界面上多一次请求就可能把行号理解错，而 id 不会。
        """

        async def work() -> dict[str, Any]:
            return await self.receive_goods(
                request_id=request_id,
                actor_id=actor_id,
                roles=roles,
                department_id=department_id,
                accepted=accepted,
                note=note,
                expected_version=expected_version,
            )

        result = await self._idempotency.run(
            actor_id=actor_id,
            scope=SCOPE_RECEIVE,
            key=idempotency_key,
            payload={
                "request_id": str(request_id),
                # 载荷哈希要含**逐行数量**：同键但改了数量 = 改了主意 → 409。
                # 只哈希 request_id 的话，第二次提交会静默拿回第一次的结果，
                # 而用户以为"我改了数量并重新提交了"。
                "accepted": {key: int(value) for key, value in sorted(accepted.items())},
                "note": note or "",
            },
            work=work,
            dump=lambda value: value,
        )
        return result.value

    async def receive_goods(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
        accepted: Mapping[str, int],
        note: str | None,
        expected_version: int,
    ) -> dict[str, Any]:
        """收货登记（整单收货）。"""
        request = await self._require_visible_request(
            request_id=request_id,
            actor_id=actor_id,
            roles=roles,
            department_id=department_id,
        )
        if request.status != RequestStatus.ORDERED.value:
            raise ConflictError(
                f"这张单子现在是 {request.status}，不能收货"
                "（只有已下单（ORDERED）的申请可以登记收货）"
            )

        order = await self._fulfillment.order_of(request_id)
        if order is None:  # pragma: no cover - 状态机保证 ORDERED 一定有订单
            raise ConflictError("这张单子标着已下单，但找不到订单 —— 数据不一致，请人工核对")
        lines = await self._fulfillment.order_lines(order.id)

        # ② 纯函数判定：超收 / 负数 / 别的订单的行，一次说完所有问题
        try:
            plan = receipt_plan(
                order_lines=[(str(line.id), int(line.quantity), line.name) for line in lines],
                accepted={str(key): int(value) for key, value in accepted.items()},
            )
        except SourcingOrderError as exc:
            raise BusinessError(str(exc), status_code=422, code=exc.code) from exc

        fully = is_fully_received(
            [(line_id, quantity) for line_id, quantity in plan],
            ordered={str(line.id): int(line.quantity) for line in lines},
        )
        # **不收部分货**：整单收货是"每一行都收齐"，没收齐当场拒绝并说清差哪几行。
        # 允许"收了一半、主单不动"会造出模型禁止的组合（订单已收货 + 主单 ORDERED），
        # 而那个组合走不到任何终态 —— 详见 `domain/fulfillment.receipt_problems`。
        shortfalls = receipt_problems(
            plan,
            ordered={str(line.id): int(line.quantity) for line in lines},
            names={str(line.id): line.name for line in lines},
        )
        if shortfalls:
            raise BusinessError(
                "；".join(shortfalls), status_code=422, code="RECEIPT_NOT_COMPLETE"
            )

        # ③ 落收货单（**先插后改**：收货单只认"订单存在"，不需要订单先变状态）
        try:
            receipt = await self._fulfillment.create_receipt(
                request_id=request_id,
                order_id=order.id,
                receiver_id=actor_id,
                plan=[(uuid.UUID(line_id), quantity) for line_id, quantity in plan],
                note=note,
            )
        except IntegrityError as exc:
            # `UNIQUE(request_id)`：这张申请已经登记过收货了（第二道防线）
            raise ConflictError("这张申请已经登记过收货了") from exc

        # ④ 抢"收货权"：条件更新，**放在收货单之后**。
        #    反过来的话，"抢输了"会把已经插进去的收货单留在事务里，
        #    虽然最终会一起回滚，但日志与异常路径会变得难读；
        #    而且失败信息会先指向数据库约束、而不是那句"已被别人收货"。
        if not await self._fulfillment.mark_order_received(order.id):
            raise ConflictError("这张订单刚刚已被收货登记，请刷新后重试")

        # ⚠️ **条件更新之后必须强制重读**：`mark_order_received` 是 bulk UPDATE
        # （`synchronize_session=False`），会话里那份 order 仍是 `ORDERED`，
        # 直接拿它拼响应的话，"收齐了"这件事在响应里显示成没收齐
        # （库里明明已经改了）。本项目第四次踩这个坑，这次连断言一起写进测试。
        order = await self._fulfillment.order_of(request_id, populate_existing=True)
        assert order is not None  # 刚更新过，不可能没有

        # ⑤ **收齐才推主单** —— 没收齐就停在 ORDERED（没有"部分收货"这个状态）
        moved = False
        if fully:
            moved = await self._requests.transition_status(
                request_id=request_id,
                expected_version=expected_version,
                from_status=RequestStatus.ORDERED.value,
                to_status=RequestStatus.RECEIVED.value,
                extra={},
            )
            if not moved:
                raise ConflictError("版本已过期，或状态已被其他人改变，请刷新后重试")

        # ⚠️ `fully_received` 断言的是**主单真的进了 RECEIVED**，不是"算出来应该进"。
        # 两者在正常路径上恒等（`fully` 为真但转移没成就抛 409 了），
        # 但写成"算出来的那个"会让响应与主单状态各说各话 ——
        # 而这个标志是**下游付款的闸门**（"未收货不能付款"），
        # 它一旦说"收齐了"而主单还在 ORDERED，闸门就被绕过了。少一个分支不值得冒这个险。
        fully = moved

        await self._events.append(
            request_id=request_id,
            event_type=GOODS_RECEIVED,
            actor_id=actor_id,
            from_status=RequestStatus.ORDERED.value,
            to_status=(
                RequestStatus.RECEIVED.value if fully else RequestStatus.ORDERED.value
            ),
            payload={
                "receipt_id": str(receipt.id),
                "order_id": str(order.id),
                "fully_received": fully,
                # 逐行实收数量：三单匹配要用（订单级粒度下是各行之和）
                "accepted": [
                    {"order_line_id": line_id, "quantity": quantity}
                    for line_id, quantity in plan
                ],
                "note": note,
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
            "order": _order_payload(order, reloaded),
            "receipt": _receipt_payload(receipt, fully_received=fully),
        }


# ------------------------------------------------------------------ 响应体构造


def _receipt_payload(receipt: Any, *, fully_received: bool) -> dict[str, Any]:
    """收货单响应体。

    ⚠️ `fully_received` **不是表上的列**，是"这张申请收齐了没有"的判定 ——
    读的时候由**主单状态**回答（`RECEIVED` 就是收齐了；第一版只有整单收货，
    所以这两件事是同一件事）。

    它必须由协调者算出来交给响应，不能让读接口用 ORM 记录直接解析：
    记录上没有这个属性，Pydantic 会退到字段默认值，于是**"收齐了"被显示成"没收齐"**
    （实测踩到：写接口说 true、读接口说 false，同一张收货单两个说法）。
    所以这个模型里 `fully_received` **不给默认值** —— 少给一个字段就当场报错，
    而不是静默地给一个可能相反的值。
    """
    return {
        "id": str(receipt.id),
        "request_id": str(receipt.request_id),
        "order_id": str(receipt.order_id),
        "receiver_id": str(receipt.receiver_id),
        "status": receipt.status,
        "note": receipt.note,
        "received_at": receipt.received_at.isoformat(),
        "fully_received": fully_received,
    }


def _order_payload(order: Any, request: Any) -> dict[str, Any]:
    """订单响应体（**幂等层要存的就是它**，所以必须是可序列化的 dict）。

    `ordered_at` 用 ISO 串而不是 datetime：它在 dict 里要能进 JSONB。
    读接口那边由 Pydantic 从 ORM 记录直接解析（`from_attributes`），不走这里。
    """
    return {
        "id": str(order.id),
        "request_id": str(order.request_id),
        "order_no": order.order_no,
        "supplier_id": str(order.supplier_id),
        "amount_fen": int(order.amount),
        "status": order.status,
        "ordered_at": order.ordered_at.isoformat(),
        # 顺带回一个申请摘要：下单的响应里前端要立刻显示"这张单子进到哪一步了"，
        # 让它再查一次申请就是白花一次往返。
        "request_status": request.status,
        "request_version": request.version,
    }


def _line_payload(line: Any) -> dict[str, Any]:
    return {
        "id": str(line.id),
        "order_id": str(line.order_id),
        "item_id": str(line.item_id) if line.item_id else None,
        "line_no": line.line_no,
        "name": line.name,
        "spec": line.spec,
        "quantity": line.quantity,
        "unit_price_fen": int(line.unit_price),
        "amount_fen": int(line.amount),
    }
