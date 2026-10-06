"""付款与归档的集成测试（M10 / 8.4）：真数据库 + 真 HTTP。

验收项对齐实施计划 2486-2495 行的清单：

    未收货就创建付款单 → **422/409**
    故意让发票金额超容差 → 付款 **422** 并列出具体差异
    **两个人同时点"付款"，只有一个成功，另一个 409；`amount_spent` 只增加一次**
    付款成功后：已预留减少、已花增加、**总额不变**
    **预留 1000 元、实付 900 元 → `amount_spent` +900、该申请 `amount_reserved` 归零、
      流水里有 `SPEND 900` 和 `RELEASE 100`**
    `FAILED` 之后允许重新发起（部分唯一索引 `WHERE status <> 'FAILED'` 的作用）

============================ 这套用例的核心是"钱的算对没有" ============================
所以断言全部落在**预算表的三个数**与**流水表的两行**上，而不是只看响应码：
`amount_total` 不变、`amount_reserved` 与 `amount_spent` 此消彼长、
差额必须有一条 `RELEASE`。这三条**错了不会报错**，只会让账慢慢不对。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.database import SessionFactory
from app.domain.enums import TX_RELEASE, TX_SPEND, RequestStatus
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
from app.infrastructure.records.payment import PaymentRecord
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

#: 订单金额 = (10 + 2) × 2000 分 = 24000 分 = 240 元
ORDER_AMOUNT_FEN = 24_000
#: 申请金额 = (10 + 2) × 50000 分 = 600000 分 = 6000 元。
#: ⚠️ 必须 >= 100000 分才够得到第二级审批（`DEPT_HEAD`）。
REQUEST_AMOUNT_FEN = 600_000
#: 预算总额（分）= 10000 元。够大，便于观察"释放差额"。
BUDGET_AMOUNT_FEN = 1_000_000
ITEM_UNIT_PRICE_FEN = 50_000


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
        self.budget_id: uuid.UUID | None = None
        self.admin_client: AsyncClient
        self.requester: AsyncClient
        self.manager: AsyncClient
        self.head: AsyncClient
        self.procurement: AsyncClient
        self.receiver: AsyncClient
        self.finance: AsyncClient
        self.finance2: AsyncClient
        self._factory: UserFactory

    async def make(
        self, roles: list[str], department_id: uuid.UUID | None
    ) -> tuple[AsyncClient, Account]:
        account = await self._factory(roles=roles, department_id=department_id)
        self.actors.append(account.id)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        await login_as(client, account)
        return client, account

    async def add_supplier(self, name: str = "付款供应商") -> dict:
        response = await self.admin_client.post(
            "/api/v1/admin/suppliers",
            json={"name": f"{name}-{uuid.uuid4().hex[:6]}", "code": _code("SUP")},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        self.suppliers.append(uuid.UUID(body["id"]))
        return body

    async def purge(self) -> None:
        async with SessionFactory() as session:
            if self.requests:
                await session.execute(
                    delete(PaymentRecord).where(
                        PaymentRecord.request_id.in_(self.requests)
                    )
                )
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
                await session.execute(
                    delete(PaymentRecord).where(
                        PaymentRecord.created_by.in_(self.actors)
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
            "/api/v1/admin/departments", json={"name": "付款测试部", "code": _code("TDEP")}
        )
    ).json()
    state.departments.append(uuid.UUID(department["id"]))
    dept_id = uuid.UUID(department["id"])

    category = (
        await admin_client.post(
            "/api/v1/admin/categories",
            json={
                "name": "付款测试品类",
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
        amount_fen=BUDGET_AMOUNT_FEN,
    )
    state.budgets.append(uuid.UUID(budget["id"]))
    state.budget_id = uuid.UUID(budget["id"])

    state.requester, _ = await state.make([ROLE_REQUESTER], dept_id)
    state.manager, _ = await state.make([ROLE_DEPT_MANAGER], dept_id)
    state.head, _ = await state.make([ROLE_DEPT_HEAD], dept_id)
    state.procurement, _ = await state.make([ROLE_PROCUREMENT], dept_id)
    state.receiver, _ = await state.make([ROLE_RECEIVER], dept_id)
    state.finance, _ = await state.make([ROLE_FINANCE], dept_id)
    # 第二个财务账号：用来验"两个人同时付款只有一个成功"
    state.finance2, _ = await state.make([ROLE_FINANCE], dept_id)

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
            state.finance2,
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


async def _version(request_id: str) -> int:
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


async def _budget_numbers(budget_id: uuid.UUID) -> tuple[int, int, int]:
    """`(总额, 已预留, 已花)` —— 这三个数是本阶段所有断言的核心。"""
    async with SessionFactory() as session:
        row = (
            await session.execute(
                select(
                    BudgetRecord.amount_total,
                    BudgetRecord.amount_reserved,
                    BudgetRecord.amount_spent,
                ).where(BudgetRecord.id == budget_id)
            )
        ).one()
    return int(row[0]), int(row[1]), int(row[2])


async def _transactions(request_id: str) -> list[tuple[str, int]]:
    """这张申请的预算流水 `[(类型, 金额)]`，按写入次序。"""
    async with SessionFactory() as session:
        rows = await session.execute(
            select(BudgetTransactionRecord.kind, BudgetTransactionRecord.amount)
            .where(BudgetTransactionRecord.request_id == uuid.UUID(request_id))
            .order_by(BudgetTransactionRecord.seq)
        )
    return [(kind, int(amount)) for kind, amount in rows.all()]


async def _drive_to_invoiced(
    world: World, *, invoice_amount_fen: int | None = None, deal_unit_price_fen: int = 2_000
) -> dict:
    """把一张单子推到 `INVOICED`（收货完 + 登记了一张发票）。

    `invoice_amount_fen` 默认等于订单金额（匹配通过）；传别的值就能造出超容差。
    """
    draft = await world.requester.post(
        "/api/v1/requests", json={"category_id": world.category["id"], "title": "付款单"}
    )
    body = draft.json()
    world.requests.append(uuid.UUID(body["id"]))

    items = await world.requester.put(
        f"/api/v1/requests/{body['id']}/items",
        json={
            "version": body["version"],
            "items": [
                {"name": "A4 复印纸", "quantity": 10, "unit_price_fen": ITEM_UNIT_PRICE_FEN},
                {"name": "订书机", "quantity": 2, "unit_price_fen": ITEM_UNIT_PRICE_FEN},
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

    version = await _version(body["id"])
    awarded = await world.procurement.post(
        f"/api/v1/requests/{body['id']}/award",
        json={"supplier_id": supplier["id"], "version": version},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert awarded.status_code == 200, awarded.text

    version = await _version(body["id"])
    placed = await world.procurement.post(
        f"/api/v1/requests/{body['id']}/orders",
        json={"version": version},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert placed.status_code == 201, placed.text
    order_lines = placed.json()["lines"]

    version = await _version(body["id"])
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

    version = await _version(body["id"])
    invoice = await world.finance.post(
        f"/api/v1/requests/{body['id']}/invoices",
        json={
            "supplier_id": supplier["id"],
            "invoice_no": _code("INV"),
            "amount_fen": (
                ORDER_AMOUNT_FEN if invoice_amount_fen is None else invoice_amount_fen
            ),
            "tax_amount_fen": 0,
            "issued_on": None,
            "attachment_id": None,
            "version": version,
        },
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert invoice.status_code == 201, invoice.text
    return {
        "request_id": body["id"],
        "supplier": supplier,
        "order": placed.json()["order"],
    }


async def _create_payment(world: World, request_id: str, *, client: AsyncClient | None = None):
    return await (client or world.finance).post(
        f"/api/v1/requests/{request_id}/payments",
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )


async def _pay(
    world: World,
    payment_id: str,
    version: int,
    *,
    client: AsyncClient | None = None,
    key: str | None = None,
):
    return await (client or world.finance).post(
        f"/api/v1/payments/{payment_id}/pay",
        json={"version": version},
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
    )


async def _close(world: World, request_id: str, version: int, *, client: AsyncClient | None = None):
    return await (client or world.finance).post(
        f"/api/v1/requests/{request_id}/close", json={"version": version}
    )


# ================================================================ 1. 创建付款单


async def test_create_payment_only_needs_invoice_not_a_match(world: World) -> None:
    """**创建付款单只校验"已收票 + 有有效发票"**，不跑三单匹配（实施计划 2429 行）。

    这里让发票金额**超容差**（订单 240 元、开票 300 元）—— 付款单**仍然建得出来**，
    因为匹配跑在"执行付款"那一刻。这条用例把那个时机口径钉死：
    如果在创建时就拦，用户在预检页改完发票前根本没法开始付款流程。
    """
    driven = await _drive_to_invoiced(world, invoice_amount_fen=30_000)
    response = await _create_payment(world, driven["request_id"])
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["payment"]["status"] == "PENDING"
    assert body["payment"]["amount_fen"] == 30_000, "金额取有效发票之和"
    assert body["request_status"] == RequestStatus.INVOICED.value, "钱还没动，主单不变"
    assert body["payment"]["attempt_no"] == 1


async def test_create_payment_before_invoiced_is_refused(world: World) -> None:
    """**验收：没收货（没收票）就创建付款单 → 409。**

    状态机里 `RECEIVED` 之前没有通向 `INVOICED` 的转移，所以"收货才能付款"
    是在**状态**上强制的，不是靠前端按钮控制（业务规则第 7 条）。
    """
    driven = await _drive_to_invoiced(world)
    # 另起一张只到 DRAFT 的单子
    draft = await world.requester.post(
        "/api/v1/requests", json={"category_id": world.category["id"], "title": "还没收货"}
    )
    body = draft.json()
    world.requests.append(uuid.UUID(body["id"]))

    response = await _create_payment(world, body["id"])
    assert response.status_code == 409, response.text
    assert "不能创建付款单" in response.json()["error"]["message"]
    del driven  # 只是为了让 driven 的清理生效（它把申请登记进了 world）


async def test_no_active_invoice_is_refused(world: World) -> None:
    """发票**全部作废**之后创建付款单 → **422**（不是带着 0 元建出来）。"""
    driven = await _drive_to_invoiced(world)
    request_id = driven["request_id"]
    listed = (await world.finance.get(f"/api/v1/requests/{request_id}/invoices")).json()
    assert len(listed) == 1
    voided = await world.finance.post(
        f"/api/v1/invoices/{listed[0]['id']}/void", json={"reason": "先作废"}
    )
    assert voided.status_code == 200, voided.text

    response = await _create_payment(world, request_id)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "NO_ACTIVE_INVOICE"


async def test_second_active_payment_is_refused(world: World) -> None:
    """同一张申请再建一张付款单 → **409**（部分唯一索引 + 入口校验两层）。"""
    driven = await _drive_to_invoiced(world)
    first = await _create_payment(world, driven["request_id"])
    assert first.status_code == 201, first.text

    second = await _create_payment(world, driven["request_id"])
    assert second.status_code == 409, second.text
    assert "已经有一张付款单" in second.json()["error"]["message"]


async def test_create_payment_is_idempotent(world: World) -> None:
    """同键重放创建付款单 → 逐字节相同的响应，且库里只有一张。"""
    driven = await _drive_to_invoiced(world)
    key = uuid.uuid4().hex
    first = await world.finance.post(
        f"/api/v1/requests/{driven['request_id']}/payments",
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 201, first.text
    replay = await world.finance.post(
        f"/api/v1/requests/{driven['request_id']}/payments",
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == 201, replay.text
    assert replay.json() == first.json()

    async with SessionFactory() as session:
        count = len(
            list(
                (
                    await session.scalars(
                        select(PaymentRecord.id).where(
                            PaymentRecord.request_id == uuid.UUID(driven["request_id"])
                        )
                    )
                ).all()
            )
        )
    assert count == 1


async def test_create_payment_requires_finance_role(world: World) -> None:
    """采购创建付款单 → **403**（付款是财务的职权）。"""
    driven = await _drive_to_invoiced(world)
    response = await _create_payment(world, driven["request_id"], client=world.procurement)
    assert response.status_code == 403, response.text


# ================================================================ 2. 执行付款：验收 8.4


async def test_pay_settles_budget_and_releases_the_difference(world: World) -> None:
    """**验收：预留 1000 元、实付 900 元 → `amount_spent` +900、该申请
    `amount_reserved` 归零、流水里有 `SPEND 900` 和 `RELEASE 100`。**

    ============================ 这条用例怎么造出"实付 < 预留" ============================
    预留发生在**提交**那一刻，按**申请金额**（6000 元）预留；
    实付是按**有效发票之和**（= 订单金额 240 元）付。
    所以差额 = 6000 − 240 = 5760 元 —— 这就是"询价压价之后订单小于申请"
    在预算上的必然结果，也正是 M7 那条"差额必须显式释放"存在的理由。

    ⚠️ 断言落在**三个数**与**两行流水**上，不只看响应码：
    少了 RELEASE 那 5760 元会永久挂在 `amount_reserved` 上 ——
    账面正常、日志不报，可用额度却虚低。
    """
    driven = await _drive_to_invoiced(world)
    request_id = driven["request_id"]
    assert world.budget_id is not None

    reserved_before = await _budget_numbers(world.budget_id)
    assert reserved_before[0] == BUDGET_AMOUNT_FEN, "总额全程不变"
    assert reserved_before[1] == REQUEST_AMOUNT_FEN, "提交时按申请金额预留 6000 元"
    assert reserved_before[2] == 0

    created = await _create_payment(world, request_id)
    assert created.status_code == 201, created.text
    payment_id = created.json()["payment"]["id"]

    version = await _version(request_id)
    response = await _pay(world, payment_id, version)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["request_status"] == RequestStatus.PAID.value
    assert body["amount_fen"] == ORDER_AMOUNT_FEN == 24_000
    assert body["released_fen"] == REQUEST_AMOUNT_FEN - ORDER_AMOUNT_FEN == 576_000

    total, reserved, spent = await _budget_numbers(world.budget_id)
    assert total == BUDGET_AMOUNT_FEN, "总额不变"
    assert spent == ORDER_AMOUNT_FEN, "amount_spent 只按实付增加"
    assert reserved == 0, "该申请的预留必须归零（差额显式释放）"

    kinds = await _transactions(request_id)
    assert (TX_SPEND, ORDER_AMOUNT_FEN) in kinds
    assert (TX_RELEASE, REQUEST_AMOUNT_FEN - ORDER_AMOUNT_FEN) in kinds


async def test_pay_moves_status_and_writes_the_event(world: World) -> None:
    """付款把主单推到 `PAID`，事件里记下**实付与释放的差额两个数**。"""
    driven = await _drive_to_invoiced(world)
    request_id = driven["request_id"]
    created = await _create_payment(world, request_id)
    payment_id = created.json()["payment"]["id"]
    version = await _version(request_id)

    response = await _pay(world, payment_id, version)
    assert response.status_code == 200, response.text

    timeline = (await world.requester.get(f"/api/v1/requests/{request_id}/timeline")).json()
    last = timeline["events"][-1]
    assert last["event_type"] == "PAYMENT_COMPLETED"
    assert last["from_status"] == RequestStatus.INVOICED.value
    assert last["to_status"] == RequestStatus.PAID.value
    assert last["payload"]["amount_fen"] == ORDER_AMOUNT_FEN
    assert last["payload"]["released_fen"] == REQUEST_AMOUNT_FEN - ORDER_AMOUNT_FEN


async def test_pay_is_refused_when_the_match_fails(world: World) -> None:
    """**验收：发票金额超容差 → 付款 422 并列出具体差异。**

    这是"收货才能付款、金额要匹配"两条业务规则真正的执行点
    （实施计划 2478 行：第 2 步不能省）。
    """
    driven = await _drive_to_invoiced(world, invoice_amount_fen=30_000)
    request_id = driven["request_id"]
    created = await _create_payment(world, request_id)
    assert created.status_code == 201, created.text
    payment_id = created.json()["payment"]["id"]
    version = await _version(request_id)

    response = await _pay(world, payment_id, version)
    assert response.status_code == 422, response.text
    message = response.json()["error"]["message"]
    assert "三单匹配未通过" in message
    assert "6000" in message, "要说清差多少"
    assert "5000" in message, "要说清容差是多少"

    # 失败不留半截状态：主单仍 INVOICED、付款单仍 PENDING、预算没动
    assert (await _version(request_id)) == version
    assert world.budget_id is not None
    total, reserved, spent = await _budget_numbers(world.budget_id)
    assert (total, reserved, spent) == (BUDGET_AMOUNT_FEN, REQUEST_AMOUNT_FEN, 0)
    async with SessionFactory() as session:
        status_after = (
            await session.scalars(
                select(PaymentRecord.status).where(PaymentRecord.id == uuid.UUID(payment_id))
            )
        ).one()
    assert status_after == "PENDING", "匹配没过不该把付款单标成已付"


async def test_two_finance_users_paying_at_once_only_one_wins(world: World) -> None:
    """**验收：两个人同时点"付款"，只有一个成功，另一个 409；`amount_spent` 只增加一次。**

    ============================ 这条为什么必须用真并发 ============================
    顺序调用的话，第二个请求会被"付款单不是 PENDING"挡住 ——
    那验证的是"第二次调用会失败"，而**不是**"两个请求同时到达时只有一个赢"。
    并发下的正确性来自 `UPDATE ... WHERE status='PENDING'` 的 `rowcount` 判定
    （条件更新），而不是来自"先查了一下发现它不是 PENDING"。

    ⚠️ 两个请求用**不同的幂等键**：同键的话第二个会被幂等层拦成"重放"，
    于是测不到条件更新那一层。
    """
    driven = await _drive_to_invoiced(world)
    request_id = driven["request_id"]
    created = await _create_payment(world, request_id)
    payment_id = created.json()["payment"]["id"]
    version = await _version(request_id)

    first, second = await asyncio.gather(
        _pay(world, payment_id, version, client=world.finance),
        _pay(world, payment_id, version, client=world.finance2),
    )
    codes = sorted([first.status_code, second.status_code])
    assert codes == [200, 409], f"应当一胜一败，实际 {codes}：{first.text} / {second.text}"

    assert world.budget_id is not None
    total, reserved, spent = await _budget_numbers(world.budget_id)
    assert spent == ORDER_AMOUNT_FEN, "**amount_spent 只能增加一次**"
    assert reserved == 0
    assert total == BUDGET_AMOUNT_FEN

    spend_rows = [row for row in await _transactions(request_id) if row[0] == TX_SPEND]
    assert len(spend_rows) == 1, f"SPEND 流水只能有一条，实际 {spend_rows}"


async def test_pay_replay_with_same_key_returns_the_first_response(world: World) -> None:
    """同键重放付款 → 逐字节相同的响应（网络超时后重试不该看到 409）。"""
    driven = await _drive_to_invoiced(world)
    request_id = driven["request_id"]
    created = await _create_payment(world, request_id)
    payment_id = created.json()["payment"]["id"]
    version = await _version(request_id)
    key = uuid.uuid4().hex

    first = await _pay(world, payment_id, version, key=key)
    assert first.status_code == 200, first.text
    replay = await _pay(world, payment_id, version, key=key)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()


async def test_second_pay_with_another_key_is_409(world: World) -> None:
    """换一把键再付一次 → **409**（条件更新那一层，不是幂等层）。"""
    driven = await _drive_to_invoiced(world)
    request_id = driven["request_id"]
    created = await _create_payment(world, request_id)
    payment_id = created.json()["payment"]["id"]
    version = await _version(request_id)

    assert (await _pay(world, payment_id, version)).status_code == 200
    again = await _pay(world, payment_id, version)
    assert again.status_code in (409, 422), again.text


async def test_pay_with_stale_version_is_409(world: World) -> None:
    """版本过期 → **409**（乐观锁，与全项目同一条纪律）。"""
    driven = await _drive_to_invoiced(world)
    request_id = driven["request_id"]
    created = await _create_payment(world, request_id)
    payment_id = created.json()["payment"]["id"]
    version = await _version(request_id)

    response = await _pay(world, payment_id, version - 1)
    assert response.status_code == 409, response.text


async def test_pay_requires_finance_role(world: World) -> None:
    """采购执行付款 → **403**。"""
    driven = await _drive_to_invoiced(world)
    request_id = driven["request_id"]
    created = await _create_payment(world, request_id)
    payment_id = created.json()["payment"]["id"]
    version = await _version(request_id)

    response = await _pay(world, payment_id, version, client=world.procurement)
    assert response.status_code == 403, response.text


# ================================================================ 3. FAILED 与重新发起


async def test_failed_payment_allows_a_new_attempt(world: World) -> None:
    """**`FAILED` 之后允许重新发起** —— 这正是部分唯一索引要解决的问题。

    普通 `UNIQUE(request_id)` 会让这里的第二次创建撞唯一约束，
    于是"保留痕迹、允许重新发起"这条规则**根本执行不了**。
    """
    driven = await _drive_to_invoiced(world)
    request_id = driven["request_id"]
    first = await _create_payment(world, request_id)
    assert first.status_code == 201, first.text
    payment_id = first.json()["payment"]["id"]

    failed = await world.finance.post(
        f"/api/v1/payments/{payment_id}/fail", json={"reason": "银行退回：账号不符"}
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["payment"]["status"] == "FAILED"
    assert failed.json()["payment"]["failure_reason"] == "银行退回：账号不符"
    assert failed.json()["request_status"] == RequestStatus.INVOICED.value, "主单不动"

    # 重新发起（第二次尝试）
    second = await _create_payment(world, request_id)
    assert second.status_code == 201, second.text
    assert second.json()["payment"]["attempt_no"] == 2, "尝试号要递增，界面才说得出第几次"
    assert second.json()["payment"]["id"] != payment_id

    # 列表里两条都在（失败那条不能消失）
    listed = (await world.finance.get(f"/api/v1/requests/{request_id}/payments")).json()
    assert [row["status"] for row in listed] == ["FAILED", "PENDING"]

    # 第二次可以正常付成功
    version = await _version(request_id)
    paid = await _pay(world, second.json()["payment"]["id"], version)
    assert paid.status_code == 200, paid.text
    assert paid.json()["request_status"] == RequestStatus.PAID.value


async def test_only_one_active_payment_at_a_time(world: World) -> None:
    """有 `PENDING` 在的时候再建一张 → 409（部分唯一索引挡住"两张有效"）。"""
    driven = await _drive_to_invoiced(world)
    request_id = driven["request_id"]
    assert (await _create_payment(world, request_id)).status_code == 201
    assert (await _create_payment(world, request_id)).status_code == 409


async def test_fail_is_refused_when_not_pending(world: World) -> None:
    """已付款的付款单再标失败 → **409**。"""
    driven = await _drive_to_invoiced(world)
    request_id = driven["request_id"]
    created = await _create_payment(world, request_id)
    payment_id = created.json()["payment"]["id"]
    version = await _version(request_id)
    assert (await _pay(world, payment_id, version)).status_code == 200

    response = await world.finance.post(
        f"/api/v1/payments/{payment_id}/fail", json={"reason": "手滑"}
    )
    assert response.status_code == 409, response.text


async def test_failed_payment_does_not_release_the_budget(world: World) -> None:
    """标记失败**不动预算**：钱没出去，预留就还留着。

    这条挡的是"把失败也当成结算"：那样预留会被释放，而货还在、
    下次重新发起时预算已经不在自己名下了 —— 账就乱了。
    """
    driven = await _drive_to_invoiced(world)
    request_id = driven["request_id"]
    created = await _create_payment(world, request_id)
    payment_id = created.json()["payment"]["id"]

    before = await _budget_numbers(world.budget_id)  # type: ignore[arg-type]
    response = await world.finance.post(
        f"/api/v1/payments/{payment_id}/fail", json={"reason": "通道超时"}
    )
    assert response.status_code == 200, response.text
    assert (await _budget_numbers(world.budget_id)) == before  # type: ignore[arg-type]
    assert await _transactions(request_id) == [("RESERVE", REQUEST_AMOUNT_FEN)], (
        "失败不该写任何预算流水"
    )


# ================================================================ 4. 归档


async def test_close_moves_request_to_closed(world: World) -> None:
    """归档：`PAID → CLOSED`（终态），并写事件。"""
    driven = await _drive_to_invoiced(world)
    request_id = driven["request_id"]
    created = await _create_payment(world, request_id)
    payment_id = created.json()["payment"]["id"]
    version = await _version(request_id)
    assert (await _pay(world, payment_id, version)).status_code == 200

    version = await _version(request_id)
    response = await _close(world, request_id, version)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == RequestStatus.CLOSED.value

    timeline = (await world.requester.get(f"/api/v1/requests/{request_id}/timeline")).json()
    last = timeline["events"][-1]
    assert last["event_type"] == "REQUEST_CLOSED"
    assert last["from_status"] == RequestStatus.PAID.value
    assert last["to_status"] == RequestStatus.CLOSED.value


async def test_close_before_paid_is_refused(world: World) -> None:
    """还没付款就归档 → **409**。"""
    driven = await _drive_to_invoiced(world)
    version = await _version(driven["request_id"])
    response = await _close(world, driven["request_id"], version)
    assert response.status_code == 409, response.text
    assert "不能归档" in response.json()["error"]["message"]


async def test_closed_request_is_terminal(world: World) -> None:
    """归档之后不能再次归档（终态没有出口）。"""
    driven = await _drive_to_invoiced(world)
    request_id = driven["request_id"]
    created = await _create_payment(world, request_id)
    payment_id = created.json()["payment"]["id"]
    version = await _version(request_id)
    assert (await _pay(world, payment_id, version)).status_code == 200

    version = await _version(request_id)
    assert (await _close(world, request_id, version)).status_code == 200

    version = await _version(request_id)
    again = await _close(world, request_id, version)
    assert again.status_code == 409, again.text


async def test_closed_request_leaves_the_payment_queue(world: World) -> None:
    """归档之后不再出现在待付款队列里（队列按状态过滤）。"""
    driven = await _drive_to_invoiced(world)
    request_id = driven["request_id"]
    queue_before = (await world.finance.get("/api/v1/payments/queue")).json()
    assert request_id in [item["id"] for item in queue_before["items"]]

    created = await _create_payment(world, request_id)
    payment_id = created.json()["payment"]["id"]
    version = await _version(request_id)
    assert (await _pay(world, payment_id, version)).status_code == 200
    version = await _version(request_id)
    assert (await _close(world, request_id, version)).status_code == 200

    queue_after = (await world.finance.get("/api/v1/payments/queue")).json()
    assert request_id not in [item["id"] for item in queue_after["items"]]


# ================================================================ 5. 历史单据（budget_id 为 NULL）


async def test_pay_without_a_budget_still_works_and_writes_no_budget_rows(
    world: World,
) -> None:
    """**`budget_id` 为 NULL 的历史单据也能付款，但不写任何预算流水。**

    ============================ 这条路径为什么必须有一个用例 ============================
    `budget_id` 是 **M7 加上的**：M7 之前提交的单据没有它，也就没有预留。
    付款时 `pay` 里对 `budget_id is None` 的处理是**不报错**、按"这笔钱不走预算"继续
    （与 M7 的 `_release_all` 同一口径）。那条分支**只有这一种构造方式能走到** ——
    正常链路必带预算，所以不特意造一张没有预算的单子，它就永远不会被执行。

    一个"永远不被执行的分支"风险最大：它既不报错、也没人验证，
    将来重构时很容易被顺手改成 `raise`（那就把历史单据全部堵死在付款这一步），
    或者被改成"照常扣预算"（那就去动一条不属于这张单子的预算）。

    ⚠️ **它不是在认可"跳过预算是好事"**，它钉的是**现状**：
    历史单据必须付得出去，而且**不能**因此去写别人的预算流水。
    代价是"这笔钱没走预算"在账上没有任何痕迹 ——
    那条留痕的账记在 `Development progress.md` 的真债里。
    """
    driven = await _drive_to_invoiced(world)
    request_id = driven["request_id"]
    assert world.budget_id is not None

    # 造出"没有预算"的历史单据：预留流水**不删**（只追加的纪律），
    # 只把主单上的 `budget_id` 置空 —— 那正是 M7 之前提交的单据的样子。
    async with SessionFactory() as session:
        record = await session.get(PurchaseRequestRecord, uuid.UUID(request_id))
        assert record is not None
        record.budget_id = None
        await session.commit()

    created = await _create_payment(world, request_id)
    assert created.status_code == 201, created.text
    payment_id = created.json()["payment"]["id"]
    version = await _version(request_id)

    response = await _pay(world, payment_id, version)
    assert response.status_code == 200, (
        f"没有预算的历史单据必须付得出去，实际 {response.status_code}：{response.text}"
    )
    body = response.json()
    assert body["request_status"] == RequestStatus.PAID.value
    assert body["amount_fen"] == ORDER_AMOUNT_FEN, "实付金额照常记录"
    assert body["released_fen"] == 0, "没有预算就没有可释放的差额"

    # **不写任何预算流水**：不能去动那条不属于它的预算。
    # （留在这里的那条 RESERVE 是提交时写的，与 `budget_id` 是否还在无关。）
    assert await _transactions(request_id) == [("RESERVE", REQUEST_AMOUNT_FEN)]

    # 预算三个数**一个都不许变**
    assert await _budget_numbers(world.budget_id) == (
        BUDGET_AMOUNT_FEN,
        REQUEST_AMOUNT_FEN,
        0,
    ), "别人的预算不能被这张无预算单据动到"

    # 付款单上仍然写着实付金额（钱确实付了，只是没走预算）
    async with SessionFactory() as session:
        paid_amount = (
            await session.scalars(
                select(PaymentRecord.amount).where(
                    PaymentRecord.id == uuid.UUID(payment_id)
                )
            )
        ).one()
    assert int(paid_amount) == ORDER_AMOUNT_FEN


# ================================================================ 6. 全链路


async def test_full_chain_from_draft_to_closed(world: World) -> None:
    """**验收：走通一笔完整流程：申请 → 审批 → 询价 → 定标 → 订单 → 收货 → 发票 → 付款 → 归档。**

    `_drive_to_invoiced` 已经跑完了前八段，这里补最后两段，
    并逐个断言主单状态确实是沿着状态机一路走过来的。
    """
    driven = await _drive_to_invoiced(world)
    request_id = driven["request_id"]
    assert world.budget_id is not None

    detail = (await world.requester.get(f"/api/v1/requests/{request_id}")).json()
    assert detail["request"]["status"] == RequestStatus.INVOICED.value

    # 事件链要覆盖从提交到收票的全过程
    events = [
        event["event_type"]
        for event in (
            await world.requester.get(f"/api/v1/requests/{request_id}/timeline")
        ).json()["events"]
    ]
    for expected in (
        "REQUEST_SUBMITTED",
        "QUOTATION_ADDED",
        "QUOTATION_AWARDED",
        "ORDER_PLACED",
        "GOODS_RECEIVED",
        "INVOICE_ADDED",
    ):
        assert expected in events, f"事件链缺少 {expected}：{events}"

    created = await _create_payment(world, request_id)
    assert created.status_code == 201, created.text
    version = await _version(request_id)
    assert (await _pay(world, created.json()["payment"]["id"], version)).status_code == 200

    version = await _version(request_id)
    closed = await _close(world, request_id, version)
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == RequestStatus.CLOSED.value

    # 钱最终落定：总额不变、预留归零、已花 = 实付
    total, reserved, spent = await _budget_numbers(world.budget_id)
    assert (total, reserved, spent) == (BUDGET_AMOUNT_FEN, 0, ORDER_AMOUNT_FEN)

    # 完整流水：RESERVE(申请金额) → SPEND(实付) → RELEASE(差额)
    assert await _transactions(request_id) == [
        ("RESERVE", REQUEST_AMOUNT_FEN),
        (TX_SPEND, ORDER_AMOUNT_FEN),
        (TX_RELEASE, REQUEST_AMOUNT_FEN - ORDER_AMOUNT_FEN),
    ]
