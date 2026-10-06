"""发票与三单匹配的集成测试（M10 / 8.3）：真数据库 + 真 HTTP。

验收项对齐 `PROGRESS.md` 的 M10 / 8.3 清单：

    登记两张发票 → 匹配用的是**两张之和**（订单级粒度）
    发票金额超容差 → 问题列表里列出**具体差异**（expected / actual）
    预检是**只读**的（跑完主单状态不变、没有任何新事件）

============================ 走完整链路才到得了 RECEIVED ============================
发票的前置是"已收货"，收货的前置是"已下单"，下单的前置是"已定标"。
所以每个用例都要跑一段**真实链路**：

    建草稿 → 提交 → 审批 → 报价 → 定标 → 下单 → 收货 → 登记发票

`_drive_to_received` 把前六段收在一处；`_add_invoice` 是第七段。
**不跳过任何一段** —— 三单匹配比的是这条链上真实的三个数。
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
from app.infrastructure.records.invoice import InvoiceRecord
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

HIGH_THRESHOLD = 100_000_000

FULL_POLICY = {
    "steps": [
        {"order": 10, "role": "DEPT_MANAGER", "mode": "SINGLE", "when": {"gte": ["amount_fen", 0]}},
        {"order": 20, "role": "DEPT_HEAD", "mode": "SINGLE", "when": {"gte": ["amount_fen", 100_000]}},
    ],
}

#: 订单金额（= 12 × 成交单价 2000 分 = 24000 分 = 240 元）
ORDER_AMOUNT_FEN = 24_000
ORDER_QUANTITY = 12  # 10 + 2
#: 申请明细：单片 50000 分（500 元），10 片 + 2 个 = 580000 分（5800 元）。
#: ⚠️ 金额必须 >= 10 万元（100000 分）才够得到第二级审批（`DEPT_HEAD`，`gte: 100_000`）——
#: 金额太小的话链上只有一级，`_approve_my_level(world.head…)` 会找不到待办。
#: 顺带它让"申请金额 vs 订单金额"的差额很明显：**5800 元 vs 240 元**，
#: 正是"询价压价之后订单总额远小于申请总额"的真实样子。
ITEM_UNIT_PRICE_FEN = 50_000
ITEM_QUANTITIES = (10, 2)


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
        self.finance: AsyncClient
        self._factory: UserFactory

    async def make(
        self, roles: list[str], department_id: uuid.UUID | None
    ) -> tuple[AsyncClient, Account]:
        account = await self._factory(roles=roles, department_id=department_id)
        self.actors.append(account.id)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        await login_as(client, account)
        return client, account

    async def add_supplier(self, name: str = "开票供应商") -> dict:
        response = await self.admin_client.post(
            "/api/v1/admin/suppliers",
            json={"name": f"{name}-{uuid.uuid4().hex[:6]}", "code": _code("SUP")},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        self.suppliers.append(uuid.UUID(body["id"]))
        return body

    async def purge(self) -> None:
        """按外键顺序清理（发票 → 收货明细 → 收货单 → 订单明细 → 订单 → 报价 → 申请）。"""
        async with SessionFactory() as session:
            if self.requests:
                await session.execute(
                    delete(InvoiceRecord).where(
                        InvoiceRecord.request_id.in_(self.requests)
                    )
                )
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
            "/api/v1/admin/departments", json={"name": "结算测试部", "code": _code("TDEP")}
        )
    ).json()
    state.departments.append(uuid.UUID(department["id"]))
    dept_id = uuid.UUID(department["id"])

    category = (
        await admin_client.post(
            "/api/v1/admin/categories",
            json={
                "name": "结算测试品类",
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
    state.finance, _ = await state.make([ROLE_FINANCE], dept_id)

    try:
        yield state
    finally:
        for client in (
            state.requester,
            state.manager,
            state.head,
            state.procurement,
            state.receiver,
            state.finance,
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


async def _drive_to_received(world: World, *, deal_unit_price_fen: int = 2_000) -> dict:
    """把一张单子推到 `RECEIVED`（收货完毕），返回 `{request_id, supplier, order}`。

    链路：提交 → 审批 → 报价 → 定标 → 下单 → 收货。**一段都不跳**。
    """
    draft = await world.requester.post(
        "/api/v1/requests", json={"category_id": world.category["id"], "title": "结算单"}
    )
    body = draft.json()
    world.requests.append(uuid.UUID(body["id"]))

    items = await world.requester.put(
        f"/api/v1/requests/{body['id']}/items",
        json={
            "version": body["version"],
            "items": [
                {"name": "A4 复印纸", "quantity": 10, "unit_price_fen": 50_000},
                {"name": "订书机", "quantity": 2, "unit_price_fen": 50_000},
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
    await _approve_my_level(world.head, body["id"])

    supplier = await world.add_supplier()
    quoted = await world.procurement.post(
        f"/api/v1/requests/{body['id']}/quotations",
        json={"supplier_id": supplier["id"], "amount_fen": deal_unit_price_fen},
    )
    assert quoted.status_code == 201, quoted.text

    version = await _current_version(body["id"])
    awarded = await world.procurement.post(
        f"/api/v1/requests/{body['id']}/award",
        json={"supplier_id": supplier["id"], "version": version},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert awarded.status_code == 200, awarded.text

    version = await _current_version(body["id"])
    placed = await world.procurement.post(
        f"/api/v1/requests/{body['id']}/orders",
        json={"version": version},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert placed.status_code == 201, placed.text
    order = placed.json()["order"]
    order_lines = placed.json()["lines"]

    version = await _current_version(body["id"])
    received = await world.receiver.post(
        f"/api/v1/requests/{body['id']}/receipts",
        json={
            "accepted": {line["id"]: line["quantity"] for line in order_lines},
            "note": None,
            "version": version,
        },
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert received.status_code == 201, received.text
    return {"request_id": body["id"], "supplier": supplier, "order": order}


async def _add_invoice(
    world: World,
    request_id: str,
    *,
    invoice_no: str,
    amount_fen: int,
    tax_amount_fen: int = 0,
    supplier_id: str | None = None,
    version: int | None = None,
    key: str | None = None,
    client: AsyncClient | None = None,
):
    if version is None:
        version = await _current_version(request_id)
    if supplier_id is None:
        supplier_id = (await _match(world, request_id))["order"]["supplier_id"]
    return await (client or world.finance).post(
        f"/api/v1/requests/{request_id}/invoices",
        json={
            "supplier_id": supplier_id,
            "invoice_no": invoice_no,
            "amount_fen": amount_fen,
            "tax_amount_fen": tax_amount_fen,
            "issued_on": None,
            "attachment_id": None,
            "version": version,
        },
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
    )


async def _match(world: World, request_id: str) -> dict:
    response = await world.finance.get(f"/api/v1/requests/{request_id}/match")
    assert response.status_code == 200, response.text
    return response.json()


async def _invoices(world: World, request_id: str) -> list[dict]:
    response = await world.finance.get(f"/api/v1/requests/{request_id}/invoices")
    assert response.status_code == 200, response.text
    return response.json()


async def _void(
    world: World,
    invoice_id: str,
    *,
    reason: str | None = None,
    client: AsyncClient | None = None,
):
    return await (client or world.finance).post(
        f"/api/v1/invoices/{invoice_id}/void", json={"reason": reason}
    )


# ================================================================ 1. 待付款队列


async def test_payment_queue_lists_invoiced_requests(world: World) -> None:
    """待付款队列：只列 `INVOICED` 的单子；还没收票的 `RECEIVED` 不该出现。"""
    just_received = await _drive_to_received(world)
    invoiced = await _drive_to_received(world)
    assert (
        await _add_invoice(
            world, invoiced["request_id"], invoice_no="Q-001", amount_fen=ORDER_AMOUNT_FEN
        )
    ).status_code == 201

    response = await world.finance.get("/api/v1/payments/queue")
    assert response.status_code == 200, response.text
    ids = [item["id"] for item in response.json()["items"]]
    assert invoiced["request_id"] in ids
    assert just_received["request_id"] not in ids, "只收货、还没收票的单子不该进待付款队列"


async def test_payment_queue_requires_finance_role(world: World) -> None:
    """申请人调待付款队列 → 403（财务的工作台）。"""
    denied = await world.requester.get("/api/v1/payments/queue")
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "FORBIDDEN"


# ================================================================ 2. 登记发票


async def test_add_invoice_moves_request_to_invoiced(world: World) -> None:
    """**登记发票把主单推到 `INVOICED`**，并写时间线事件。"""
    driven = await _drive_to_received(world)
    request_id = driven["request_id"]

    response = await _add_invoice(
        world, request_id, invoice_no="INV-0001", amount_fen=ORDER_AMOUNT_FEN, tax_amount_fen=2_000
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["invoice"]["invoice_no"] == "INV-0001"
    assert body["invoice"]["amount_fen"] == ORDER_AMOUNT_FEN
    assert body["invoice"]["tax_amount_fen"] == 2_000
    assert body["request_status"] == RequestStatus.INVOICED.value
    assert body["invoice_count"] == 1
    assert body["invoice_total_fen"] == ORDER_AMOUNT_FEN

    timeline = (await world.requester.get(f"/api/v1/requests/{request_id}/timeline")).json()
    last = timeline["events"][-1]
    assert last["event_type"] == "INVOICE_ADDED"
    assert last["from_status"] == RequestStatus.RECEIVED.value
    assert last["to_status"] == RequestStatus.INVOICED.value
    assert last["payload"]["invoice_total_fen"] == ORDER_AMOUNT_FEN


async def test_second_invoice_is_allowed_and_does_not_move_the_status_again(world: World) -> None:
    """**发票可以多张**：第二张也能登记，但主单不再重复推进（已经是 `INVOICED`）。

    这条挡的是"每登记一张就推一次状态"：第二次会撞状态校验（那时主单已经不是
    `RECEIVED`），于是分批开票只能用一次 —— 而分批开票恰恰是常见情形。
    """
    driven = await _drive_to_received(world)
    request_id = driven["request_id"]
    first = await _add_invoice(
        world, request_id, invoice_no="INV-A", amount_fen=10_000
    )
    assert first.status_code == 201, first.text

    second = await _add_invoice(world, request_id, invoice_no="INV-B", amount_fen=14_000)
    assert second.status_code == 201, second.text
    body = second.json()
    assert body["request_status"] == RequestStatus.INVOICED.value
    assert body["invoice_count"] == 2
    assert body["invoice_total_fen"] == 24_000, "发票总额是两张之和"

    # 时间线里第二条 INVOICE_ADDED 的 from == to（状态没再变）
    timeline = (await world.requester.get(f"/api/v1/requests/{request_id}/timeline")).json()
    added = [e for e in timeline["events"] if e["event_type"] == "INVOICE_ADDED"]
    assert len(added) == 2
    assert added[1]["from_status"] == added[1]["to_status"] == RequestStatus.INVOICED.value
    assert added[1]["payload"]["invoice_total_fen"] == 24_000


async def test_duplicate_invoice_no_is_refused(world: World) -> None:
    """同一张申请里发票号重复 → **409**（重复录入要让人看见，不能静默换号）。"""
    driven = await _drive_to_received(world)
    request_id = driven["request_id"]
    assert (
        await _add_invoice(world, request_id, invoice_no="INV-DUP", amount_fen=10_000)
    ).status_code == 201

    again = await _add_invoice(world, request_id, invoice_no="INV-DUP", amount_fen=12_000)
    assert again.status_code == 409, again.text
    assert "INV-DUP" in again.json()["error"]["message"]


async def test_invoice_from_another_supplier_is_refused(world: World) -> None:
    """发票供应商 ≠ 订单供应商 → **422**（在入口挡掉，而不是等付款那一刻）。"""
    driven = await _drive_to_received(world)
    other = await world.add_supplier("别家供应商")
    response = await _add_invoice(
        world,
        driven["request_id"],
        invoice_no="INV-WRONG",
        amount_fen=ORDER_AMOUNT_FEN,
        supplier_id=other["id"],
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "INVOICE_SUPPLIER_MISMATCH"


async def test_invoice_before_receipt_is_refused(world: World) -> None:
    """没收货就登记发票 → **409**（跳过了验收这一环）。"""
    driven = await _drive_to_received(world)
    # 另起一张只走到 ORDERED 的单子
    draft = await world.requester.post(
        "/api/v1/requests", json={"category_id": world.category["id"], "title": "没收货"}
    )
    body = draft.json()
    world.requests.append(uuid.UUID(body["id"]))
    response = await _add_invoice(
        world,
        body["id"],
        invoice_no="INV-EARLY",
        amount_fen=1_000,
        supplier_id=driven["supplier"]["id"],
    )
    assert response.status_code == 409, response.text
    assert "不能登记发票" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    ("amount_fen", "tax_amount_fen", "code"),
    [
        (0, 0, "INVOICE_AMOUNT_INVALID"),
        (-100, 0, "INVOICE_AMOUNT_INVALID"),
        (10_000, -1, "INVOICE_TAX_INVALID"),
        (10_000, 10_001, "INVOICE_TAX_INVALID"),
    ],
)
async def test_invalid_invoice_amounts_are_refused(
    world: World, amount_fen: int, tax_amount_fen: int, code: str
) -> None:
    """金额 / 税额非法 → 422。

    税额大于总额那一档不是多余的：它意味着"价税合计"自相矛盾，
    数据库 CHECK 也拦，但这里给的是一句人话。
    """
    driven = await _drive_to_received(world)
    response = await _add_invoice(
        world,
        driven["request_id"],
        invoice_no=f"INV-{uuid.uuid4().hex[:6]}",
        amount_fen=amount_fen,
        tax_amount_fen=tax_amount_fen,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == code


async def test_invoice_registration_is_idempotent(world: World) -> None:
    """同键重放 → 拿回第一次的响应，且**库里只有一张发票**。"""
    driven = await _drive_to_received(world)
    request_id = driven["request_id"]
    key = uuid.uuid4().hex

    first = await _add_invoice(
        world, request_id, invoice_no="INV-REPLAY", amount_fen=ORDER_AMOUNT_FEN, key=key
    )
    assert first.status_code == 201, first.text
    replay = await _add_invoice(
        world, request_id, invoice_no="INV-REPLAY", amount_fen=ORDER_AMOUNT_FEN, key=key
    )
    assert replay.status_code == 201, replay.text
    assert replay.json() == first.json()

    async with SessionFactory() as session:
        count = len(
            list(
                (
                    await session.scalars(
                        select(InvoiceRecord.id).where(
                            InvoiceRecord.request_id == uuid.UUID(request_id)
                        )
                    )
                ).all()
            )
        )
    assert count == 1


async def test_invoice_requires_finance_role(world: World) -> None:
    """采购登记发票 → **403**（发票是付款依据，责任落在财务）。"""
    driven = await _drive_to_received(world)
    response = await _add_invoice(
        world,
        driven["request_id"],
        invoice_no="INV-PROC",
        amount_fen=ORDER_AMOUNT_FEN,
        client=world.procurement,
    )
    assert response.status_code == 403, response.text


async def test_invoice_list_returns_an_empty_array_before_any_invoice(world: World) -> None:
    """还没登记发票 → **空数组**（不是 404，也不是 null）。"""
    driven = await _drive_to_received(world)
    response = await world.procurement.get(
        f"/api/v1/requests/{driven['request_id']}/invoices"
    )
    assert response.status_code == 200, response.text
    assert response.json() == []


# ================================================================ 3. 三单匹配：验收 8.3


async def test_match_uses_the_sum_of_two_invoices(world: World) -> None:
    """**验收 8.3：登记两张发票，匹配用的是两张之和。**

    订单 240 元；两张票各 120 元 —— 单看任何一张都与订单差 120 元（远超容差），
    但**合计**正好相等 → 匹配通过。这条用例专门挡"拿第一张票去比"的写法。
    """
    driven = await _drive_to_received(world)
    request_id = driven["request_id"]
    assert (await _add_invoice(world, request_id, invoice_no="SUM-1", amount_fen=12_000)).status_code == 201
    assert (await _add_invoice(world, request_id, invoice_no="SUM-2", amount_fen=12_000)).status_code == 201

    body = await _match(world, request_id)
    assert body["invoice_count"] == 2
    assert body["invoice_total_fen"] == ORDER_AMOUNT_FEN
    assert body["order"]["amount_fen"] == ORDER_AMOUNT_FEN
    assert body["passed"] is True, f"两张之和等于订单金额，应当通过：{body['issues']}"
    assert body["issues"] == []
    assert len(body["invoices"]) == 2


async def test_match_lists_amount_difference_when_over_tolerance(world: World) -> None:
    """**验收 8.3：发票金额超容差 → 列出具体差异**（expected / actual）。

    订单 240 元，容差 = max(240×2%, 50) = **50 元** → 开到 300 元超了 10 元。
    """
    driven = await _drive_to_received(world)
    request_id = driven["request_id"]
    assert (
        await _add_invoice(world, request_id, invoice_no="OVER-1", amount_fen=30_000)
    ).status_code == 201

    body = await _match(world, request_id)
    assert body["passed"] is False
    assert [issue["code"] for issue in body["issues"]] == ["AMOUNT_MISMATCH"]
    issue = body["issues"][0]
    assert issue["expected"] == ORDER_AMOUNT_FEN
    assert issue["actual"] == 30_000
    # 差异与容差都要在人话里说清楚（前端直接显示它）
    assert "6000" in issue["message"], "要说清差多少"
    assert "5000" in issue["message"], "要说清容差是多少"


async def test_match_passes_within_tolerance(world: World) -> None:
    """金额在容差内（差 50 元正好等于容差）→ **通过**（闭区间）。

    这条与上一条配对：容差的意义是"允许差多少"，不是"必须完全相等"。
    """
    driven = await _drive_to_received(world)
    request_id = driven["request_id"]
    assert (
        await _add_invoice(
            world, request_id, invoice_no="EDGE-1", amount_fen=ORDER_AMOUNT_FEN + 5_000
        )
    ).status_code == 201

    body = await _match(world, request_id)
    assert body["passed"] is True, f"正好等于容差应当通过：{body['issues']}"


async def test_match_reports_without_order_or_invoice(world: World) -> None:
    """没有订单 / 没有发票 → 说清是**哪一步还没到**，而不是"金额差超容差"。

    这条挡的是"把 0 传进 match_three_way"：那样会得到
    "发票金额 0 与订单金额 24000 相差 24000，超出容差" —— 指错方向。
    """
    draft = await world.requester.post(
        "/api/v1/requests", json={"category_id": world.category["id"], "title": "还没下单"}
    )
    body = draft.json()
    world.requests.append(uuid.UUID(body["id"]))

    result = await _match(world, body["id"])
    assert result["order"] is None
    assert result["passed"] is False
    assert [issue["code"] for issue in result["issues"]] == ["NO_ORDER"]

    # 走到 RECEIVED 但还没收票 → NO_INVOICE
    driven = await _drive_to_received(world)
    result = await _match(world, driven["request_id"])
    assert [issue["code"] for issue in result["issues"]] == ["NO_INVOICE"]


async def test_precheck_is_read_only(world: World) -> None:
    """**预检不做任何状态变更**（实施计划 2431 行）：跑几次都不改状态、不写事件。

    这条很重要：真拦截在"执行付款"（8.4），预检只是给前端渲染对比页用的。
    它一旦偷偷写了什么（比如把 `matched` 标记上），就会出现
    "打开过页面"与"没打开过页面"两条不同的路径。
    """
    driven = await _drive_to_received(world)
    request_id = driven["request_id"]
    assert (
        await _add_invoice(world, request_id, invoice_no="RO-1", amount_fen=ORDER_AMOUNT_FEN)
    ).status_code == 201

    before_events = len(
        (await world.requester.get(f"/api/v1/requests/{request_id}/timeline")).json()["events"]
    )
    version_before = await _current_version(request_id)

    for _ in range(3):
        body = await _match(world, request_id)
        assert body["passed"] is True

    assert (await _current_version(request_id)) == version_before, "预检不该推进版本"
    after_events = len(
        (await world.requester.get(f"/api/v1/requests/{request_id}/timeline")).json()["events"]
    )
    assert after_events == before_events, "预检不该写事件"


async def test_match_reports_supplier_mismatch_from_the_order_side(world: World) -> None:
    """匹配里的供应商一致性判定仍然在（入口挡住之后，它是付款前的第二道）。

    这里直接**在库里**把发票供应商改成别家（模拟"入口被绕过"或历史数据），
    再验匹配会说 `SUPPLIER_MISMATCH` —— 因为动钱之前必须自己判一次，
    不能假设入口永远挡得住。
    """
    driven = await _drive_to_received(world)
    request_id = driven["request_id"]
    assert (
        await _add_invoice(world, request_id, invoice_no="SM-1", amount_fen=ORDER_AMOUNT_FEN)
    ).status_code == 201

    other = await world.add_supplier("被改到别家")
    async with SessionFactory() as session:
        record = (
            await session.scalars(
                select(InvoiceRecord).where(
                    InvoiceRecord.request_id == uuid.UUID(request_id)
                )
            )
        ).one()
        record.supplier_id = uuid.UUID(other["id"])
        await session.commit()

    body = await _match(world, request_id)
    assert body["all_suppliers_match"] is False
    assert "SUPPLIER_MISMATCH" in [issue["code"] for issue in body["issues"]]


async def test_match_precheck_visible_to_procurement_but_not_to_applicant(world: World) -> None:
    """匹配预检：采购看得到（跟进到票情况），申请人看不到（403）。"""
    driven = await _drive_to_received(world)
    request_id = driven["request_id"]

    ok = await world.procurement.get(f"/api/v1/requests/{request_id}/match")
    assert ok.status_code == 200, ok.text

    denied = await world.requester.get(f"/api/v1/requests/{request_id}/match")
    assert denied.status_code == 403, denied.text


# ================================================================ 4. 发票作废


async def test_void_excludes_the_invoice_from_matching_totals(world: World) -> None:
    """**作废之后这张票不再参与金额求和** —— 这是"作废"唯一的作用。

    订单 240 元；登记两张各 240 元（合计 480 元 → 超容差），
    作废其中一张 → 合计回到 240 元 → **匹配通过**。
    如果作废没把金额排掉，这里仍然会报 `AMOUNT_MISMATCH`，
    而用户明明已经把多余那张作废了（他会以为作废没生效）。
    """
    driven = await _drive_to_received(world)
    request_id = driven["request_id"]
    first = await _add_invoice(world, request_id, invoice_no="V-1", amount_fen=ORDER_AMOUNT_FEN)
    second = await _add_invoice(world, request_id, invoice_no="V-2", amount_fen=ORDER_AMOUNT_FEN)
    assert first.status_code == 201 and second.status_code == 201

    before = await _match(world, request_id)
    assert before["invoice_total_fen"] == ORDER_AMOUNT_FEN * 2
    assert before["passed"] is False, "两张加起来 480 元，超容差"

    target = second.json()["invoice"]["id"]
    response = await _void(world, target, reason="重复开票")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["invoice"]["status"] == "VOIDED"
    assert body["invoice_total_fen"] == ORDER_AMOUNT_FEN, "有效合计要减掉作废那张"
    assert body["invoice_count"] == 1

    after = await _match(world, request_id)
    assert after["invoice_total_fen"] == ORDER_AMOUNT_FEN
    assert after["passed"] is True, f"作废之后只剩一张 240 元，应当通过：{after['issues']}"

    # 列表里**仍然看得到**那张作废的票（否则用户会以为从没登记过，又去登记一次）
    listed = await _invoices(world, request_id)
    assert len(listed) == 2
    assert {row["status"] for row in listed} == {"ACTIVE", "VOIDED"}


async def test_void_writes_a_timeline_event_without_changing_the_status(world: World) -> None:
    """作废写时间线事件，且**不改变主单状态**（留在 `INVOICED`）。

    "这张单子收过票"是历史事实，不会因为票作废而没发生过 ——
    把状态退回去会让"付过款的单子"与"收过票但票已作废的单子"长得一样。
    """
    driven = await _drive_to_received(world)
    request_id = driven["request_id"]
    created = await _add_invoice(world, request_id, invoice_no="EV-1", amount_fen=ORDER_AMOUNT_FEN)
    assert created.status_code == 201

    version_before = await _current_version(request_id)
    response = await _void(
        world, created.json()["invoice"]["id"], reason="供应商开错"
    )
    assert response.status_code == 200, response.text
    assert response.json()["request_status"] == RequestStatus.INVOICED.value
    assert (await _current_version(request_id)) == version_before, "作废不该推进主单版本"

    timeline = (await world.requester.get(f"/api/v1/requests/{request_id}/timeline")).json()
    last = timeline["events"][-1]
    assert last["event_type"] == "INVOICE_VOIDED"
    assert last["from_status"] == last["to_status"] == RequestStatus.INVOICED.value
    assert last["payload"]["reason"] == "供应商开错"
    assert last["payload"]["invoice_no"] == "EV-1"
    assert last["payload"]["invoice_count"] == 0, "作废后有效发票张数是 0"


async def test_voided_invoice_number_can_be_registered_again(world: World) -> None:
    """**作废之后同一个发票号可以重新登记**（最正常的纠错路径）。

    这条是"部分唯一索引"而不是普通唯一约束的理由：
    录错了号 → 作废 → 按正确的号重录 —— 普通唯一约束会让作废的那行
    一直占着号，把这条路径挡住。
    """
    driven = await _drive_to_received(world)
    request_id = driven["request_id"]
    wrong = await _add_invoice(world, request_id, invoice_no="REUSE-1", amount_fen=10_000)
    assert wrong.status_code == 201

    # 没作废之前，同号再登记 → 409
    assert (
        await _add_invoice(world, request_id, invoice_no="REUSE-1", amount_fen=11_000)
    ).status_code == 409

    assert (await _void(world, wrong.json()["invoice"]["id"])).status_code == 200

    # 作废之后同号可以重登记（这次是"正确的号"）
    again = await _add_invoice(world, request_id, invoice_no="REUSE-1", amount_fen=ORDER_AMOUNT_FEN)
    assert again.status_code == 201, again.text
    assert again.json()["invoice"]["status"] == "ACTIVE"
    assert again.json()["invoice_count"] == 1, "作废的那张不算有效发票"


async def test_voiding_twice_is_refused(world: World) -> None:
    """重复作废 → **409**（不是"宽容地当成成功"）。"""
    driven = await _drive_to_received(world)
    request_id = driven["request_id"]
    created = await _add_invoice(world, request_id, invoice_no="TWICE-1", amount_fen=10_000)
    invoice_id = created.json()["invoice"]["id"]

    assert (await _void(world, invoice_id)).status_code == 200
    again = await _void(world, invoice_id)
    assert again.status_code == 409, again.text
    assert "已经作废过" in again.json()["error"]["message"]


async def test_void_requires_finance_role(world: World) -> None:
    """采购作废发票 → **403**（与登记发票同一道角色闸门）。"""
    driven = await _drive_to_received(world)
    request_id = driven["request_id"]
    created = await _add_invoice(world, request_id, invoice_no="ROLE-1", amount_fen=10_000)
    denied = await _void(
        world, created.json()["invoice"]["id"], client=world.procurement
    )
    assert denied.status_code == 403, denied.text


async def test_void_unknown_invoice_is_404(world: World) -> None:
    """作废一张不存在的发票 → **404**（不告诉它"这个 id 有没有可能是别的公司的"）。"""
    response = await _void(world, str(uuid.uuid4()))
    assert response.status_code == 404, response.text


async def test_all_invoices_voided_reports_no_invoice_not_amount_mismatch(world: World) -> None:
    """**全部作废 → `NO_INVOICE`**（而不是"金额差超容差"）。

    这是最容易写错的一处：拿"登记条数 > 0"判有没有发票，
    全作废之后仍然算有发票，于是匹配拿着 0 元去比，报"金额差超容差" ——
    而真正的原因是**一张有效发票都没有**，报错指错了方向。
    """
    driven = await _drive_to_received(world)
    request_id = driven["request_id"]
    created = await _add_invoice(world, request_id, invoice_no="ALL-VOID", amount_fen=ORDER_AMOUNT_FEN)
    assert created.status_code == 201
    assert (await _void(world, created.json()["invoice"]["id"])).status_code == 200

    body = await _match(world, request_id)
    assert body["invoice_count"] == 0
    assert body["invoice_total_fen"] == 0
    assert [issue["code"] for issue in body["issues"]] == ["NO_INVOICE"]
    assert body["passed"] is False


async def test_voided_invoice_does_not_affect_supplier_consistency(world: World) -> None:
    """作废掉那张"供应商不对"的票之后，匹配不再报 `SUPPLIER_MISMATCH`。

    否则用户明明已经把错票作废了，匹配却还在报供应商不一致
    （他会以为作废没生效）。
    """
    driven = await _drive_to_received(world)
    request_id = driven["request_id"]
    good = await _add_invoice(world, request_id, invoice_no="SC-1", amount_fen=ORDER_AMOUNT_FEN)
    bad = await _add_invoice(world, request_id, invoice_no="SC-2", amount_fen=1_000)
    assert good.status_code == 201 and bad.status_code == 201

    # 直接在库里把第二张的供应商改成别家（模拟历史脏数据 / 绕过入口）
    other = await world.add_supplier("别家")
    async with SessionFactory() as session:
        record = await session.get(InvoiceRecord, uuid.UUID(bad.json()["invoice"]["id"]))
        record.supplier_id = uuid.UUID(other["id"])
        await session.commit()

    before = await _match(world, request_id)
    assert before["all_suppliers_match"] is False
    assert "SUPPLIER_MISMATCH" in [issue["code"] for issue in before["issues"]]

    assert (await _void(world, bad.json()["invoice"]["id"])).status_code == 200

    after = await _match(world, request_id)
    assert after["all_suppliers_match"] is True, "错票作废之后只剩那张对的"
    assert "SUPPLIER_MISMATCH" not in [issue["code"] for issue in after["issues"]]


async def test_match_reports_over_receipt(world: World) -> None:
    """超收也会在匹配里报出来（`OVER_RECEIPT`）。

    正常链路走不到（收货时就拦住了），所以这里直接改库造出超收，
    验"匹配自己也会判"—— 动钱之前的那道关不能假设上游永远干净。
    """
    driven = await _drive_to_received(world)
    request_id = driven["request_id"]
    assert (
        await _add_invoice(world, request_id, invoice_no="OR-1", amount_fen=ORDER_AMOUNT_FEN)
    ).status_code == 201

    async with SessionFactory() as session:
        line = (
            await session.scalars(
                select(GoodsReceiptLineRecord)
                .join(
                    GoodsReceiptRecord,
                    GoodsReceiptLineRecord.receipt_id == GoodsReceiptRecord.id,
                )
                .where(GoodsReceiptRecord.request_id == uuid.UUID(request_id))
                .limit(1)
            )
        ).one()
        line.accepted_quantity = line.accepted_quantity + 5
        await session.commit()

    body = await _match(world, request_id)
    codes = [issue["code"] for issue in body["issues"]]
    assert "OVER_RECEIPT" in codes
    assert body["receipt"]["accepted_qty"] == ORDER_QUANTITY + 5
