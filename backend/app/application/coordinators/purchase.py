"""采购申请用例（M5）：草稿、明细、提交、撤回、丢弃、时间线。

事务边界在这一层（仓库只 `flush()`），业务判断在 `domain/`。

============================ 三个反复出现的关键词 ============================
**可见范围**：每次读写主单都要先算出 `RequestScope` 并交给仓库 ——
范围外一律当"不存在"（404），不是 403。

**条件更新**：状态推进与改草稿都走 `version + status` 一起进 WHERE 的条件更新，
受影响行数为 0 就是 409。绝不"先查再比再写"。

**幂等**：提交、撤回这类"重复执行会改变结果"的动作必须带 `Idempotency-Key`；
同一个 key 重放返回上次响应，同键不同载荷 409。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 只在类型检查时导入，避免两个协调者互相 import 成环
    from app.application.coordinators.approval import ApprovalCoordinator

from app.application.idempotency import IdempotencyCoordinator
from app.application.ports import (
    AttachmentRepositoryPort,
    AttachmentView,
    BudgetLedgerPort,
    CatalogRepositoryPort,
    ObjectStorePort,
    PurchaseItemView,
    PurchaseRepositoryPort,
    PurchaseRequestView,
    RequestEventRepositoryPort,
    RequestEventView,
    UnitOfWork,
)
from app.database import SessionFactory
from app.domain import events as event_types
from app.domain.attachments import (
    AttachmentError,
    ImageDecoder,
    safe_kind,
    validate_upload,
)
from app.domain.budget_policy import (
    BudgetError,
    check_reservable,
    resolve_budget_year,
)
from app.domain.enums import RequestStatus
from app.domain.events import ATTACHMENT_ADDED
from app.domain.org import descendant_ids, parent_map
from app.domain.permissions import (
    RequestScope,
    resolve_request_scope,
)
from app.domain.purchase import (
    PATCHABLE_FIELDS,
    PurchaseError,
    check_submittable,
    parse_lines,
    parse_urgency,
)
from app.domain.state_machine import allowed_targets, can_transition
from app.errors import (
    BusinessError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from app.infrastructure.repositories.budget_repo import BudgetRepository
from app.infrastructure.repositories.catalog_repo import CatalogRepository
from app.infrastructure.repositories.event_repo import RequestEventRepository
from app.infrastructure.repositories.purchase_repo import PurchaseRepository

# 幂等 scope：**端点标识**，写死在这里。
# 同一把 key 在"提交"和"撤回"之间必须互不干扰 —— scope 混了的话，
# 撤回请求会拿到提交请求的响应，而且看起来一切正常。
SCOPE_SUBMIT = "POST:/api/v1/requests/{id}/submit"
SCOPE_WITHDRAW = "POST:/api/v1/requests/{id}/withdraw"

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class PurchaseCoordinator:
    def __init__(
        self,
        *,
        requests: PurchaseRepositoryPort,
        events: RequestEventRepositoryPort,
        catalog: CatalogRepositoryPort,
        idempotency: IdempotencyCoordinator,
        uow: UnitOfWork,
        budgets: BudgetLedgerPort | None = None,
        approval: ApprovalCoordinator | None = None,
        attachments: AttachmentRepositoryPort | None = None,
        objects: ObjectStorePort | None = None,
        image_decoder: ImageDecoder | None = None,
    ) -> None:
        self._requests = requests
        self._events = events
        self._catalog = catalog
        self._idempotency = idempotency
        self._uow = uow
        # 预算账本（M7）。提交时预留、撤回时释放，都与状态推进**同一个事务**。
        self._budgets = budgets
        # 审批链在**提交**时建。用可选参数注入而不是直接 import：
        # 两个协调者若互相构造就会绕成环，而这里只是"提交时需要它做一件事"。
        self._approval = approval
        # 附件（M9）。`objects` 是磁盘那一侧，`image_decoder` 是 Pillow 那一侧 ——
        # 三个都是可选注入，这样"M9 之前的用例"构造协调者时不用管它们。
        self._attachments = attachments
        self._objects = objects
        self._image_decoder = image_decoder

    # ------------------------------------------------------------------ 可见范围

    async def _scope_for(
        self, *, actor_id: uuid.UUID, roles: Sequence[str], department_id: uuid.UUID | None
    ) -> RequestScope:
        """算出这次查询的可见范围（含子部门）。

        只有 DEPARTMENT 档才需要查部门树 —— 另外两档（全公司 / 只看自己）
        用不上，多查一次就是白花一次往返。
        """
        if resolve_request_scope(
            roles, actor_id=actor_id, department_id=department_id
        ).sees_all or department_id is None:
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

    # ------------------------------------------------------------------ 草稿

    async def create_draft(
        self,
        *,
        actor_id: uuid.UUID,
        department_id: uuid.UUID | None,
        category_id: uuid.UUID,
        title: str,
        reason: str | None,
        urgency: Any,
        form_data: Mapping[str, Any] | None,
    ) -> PurchaseRequestView:
        """建草稿。

        `department_id` 取**登录用户所属部门**，不接受客户端指定 ——
        否则申请人可以挑一个"审批更松"的部门来提自己的单子。
        """
        if department_id is None:
            # 文案要同时说得通"保存草稿"与"提交"两个动作 —— 它们都会走到这里，
            # 而只说"无法提交"会让只想存草稿的人困惑（实测被用户抓到过）。
            raise BusinessError(
                "当前账号没有所属部门，无法创建采购申请",
                status_code=422,
                code="NO_DEPARTMENT",
            )
        if await self._catalog.get_category(category_id) is None:
            raise BusinessError(
                f"品类不存在：{category_id}", status_code=422, code="CATEGORY_NOT_FOUND"
            )

        cleaned_title = title.strip()
        if not cleaned_title:
            raise BusinessError("标题不能为空", status_code=422, code="INVALID_REQUEST")

        record = await self._requests.create_request(
            requester_id=actor_id,
            department_id=department_id,
            category_id=category_id,
            title=cleaned_title,
            reason=(reason or "").strip() or None,
            urgency=parse_urgency(urgency),
            form_data=form_data or {},
        )
        await self._events.append(
            request_id=record.id,
            event_type=event_types.REQUEST_CREATED,
            actor_id=actor_id,
            to_status=RequestStatus.DRAFT.value,
            payload={"code": record.code, "title": record.title},
        )
        await self._uow.commit()
        return record

    async def list_requests(
        self,
        *,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
        status: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> tuple[Sequence[PurchaseRequestView], int]:
        scope = await self._scope_for(
            actor_id=actor_id, roles=roles, department_id=department_id
        )
        if status is not None and status not in {item.value for item in RequestStatus}:
            raise BusinessError(
                f"状态只能是 {' / '.join(item.value for item in RequestStatus)}",
                status_code=422,
                code="INVALID_STATUS",
            )
        page_size = max(1, min(limit, MAX_PAGE_SIZE))
        return await self._requests.list_requests(
            scope=scope,
            actor_id=actor_id,
            status=status,
            limit=page_size,
            offset=max(0, offset),
        )

    async def get_request_detail(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
    ) -> tuple[PurchaseRequestView, Sequence[PurchaseItemView], Sequence[RequestEventView]]:
        """取一张单子 + 明细 + 时间线。

        可见范围外的 id 一律 404：**不告诉调用方"这东西存在"**。
        """
        scope = await self._scope_for(
            actor_id=actor_id, roles=roles, department_id=department_id
        )
        record = await self._requests.get_request(
            request_id, scope=scope, actor_id=actor_id
        )
        if record is None:
            raise NotFoundError(f"采购申请不存在：{request_id}")
        items = await self._requests.list_items(request_id)
        timeline = await self._events.list_for_request(request_id)
        return record, items, timeline

    async def patch_draft(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        department_id: uuid.UUID | None,
        roles: Sequence[str],
        expected_version: int,
        changes: Mapping[str, Any],
    ) -> PurchaseRequestView:
        """改草稿。只允许改 `PATCHABLE_FIELDS` 里的字段。

        **`amount_total` 与 `status` 不在其中**：前者由明细算出来，
        后者只能经状态机改。接受它们等于给客户端开后门改账。
        """
        await self._require_own_draft(
            request_id=request_id,
            actor_id=actor_id,
            roles=roles,
            department_id=department_id,
        )

        unknown = set(changes) - PATCHABLE_FIELDS
        if unknown:
            raise BusinessError(
                f"不允许修改这些字段：{sorted(unknown)}"
                "（金额由明细算出，状态只能经提交/撤回/丢弃改变）",
                status_code=422,
                code="FIELD_NOT_PATCHABLE",
            )

        values: dict[str, Any] = {}
        for field, value in changes.items():
            if field == "urgency":
                values["urgency"] = parse_urgency(value)
            elif field == "title":
                cleaned = str(value or "").strip()
                if not cleaned:
                    raise BusinessError("标题不能为空", status_code=422, code="INVALID_REQUEST")
                values["title"] = cleaned
            elif field == "reason":
                values["reason"] = (str(value).strip() or None) if value is not None else None
            elif field == "category_id":
                if await self._catalog.get_category(value) is None:
                    raise BusinessError(
                        f"品类不存在：{value}", status_code=422, code="CATEGORY_NOT_FOUND"
                    )
                values["category_id"] = value
            else:
                values["form_data"] = value

        if not values:
            raise BusinessError("没有需要修改的字段", status_code=422, code="EMPTY_PATCH")

        updated = await self._requests.apply_draft_patch(
            request_id=request_id,
            expected_version=expected_version,
            changes=values,
        )
        if not updated:
            raise ConflictError("版本已过期，或该申请已经不是草稿")

        await self._events.append(
            request_id=request_id,
            event_type=event_types.REQUEST_UPDATED,
            actor_id=actor_id,
            from_status=RequestStatus.DRAFT.value,
            to_status=RequestStatus.DRAFT.value,
            payload={"fields": sorted(values)},
        )
        await self._uow.commit()
        return await self._reload(request_id, actor_id=actor_id, roles=roles,
                                  department_id=department_id)

    async def replace_items(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        department_id: uuid.UUID | None,
        roles: Sequence[str],
        expected_version: int,
        raw_items: Sequence[Mapping[str, Any]],
    ) -> PurchaseRequestView:
        """覆盖式保存明细，并**重算 `amount_total`**。

        金额与明细在同一个事务里落库 —— 于是"明细加起来"和"题面金额"
        永远不会出现对不上的中间状态。
        """
        await self._require_own_draft(
            request_id=request_id,
            actor_id=actor_id,
            roles=roles,
            department_id=department_id,
        )

        # 明细校验失败 = 422（把 domain 的报错原样给用户，里面带行号）
        try:
            lines = parse_lines(raw_items)
        except PurchaseError as exc:
            raise BusinessError(str(exc), status_code=422, code="INVALID_ITEMS") from exc

        replaced = await self._requests.replace_items(
            request_id=request_id, expected_version=expected_version, lines=lines
        )
        if not replaced:
            raise ConflictError("版本已过期，或该申请已经不是草稿")

        # 刻意**不**为"换明细"写事件：事件流记的是**状态变更**与关键动作，
        # 草稿阶段反复改内容是常态，每次都记会把时间线淹掉。
        # 这也是 M5 验收"提交后时间线上出现两条事件"（创建 + 提交）的前提。
        await self._uow.commit()
        return await self._reload(request_id, actor_id=actor_id, roles=roles,
                                  department_id=department_id)

    # ------------------------------------------------------------------ 状态推进

    async def submit(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        department_id: uuid.UUID | None,
        roles: Sequence[str],
        expected_version: int,
        idempotency_key: str,
    ) -> Any:
        """提交（**需要幂等键**）。

        重复提交是会改变结果的动作：没有幂等保护时，客户端重试就会
        第二次推进状态（在 M6 还会第二次生成审批链）。所以它必须包在幂等里。
        """

        async def work() -> dict[str, Any]:
            return await self._do_transition(
                request_id=request_id,
                actor_id=actor_id,
                roles=roles,
                department_id=department_id,
                expected_version=expected_version,
                target=RequestStatus.IN_APPROVAL,
                event_type=event_types.REQUEST_SUBMITTED,
                require_items=True,
            )

        result = await self._idempotency.run(
            actor_id=actor_id,
            scope=SCOPE_SUBMIT,
            key=idempotency_key,
            # 载荷哈希只含"这次请求的内容"：同键但版本不同 = 不同载荷 → 409
            payload={"request_id": str(request_id), "version": expected_version},
            work=work,
            dump=lambda value: value,
        )
        return result.value

    async def withdraw(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        department_id: uuid.UUID | None,
        roles: Sequence[str],
        expected_version: int,
        idempotency_key: str,
    ) -> Any:
        async def work() -> dict[str, Any]:
            return await self._do_transition(
                request_id=request_id,
                actor_id=actor_id,
                roles=roles,
                department_id=department_id,
                expected_version=expected_version,
                target=RequestStatus.WITHDRAWN,
                event_type=event_types.REQUEST_WITHDRAWN,
                require_items=False,
            )

        result = await self._idempotency.run(
            actor_id=actor_id,
            scope=SCOPE_WITHDRAW,
            key=idempotency_key,
            payload={"request_id": str(request_id), "version": expected_version},
            work=work,
            dump=lambda value: value,
        )
        return result.value

    async def discard(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        department_id: uuid.UUID | None,
        roles: Sequence[str],
        expected_version: int,
    ) -> PurchaseRequestView:
        """丢弃草稿。不改变金额、不涉及并发抢占，所以不要求幂等键。"""
        await self._require_own_draft(
            request_id=request_id,
            actor_id=actor_id,
            roles=roles,
            department_id=department_id,
        )
        moved = await self._requests.transition_status(
            request_id=request_id,
            expected_version=expected_version,
            from_status=RequestStatus.DRAFT.value,
            to_status=RequestStatus.DISCARDED.value,
        )
        if not moved:
            raise ConflictError("版本已过期，或该申请已经不是草稿")

        await self._events.append(
            request_id=request_id,
            event_type=event_types.REQUEST_DISCARDED,
            actor_id=actor_id,
            from_status=RequestStatus.DRAFT.value,
            to_status=RequestStatus.DISCARDED.value,
        )
        await self._uow.commit()
        return await self._reload(request_id, actor_id=actor_id, roles=roles,
                                  department_id=department_id)

    # ------------------------------------------------------------------ 内部

    async def _do_transition(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
        expected_version: int,
        target: RequestStatus,
        event_type: str,
        require_items: bool,
    ) -> dict[str, Any]:
        """一次状态推进的全部动作。

        顺序很讲究：
          1. 先做**状态机检查**（转移合不合法）—— 不合法直接 409，别去动数据库
          2. 再做**业务前置检查**（明细、金额）—— 提交前的最后一道闸
          3. 最后才是条件更新；受影响行数为 0 = 版本过期或状态不对 → 409
        """
        record = await self._reload(
            request_id, actor_id=actor_id, roles=roles, department_id=department_id
        )
        current = record.status

        if not can_transition(current, target.value):
            raise ConflictError(
                f"当前状态是 {current}，不能变成 {target.value}"
                f"（可去的状态：{sorted(item.value for item in allowed_targets(current))}）"
            )

        if require_items:
            items = await self._requests.list_items(request_id)
            if not items:
                # 空明细要在**调用 domain 之前**拦住：`parse_lines` 对空列表的报错是
                # "至少要有 1 行明细"，而用户真正需要知道的是"这张单子还不能提交"
                raise BusinessError(
                    "申请没有任何明细，不能提交",
                    status_code=422,
                    code="NOT_SUBMITTABLE",
                )
            lines = parse_lines(
                [
                    {
                        "name": item.name,
                        "spec": item.spec,
                        "quantity": item.quantity,
                        "unit_price_fen": item.unit_price,
                    }
                    for item in items
                ]
            )
            try:
                check_submittable(
                    status=current,
                    lines=lines,
                    amount_total_fen=record.amount_total,
                )
            except PurchaseError as exc:
                raise BusinessError(str(exc), status_code=422, code="NOT_SUBMITTABLE") from exc

        extra: dict[str, Any] = {}
        budget: Any = None
        if target is RequestStatus.IN_APPROVAL:
            extra["submitted_at"] = _now()
            # ---- M7：提交时**认领**预算（此后这张单子占的就是这一条，不再改）----
            budget = await self._claim_budget(record=record)
            extra["budget_id"] = budget.id
        if target in {RequestStatus.WITHDRAWN, RequestStatus.DISCARDED}:
            extra["closed_at"] = _now()

        moved = await self._requests.transition_status(
            request_id=request_id,
            expected_version=expected_version,
            from_status=current,
            to_status=target.value,
            extra=extra,
        )
        if not moved:
            raise ConflictError("版本已过期，或状态已被其他人改变，请刷新后重试")

        await self._events.append(
            request_id=request_id,
            event_type=event_type,
            actor_id=actor_id,
            from_status=current,
            to_status=target.value,
            payload={"amount_fen": record.amount_total},
        )

        # ---- M7：提交时在**同一个事务里**预留预算（条件更新 + RESERVE 流水）----
        # 顺序无关紧要，但"同一事务"是硬要求：预留失败（预算不足 422）时，
        # 状态推进、事件、审批链都必须一起回滚 —— 否则会出现
        # "状态已进审批中、钱却没占住"这种查不出来的坏账。
        if target is RequestStatus.IN_APPROVAL and budget is not None:
            await self._reserve_budget(
                request_id=request_id,
                amount_fen=record.amount_total,
                budget=budget,
            )

        # ---- M6：提交时在**同一个事务里**建审批流与任务 ----
        # 顺序要紧：状态先推进到 IN_APPROVAL，再按品类策略建链。
        # 建链失败（例如某一级没人可审）会让整个事务回滚 —— 状态也一起退回去，
        # 不会留下"状态已进审批中、但没人能审"的卡死单据。
        if target is RequestStatus.IN_APPROVAL and self._approval is not None:
            await self._build_approval_chain(
                request_id=request_id,
                requester_id=record.requester_id,
                department_id=record.department_id,
                category_id=record.category_id,
                amount_fen=record.amount_total,
                urgency=record.urgency,
                actor_id=actor_id,
            )

        # ---- M7：撤回时释放预留（驳回那条在审批模块，见 ApprovalCoordinator.reject）----
        if target is RequestStatus.WITHDRAWN:
            await self._release_reservation(
                request_id=request_id, budget_id=record.budget_id
            )

        # 业务事务到这里结束：状态、事件、审批链在同一个事务里落库，**先提交再回读**。
        # 幂等层随后把响应回填到占位行（那是另一个事务，见 application/idempotency.py）。
        await self._uow.commit()
        view = await self._reload(
            request_id, actor_id=actor_id, roles=roles, department_id=department_id
        )
        return _request_to_dict(view)

    # ------------------------------------------------------------------ 预算（M7）

    async def _claim_budget(self, *, record: PurchaseRequestView) -> Any:
        """提交时认领预算：**部门 × 品类 × 提交当年**。

        三个口径写在这里（业务规则第 1 条）：

        1. **年度取提交时刻所在年度**（UTC）—— 草稿可以跨年躺着，占额度的是"提交"这个动作；
        2. **找到就固定下来**（`budget_id` 随状态推进一起写进主单），此后部门/品类再变也不改，
           这样"这笔申请当初占的是哪条预算"是事实而不是推断；
        3. **找不到 = 计划外 → 直接 422**（2026-10-03 定稿）。没配预算就没有控制依据，
           放行等于把"计划外采购"变成一条无人知晓的通道；与"超预算"同一个处置：
           先配 / 调预算，再由申请人重新提交。
        """
        assert self._budgets is not None
        year = resolve_budget_year(_now())
        budget = await self._budgets.find_budget(
            department_id=record.department_id,
            category_id=record.category_id,
            year=year,
        )
        if budget is None:
            category = await self._catalog.get_category(record.category_id)
            label = category.name if category is not None else str(record.category_id)
            raise BusinessError(
                f"{year} 年度「{label}」还没有预算，无法提交："
                "请先由管理员或财务配置这条预算，再重新提交"
                "（预算外采购应先履行预算调整程序）",
                status_code=422,
                code="NO_BUDGET",
            )
        return budget

    async def _reserve_budget(
        self, *, request_id: uuid.UUID, amount_fen: int, budget: Any
    ) -> None:
        """预留：条件更新 + `RESERVE` 流水（都在 `BudgetRepository` 里）。

        ⚠️ **判定完全在数据库里。** 这里只在**失败之后**用纯函数把"还差多少"算成人话 ——
        顺序反过来（先算够不够、再决定扣不扣）在并发下必然超额占用，
        而且不会报任何错（业务规则第 1 条）。
        """
        assert self._budgets is not None
        reserved = await self._budgets.reserve(
            budget_id=budget.id, request_id=request_id, amount_fen=amount_fen
        )
        if reserved:
            return

        # 0 行受影响 = 守卫没过。**必须重新读一次库**再解释原因：
        # 并发失败的那一方手里的对象是"本次请求开始时"的快照（甚至可能是身份映射里的旧值），
        # 拿它算会得出"钱明明够"的结论，错误码就退化成一句含糊的冲突（实测撞到过 409）。
        snapshot = await self._budgets.snapshot(budget.id)
        if snapshot is None:
            raise ConflictError("预算已不存在，请刷新后重试")
        total, reserved_now, spent_now = snapshot
        try:
            check_reservable(
                amount_total=total,
                amount_reserved=reserved_now,
                amount_spent=spent_now,
                amount_fen=amount_fen,
            )
        except BudgetError as exc:
            raise BusinessError(
                f"{exc}（预算调整后请重新提交）",
                status_code=422,
                code="BUDGET_INSUFFICIENT",
            ) from exc
        # 纯函数按读到的数字算"够"，条件更新却说"没扣成" → 说明 0 行是别的原因
        raise ConflictError("预算预留失败，请刷新后重试")

    async def _release_reservation(
        self, *, request_id: uuid.UUID, budget_id: uuid.UUID | None
    ) -> int:
        """释放这笔申请**还占着**的预留（驳回 / 撤回各调一次），返回释放金额（分）。

        `budget_id` 为 NULL 说明这是 **M7 之前提交的历史单据**：当时没有预留，
        也就没有东西可释放 —— 跳过而不是报错，否则老单子一撤回就变成 500。

        金额取**流水算出来的**还占着多少（不是主单金额）：主单金额是"申请多少"，
        流水才是"账上是多少"，两者在将来支持部分付款时会分叉。
        """
        if budget_id is None or self._budgets is None:
            return 0
        outstanding = await self._budgets.reserved_fen(request_id)
        if outstanding <= 0:
            return 0
        released = await self._budgets.release(
            budget_id=budget_id, request_id=request_id, amount_fen=outstanding
        )
        if not released:
            # 预留与流水对不上：宁可让整个事务回滚并报出来，也不要静默留下坏账
            raise ConflictError("预算释放失败：预留状态与流水不一致，请人工核对")
        return outstanding

    # ------------------------------------------------------------------ 附件（M9）

    async def upload_attachment(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
        filename: str | None,
        declared_type: str | None,
        content: bytes,
        kind: str | None = None,
    ) -> AttachmentView:
        """上传附件。六步校验的**第 1 步**在这里（要查库），其余五步在 domain 层。

        ============================ 第 1 步为什么是"可见范围"而不是"是不是我的" ============================
        附件挂在申请上，所以能不能传，取决于**能不能看到这张单子** —— 这与读详情
        用的是同一个判定（`get_request` 带 scope），也就不会出现"详情看不到、附件却能传"
        这种两个口径打架的情况。

        可见范围外一律 **404** 而不是 403：不告诉调用方"这张单子存在"
        （与全项目的越权语义一致，见 `boundaries/deps.py` 的文件头）。

        ============================ 磁盘与数据库的一致性 ============================
        顺序是：**先发键 → 再落盘 → 后写库**。任何一步失败都要把文件删掉：

            写库失败（回滚）  → 文件必须删，否则每次失败都留一个孤儿文件
            落盘失败          → 直接抛，库里什么都没有，无残留

        反过来（先写库再落盘）就会出现"库里有记录、文件不存在"——
        那种附件点下载必然 500，而且**没人知道有多少条**。
        """
        if self._attachments is None or self._objects is None:
            raise BusinessError(
                "附件功能未启用（未装配对象存储）", status_code=503, code="ATTACHMENT_DISABLED"
            )

        scope = await self._scope_for(
            actor_id=actor_id, roles=roles, department_id=department_id
        )
        record = await self._requests.get_request(
            request_id, scope=scope, actor_id=actor_id
        )
        if record is None:
            raise NotFoundError(f"采购申请不存在：{request_id}")

        try:
            validated = validate_upload(
                filename=filename,
                declared_type=declared_type,
                content=content,
                decode_image=self._image_decoder,
            )
        except AttachmentError as exc:
            raise BusinessError(str(exc), status_code=422, code=exc.code) from exc

        # 先发键：这样"后面任何一步失败"时手里都有键可以把文件删掉
        storage_key = self._objects.new_key(validated.suffix)
        self._objects.save(content, validated.suffix, key=storage_key)

        try:
            attachment = await self._attachments.create(
                request_id=request_id,
                kind=safe_kind(kind),
                original_filename=(filename or "").strip(),
                content_type=validated.content_type,
                size_bytes=validated.size_bytes,
                sha256=validated.sha256,
                storage_key=storage_key,
                uploaded_by=actor_id,
            )
            # 时间线上记一笔：**不改变状态**（from/to 都是当前状态），
            # 只是让"这份报价单什么时候挂上来的"有据可查。
            # 注意它不会进审批链视图（那个读 approval_tasks，是另一套数据）。
            await self._events.append(
                request_id=request_id,
                event_type=ATTACHMENT_ADDED,
                actor_id=actor_id,
                from_status=record.status,
                to_status=record.status,
                payload={
                    "attachment_id": str(attachment.id),
                    "filename": attachment.original_filename,
                    "size_bytes": attachment.size_bytes,
                    "kind": attachment.kind,
                    # 图片尺寸只在这一刻算得出来（解码过），顺手记进事件快照。
                    # 不落库到 attachments：那是能重算的派生数据，存一份就多一处会漂移。
                    "image_size": list(validated.image_size) if validated.image_size else None,
                    # 类型是从扩展名补出来的吗？记下来，"为什么这份文件被判成 PNG"就有据可查。
                    "type_inferred": validated.type_inferred,
                },
            )
            await self._uow.commit()
        except Exception:
            # 库这一侧没成，磁盘上那份就是垃圾 —— 删掉再抛。
            # 删文件本身不抛异常（`delete` 返回 bool），免得把真实错误盖掉。
            self._objects.delete(storage_key)
            raise

        return attachment

    async def list_attachments(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
    ) -> Sequence[AttachmentView]:
        """某张申请的附件列表（新的在前）。可见范围外 → 404（与上传同一条判定）。"""
        if self._attachments is None:
            raise BusinessError(
                "附件功能未启用（未装配对象存储）", status_code=503, code="ATTACHMENT_DISABLED"
            )
        scope = await self._scope_for(
            actor_id=actor_id, roles=roles, department_id=department_id
        )
        if (
            await self._requests.get_request(request_id, scope=scope, actor_id=actor_id)
            is None
        ):
            raise NotFoundError(f"采购申请不存在：{request_id}")
        return await self._attachments.list_for_request(request_id)

    async def open_attachment(
        self,
        *,
        attachment_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
    ) -> tuple[AttachmentView, bytes]:
        """取附件内容（下载用）。返回 `(元数据, 字节)`。

        ============================ 这里为什么是 403 而不是 404 ============================
        与"换 id 读别人的申请 → 404"看着矛盾，其实不是一回事：

            申请：id 是**猜得到的资源标识**，返回 404 是为了不泄露"它存在"
            附件：id 同样是 uuid，但**这一条附件确实存在于库里**，
                  调用方拿到的是"可见但无权"这一档

        所以判定分两层，与 `boundaries/deps.py` 的越权语义完全对齐：

            附件不存在（或已软删除）        → 404
            附件在，但它挂的申请看不到      → **403**
            申请也看得到                    → 200 内容

        元数据里的 `content_type` 用**入库时校验过的那一个**，
        不用请求里带的：那是服务端在"已经看过字节"之后写下来的事实。
        """
        if self._attachments is None or self._objects is None:
            raise BusinessError(
                "附件功能未启用（未装配对象存储）", status_code=503, code="ATTACHMENT_DISABLED"
            )
        attachment = await self._attachments.get(attachment_id)
        if attachment is None:
            raise NotFoundError(f"附件不存在：{attachment_id}")

        scope = await self._scope_for(
            actor_id=actor_id, roles=roles, department_id=department_id
        )
        if (
            await self._requests.get_request(
                attachment.request_id, scope=scope, actor_id=actor_id
            )
            is None
        ):
            raise PermissionDeniedError("无权访问该附件")

        return attachment, self._objects.load(attachment.storage_key)

    async def _build_approval_chain(
        self,
        *,
        request_id: uuid.UUID,
        requester_id: uuid.UUID,
        department_id: uuid.UUID,
        category_id: uuid.UUID,
        amount_fen: int,
        urgency: str,
        actor_id: uuid.UUID,
    ) -> None:
        """读品类策略（连同版本号）→ 建链。

        **`policy_snapshot` 存的是"提交那一刻的原件"**，不是品类 id：
        策略是会被改的，现查等于拿今天的规则解释三个月前的申请。
        """
        category = await self._catalog.get_category(category_id)
        if category is None:  # pragma: no cover - 建草稿时已校验过存在
            raise BusinessError(f"品类不存在：{category_id}", status_code=422)

        policy = category.approval_policy or {}
        assert self._approval is not None
        await self._approval.build_chain(
            request_id=request_id,
            requester_id=requester_id,
            department_id=department_id,
            category_id=category_id,
            amount_fen=amount_fen,
            urgency=urgency,
            policy=policy,
            policy_version=category.version,
            actor_id=actor_id,
        )

    async def _require_own_draft(
        self,
        *,
        request_id: uuid.UUID,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
    ) -> PurchaseRequestView:
        """写草稿前的两道门：可见范围内（否则 404）+ 是自己的草稿（否则 403）。

        "不是自己的"给 403 而不是 404：这张单子他**本来就看得见**
        （部门主管能看见本部门的单子），只是不许改 —— 两种语义要分清。
        """
        scope = await self._scope_for(
            actor_id=actor_id, roles=roles, department_id=department_id
        )
        record = await self._requests.get_request(
            request_id, scope=scope, actor_id=actor_id
        )
        if record is None:
            raise NotFoundError(f"采购申请不存在：{request_id}")
        if record.requester_id != actor_id:
            raise BusinessError(
                "只能修改自己提交的申请",
                status_code=403,
                code="FORBIDDEN",
            )
        if record.status != RequestStatus.DRAFT.value:
            raise ConflictError(
                f"只有草稿可以修改，当前状态是 {record.status}"
            )
        return record

    async def _reload(
        self,
        request_id: uuid.UUID,
        *,
        actor_id: uuid.UUID,
        roles: Sequence[str],
        department_id: uuid.UUID | None,
    ) -> PurchaseRequestView:
        """回读主单——**用一个全新的会话**。

        为什么不在当前会话里查：条件更新/批量 UPDATE **不会**刷新会话中已经加载的对象，
        而 `expire_on_commit=False`（异步下必须这么设）又不会在提交时让它过期。
        于是"提交成功、响应里却还是 DRAFT"这种假象就会出现 —— 实测踩过。

        在旧的 `expire_all()` 方案上还踩了第二个坑：异步会话里惰性刷新会抛
        `MissingGreenlet`，而它被 503 处理器当成了"数据库不可用"。
        新开会话最直接：它没有任何缓存，读到的必然是已提交的真实状态。
        """
        async with SessionFactory() as session:
            reader = PurchaseCoordinator(
                requests=PurchaseRepository(session),
                events=RequestEventRepository(session),
                catalog=CatalogRepository(session),
                idempotency=self._idempotency,
                uow=session,
                budgets=BudgetRepository(session),
            )
            record, _, _ = await reader.get_request_detail(
                request_id=request_id,
                actor_id=actor_id,
                roles=roles,
                department_id=department_id,
            )
            # 把对象从会话里"摘"出来：会话一关，未过期的属性就取不到了。
            # 这里读的都是标量列，`expunge` 之后照样能用。
            session.expunge(record)
            return record


def _now() -> datetime:
    return datetime.now(UTC)


def _request_to_dict(record: PurchaseRequestView) -> dict[str, Any]:
    """把主单转成可 JSON 序列化的 dict。

    这是**幂等重放时存下来、之后原样返回**的东西，所以字段名必须与边界层
    `RequestOut` 的**字段名**一致（不是它的响应别名）——
    重放回来的 dict 走的是模型校验，键名对不上就会 500，
    而那恰恰是幂等最该正常工作的时候（第一次超时、客户端重试）。

    单位仍然是「分」：字典里的 `amount_total` 就是分，
    带 `_fen` 的是**响应**里的名字（给外部调用方看的）。
    """
    return {
        "id": str(record.id),
        "code": record.code,
        "requester_id": str(record.requester_id),
        "department_id": str(record.department_id),
        "category_id": str(record.category_id),
        "title": record.title,
        "reason": record.reason,
        "amount_total": record.amount_total,
        "urgency": record.urgency,
        "status": record.status,
        "version": record.version,
        "form_data": record.form_data,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "submitted_at": record.submitted_at.isoformat() if record.submitted_at else None,
        "closed_at": record.closed_at.isoformat() if record.closed_at else None,
    }
