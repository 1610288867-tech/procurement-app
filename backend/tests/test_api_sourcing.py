"""询价与定标的集成测试（M10 / 8.1）：真数据库 + 真 HTTP。

验收项对齐 `PROGRESS.md` 的 M10 / 8.1 清单：

    只有 2 家报价且金额超过比价线 → 定标 422（并说清还差几家）
    补到 3 家 → 定标成功
    定标后：选中者 SELECTED、其余 NOT_SELECTED、主单 SOURCING → AWARDED
    成交单价 = 选中报价的原值（订单金额以它为准）

============================ 走完整链路才到得了 SOURCING ============================
定标的前提是"审批全通过"。所以每个用例都要先跑一段真实链路：

    建草稿 → 填明细 → 提交 → DEPT_MANAGER 通过 →（金额 >= 10 万元时）DEPT_HEAD 通过 → SOURCING

这段链路**不跳过**（不直接改库把状态设成 SOURCING）：询价的前置条件正是
"审批真的走完了"，绕过它就等于没验前置条件。`_drive_to_sourcing` 把它收在一处。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.application.coordinators.sourcing import build_sourcing_coordinator
from app.database import SessionFactory
from app.domain.enums import RequestStatus
from app.domain.permissions import (
    ROLE_DEPT_HEAD,
    ROLE_DEPT_MANAGER,
    ROLE_FINANCE,
    ROLE_PROCUREMENT,
    ROLE_REQUESTER,
)
from app.domain.sourcing_policy import (
    QUOTE_NOT_SELECTED,
    QUOTE_SELECTED,
    QUOTE_SUBMITTED,
    is_effective,
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

#: 比价线在测试里当**入参**用：默认调高，让"金额低于比价线"的用例不必凑三家。
#: 这一点本身就是"阈值来自配置"的好处 —— 规则与数值解耦。
HIGH_THRESHOLD = 100_000_000  # 100 万元

FULL_POLICY = {
    "steps": [
        {"order": 10, "role": "DEPT_MANAGER", "mode": "SINGLE", "when": {"gte": ["amount_fen", 0]}},
        {"order": 20, "role": "DEPT_HEAD", "mode": "SINGLE", "when": {"gte": ["amount_fen", 100_000]}},
        {"order": 30, "role": "PROCUREMENT", "mode": "SINGLE", "when": {"gte": ["amount_fen", 1_000_000]}},
        {"order": 40, "role": "FINANCE", "mode": "SINGLE", "when": {"gte": ["amount_fen", 1_000_000]}},
    ],
}


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


@pytest.fixture(autouse=True)
def threshold(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[int]:
    """默认把比价线调高（100 万元），让绝大多数用例不必凑三家。

    想验"超过比价线"的用例自己 `monkeypatch.setenv` 成一个小值 ——
    这就是"阈值来自配置、规则是纯函数"在测试上的直接好处。
    """
    monkeypatch.setenv("SOURCING_THRESHOLD_FEN", str(HIGH_THRESHOLD))
    from app.config import get_settings

    get_settings.cache_clear()
    yield HIGH_THRESHOLD
    get_settings.cache_clear()


class World:
    """一次用例的世界：部门 + 品类 + 预算 + 五个角色的客户端 + 清理。"""

    def __init__(self) -> None:
        self.departments: list[uuid.UUID] = []
        self.categories: list[uuid.UUID] = []
        self.budgets: list[uuid.UUID] = []
        self.requests: list[uuid.UUID] = []
        self.actors: list[uuid.UUID] = []
        self.suppliers: list[uuid.UUID] = []
        self.quotation_ids: list[uuid.UUID] = []
        self.category: dict = {}
        self.admin_client: AsyncClient
        self.requester: AsyncClient
        self.manager: AsyncClient
        self.head: AsyncClient
        self.procurement: AsyncClient
        self._factory: UserFactory

    async def make(
        self, roles: list[str], department_id: uuid.UUID | None
    ) -> tuple[AsyncClient, Account]:
        account = await self._factory(roles=roles, department_id=department_id)
        self.actors.append(account.id)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        await login_as(client, account)
        return client, account

    async def add_supplier(self, name: str = "报价供应商") -> dict:
        """建一个合格供应商（管理员接口）。

        名称带随机码：`suppliers.code` 唯一，重复跑用例会撞。
        """
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

        报价必须在申请**之前**删：`quotations.request_id` 是 CASCADE，
        但显式删更稳（清理顺序不依赖建表选项）；供应商是 RESTRICT，
        所以报价不删干净就删不掉供应商。
        """
        async with SessionFactory() as session:
            if self.requests:
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
    admin_client: AsyncClient, user_factory: UserFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[World]:
    """造一个自洽的世界（含附件目录指向临时目录 —— 报价可以挂附件）。"""
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()

    state = World()
    state.admin_client = admin_client
    state._factory = user_factory

    department = (
        await admin_client.post(
            "/api/v1/admin/departments", json={"name": "询价测试部", "code": _code("TDEP")}
        )
    ).json()
    state.departments.append(uuid.UUID(department["id"]))
    dept_id = uuid.UUID(department["id"])

    category = (
        await admin_client.post(
            "/api/v1/admin/categories",
            json={
                "name": "询价测试品类",
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

    # ⚠️ 审批人必须在**提交之前**造好：建链时会检查每一级有没有可用审批人，
    # 缺人直接 422（"审批链不许为空"那条约束）。
    state.requester, _ = await state.make([ROLE_REQUESTER], dept_id)
    state.manager, _ = await state.make([ROLE_DEPT_MANAGER], dept_id)
    state.head, _ = await state.make([ROLE_DEPT_HEAD], dept_id)
    state.procurement, _ = await state.make([ROLE_PROCUREMENT], dept_id)

    try:
        yield state
    finally:
        for client in (
            state.requester,
            state.manager,
            state.head,
            state.procurement,
        ):
            await client.aclose()
        await state.purge()
        get_settings.cache_clear()


# ------------------------------------------------------------------ 辅助


async def _submit(
    client: AsyncClient, world: World, title: str, amount_fen: int
) -> dict:
    """建草稿 → 填明细 → 提交。返回申请详情（**已登记进 world.requests**）。"""
    draft = await client.post(
        "/api/v1/requests", json={"category_id": world.category["id"], "title": title}
    )
    assert draft.status_code == 201, draft.text
    body = draft.json()
    world.requests.append(uuid.UUID(body["id"]))

    items = await client.put(
        f"/api/v1/requests/{body['id']}/items",
        json={
            "version": body["version"],
            "items": [{"name": "测试物资", "quantity": 1, "unit_price_fen": amount_fen}],
        },
    )
    assert items.status_code == 200, items.text

    submitted = await client.post(
        f"/api/v1/requests/{body['id']}/submit",
        json={"version": items.json()["version"]},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert submitted.status_code == 200, submitted.text
    return submitted.json()


async def _approve_my_level(client: AsyncClient, request_id: str) -> None:
    """用**页面上会用的那条接口**批掉当前级（不直接改库）。"""
    todo = (await client.get("/api/v1/approvals/tasks")).json()
    mine = [item for item in todo["items"] if item["request"]["id"] == request_id]
    assert mine, f"这个账号的待办里没有这张单子：{request_id}"
    task = mine[0]["task"]
    response = await client.post(
        f"/api/v1/approvals/tasks/{task['id']}/approve",
        json={"version": task["version"]},
    )
    assert response.status_code == 200, response.text


async def _drive_to_sourcing(client: AsyncClient, world: World, amount_fen: int) -> dict:
    """把一张单子从草稿推到 `SOURCING`（审批全通过）。

    金额 >= 10 万元时链上有两级，两级都要批 —— **不跳过前置条件**：
    询价的前提正是"审批真的走完了"。
    """
    submitted = await _submit(client, world, "询价单", amount_fen)
    request_id = submitted["id"]
    await _approve_my_level(world.manager, request_id)
    if amount_fen >= 100_000:
        await _approve_my_level(world.head, request_id)
    detail = (await client.get(f"/api/v1/requests/{request_id}")).json()
    assert detail["request"]["status"] == RequestStatus.SOURCING.value, detail["request"]
    return detail["request"]


async def _quote(
    client: AsyncClient, request_id: str, supplier_id: str, amount_fen: int, **extra
) -> object:
    body = {"supplier_id": supplier_id, "amount_fen": amount_fen, **extra}
    return await client.post(f"/api/v1/requests/{request_id}/quotations", json=body)


async def _award(
    client: AsyncClient, request_id: str, supplier_id: str, version: int, *, key: str | None = None
):
    return await client.post(
        f"/api/v1/requests/{request_id}/award",
        json={"supplier_id": supplier_id, "version": version},
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
    )


async def _statuses(request_id: str) -> dict[str, str]:
    """该申请下每家报价的状态（**供应商 id → 状态**）。

    ⚠️ 键是 `supplier_id` 而不是报价 id：用例是按供应商断言"选中/落选"的，
    拿报价 id 当键会得到一份"永远对不上"的字典（第一版就写错成 id，
    断言过不去而看不出原因）。
    """
    from sqlalchemy import select

    async with SessionFactory() as session:
        rows = await session.execute(
            select(QuotationRecord.supplier_id, QuotationRecord.status).where(
                QuotationRecord.request_id == uuid.UUID(request_id)
            )
        )
    return {str(supplier): status for supplier, status in rows.all()}


# ================================================================ 1. 待询价队列


async def test_queue_lists_only_sourcing_requests(world: World) -> None:
    """队列只列 `SOURCING` 的单子 —— 审批中的不该出现。"""
    drafting = await _submit(world.requester, world, "还在审批", 50_000)
    sourced = await _drive_to_sourcing(world.requester, world, 50_000)

    queue = await world.procurement.get("/api/v1/sourcing/queue")
    assert queue.status_code == 200, queue.text
    ids = [item["id"] for item in queue.json()["items"]]
    assert sourced["id"] in ids
    assert drafting["id"] not in ids, "审批中的单子不该出现在待询价队列里"


async def test_queue_requires_procurement_role(world: World) -> None:
    """申请人调队列 → **403**（询价是采购的工作台）。

    可见范围是一层，角色是另一层：少了这一层，任何登录用户都能拿到全公司的待询价清单。
    """
    denied = await world.requester.get("/api/v1/sourcing/queue")
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "FORBIDDEN"


async def test_queue_is_empty_without_login(world: World) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        response = await anon.get("/api/v1/sourcing/queue")
    assert response.status_code == 401, response.text


# ================================================================ 2. 登记报价


async def test_register_three_quotations(world: World) -> None:
    """登记三家报价：各返回 201，比价表里三行，问题列表为空（金额低于比价线）。"""
    request = await _drive_to_sourcing(world.requester, world, 50_000)
    suppliers = [await world.add_supplier(f"供应商{index}") for index in range(3)]

    for index, supplier in enumerate(suppliers):
        response = await _quote(
            world.procurement, request["id"], supplier["id"], 40_000 + index * 100
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == QUOTE_SUBMITTED
        assert body["amount_fen"] == 40_000 + index * 100
        assert body["supplier_name"] == supplier["name"], "响应要带上供应商名（界面直接用）"

    compare = await world.procurement.get(f"/api/v1/requests/{request['id']}/quotations")
    assert compare.status_code == 200, compare.text
    body = compare.json()
    assert len(body["quotations"]) == 3
    assert body["problems"] == [], "金额低于比价线时，问题列表该是空的"
    assert body["min_quotations"] == 3


async def test_quotation_requires_the_request_to_be_sourcing(world: World) -> None:
    """审批中的单子不能登记报价 → **409**（先走完审批再询价）。"""
    drafting = await _submit(world.requester, world, "还没批完", 50_000)
    supplier = await world.add_supplier()
    response = await _quote(world.procurement, drafting["id"], supplier["id"], 40_000)
    assert response.status_code == 409, response.text
    assert "SOURCING" in response.json()["error"]["message"]


async def test_duplicate_supplier_is_refused(world: World) -> None:
    """同一家报两次 → **409**，且提示说清原因。

    这条守的是唯一约束 `(request_id, supplier_id)`：少了它，
    "三家比价"可以靠同一家传三次凑出来 —— 而那条控制的意义正是竞争。
    """
    request = await _drive_to_sourcing(world.requester, world, 50_000)
    supplier = await world.add_supplier()

    assert (await _quote(world.procurement, request["id"], supplier["id"], 40_000)).status_code == 201
    again = await _quote(world.procurement, request["id"], supplier["id"], 39_000)
    assert again.status_code == 409, again.text
    assert "已经报过价" in again.json()["error"]["message"]


async def test_quotation_amount_must_be_positive(world: World) -> None:
    """0 元报价 → **422**：它会让家数虚增，而比价表上看着像一家真实报价。"""
    request = await _drive_to_sourcing(world.requester, world, 50_000)
    supplier = await world.add_supplier()
    response = await _quote(world.procurement, request["id"], supplier["id"], 0)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "QUOTATION_AMOUNT_INVALID"


async def test_disabled_supplier_cannot_quote(world: World) -> None:
    """停用的供应商不能参与比价 → **422**（业务规则第 3 条的硬控制之一）。"""
    request = await _drive_to_sourcing(world.requester, world, 50_000)
    supplier = await world.add_supplier()
    disabled = await world.admin_client.post(
        f"/api/v1/admin/suppliers/{supplier['id']}/disable",
        json={"reason": "询价用例：停用"},
    )
    assert disabled.status_code == 200, disabled.text

    response = await _quote(world.procurement, request["id"], supplier["id"], 40_000)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "SUPPLIER_NOT_QUALIFIED"


async def test_quotation_records_a_timeline_event(world: World) -> None:
    """登记报价写时间线事件，且**不改变主单状态**（还是 SOURCING）。"""
    request = await _drive_to_sourcing(world.requester, world, 50_000)
    supplier = await world.add_supplier()
    await _quote(world.procurement, request["id"], supplier["id"], 40_000)

    timeline = (await world.requester.get(f"/api/v1/requests/{request['id']}/timeline")).json()
    last = timeline["events"][-1]
    assert last["event_type"] == "QUOTATION_ADDED"
    assert last["from_status"] == last["to_status"] == RequestStatus.SOURCING.value
    assert last["payload"]["amount_fen"] == 40_000
    assert last["payload"]["supplier_name"] == supplier["name"]


# ================================================================ 3. 定标：验收 8.1


async def test_two_quotations_above_threshold_cannot_award(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**验收 8.1 前半：只有 2 家报价且金额超过比价线 → 定标 422。**

    报错要说清"还差 1 家"，否则采购专员不知道该干什么。
    """
    monkeypatch.setenv("SOURCING_THRESHOLD_FEN", "100000")  # 比价线 1000 元
    from app.config import get_settings

    get_settings.cache_clear()

    request = await _drive_to_sourcing(world.requester, world, 500_000)  # 5000 元
    suppliers = [await world.add_supplier(f"两家{index}") for index in range(2)]
    for supplier in suppliers:
        assert (
            await _quote(world.procurement, request["id"], supplier["id"], 400_000)
        ).status_code == 201

    response = await _award(
        world.procurement, request["id"], suppliers[0]["id"], request["version"]
    )
    assert response.status_code == 422, response.text
    message = response.json()["error"]["message"]
    assert "至少需要 3 家" in message
    assert "当前 2 家" in message
    assert "还差 1 家" in message

    # 定标失败**不能留下半截状态**：主单还在 SOURCING，报价都还是 SUBMITTED
    detail = (await world.requester.get(f"/api/v1/requests/{request['id']}")).json()
    assert detail["request"]["status"] == RequestStatus.SOURCING.value
    assert set((await _statuses(request["id"])).values()) == {QUOTE_SUBMITTED}


async def test_three_quotations_above_threshold_can_award(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**验收 8.1 后半：补到 3 家 → 定标成功**，且选中/落选、主单状态都对。"""
    monkeypatch.setenv("SOURCING_THRESHOLD_FEN", "100000")
    from app.config import get_settings

    get_settings.cache_clear()

    request = await _drive_to_sourcing(world.requester, world, 500_000)
    suppliers = [await world.add_supplier(f"三家{index}") for index in range(3)]
    amounts = [400_000, 380_000, 410_000]
    for supplier, amount in zip(suppliers, amounts, strict=True):
        assert (
            await _quote(world.procurement, request["id"], supplier["id"], amount)
        ).status_code == 201

    # 选中间那家（最便宜）
    response = await _award(
        world.procurement, request["id"], suppliers[1]["id"], request["version"]
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["request"]["status"] == RequestStatus.AWARDED.value
    assert body["quotation"]["status"] == QUOTE_SELECTED
    assert body["deal_unit_price_fen"] == 380_000, (
        "成交单价必须是**选中报价的原值**：订单金额以它为准（ER 图第 136-142 行），"
        "写错会连带把三单匹配也带错"
    )

    statuses = await _statuses(request["id"])
    assert statuses[suppliers[1]["id"]] == QUOTE_SELECTED
    assert statuses[suppliers[0]["id"]] == QUOTE_NOT_SELECTED
    assert statuses[suppliers[2]["id"]] == QUOTE_NOT_SELECTED

    timeline = (await world.requester.get(f"/api/v1/requests/{request['id']}/timeline")).json()
    awarded = [event for event in timeline["events"] if event["event_type"] == "QUOTATION_AWARDED"]
    assert len(awarded) == 1
    assert awarded[0]["payload"]["deal_unit_price_fen"] == 380_000
    assert awarded[0]["from_status"] == RequestStatus.SOURCING.value
    assert awarded[0]["to_status"] == RequestStatus.AWARDED.value


async def test_award_rejects_a_supplier_that_did_not_quote(world: World) -> None:
    """定标给一家**没参与报价**的供应商 → 422（不是 500、也不是默默成交）。"""
    request = await _drive_to_sourcing(world.requester, world, 50_000)
    quoting = await world.add_supplier("报了价的")
    outsider = await world.add_supplier("没报价的")
    assert (await _quote(world.procurement, request["id"], quoting["id"], 40_000)).status_code == 201

    response = await _award(
        world.procurement, request["id"], outsider["id"], request["version"]
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "SUPPLIER_NOT_QUOTED"


async def test_award_with_stale_version_is_409(world: World) -> None:
    """版本过期 → 409（乐观锁，与全项目同一条纪律）。"""
    request = await _drive_to_sourcing(world.requester, world, 50_000)
    supplier = await world.add_supplier()
    await _quote(world.procurement, request["id"], supplier["id"], 40_000)

    response = await _award(
        world.procurement, request["id"], supplier["id"], request["version"] - 1
    )
    assert response.status_code == 409, response.text


async def test_second_award_is_refused(world: World) -> None:
    """已经定过标再定一次 → **409**（不是 200 也不是 500）。

    这条守的是"条件更新 + 状态校验"两层：`mark_awarded` 的守卫是
    `status = SUBMITTED`，主单状态校验是 `SOURCING`。第二次两边都会挡住。
    """
    request = await _drive_to_sourcing(world.requester, world, 50_000)
    suppliers = [await world.add_supplier(f"重复定标{index}") for index in range(2)]
    for supplier in suppliers:
        await _quote(world.procurement, request["id"], supplier["id"], 40_000)

    first = await _award(world.procurement, request["id"], suppliers[0]["id"], request["version"])
    assert first.status_code == 200, first.text

    current = (await world.requester.get(f"/api/v1/requests/{request['id']}")).json()
    second = await _award(
        world.procurement, request["id"], suppliers[1]["id"], current["request"]["version"]
    )
    assert second.status_code == 409, second.text


async def test_award_replay_with_the_same_key_returns_the_first_response(world: World) -> None:
    """同键重放 → 拿到**第一次的响应**（逐字节相同），而不是 409。

    这就是"带幂等键"要解决的问题：网络超时后用户重试，
    不该因为"已经定过标了"而收到一个让人以为失败的冲突。
    """
    request = await _drive_to_sourcing(world.requester, world, 50_000)
    suppliers = [await world.add_supplier(f"重放{index}") for index in range(2)]
    for supplier in suppliers:
        await _quote(world.procurement, request["id"], supplier["id"], 40_000)

    key = uuid.uuid4().hex
    first = await _award(
        world.procurement, request["id"], suppliers[0]["id"], request["version"], key=key
    )
    assert first.status_code == 200, first.text

    replay = await _award(
        world.procurement, request["id"], suppliers[0]["id"], request["version"], key=key
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json(), "同键重放必须返回逐字节相同的响应"


async def test_award_with_same_key_but_another_supplier_is_409(world: World) -> None:
    """同键**不同供应商** = 改了主意 → 409（与提交那条同一个约定）。

    否则用户以为"改成定标给另一家"成功了，实际拿到的是第一次的结果 ——
    而且系统里记的是第一家，两边说法不一致。
    """
    request = await _drive_to_sourcing(world.requester, world, 50_000)
    suppliers = [await world.add_supplier(f"改主意{index}") for index in range(2)]
    for supplier in suppliers:
        await _quote(world.procurement, request["id"], supplier["id"], 40_000)

    key = uuid.uuid4().hex
    first = await _award(
        world.procurement, request["id"], suppliers[0]["id"], request["version"], key=key
    )
    assert first.status_code == 200, first.text

    changed = await _award(
        world.procurement, request["id"], suppliers[1]["id"], request["version"], key=key
    )
    assert changed.status_code == 409, changed.text


# ================================================================ 4. 过期报价


async def test_expired_quotation_does_not_count_and_gets_settled(world: World) -> None:
    """过期报价**不算家数**，而且读比价表时会被**惰性结算**成 `EXPIRED`。

    两件事一起验：
      1. 判定按时间算（3 家里有 1 家过期 → 仍然不满足）；
      2. 库里那行的状态会从 `SUBMITTED` 变成 `EXPIRED`（投影跟上判定）。
    """
    request = await _drive_to_sourcing(world.requester, world, 50_000)
    suppliers = [await world.add_supplier(f"过期{index}") for index in range(3)]
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()

    for index, supplier in enumerate(suppliers):
        extra = {"valid_until": past} if index == 2 else {}
        assert (
            await _quote(world.procurement, request["id"], supplier["id"], 40_000, **extra)
        ).status_code == 201

    before = await _statuses(request["id"])
    assert before[suppliers[2]["id"]] == QUOTE_SUBMITTED, "登记时并不知道自己已经过期"

    # 读一次比价表 —— 惰性结算在这里发生
    compare = await world.procurement.get(f"/api/v1/requests/{request['id']}/quotations")
    assert compare.status_code == 200, compare.text

    after = await _statuses(request["id"])
    assert after[suppliers[2]["id"]] == "EXPIRED", "读比价表要顺手把过期报价结算掉"

    # 定标给过期那家 → 422（它不是有效报价）
    denial = await _award(
        world.procurement, request["id"], suppliers[2]["id"], request["version"]
    )
    assert denial.status_code == 422, denial.text


async def test_settlement_is_idempotent_and_the_status_column_stays_truthful(
    world: World,
) -> None:
    """**报表口径的守门用例**（2026-10-05 补）。

    ============================ 它守的是什么 ============================
    `EXPIRED` 只在有人结算时才写进状态列。所以"直接读状态列做统计"是危险的：
    会数到"过期却写着 SUBMITTED"的行。

    修法不是让每处都记得调用结算，而是把"结算 + 读"合成**唯一入口**
    （`SourcingCoordinator.quotations_ready`），并要求任何按状态数报价的地方
    都只能走它。这条用例钉两件事：

      1. 走那个入口之后，状态列与时间判定（`is_effective`）**一致**；
      2. 重复走是**幂等**的（第二次没有东西可结算，也不会把已结算的改回去）。
    """
    request = await _drive_to_sourcing(world.requester, world, 50_000)
    suppliers = [await world.add_supplier(f"口径{index}") for index in range(2)]
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    assert (
        await _quote(world.procurement, request["id"], suppliers[0]["id"], 40_000)
    ).status_code == 201
    assert (
        await _quote(
            world.procurement, request["id"], suppliers[1]["id"], 41_000, valid_until=past
        )
    ).status_code == 201

    # 直接读**状态列**（不走那个入口）：此刻它还是过时的 —— 这正是危险所在
    assert (await _statuses(request["id"]))[suppliers[1]["id"]] == QUOTE_SUBMITTED

    async def ready():  # noqa: ANN202
        async with SessionFactory() as session:
            coordinator = build_sourcing_coordinator(
                session,
                claim_session_factory=SessionFactory,
                threshold_fen=100_000_000,
            )
            return await coordinator.quotations_ready(uuid.UUID(request["id"]))

    rows, facts = await ready()
    for row, fact in zip(rows, facts, strict=True):
        assert (row.status == QUOTE_SUBMITTED) is is_effective(
            status=fact.status,
            valid_until=fact.valid_until,
            supplier_usable=fact.supplier_usable,
        ), f"状态列与 is_effective 判定不一致：{row.status} / {fact.valid_until}"

    # 幂等：再走一次，没有东西可结算，状态不变
    again_rows, _ = await ready()
    assert [row.status for row in again_rows] == [row.status for row in rows]

    statuses = await _statuses(request["id"])
    assert statuses[suppliers[0]["id"]] == QUOTE_SUBMITTED
    assert statuses[suppliers[1]["id"]] == "EXPIRED"


async def test_long_valid_quotation_never_expires(world: World) -> None:
    """不设有效期 = **长期有效**（不是"已过期"）—— 一条报价也能在低价单上定标。

    这条挡的是把 `NULL` 当过期的那种写法：它会让所有"没填有效期"的报价
    永远定不了标，而提示还说"报价已过期"，指错方向。
    """
    request = await _drive_to_sourcing(world.requester, world, 50_000)
    supplier = await world.add_supplier("长期有效")
    assert (
        await _quote(world.procurement, request["id"], supplier["id"], 40_000)
    ).status_code == 201

    response = await _award(
        world.procurement, request["id"], supplier["id"], request["version"]
    )
    assert response.status_code == 200, response.text
    assert response.json()["request"]["status"] == RequestStatus.AWARDED.value


# ================================================================ 5. 权限


async def test_applicant_cannot_award(world: World) -> None:
    """申请人定标 → **403**（询价与定标是采购的职权）。"""
    request = await _drive_to_sourcing(world.requester, world, 50_000)
    supplier = await world.add_supplier()
    await _quote(world.procurement, request["id"], supplier["id"], 40_000)

    response = await _award(
        world.requester, request["id"], supplier["id"], request["version"]
    )
    assert response.status_code == 403, response.text


async def test_finance_cannot_quote(world: World) -> None:
    """财务登记报价 → **403**：那条"询价与定标由采购完成"的口径要在**入口**上体现。"""
    request = await _drive_to_sourcing(world.requester, world, 50_000)
    supplier = await world.add_supplier()
    finance, _ = await world.make([ROLE_FINANCE], uuid.UUID(world.departments[0].hex))
    response = await _quote(finance, request["id"], supplier["id"], 40_000)
    assert response.status_code == 403, response.text
    await finance.aclose()
