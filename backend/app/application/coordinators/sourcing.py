"""询价与定标的用例（M10 / 8.1）。

============================ 这个协调者负责的三件事 ============================
1. **待询价队列**：状态 = `SOURCING` 的单子（审批通过、还没定标）；
2. **登记报价**：按"申请 × 供应商"记一笔金额（可挂报价单附件，接 M9）+ 写事件；
3. **定标**：跑 `check_awardable` 纯函数 → 条件更新选中那家 → 其余落选 →
   主单 `SOURCING → AWARDED`（**同一个事务**）+ 写事件。

============================ 三个"顺序不能调"的地方 ============================
1. **先惰性结算过期报价，再数家数**：`expire_overdue` 必须在 `check_awardable`
   之前跑。顺序反了的话，判定读到的状态列还是旧的 ——
   结果不会错（判定只认时间），但界面刚看到的"3 家"会在定标后变成"1 家"，
   用户没法理解为什么刚才还能定、现在不行。
2. **先抢定标权，再改其余报价**：`mark_awarded` 是条件更新，
   受影响行数 = 0 说明别人先定了（或这条报价已经不是 SUBMITTED）→ 直接 409。
   把"改其余报价"放在它前面的话，抢输的一方已经把别人的报价改成落选了。
3. **供应商合格性用查询现算，不用报价时的快照**：报价可能是三天前登记的，
   中间供应商可能被停用。定标那一刻的"合格"才是有效结论 ——
   与"资质过期用查询时过滤"同一条口径。

============================ 成交单价的去向 ============================
定标会把**选中报价的原值**写进事件 payload（`deal_unit_price_fen`）。
8.2 生成订单时取它当明细行单价 —— 订单金额因此通常**小于**申请金额，
差额由 M7 的"实付小于预留则显式释放"处理（ER 图第 136-142 行定死的口径）。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.application.idempotency import IdempotencyCoordinator
from app.application.ports import (
    CatalogRepositoryPort,
    QuotationRepositoryPort,
    QuotationView,
    UnitOfWork,
)
from app.domain.enums import RequestStatus
from app.domain.events import QUOTATION_ADDED, QUOTATION_AWARDED
from app.domain.sourcing_policy import (
    QUOTE_SELECTED,
    QuotationFacts,
    award_plan,
    check_awardable,
    deal_unit_price_fen,
    is_effective,
)
from app.errors import BusinessError, ConflictError, NotFoundError
from app.infrastructure.repositories.catalog_repo import CatalogRepository
from app.infrastructure.repositories.event_repo import RequestEventRepository
from app.infrastructure.repositories.purchase_repo import PurchaseRepository
from app.infrastructure.repositories.quotation_repo import QuotationRepository

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

#: 幂等 scope：**端点标识**，写死在这里（与提交 / 撤回同一条约定）。
#: 同一把 key 在"提交"与"定标"之间必须互不干扰 —— scope 混了的话，
#: 定标请求会拿到提交请求的响应，而且看起来一切正常。
SCOPE_AWARD = "POST:/api/v1/requests/{id}/award"


def _summary_payload(request: Any) -> dict[str, Any]:
    """申请摘要（幂等响应体的一份，形状与边界的 `RequestSummaryOut` 对齐）。"""
    return {
        "id": str(request.id),
        "code": request.code,
        "title": request.title,
        "requester_id": str(request.requester_id),
        "department_id": str(request.department_id),
        "amount_total_fen": int(request.amount_total),
        "status": request.status,
        "version": request.version,
    }


def _quotation_payload(row: Any, supplier_name: str) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "request_id": str(row.request_id),
        "supplier_id": str(row.supplier_id),
        "supplier_name": supplier_name,
        "amount_fen": int(row.amount),
        "currency": row.currency,
        "status": row.status,
        "valid_until": row.valid_until.isoformat() if row.valid_until else None,
        "attachment_id": str(row.attachment_id) if row.attachment_id else None,
        "created_by": str(row.created_by),
        "created_at": row.created_at.isoformat(),
    }


def build_sourcing_coordinator(
    session: Any,
    *,
    claim_session_factory: Any,
    threshold_fen: int,
) -> SourcingCoordinator:
    """按一次请求的会话装配询价用例的依赖。

    ============================ 为什么要有这个工厂 ============================
    装配代码原先写在边界层里，于是**测试想直接调协调者**（例如验证
    `quotations_ready` 的"结算 + 读"口径）就得把那 7 个依赖再抄一遍 ——
    抄一遍就多一处会漂移的接线，而且抄错的时候测试会"通过"得莫名其妙。

    把它放在协调者所在模块，边界层与测试**共用同一份装配**：
    生产里 `threshold_fen` 来自配置，测试里直接传值（这正是"规则与阈值分开"的好处）。
    """
    return SourcingCoordinator(
        quotations=QuotationRepository(session),
        requests=PurchaseRepository(session),
        catalog=CatalogRepository(session),
        events=RequestEventRepository(session),
        idempotency=IdempotencyCoordinator(
            claim_session_factory=claim_session_factory, session=session
        ),
        uow=session,
        threshold_fen=threshold_fen,
    )


class SourcingCoordinator:
    def __init__(
        self,
        *,
        quotations: QuotationRepositoryPort,
        requests: Any,  # PurchaseRepositoryPort：只为 list_requests / get_request / transition_status
        catalog: CatalogRepositoryPort,
        events: Any,  # RequestEventRepositoryPort
        idempotency: IdempotencyCoordinator,
        uow: UnitOfWork,
        threshold_fen: int,
    ) -> None:
        self._quotations = quotations
        self._requests = requests
        self._catalog = catalog
        self._events = events
        #: 定标是"重复执行会改变结果"的动作，所以它走与提交 / 撤回同一套幂等。
        self._idempotency = idempotency
        self._uow = uow
        #: 比价线（分）。来自配置，**不在代码里写字面量** ——
        #: 纯函数把它当入参，所以"规则"与"阈值是多少"是两件独立的事。
        self._threshold_fen = threshold_fen

    # ------------------------------------------------------------------ 可见范围

    async def _scope_for(
        self, *, actor_id: uuid.UUID, roles: Sequence[str], department_id: uuid.UUID | None
    ) -> Any:
        """复用采购申请的可见范围判定（与列表、详情、附件、订单、收货同源）。

        询价是采购专员的工作，全公司可见；但**别的角色**（例如部门主管）
        也可能走到这些接口上，所以这里仍然按统一判据算，
        而不是写死 `sees_all`。

        "要不要查部门树"由 `needs_department_subtree` 判定 —— 与
        `PurchaseCoordinator` 用的是同一个判据（抄一遍就会有一处漏掉子部门）。
        """
        from app.domain.org import descendant_ids, parent_map
        from app.domain.permissions import needs_department_subtree, resolve_request_scope

        # `department_id is None` 单独判一次：不只是为了类型收窄 ——
        # 没有部门的账号（如系统管理员）本来就没有"本部门"可算，
        # 直接落到 `only_self` 那一档（兜底往窄里兜）。
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
        """把"这张单子能不能看见"收在一处。可见范围外 → **404**（不告诉它存在）。"""
        scope = await self._scope_for(
            actor_id=actor_id, roles=roles, department_id=department_id
        )
        request = await self._requests.get_request(
            request_id, scope=scope, actor_id=actor_id
        )
        if request is None:
            raise NotFoundError(f"采购申请不存在：{request_id}")
        return request

    # ------------------------------------------------------------------ 待询价队列

    async def list_queue(
        self,
        *,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> tuple[Sequence[Any], int]:
        """待询价队列：状态为 `SOURCING` 的单子（审批通过、等着比价定标）。"""
        return await self._requests.list_requests(
            scope=await self._scope_for(
                actor_id=actor_id, roles=roles, department_id=department_id
            ),
            actor_id=actor_id,
            status=RequestStatus.SOURCING.value,
            limit=max(1, min(limit, MAX_PAGE_SIZE)),
            offset=max(0, offset),
        )

    # ------------------------------------------------------------------ 比价表

    async def quotations_ready(
        self, request_id: uuid.UUID
    ) -> tuple[list[QuotationView], list[QuotationFacts]]:
        """**唯一**该被用来"按状态数报价"的读法：先惰性结算，再读。

        ============================ 为什么要有这个名字 ============================
        `EXPIRED` 是"随时间自己成立"的状态，它只在**有人结算**时才写进状态列。
        于是直接读状态列的人会看到"过期却写着 `SUBMITTED`"的行 ——
        判定（`is_effective`，只认时间）不受影响，但**任何按状态计数的统计会错**。

        实测踩到的第一个受害者是这条路径自己：`list_quotations` 结算了却没提交，
        结果状态列永远追不上判定。修好之后这里再往前一步 ——
        把"结算 + 读"合成**一个入口**，并让所有决策/展示/统计都只用它：

            要按状态数报价 → 只能走 `quotations_ready()`

        这样"忘了结算"这件事从"每处都要记得"变成"根本绕不过去"。
        数据量上来之后，这里的惰性结算可以换成"写入时算过期"或物化视图，
        **但那个唯一入口不用变** —— 这正是把它单独拎出来的意义。
        """
        settled = await self._quotations.expire_overdue(request_id)
        if settled:
            # 只在真的改了行时提交：没改动就不必多一次事务往返。
            # ⚠️ 不提交的话请求结束会回滚，结算被丢掉 —— 那是静默失效
            # （界面上永远看不到 EXPIRED，而判定又是对的）。
            await self._uow.commit()
        rows = list(await self._quotations.list_for_request(request_id))
        usable = await self._usable_suppliers()
        return rows, quotation_facts(rows, usable=usable)

    async def list_quotations(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
    ) -> tuple[Any, Sequence[QuotationView], dict[str, str], list[str]]:
        """比价表：申请 + 各家报价 + 供应商名 + **当前能不能定标的说明**。

        返回"问题列表"而不是抛异常：比价表是**只读视图**，
        采购专员需要看到"还差几家"才能决定下一步，而不是被一个 422 顶回来。
        （定标接口才是那个会拒绝的地方。）

        报价从 `quotations_ready()` 拿 —— 那是结算 + 读的唯一入口。
        """
        request = await self._require_visible_request(
            request_id=request_id,
            actor_id=actor_id,
            roles=roles,
            department_id=department_id,
        )
        rows, facts = await self.quotations_ready(request_id)
        problems = check_awardable(
            facts, amount_fen=int(request.amount_total), threshold_fen=self._threshold_fen
        )
        names = {str(row.id): await self._supplier_name(row.supplier_id) for row in rows}
        return request, rows, names, problems

    # ------------------------------------------------------------------ 登记报价

    async def add_quotation(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
        supplier_id: uuid.UUID,
        amount_fen: int,
        valid_until: datetime | None,
        attachment_id: uuid.UUID | None,
    ) -> tuple[QuotationView, str]:
        """登记一家报价。返回 `(报价记录, 供应商名)`。

        为什么把供应商名一起返回：界面登记完要立刻显示"某家报了多少"，
        让边界层再查一次供应商既白花一次往返，又多一处"名称从哪来"的口径。

        ============================ 三道校验，顺序有意为之 ============================
        1. **申请在 `SOURCING`**：`AWARDED` 之后再报价没有意义（已经成交了），
           而"报完价才发现单子已经定标"会让人以为报价生效了；
        2. **供应商此刻合格**：停用 / 资质过期的供应商不能进比价表 ——
           这就是业务规则第 3 条那条"供应商本身合格"的硬控制；
        3. **同一家只能报一次**：由唯一约束兜底，这里转成 409 并说清原因。

        金额必须 > 0（由 `domain` 的 CHECK 与这里的显式校验两道挡住）：
        0 元报价会让家数虚增，而它在比价表上看着像一家真实报价。
        """
        request = await self._require_visible_request(
            request_id=request_id,
            actor_id=actor_id,
            roles=roles,
            department_id=department_id,
        )
        if request.status != RequestStatus.SOURCING.value:
            raise ConflictError(
                f"这张单子现在是 {request.status}，只有询价中（SOURCING）的申请可以登记报价"
            )
        if amount_fen <= 0:
            raise BusinessError("报价金额必须大于 0", status_code=422, code="QUOTATION_AMOUNT_INVALID")

        supplier = await self._catalog.get_supplier(supplier_id)
        if supplier is None:
            raise BusinessError(
                f"供应商不存在：{supplier_id}", status_code=422, code="SUPPLIER_NOT_FOUND"
            )
        if not await self._supplier_is_usable(supplier_id):
            raise BusinessError(
                "该供应商未通过准入（已停用或资质已过期），不能参与比价",
                status_code=422,
                code="SUPPLIER_NOT_QUALIFIED",
            )

        try:
            record = await self._quotations.create(
                request_id=request_id,
                supplier_id=supplier_id,
                amount_fen=amount_fen,
                valid_until=valid_until,
                attachment_id=attachment_id,
                created_by=actor_id,
            )
        except IntegrityError as exc:
            # 唯一约束 (request_id, supplier_id)：同一家报了两次。
            # 这是**必须让人看见**的事（否则同一家可以刷家数），所以转成明确的 409。
            raise ConflictError(
                "该供应商已经报过价了，同一家在同一张申请上只能报一次"
                "（三家比价要的是不同供应商之间的竞争）"
            ) from exc

        await self._events.append(
            request_id=request_id,
            event_type=QUOTATION_ADDED,
            actor_id=actor_id,
            from_status=request.status,
            to_status=request.status,
            payload={
                "quotation_id": str(record.id),
                "supplier_id": str(supplier_id),
                "supplier_name": supplier.name,
                "amount_fen": int(record.amount),
                "valid_until": record.valid_until.isoformat() if record.valid_until else None,
                "attachment_id": str(attachment_id) if attachment_id else None,
            },
        )
        await self._uow.commit()
        return record, supplier.name

    # ------------------------------------------------------------------ 定标

    async def award(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
        supplier_id: uuid.UUID,
        expected_version: int,
    ) -> dict[str, Any]:
        """定标。返回**可直接序列化的响应体**（幂等重放要存的就是它）。

        ============================ 为什么返回 dict 而不是 ORM 对象 ============================
        定标要带幂等键：同键重放必须返回**第一次的响应**。幂等层把响应体存进
        `idempotency_records`，所以这个返回值必须是 JSON 可序列化的 dict ——
        返回 ORM 对象的话，重放那一次还得重新查一遍（而且两次的形状可能不同，
        比如报价被后续操作改了状态）。

        键是界面渲染要用的字段（`supplier_name` 也在这里查好），
        这样**重放与首次执行的响应逐字节相同**，测试可以直接比对。

        顺序见文件头；每一步写在一次事务里，任何一步失败都整体回滚 ——
        绝不出现"报价改了、主单没动"这种半截状态。
        """
        request = await self._require_visible_request(
            request_id=request_id,
            actor_id=actor_id,
            roles=roles,
            department_id=department_id,
        )
        if request.status != RequestStatus.SOURCING.value:
            raise ConflictError(
                f"这张单子现在是 {request.status}，不能定标"
                "（只有询价中的申请可以定标；已定标的单子会一直停在 AWARDED）"
            )

        # ① 先惰性结算再读（**唯一入口** `quotations_ready`）：
        #    这样"判定用的家数"与"界面上看到的家数"是同一份事实
        rows, facts = await self.quotations_ready(request_id)

        # ② 纯函数判定：返回**问题列表**，原样进 422 的 message
        problems = check_awardable(
            facts, amount_fen=int(request.amount_total), threshold_fen=self._threshold_fen
        )
        if problems:
            raise BusinessError(
                "；".join(problems), status_code=422, code="NOT_AWARDABLE"
            )

        # ③ 选中的那家必须在报价表里，且**它本身是有效报价**
        plan = award_plan(facts, selected_supplier_id=str(supplier_id))
        target = next(
            (index for index, (_, status) in enumerate(plan) if status == QUOTE_SELECTED),
            None,
        )
        if target is None:
            raise BusinessError(
                "这家供应商没有参与本单报价，不能定标给它",
                status_code=422,
                code="SUPPLIER_NOT_QUOTED",
            )
        target_row = rows[target]
        # 用领域层的 `is_effective` 判定，**不在这里重写一遍条件**：
        # "有效"的定义只有一处（状态 + 有效期 + 供应商合格），
        # 抄一份出来早晚会出现"数家数时算有效、定标时不算"的矛盾。
        if not is_effective(
            status=facts[target].status,
            valid_until=facts[target].valid_until,
            supplier_usable=facts[target].supplier_usable,
        ):
            raise BusinessError(
                "选中的报价不是有效报价（已过期、已落选，或供应商不合格），不能定标",
                status_code=422,
                code="QUOTATION_NOT_EFFECTIVE",
            )

        # ④ **抢定标权**：条件更新。抢输的一方在动任何数据之前就退出
        if not await self._quotations.mark_awarded(target_row.id):
            raise ConflictError("该申请刚刚已被定标（或这条报价状态已变），请刷新后重试")

        # ⑤ 其余落选（含过期那些：这一轮结束了，它们不该还留在可投标状态）
        await self._quotations.mark_not_selected(request_id, keep_id=target_row.id)

        # ⑥ 主单推进（带 version 的条件更新，与上面同一个事务）
        moved = await self._requests.transition_status(
            request_id=request_id,
            expected_version=expected_version,
            from_status=RequestStatus.SOURCING.value,
            to_status=RequestStatus.AWARDED.value,
            extra={},
        )
        if not moved:
            raise ConflictError("版本已过期，或状态已被其他人改变，请刷新后重试")

        unit_price = deal_unit_price_fen(facts, supplier_id=str(supplier_id))
        supplier = await self._catalog.get_supplier(supplier_id)
        await self._events.append(
            request_id=request_id,
            event_type=QUOTATION_AWARDED,
            actor_id=actor_id,
            from_status=RequestStatus.SOURCING.value,
            to_status=RequestStatus.AWARDED.value,
            payload={
                "quotation_id": str(target_row.id),
                "supplier_id": str(supplier_id),
                "supplier_name": supplier.name if supplier is not None else None,
                # 成交单价：8.2 生成订单时取它当明细行单价（ER 图第 136-142 行）
                "deal_unit_price_fen": unit_price,
                "request_amount_fen": int(request.amount_total),
            },
        )
        await self._uow.commit()

        # ⚠️ **必须强制从库里重读**（`populate_existing=True`）：
        # `transition_status` 是 bulk UPDATE（`synchronize_session=False`），
        # 会话里那份对象仍是**改之前**的值；而它已经在身份映射里，
        # 普通 `get_request` 会把它原样还给你 —— 于是响应里状态还是 SOURCING，
        # 看起来像"定标没生效"（M5 的条件更新回读、M6 的步骤完成判定都踩过同一个坑）。
        reloaded = await self._requests.get_request(
            request_id,
            scope=await self._scope_for(
                actor_id=actor_id, roles=roles, department_id=department_id
            ),
            actor_id=actor_id,
            populate_existing=True,
        )
        # ⚠️ 同理：`mark_awarded` 也是 bulk UPDATE（`synchronize_session=False`），
        # 手里那条报价对象还写着 SUBMITTED —— 必须重读，否则响应里"选中的那家"
        # 看起来根本没被选中（而库里已经改了）。
        awarded_row = await self._quotations.get(target_row.id, populate_existing=True)
        assert awarded_row is not None  # 刚更新过，不可能没有

        return {
            "request": _summary_payload(reloaded),
            "quotation": _quotation_payload(
                awarded_row, supplier.name if supplier is not None else str(supplier_id)
            ),
            "deal_unit_price_fen": int(unit_price or 0),
        }

    async def award_idempotent(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
        supplier_id: uuid.UUID,
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """带幂等的定标（边界层走这个）。

        `payload` 哈希只含"这次请求的内容"：**同一个键带不同供应商 = 改了主意**，
        幂等层会返回 409（与提交那条同一个约定），而不是把第一次的结果又给一遍 ——
        后者会让用户以为"改成定标给另一家"成功了。

        scope 写成端点标识（`POST:/api/v1/requests/{id}/award`），
        与提交 / 撤回的 scope 一样：**同一把 key 在"提交"和"定标"之间必须互不干扰**。
        """

        async def work() -> dict[str, Any]:
            return await self.award(
                request_id=request_id,
                actor_id=actor_id,
                roles=roles,
                department_id=department_id,
                supplier_id=supplier_id,
                expected_version=expected_version,
            )

        result = await self._idempotency.run(
            actor_id=actor_id,
            scope=SCOPE_AWARD,
            key=idempotency_key,
            payload={"request_id": str(request_id), "supplier_id": str(supplier_id)},
            work=work,
            dump=lambda value: value,
        )
        return result.value

    # ------------------------------------------------------------------ 供应商合格性

    async def _usable_suppliers(self) -> dict[uuid.UUID, bool]:
        """当前**合格**的供应商 id 集合（未停用 + 资质未过期）。

        直接复用主数据那条查询（`only_active` + `only_qualified`）：
        "合格"的判据只允许有一处实现，否则两处不一致时必然有一处是越权或误伤。
        """
        suppliers = await self._catalog.list_suppliers()
        return {row.id: True for row in suppliers}

    async def _supplier_is_usable(self, supplier_id: uuid.UUID) -> bool:
        return supplier_id in await self._usable_suppliers()

    async def _supplier_name(self, supplier_id: uuid.UUID) -> str:
        supplier = await self._catalog.get_supplier(supplier_id)
        return supplier.name if supplier is not None else str(supplier_id)


def quotation_facts(
    records: Sequence[Any], *, usable: Mapping[uuid.UUID, bool]
) -> list[QuotationFacts]:
    """把报价记录转成领域层的 `QuotationFacts`（**适配**，不是业务逻辑）。

    放在协调者里而不是领域层：`domain/` 不认识 ORM 记录，只吃自己的 dataclass。
    也**不放仓库层**：那是应用层往领域层送数据时的形状转换，
    仓库的职责是"存取"，不是"决定规则要吃哪些字段"。

    `usable` 缺项按 **False** 处理（不可用）—— 兜底方向只能往严的一侧倒。
    """
    return [
        QuotationFacts(
            supplier_id=str(record.supplier_id),
            amount_fen=int(record.amount),
            status=record.status,
            valid_until=record.valid_until,
            supplier_usable=usable.get(record.supplier_id, False),
        )
        for record in records
    ]
