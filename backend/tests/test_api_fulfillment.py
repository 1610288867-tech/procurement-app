"""订单与收货的集成测试（M10 / 8.2）：真数据库 + 真 HTTP。

验收项对齐 `PROGRESS.md` 的 M10 / 8.2 清单：

    由定标结果生成订单（金额 = Σ(单价×数量)，单价取定标成交单价）
    收货数量超过订单数量 → **422**
    全部收齐 → 主单 `RECEIVED`；没收齐 → **停在 `ORDERED`**

============================ 为什么不跳过前面几段 ============================
订单的前置是"已定标"、收货的前置是"已下单"，所以每个用例都要跑一段**真实链路**：

    建草稿 → 提交 → 审批通过 → 登记报价 → 定标 → 下单

直接把状态改成 `AWARDED` 会绕过"定标真的发生过"这件事 ——
而订单的供应商与单价**正是从定标结果来的**，绕过它等于没验那条口径。
`_drive_to_awarded` 与 `_place_order` 把这两段收在一处。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.database import SessionFactory
from app.domain.enums import RequestStatus
from app.domain.permissions import (
    ROLE_DEPT_HEAD,
    ROLE_DEPT_MANAGER,
    ROLE_FINANCE,
    ROLE_PROCUREMENT,
    ROLE_RECEIVER,
    ROLE_REQUESTER,
)
from app.infrastructure.records.approval import ApprovalFlowRecord, ApprovalTaskRecord
from app.infrastructure.records.attachment import AttachmentRecord
from app.infrastructure.records.auth import UserRecord
from app.infrastructure.records.catalog import (
    BudgetRecord,
    BudgetTransactionRecord,
    DepartmentRecord,
    PurchaseCategoryRecord,
    SupplierRecord,
)
from app.infrastructure.records.fulfillment import (
    GoodsReceiptLineRecord,
    GoodsReceiptRecord,
    PurchaseOrderLineRecord,
    PurchaseOrderRecord,
)
from app.infrastructure.records.purchase import (
    IdempotencyRecord,
    PurchaseItemRecord,
    PurchaseRequestRecord,
    RequestEventRecord,
)
from app.infrastructure.records.sourcing import QuotationRecord
from app.main import app
from tests.conftest import Account, UserFactory, create_budget, login_as

pytestmark = pytest.mark.integration

#: 比价线调高：8.2 的用例不关心"要不要三家"，只关心订单与收货。
HIGH_THRESHOLD = 100_000_000

FULL_POLICY = {
    "steps": [
        {"order": 10, "role": "DEPT_MANAGER", "mode": "SINGLE", "when": {"gte": ["amount_fen", 0]}},
        {"order": 20, "role": "DEPT_HEAD", "mode": "SINGLE", "when": {"gte": ["amount_fen", 100_000]}},
    ],
}


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


@pytest.fixture(autouse=True)
def threshold(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[int]:
    monkeypatch.setenv("SOURCING_THRESHOLD_FEN", str(HIGH_THRESHOLD))
    from app.config import get_settings

    get_settings.cache_clear()
    yield HIGH_THRESHOLD
    get_settings.cache_clear()


class World:
    def __init__(self) -> None:
        self.departments: list[uuid.UUID] = []
        self.categories: list[uuid.UUID] = []
        self.budgets: list[uuid.UUID] = []
        self.requests: list[uuid.UUID] = []
        self.actors: list[uuid.UUID] = []
        self.suppliers: list[uuid.UUID] = []
        self.category: dict = {}
        self.admin_client: AsyncClient
        self.requester: AsyncClient
        self.manager: AsyncClient
        self.head: AsyncClient
        self.procurement: AsyncClient
        self.receiver: AsyncClient
        self._factory: UserFactory

    async def make(
        self, roles: list[str], department_id: uuid.UUID | None
    ) -> tuple[AsyncClient, Account]:
        account = await self._factory(roles=roles, department_id=department_id)
        self.actors.append(account.id)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        await login_as(client, account)
        return client, account

    async def add_supplier(self, name: str = "订单供应商") -> dict:
        response = await self.admin_client.post(
            "/api/v1/admin/suppliers",
            json={"name": f"{name}-{uuid.uuid4().hex[:6]}", "code": _code("SUP")},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        self.suppliers.append(uuid.UUID(body["id"]))
        return body

    async def purge(self) -> None:
        """按外键顺序清理。

        收货明细 → 收货单 → 订单明细 → 订单 → 报价 → 申请 → …（`goods_receipt_lines`
        对 `purchase_order_lines` 是 RESTRICT，所以**明细必须比订单先删**）。
        """
        async with SessionFactory() as session:
            if self.requests:
                receipt_ids = select(GoodsReceiptRecord.id).where(
                    GoodsReceiptRecord.request_id.in_(self.requests)
                )
                await session.execute(
                    delete(GoodsReceiptLineRecord).where(
                        GoodsReceiptLineRecord.receipt_id.in_(receipt_ids)
                    )
                )
                await session.execute(
                    delete(GoodsReceiptRecord).where(
                        GoodsReceiptRecord.request_id.in_(self.requests)
                    )
                )
                order_ids = select(PurchaseOrderRecord.id).where(
                    PurchaseOrderRecord.request_id.in_(self.requests)
                )
                await session.execute(
                    delete(PurchaseOrderLineRecord).where(
                        PurchaseOrderLineRecord.order_id.in_(order_ids)
                    )
                )
                await session.execute(
                    delete(PurchaseOrderRecord).where(
                        PurchaseOrderRecord.request_id.in_(self.requests)
                    )
                )
                await session.execute(
                    delete(QuotationRecord).where(
                        QuotationRecord.request_id.in_(self.requests)
                    )
                )
                await session.execute(
                    delete(AttachmentRecord).where(
                        AttachmentRecord.request_id.in_(self.requests)
                    )
                )
                await session.execute(
                    delete(ApprovalTaskRecord).where(
                        ApprovalTaskRecord.request_id.in_(self.requests)
                    )
                )
                await session.execute(
                    delete(ApprovalFlowRecord).where(
                        ApprovalFlowRecord.request_id.in_(self.requests)
                    )
                )
                await session.execute(
                    delete(BudgetTransactionRecord).where(
                        BudgetTransactionRecord.request_id.in_(self.requests)
                    )
                )
                await session.execute(
                    delete(RequestEventRecord).where(
                        RequestEventRecord.request_id.in_(self.requests)
                    )
                )
                await session.execute(
                    delete(PurchaseItemRecord).where(
                        PurchaseItemRecord.request_id.in_(self.requests)
                    )
                )
                await session.execute(
                    delete(PurchaseRequestRecord).where(
                        PurchaseRequestRecord.id.in_(self.requests)
                    )
                )
            if self.actors:
                await session.execute(
                    delete(IdempotencyRecord).where(
                        IdempotencyRecord.actor_id.in_(self.actors)
                    )
                )
                await session.execute(
                    delete(ApprovalTaskRecord).where(
                        ApprovalTaskRecord.decided_by.in_(self.actors)
                    )
                )
                await session.execute(
                    delete(QuotationRecord).where(
                        QuotationRecord.created_by.in_(self.actors)
                    )
                )
                await session.execute(
                    delete(PurchaseOrderRecord).where(
                        PurchaseOrderRecord.created_by.in_(self.actors)
                    )
                )
            if self.suppliers:
                await session.execute(
                    delete(SupplierRecord).where(SupplierRecord.id.in_(self.suppliers))
                )
            if self.budgets:
                await session.execute(
                    delete(BudgetRecord).where(BudgetRecord.id.in_(self.budgets))
                )
            if self.categories:
                await session.execute(
                    delete(PurchaseCategoryRecord).where(
                        PurchaseCategoryRecord.id.in_(self.categories)
                    )
                )
            if self.departments:
                await session.execute(
                    delete(UserRecord).where(
                        UserRecord.username.like("t-%"),
                        UserRecord.department_id.in_(self.departments),
                    )
                )
                await session.execute(
                    delete(DepartmentRecord).where(
                        DepartmentRecord.id.in_(self.departments)
                    )
                )
            await session.commit()


@pytest.fixture
async def world(
    admin_client: AsyncClient,
    user_factory: UserFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[World]:
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()

    state = World()
    state.admin_client = admin_client
    state._factory = user_factory

    department = (
        await admin_client.post(
            "/api/v1/admin/departments", json={"name": "履约测试部", "code": _code("TDEP")}
        )
    ).json()
    state.departments.append(uuid.UUID(department["id"]))
    dept_id = uuid.UUID(department["id"])

    category = (
        await admin_client.post(
            "/api/v1/admin/categories",
            json={
                "name": "履约测试品类",
                "code": _code("TCAT"),
                "approval_policy": FULL_POLICY,
            },
        )
    ).json()
    state.categories.append(uuid.UUID(category["id"]))
    state.category = category

    budget = await create_budget(
        admin_client,
        department_id=department["id"],
        category_id=category["id"],
        amount_fen=100_000_000,
    )
    state.budgets.append(uuid.UUID(budget["id"]))

    # 审批人必须先造好（建链时缺人会 422）
    state.requester, _ = await state.make([ROLE_REQUESTER], dept_id)
    state.manager, _ = await state.make([ROLE_DEPT_MANAGER], dept_id)
    state.head, _ = await state.make([ROLE_DEPT_HEAD], dept_id)
    state.procurement, _ = await state.make([ROLE_PROCUREMENT], dept_id)
    state.receiver, _ = await state.make([ROLE_RECEIVER], dept_id)

    try:
        yield state
    finally:
        for client in (
            state.requester,
            state.manager,
            state.head,
            state.procurement,
            state.receiver,
        ):
            await client.aclose()
        await state.purge()
        get_settings.cache_clear()


# ------------------------------------------------------------------ 辅助


async def _approve_my_level(client: AsyncClient, request_id: str) -> None:
    todo = (await client.get("/api/v1/approvals/tasks")).json()
    mine = [item for item in todo["items"] if item["request"]["id"] == request_id]
    assert mine, f"这个账号的待办里没有这张单子：{request_id}"
    task = mine[0]["task"]
    response = await client.post(
        f"/api/v1/approvals/tasks/{task['id']}/approve",
        json={"version": task["version"]},
    )
    assert response.status_code == 200, response.text


async def _drive_to_awarded(
    world: World, *, amount_fen: int = 50_000, deal_unit_price_fen: int = 2_000
) -> dict:
    """跑完整链路推到 `AWARDED`：提交 → 审批 → 报价 → 定标。

    返回定标响应体（含选中报价与成交单价）。
    """
    draft = await world.requester.post(
        "/api/v1/requests", json={"category_id": world.category["id"], "title": "履约单"}
    )
    body = draft.json()
    world.requests.append(uuid.UUID(body["id"]))

    items = await world.requester.put(
        f"/api/v1/requests/{body['id']}/items",
        json={
            "version": body["version"],
            "items": [
                {"name": "A4 复印纸", "quantity": 10, "unit_price_fen": 2_500},
                {"name": "订书机", "quantity": 2, "unit_price_fen": 4_000},
            ],
        },
    )
    assert items.status_code == 200, items.text

    submitted = await world.requester.post(
        f"/api/v1/requests/{body['id']}/submit",
        json={"version": items.json()["version"]},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert submitted.status_code == 200, submitted.text
    await _approve_my_level(world.manager, body["id"])
    if amount_fen >= 100_000:
        await _approve_my_level(world.head, body["id"])

    detail = (await world.requester.get(f"/api/v1/requests/{body['id']}")).json()
    assert detail["request"]["status"] == RequestStatus.SOURCING.value

    supplier = await world.add_supplier("中标供应商")
    quoted = await world.procurement.post(
        f"/api/v1/requests/{body['id']}/quotations",
        json={"supplier_id": supplier["id"], "amount_fen": deal_unit_price_fen},
    )
    assert quoted.status_code == 201, quoted.text

    version = (await world.requester.get(f"/api/v1/requests/{body['id']}")).json()["request"][
        "version"
    ]
    awarded = await world.procurement.post(
        f"/api/v1/requests/{body['id']}/award",
        json={"supplier_id": supplier["id"], "version": version},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert awarded.status_code == 200, awarded.text
    body_out = awarded.json()
    body_out["supplier"] = supplier
    return body_out


async def _place_order(client: AsyncClient, request_id: str, version: int, *, key: str | None = None):
    return await client.post(
        f"/api/v1/requests/{request_id}/orders",
        json={"version": version},
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
    )


async def _receive(
    client: AsyncClient,
    request_id: str,
    accepted: dict[str, int],
    version: int,
    *,
    note: str | None = None,
    key: str | None = None,
):
    return await client.post(
        f"/api/v1/requests/{request_id}/receipts",
        json={"accepted": accepted, "note": note, "version": version},
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
    )


async def _current_version(request_id: str) -> int:
    async with SessionFactory() as session:
        return int(
            (
                await session.execute(
                    select(PurchaseRequestRecord.version).where(
                        PurchaseRequestRecord.id == uuid.UUID(request_id)
                    )
                )
            ).scalar_one()
        )


# ================================================================ 1. 下单


async def test_order_is_built_from_the_awarded_quotation(world: World) -> None:
    """**8.2 主线：订单金额 = Σ(单价×数量)，单价取定标成交单价。**

    申请明细是两行（10×25 元 + 2×40 元 = 330 元），成交单价 20 元 →
    订单金额 = (10+2)×20 = 240 元 = **24000 分**。
    **订单金额小于申请金额是正常的**（询价压价），这正是要验的口径。
    """
    awarded = await _drive_to_awarded(world)
    request_id = awarded["request"]["id"]

    response = await _place_order(world.procurement, request_id, awarded["request"]["version"])
    assert response.status_code == 201, response.text
    body = response.json()

    order = body["order"]
    assert order["status"] == RequestStatus.ORDERED.value
    assert order["request_status"] == RequestStatus.ORDERED.value
    assert order["supplier_id"] == awarded["supplier"]["id"], "供应商必须来自定标结果"
    assert order["amount_fen"] == 24_000, "订单金额 = Σ(明细行金额)，单价取成交单价"
    assert order["order_no"].startswith("PO-")

    lines = body["lines"]
    assert [line["name"] for line in lines] == ["A4 复印纸", "订书机"]
    assert [line["unit_price_fen"] for line in lines] == [2_000, 2_000], "整单统一取成交单价"
    assert [line["quantity"] for line in lines] == [10, 2]
    assert sum(line["amount_fen"] for line in lines) == order["amount_fen"], (
        "订单金额必须等于明细行金额之和 —— 这是三单匹配与预算结转的基准"
    )

    # 明细是**快照**：带走了名称、规格与数量（不指向申请明细）
    assert all(line["item_id"] for line in lines), "item_id 只作来源线索（软引用）"


async def test_order_is_refused_before_award(world: World) -> None:
    """还没定标就下单 → **409**（不是 500、也不是默默按申请金额下单）。"""
    draft = await world.requester.post(
        "/api/v1/requests", json={"category_id": world.category["id"], "title": "未定标"}
    )
    body = draft.json()
    world.requests.append(uuid.UUID(body["id"]))

    response = await _place_order(world.procurement, body["id"], body["version"])
    assert response.status_code == 409, response.text
    assert "不能下单" in response.json()["error"]["message"]


async def test_second_order_is_refused(world: World) -> None:
    """同一张申请再下一次单 → **409**（`UNIQUE(request_id)` + 状态校验两层）。"""
    awarded = await _drive_to_awarded(world)
    request_id = awarded["request"]["id"]
    first = await _place_order(world.procurement, request_id, awarded["request"]["version"])
    assert first.status_code == 201, first.text

    version = await _current_version(request_id)
    second = await _place_order(world.procurement, request_id, version)
    assert second.status_code == 409, second.text


async def test_order_replay_with_the_same_key_returns_the_first_response(world: World) -> None:
    """同键重放下单 → 拿回第一次的响应（**订单号相同**，不会生成第二张订单）。"""
    awarded = await _drive_to_awarded(world)
    request_id = awarded["request"]["id"]
    key = uuid.uuid4().hex

    first = await _place_order(
        world.procurement, request_id, awarded["request"]["version"], key=key
    )
    assert first.status_code == 201, first.text
    replay = await _place_order(
        world.procurement, request_id, awarded["request"]["version"], key=key
    )
    assert replay.status_code == 201, replay.text
    assert replay.json() == first.json(), "同键重放必须逐字节相同"

    async with SessionFactory() as session:
        count = len(
            list(
                (
                    await session.scalars(
                        select(PurchaseOrderRecord.id).where(
                            PurchaseOrderRecord.request_id == uuid.UUID(request_id)
                        )
                    )
                ).all()
            )
        )
    assert count == 1, "重放不该生成第二张订单"


async def test_order_requires_procurement_role(world: World) -> None:
    """申请人下单 → **403**。"""
    awarded = await _drive_to_awarded(world)
    response = await _place_order(
        world.requester, awarded["request"]["id"], awarded["request"]["version"]
    )
    assert response.status_code == 403, response.text


async def test_order_can_be_read_by_request_and_by_id(world: World) -> None:
    """两个读接口：按申请查、按订单 id 查 —— 内容一致。"""
    awarded = await _drive_to_awarded(world)
    request_id = awarded["request"]["id"]
    placed = await _place_order(
        world.procurement, request_id, awarded["request"]["version"]
    )
    order_id = placed.json()["order"]["id"]

    by_request = await world.requester.get(f"/api/v1/requests/{request_id}/orders")
    assert by_request.status_code == 200, by_request.text
    assert by_request.json()["order"]["id"] == order_id

    by_id = await world.requester.get(f"/api/v1/orders/{order_id}")
    assert by_id.status_code == 200, by_id.text
    assert by_id.json() == by_request.json()
    assert by_id.json()["receipt"] is None, "还没收货 → receipt 为 null（不是 404）"


async def test_order_query_returns_null_when_there_is_none(world: World) -> None:
    """没下过单的申请查订单 → **null**（不是 404）：详情页要能一屏显示"还没下单"。"""
    awarded = await _drive_to_awarded(world)
    response = await world.requester.get(
        f"/api/v1/requests/{awarded['request']['id']}/orders"
    )
    assert response.status_code == 200, response.text
    assert response.json() is None


# ================================================================ 2. 收货


async def test_receipt_queue_lists_ordered_requests(world: World) -> None:
    """待收货队列：只列 `ORDERED` 的单子；收货人与采购都看得到。"""
    awarded = await _drive_to_awarded(world)
    request_id = awarded["request"]["id"]
    placed = await _place_order(
        world.procurement, request_id, awarded["request"]["version"]
    )
    assert placed.status_code == 201, placed.text

    queue = await world.receiver.get("/api/v1/receipts/queue")
    assert queue.status_code == 200, queue.text
    assert request_id in [item["id"] for item in queue.json()["items"]]

    # 申请人看不到这个队列（不是他的工作台）
    denied = await world.requester.get("/api/v1/receipts/queue")
    assert denied.status_code == 403, denied.text


async def test_full_receipt_moves_request_to_received(world: World) -> None:
    """**8.2 主线：全部收齐 → 主单 `RECEIVED`。**"""
    awarded = await _drive_to_awarded(world)
    request_id = awarded["request"]["id"]
    placed = await _place_order(
        world.procurement, request_id, awarded["request"]["version"]
    )
    lines = placed.json()["lines"]
    accepted = {line["id"]: line["quantity"] for line in lines}

    version = await _current_version(request_id)
    response = await _receive(world.receiver, request_id, accepted, version)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["receipt"]["fully_received"] is True
    assert body["order"]["status"] == RequestStatus.RECEIVED.value
    assert body["order"]["request_status"] == RequestStatus.RECEIVED.value

    # 时间线多一条 GOODS_RECEIVED，且状态迁移是 ORDERED → RECEIVED
    timeline = (await world.requester.get(f"/api/v1/requests/{request_id}/timeline")).json()
    last = timeline["events"][-1]
    assert last["event_type"] == "GOODS_RECEIVED"
    assert last["from_status"] == RequestStatus.ORDERED.value
    assert last["to_status"] == RequestStatus.RECEIVED.value
    assert last["payload"]["fully_received"] is True


async def test_partial_receipt_is_refused_with_the_short_lines(world: World) -> None:
    """**只收一部分 → 422**，并说清差哪几行、差多少。

    ============================ 为什么是拒绝而不是"收下但主单不动" ============================
    领域设计把部分收货列进"第一版先砍掉这些"，而状态机里**没有**
    `PARTIALLY_RECEIVED`（`ORDERED → RECEIVED` 是唯一一条边）。所以"收一半"只有两种落法：

        允许 → 主单停在 ORDERED，但订单上写着已收货、收货单也建了 ——
               **模型禁止的组合被真实数据造出来**，而且它走不到任何终态
               （再收一次会被"已经收过货"挡住）；
        拒绝 → 用户改完再提交，主单与订单**一起**进 RECEIVED。

    选后者。这条用例同时钉住"拒绝时要说清差哪几行"——
    否则用户只能一行一行试，而每次试都是一次被拒的请求。
    """
    awarded = await _drive_to_awarded(world)
    request_id = awarded["request"]["id"]
    placed = await _place_order(
        world.procurement, request_id, awarded["request"]["version"]
    )
    lines = placed.json()["lines"]
    # 第一行收齐、第二行一件没收
    accepted = {lines[0]["id"]: lines[0]["quantity"], lines[1]["id"]: 0}

    version = await _current_version(request_id)
    response = await _receive(world.receiver, request_id, accepted, version)
    assert response.status_code == 422, response.text
    message = response.json()["error"]["message"]
    assert response.json()["error"]["code"] == "RECEIPT_NOT_COMPLETE"
    assert lines[1]["name"] in message, "要说清是**哪一行**没收齐"
    assert "0" in message and str(lines[1]["quantity"]) in message, "还要说清差多少"
    assert lines[0]["name"] not in message, "已经收齐的行不该出现在问题清单里"

    # 失败不留半截状态：没有收货单、订单仍是 ORDERED、版本没推进
    async with SessionFactory() as session:
        assert (
            await session.scalars(
                select(GoodsReceiptRecord.id).where(
                    GoodsReceiptRecord.request_id == uuid.UUID(request_id)
                )
            )
        ).first() is None
        order_status = (
            await session.scalars(
                select(PurchaseOrderRecord.status).where(
                    PurchaseOrderRecord.request_id == uuid.UUID(request_id)
                )
            )
        ).one()
    assert order_status == RequestStatus.ORDERED.value
    assert (await _current_version(request_id)) == version


async def test_over_receipt_is_refused_with_both_numbers(world: World) -> None:
    """**验收：收货数量超过订单数量 → 422**，并说清收了多少 / 订单多少。"""
    awarded = await _drive_to_awarded(world)
    request_id = awarded["request"]["id"]
    placed = await _place_order(
        world.procurement, request_id, awarded["request"]["version"]
    )
    lines = placed.json()["lines"]
    accepted = {lines[0]["id"]: lines[0]["quantity"] + 1, lines[1]["id"]: lines[1]["quantity"]}

    version = await _current_version(request_id)
    response = await _receive(world.receiver, request_id, accepted, version)
    assert response.status_code == 422, response.text
    message = response.json()["error"]["message"]
    assert str(lines[0]["quantity"] + 1) in message and str(lines[0]["quantity"]) in message
    assert lines[0]["name"] in message

    # 失败不留半截状态：订单仍是 ORDERED、没有收货单
    async with SessionFactory() as session:
        assert (
            await session.scalars(
                select(GoodsReceiptRecord.id).where(
                    GoodsReceiptRecord.request_id == uuid.UUID(request_id)
                )
            )
        ).first() is None
    assert (await _current_version(request_id)) == version, "失败不该推进版本"


async def test_receipt_reports_all_bad_lines_at_once(world: World) -> None:
    """两行都填错 → 一次说完（界面要能一次标出所有出错的行）。"""
    awarded = await _drive_to_awarded(world)
    request_id = awarded["request"]["id"]
    placed = await _place_order(
        world.procurement, request_id, awarded["request"]["version"]
    )
    lines = placed.json()["lines"]
    accepted = {line["id"]: line["quantity"] + 5 for line in lines}

    version = await _current_version(request_id)
    response = await _receive(world.receiver, request_id, accepted, version)
    assert response.status_code == 422, response.text
    message = response.json()["error"]["message"]
    assert lines[0]["name"] in message and lines[1]["name"] in message


async def test_receipt_refuses_lines_from_another_order(world: World) -> None:
    """拿别的订单的行 id 来收货 → 422。"""
    awarded = await _drive_to_awarded(world)
    request_id = awarded["request"]["id"]
    assert (
        await _place_order(world.procurement, request_id, awarded["request"]["version"])
    ).status_code == 201
    version = await _current_version(request_id)
    response = await _receive(
        world.receiver, request_id, {str(uuid.uuid4()): 1}, version
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "RECEIPT_LINE_UNKNOWN"


async def test_receipt_requires_receiver_role(world: World) -> None:
    """**采购自己不能收货**（内控：采购、验收、记录三方分离）→ 403。"""
    awarded = await _drive_to_awarded(world)
    request_id = awarded["request"]["id"]
    placed = await _place_order(
        world.procurement, request_id, awarded["request"]["version"]
    )
    lines = placed.json()["lines"]
    accepted = {line["id"]: line["quantity"] for line in lines}
    version = await _current_version(request_id)

    denied = await _receive(world.procurement, request_id, accepted, version)
    assert denied.status_code == 403, denied.text


async def test_receipt_before_order_is_refused(world: World) -> None:
    """还没下单就收货 → 409。"""
    awarded = await _drive_to_awarded(world)
    response = await _receive(
        world.receiver, awarded["request"]["id"], {}, awarded["request"]["version"]
    )
    assert response.status_code == 409, response.text
    assert "不能收货" in response.json()["error"]["message"]


async def test_second_receipt_is_refused(world: World) -> None:
    """重复收货 → 409（条件更新 + `UNIQUE(request_id)` 两层）。"""
    awarded = await _drive_to_awarded(world)
    request_id = awarded["request"]["id"]
    placed = await _place_order(
        world.procurement, request_id, awarded["request"]["version"]
    )
    lines = placed.json()["lines"]
    accepted = {line["id"]: line["quantity"] for line in lines}
    version = await _current_version(request_id)

    first = await _receive(world.receiver, request_id, accepted, version)
    assert first.status_code == 201, first.text
    second = await _receive(world.receiver, request_id, accepted, version)
    assert second.status_code == 409, second.text


async def test_receipt_replay_with_same_key_returns_first_response(world: World) -> None:
    """同键重放收货 → 逐字节相同的响应（不会写第二张收货单）。"""
    awarded = await _drive_to_awarded(world)
    request_id = awarded["request"]["id"]
    placed = await _place_order(
        world.procurement, request_id, awarded["request"]["version"]
    )
    lines = placed.json()["lines"]
    accepted = {line["id"]: line["quantity"] for line in lines}
    version = await _current_version(request_id)
    key = uuid.uuid4().hex

    first = await _receive(world.receiver, request_id, accepted, version, key=key)
    assert first.status_code == 201, first.text
    replay = await _receive(world.receiver, request_id, accepted, version, key=key)
    assert replay.status_code == 201, replay.text
    assert replay.json() == first.json()

    async with SessionFactory() as session:
        count = len(
            list(
                (
                    await session.scalars(
                        select(GoodsReceiptRecord.id).where(
                            GoodsReceiptRecord.request_id == uuid.UUID(request_id)
                        )
                    )
                ).all()
            )
        )
    assert count == 1


async def test_receipt_with_same_key_but_changed_quantities_is_409(world: World) -> None:
    """同键**改了实收数量** = 改了主意 → 409。

    载荷哈希必须含逐行数量：只哈希 `request_id` 的话，第二次提交会静默拿回
    第一次的结果，而用户以为"我改了数量并重新提交了"。
    """
    awarded = await _drive_to_awarded(world)
    request_id = awarded["request"]["id"]
    placed = await _place_order(
        world.procurement, request_id, awarded["request"]["version"]
    )
    lines = placed.json()["lines"]
    version = await _current_version(request_id)
    key = uuid.uuid4().hex

    first = await _receive(
        world.receiver,
        request_id,
        {line["id"]: line["quantity"] for line in lines},
        version,
        key=key,
    )
    assert first.status_code == 201, first.text

    changed = await _receive(
        world.receiver,
        request_id,
        {line["id"]: 0 for line in lines},
        version,
        key=key,
    )
    assert changed.status_code == 409, changed.text


async def test_receipt_shows_up_in_the_order_detail(world: World) -> None:
    """收货之后订单详情里能看到收货单（`receipt` 不再是 null）。"""
    awarded = await _drive_to_awarded(world)
    request_id = awarded["request"]["id"]
    placed = await _place_order(
        world.procurement, request_id, awarded["request"]["version"]
    )
    order_id = placed.json()["order"]["id"]
    lines = placed.json()["lines"]
    accepted = {line["id"]: line["quantity"] for line in lines}
    version = await _current_version(request_id)
    assert (
        await _receive(world.receiver, request_id, accepted, version)
    ).status_code == 201

    detail = await world.requester.get(f"/api/v1/orders/{order_id}")
    assert detail.status_code == 200, detail.text
    receipt = detail.json()["receipt"]
    assert receipt is not None
    assert receipt["fully_received"] is True
    assert receipt["status"] == "RECEIVED"


async def test_fully_received_does_not_go_backwards_after_invoicing(world: World) -> None:
    """**收齐这个事实不会因为后续流程而倒退**：收票之后订单详情里仍是 `true`。

    ============================ 这条用例挡的是一个真实存在的 bug ============================
    `_order_detail` 原来拿"**当前**主单状态是不是 `RECEIVED`"当"收齐了没有"的判据。
    可收齐之后主单会继续往前走（登记发票把它推到 `INVOICED`），
    于是订单详情页会突然说"没收齐"—— 而仓库明明收齐了。

    它当时测试是**绿的**：另一条用例只在 `RECEIVED` 那一刻看了一眼。
    这类 bug 的共性是 **判据读的是"可变的状态"，而它要回答的是"发生过的事实"**。
    正确判据是"这张申请有没有收货单"（第一版只做整单收货，有单就等于收齐）。
    """
    awarded = await _drive_to_awarded(world)
    request_id = awarded["request"]["id"]
    placed = await _place_order(
        world.procurement, request_id, awarded["request"]["version"]
    )
    order_id = placed.json()["order"]["id"]
    lines = placed.json()["lines"]
    accepted = {line["id"]: line["quantity"] for line in lines}
    version = await _current_version(request_id)
    assert (await _receive(world.receiver, request_id, accepted, version)).status_code == 201

    # 登记发票 → 主单离开 RECEIVED（进入 INVOICED）。
    # 供应商 id 从订单那一行读（比从响应体里翻更直接）。
    async with SessionFactory() as session:
        supplier_id = str(
            (
                await session.scalars(
                    select(PurchaseOrderRecord.supplier_id).where(
                        PurchaseOrderRecord.id == uuid.UUID(order_id)
                    )
                )
            ).one()
        )
    version = await _current_version(request_id)
    # 用**管理员**账号登记发票：这个世界（8.2 的）里没有财务账号，
    # 而发票接口要求 `FINANCE`。为了不把 8.2 的 world 撑大，这里直接建一个。
    finance_client, _ = await world.make([ROLE_FINANCE], world.departments[0])
    invoiced = await finance_client.post(
        f"/api/v1/requests/{request_id}/invoices",
        json={
            "supplier_id": supplier_id,
            "invoice_no": "BACKWARDS-1",
            "amount_fen": 24_000,
            "tax_amount_fen": 0,
            "issued_on": None,
            "attachment_id": None,
            "version": version,
        },
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert invoiced.status_code == 201, invoiced.text
    await finance_client.aclose()

    detail = await world.requester.get(f"/api/v1/orders/{order_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["order"]["request_status"] == RequestStatus.INVOICED.value
    assert detail.json()["receipt"]["fully_received"] is True, (
        "收齐是**发生过的事实**，不该因为后来收了票就变回 false"
    )


async def test_order_amount_differs_from_request_amount_by_design(world: World) -> None:
    """**订单金额 ≠ 申请金额是正常的**（询价压价），差额留给预算那条差额释放。

    这条是"防有人顺手加一条 `assert order.amount == request.amount_total`"的守门用例：
    真加上去，正常业务会被挡在门外。
    """
    awarded = await _drive_to_awarded(world, deal_unit_price_fen=1_000)
    request_id = awarded["request"]["id"]
    placed = await _place_order(
        world.procurement, request_id, awarded["request"]["version"]
    )
    body = placed.json()

    request_before = awarded["request"]["amount_total_fen"]
    assert request_before == 10 * 2_500 + 2 * 4_000  # 申请金额 330 元
    assert body["order"]["amount_fen"] == 12 * 1_000  # 订单金额 120 元
    assert body["order"]["amount_fen"] < request_before
