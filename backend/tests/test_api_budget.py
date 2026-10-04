"""预算并发预留与释放的集成测试（M7）—— 需要 PostgreSQL。

跑法：`pytest -m integration`

逐条对着 M7 的验收标准写：

    1. 预算总额 1000 元时，同时提交两笔 800 元的申请，只有一笔成功，另一笔 422
    2. 驳回后预算可用额度恢复
    3. `budget_transactions` 里能看到 RESERVE / RELEASE / SPEND 三类流水
    4. **同一笔申请重放（同一 Idempotency-Key）不会产生第二条 RESERVE**
    5. **申请 1000 元、实际付款 900 元后：`amount_spent` +900、该申请预留归零、总额不变**
    6. **该申请三类流水相加（RESERVE − RELEASE − SPEND）= 0**
    7. 撤回也要释放（三个释放入口一个都不能漏）
    8. 没有预算行 = 计划外 → 422（2026-10-03 定稿口径）

============================ 第 5/6 条为什么在仓库层测 ============================
付款接口要到 M10 才做，但"预留与实付的差额必须显式释放"这条**现在就必须钉死** ——
它属于"违反不会报错、只会算错"的那一类。所以这里直接对着
`BudgetRepository.settle()` 测：M10 的付款接口只会调用它，不会另写一份算术。

清理策略：自己造部门 / 品类 / 预算 / 账号，结束后按外键顺序删干净。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, select

from app.database import SessionFactory
from app.domain.budget_policy import BudgetError
from app.domain.enums import TX_RELEASE, TX_RESERVE, TX_SPEND, RequestStatus
from app.domain.permissions import ROLE_DEPT_MANAGER, ROLE_REQUESTER
from app.errors import ConflictError
from app.infrastructure.records.approval import ApprovalFlowRecord, ApprovalTaskRecord
from app.infrastructure.records.auth import UserRecord
from app.infrastructure.records.catalog import (
    BudgetRecord,
    BudgetTransactionRecord,
    DepartmentRecord,
    PurchaseCategoryRecord,
)
from app.infrastructure.records.purchase import (
    IdempotencyRecord,
    PurchaseItemRecord,
    PurchaseRequestRecord,
    RequestEventRecord,
)
from app.infrastructure.repositories.budget_repo import BudgetRepository
from app.main import app
from tests.conftest import (
    Account,
    UserFactory,
    budget_year,
    create_budget,
    login_as,
)

pytestmark = pytest.mark.integration

#: 一级审批就够：这组用例验的是钱，不是审批链
ONE_STEP_POLICY = {
    "steps": [
        {
            "order": 10,
            "role": "DEPT_MANAGER",
            "mode": "SINGLE",
            "when": {"gte": ["amount_fen", 0]},
        }
    ]
}


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


class World:
    """一次用例的"世界"：部门 + 品类 + 预算 + 申请人 + 部门主管。"""

    def __init__(self) -> None:
        self.departments: list[uuid.UUID] = []
        self.categories: list[uuid.UUID] = []
        self.budgets: list[uuid.UUID] = []
        self.requests: list[uuid.UUID] = []
        self.actors: list[uuid.UUID] = []
        self.department: dict = {}
        self.category: dict = {}
        self.budget: dict = {}
        self.requester: AsyncClient
        self.manager: AsyncClient
        self._factory: UserFactory

    async def make(
        self, roles: list[str], department_id: uuid.UUID | None
    ) -> tuple[AsyncClient, Account]:
        account = await self._factory(roles=roles, department_id=department_id)
        self.actors.append(account.id)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        await login_as(client, account)
        return client, account

    async def purge(self) -> None:
        """按外键顺序清理（预算必须在品类与部门**之前**删：两处都是 RESTRICT）。"""
        async with SessionFactory() as session:
            if self.requests:
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
                # 预算流水是 CASCADE，但显式删一次，清理顺序就不依赖建表选项
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


WorldFactory = Callable[..., Coroutine[Any, Any, World]]


@pytest.fixture
async def make_world(
    admin_client: AsyncClient, user_factory: UserFactory
) -> AsyncIterator[WorldFactory]:
    """按**指定额度**造一个预算世界（额度就是这组用例要撞的那条线）。"""
    created: list[World] = []

    async def build(*, total_fen: int = 100_000) -> World:
        state = World()
        state._factory = user_factory

        department = (
            await admin_client.post(
                "/api/v1/admin/departments",
                json={"name": "预算测试部", "code": _code("TDEP")},
            )
        ).json()
        state.departments.append(uuid.UUID(department["id"]))
        state.department = department

        category = (
            await admin_client.post(
                "/api/v1/admin/categories",
                json={
                    "name": "预算测试品类",
                    "code": _code("TCAT"),
                    "approval_policy": ONE_STEP_POLICY,
                },
            )
        ).json()
        state.categories.append(uuid.UUID(category["id"]))
        state.category = category

        budget = await create_budget(
            admin_client,
            department_id=department["id"],
            category_id=category["id"],
            amount_fen=total_fen,
        )
        state.budgets.append(uuid.UUID(budget["id"]))
        state.budget = budget

        dept_id = uuid.UUID(department["id"])
        state.requester, _ = await state.make([ROLE_REQUESTER], dept_id)
        state.manager, _ = await state.make([ROLE_DEPT_MANAGER], dept_id)
        created.append(state)
        return state

    try:
        yield build
    finally:
        for state in created:
            await state.purge()


# ------------------------------------------------------------------ 辅助


async def _draft(client: AsyncClient, world: World, title: str, amount_fen: int) -> dict:
    """建草稿 + 填一行明细，返回最新的申请体（含 version）。"""
    created = await client.post(
        "/api/v1/requests", json={"category_id": world.category["id"], "title": title}
    )
    assert created.status_code == 201, created.text
    body = created.json()
    world.requests.append(uuid.UUID(body["id"]))

    items = await client.put(
        f"/api/v1/requests/{body['id']}/items",
        json={
            "version": body["version"],
            "items": [{"name": "测试物资", "quantity": 1, "unit_price_fen": amount_fen}],
        },
    )
    assert items.status_code == 200, items.text
    return items.json()


async def _submit(
    client: AsyncClient, body: dict, *, key: str | None = None
) -> Response:
    return await client.post(
        f"/api/v1/requests/{body['id']}/submit",
        json={"version": body["version"]},
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
    )


async def _budget_state(budget_id: uuid.UUID) -> dict[str, int]:
    """只读列、不读对象：接口那边是 bulk UPDATE，读对象会拿回身份映射里的旧值。"""
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
    return {"total": row[0], "reserved": row[1], "spent": row[2]}


async def _ledger(request_id: uuid.UUID) -> list[tuple[str, int]]:
    """这笔申请的流水（按写入顺序）。

    排序用 `seq`（数据库发的单调序号），不用 `created_at`：
    同一事务里写的两条流水时间戳可能相同，按时间排会退化成按随机的 `id` 排。
    """
    async with SessionFactory() as session:
        rows = (
            await session.execute(
                select(BudgetTransactionRecord.kind, BudgetTransactionRecord.amount)
                .where(BudgetTransactionRecord.request_id == request_id)
                .order_by(BudgetTransactionRecord.seq)
            )
        ).all()
    return [(row[0], row[1]) for row in rows]


async def _request_row(request_id: uuid.UUID) -> dict[str, Any]:
    async with SessionFactory() as session:
        row = (
            await session.execute(
                select(
                    PurchaseRequestRecord.status,
                    PurchaseRequestRecord.budget_id,
                    PurchaseRequestRecord.amount_total,
                ).where(PurchaseRequestRecord.id == request_id)
            )
        ).one()
    return {"status": row[0], "budget_id": row[1], "amount_total": row[2]}


async def _reject(client: AsyncClient, request_id: uuid.UUID) -> Response:
    """以部门主管身份驳回（走完整的 HTTP 链路，不是直接改库）。"""
    todo = await client.get("/api/v1/approvals/tasks")
    assert todo.status_code == 200, todo.text
    task = next(
        item["task"]
        for item in todo.json()["items"]
        if item["task"]["request_id"] == str(request_id)
    )
    return await client.post(
        f"/api/v1/approvals/tasks/{task['id']}/reject",
        json={"version": task["version"], "comment": "预算测试：驳回"},
    )


# ================================================================ 1. 并发预留


async def test_concurrent_submits_never_overdraw(
    make_world: WorldFactory, admin_client: AsyncClient
) -> None:
    """验收 1：总额 1000 元，同时提交两笔 800 元 → 一成一败，且只占住 800 元。

    这是"绝不先查余额再更新"那条纪律的验收：两个请求各自都读到"可用 1000 元"，
    若在 Python 里判断，两笔都会通过 —— 而且**不会报任何错**。
    """
    world = await make_world(total_fen=100_000)  # 1000 元
    budget_id = uuid.UUID(world.budget["id"])

    first = await _draft(world.requester, world, "并发 A", 80_000)
    second = await _draft(world.requester, world, "并发 B", 80_000)

    results = await asyncio.gather(
        _submit(world.requester, first), _submit(world.requester, second)
    )
    by_code = {response.status_code: response for response in results}
    assert sorted(by_code) == [200, 422], [r.text for r in results]

    error = by_code[422].json()["error"]
    assert error["code"] == "BUDGET_INSUFFICIENT"
    # 422 的文案要说清"可用多少 / 需要多少"，这是申请人唯一能自救的线索
    assert "可用 20000 分" in error["message"] and "需要 80000 分" in error["message"]

    state = await _budget_state(budget_id)
    assert state == {"total": 100_000, "reserved": 80_000, "spent": 0}

    winner = by_code[200].json()
    winner_id = uuid.UUID(winner["id"])
    loser_id = uuid.UUID(first["id"] if winner["id"] == second["id"] else second["id"])

    # 只有成功那一笔留下了 RESERVE 流水
    assert await _ledger(winner_id) == [(TX_RESERVE, 80_000)]

    # 失败那一笔必须**整体回滚**：状态还是草稿、没有认领预算、也没有任何流水
    loser_row = await _request_row(loser_id)
    assert loser_row["status"] == RequestStatus.DRAFT.value
    assert loser_row["budget_id"] is None
    assert await _ledger(loser_id) == []


async def test_insufficient_budget_returns_422_on_a_single_request(
    make_world: WorldFactory
) -> None:
    """单笔就超了也要 422（不靠并发才挡得住），并且**不自动放行**。"""
    world = await make_world(total_fen=50_000)  # 500 元
    body = await _draft(world.requester, world, "超额单", 60_000)

    response = await _submit(world.requester, body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "BUDGET_INSUFFICIENT"

    assert await _budget_state(uuid.UUID(world.budget["id"])) == {
        "total": 50_000,
        "reserved": 0,
        "spent": 0,
    }
    assert (await _request_row(uuid.UUID(body["id"])))["status"] == "DRAFT"


async def test_missing_budget_returns_422(
    make_world: WorldFactory, admin_client: AsyncClient
) -> None:
    """验收 8：**没有预算行 = 计划外 → 422**（2026-10-03 定稿口径）。

    没配预算就没有控制依据。放行等于把"计划外采购"变成一条无人知晓的通道，
    所以它和"超预算"同一个处置：先配 / 调预算，再重新提交。
    """
    world = await make_world(total_fen=100_000)

    # 再造一个品类，但**故意不给它配预算**
    other = (
        await admin_client.post(
            "/api/v1/admin/categories",
            json={
                "name": "没预算的品类",
                "code": _code("TCAT"),
                "approval_policy": ONE_STEP_POLICY,
            },
        )
    ).json()
    world.categories.append(uuid.UUID(other["id"]))

    created = await world.requester.post(
        "/api/v1/requests", json={"category_id": other["id"], "title": "计划外采购"}
    )
    draft = created.json()
    world.requests.append(uuid.UUID(draft["id"]))
    items = await world.requester.put(
        f"/api/v1/requests/{draft['id']}/items",
        json={
            "version": draft["version"],
            "items": [{"name": "测试物资", "quantity": 1, "unit_price_fen": 10_000}],
        },
    )
    assert items.status_code == 200, items.text

    response = await _submit(world.requester, items.json())
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "NO_BUDGET"
    assert str(budget_year()) in error["message"]
    assert "没预算的品类" in error["message"]


# ================================================================ 2. 释放


async def test_reject_releases_the_reservation(make_world: WorldFactory) -> None:
    """验收 2：驳回后可用额度恢复（并且留下 RELEASE 流水）。"""
    world = await make_world(total_fen=100_000)
    budget_id = uuid.UUID(world.budget["id"])

    body = await _draft(world.requester, world, "被驳回", 80_000)
    submitted = await _submit(world.requester, body)
    assert submitted.status_code == 200, submitted.text
    request_id = uuid.UUID(submitted.json()["id"])
    assert (await _budget_state(budget_id))["reserved"] == 80_000

    rejected = await _reject(world.manager, request_id)
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["request_status"] == RequestStatus.REJECTED.value

    state = await _budget_state(budget_id)
    assert state == {"total": 100_000, "reserved": 0, "spent": 0}
    assert await _ledger(request_id) == [(TX_RESERVE, 80_000), (TX_RELEASE, 80_000)]


async def test_withdraw_releases_the_reservation(make_world: WorldFactory) -> None:
    """第三个释放入口：申请人自己撤回也要把钱放回去（漏一个就永久虚低）。"""
    world = await make_world(total_fen=100_000)
    budget_id = uuid.UUID(world.budget["id"])

    body = await _draft(world.requester, world, "撤回单", 80_000)
    submitted = await _submit(world.requester, body)
    assert submitted.status_code == 200, submitted.text
    request_id = uuid.UUID(submitted.json()["id"])

    withdrawn = await world.requester.post(
        f"/api/v1/requests/{request_id}/withdraw",
        json={"version": submitted.json()["version"]},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["status"] == RequestStatus.WITHDRAWN.value

    assert (await _budget_state(budget_id))["reserved"] == 0
    assert await _ledger(request_id) == [(TX_RESERVE, 80_000), (TX_RELEASE, 80_000)]


# ================================================================ 3/4. 幂等与流水


async def test_idempotent_replay_does_not_reserve_twice(
    make_world: WorldFactory,
) -> None:
    """验收 4：同一把幂等键重放，**不会产生第二条 RESERVE**、也不会再占一份钱。"""
    world = await make_world(total_fen=100_000)
    budget_id = uuid.UUID(world.budget["id"])

    body = await _draft(world.requester, world, "重放单", 80_000)
    key = uuid.uuid4().hex

    first = await _submit(world.requester, body, key=key)
    assert first.status_code == 200, first.text
    replay = await _submit(world.requester, body, key=key)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json(), "重放必须原样返回上次的响应"

    request_id = uuid.UUID(first.json()["id"])
    assert await _ledger(request_id) == [(TX_RESERVE, 80_000)]
    assert (await _budget_state(budget_id))["reserved"] == 80_000

    # 顺带确认：预算表上的唯一约束也拦得住"绕过幂等再来一次"。
    # 直接调仓库，模拟"某条路径忘了幂等、又对同一笔申请预留一次"。
    async with SessionFactory() as session:
        repo = BudgetRepository(session)
        with pytest.raises(ConflictError, match="RESERVE"):
            await repo.reserve(
                budget_id=budget_id, request_id=request_id, amount_fen=1000
            )
            await session.commit()
        await session.rollback()


async def test_ledger_covers_all_three_kinds_and_sums_to_zero(
    make_world: WorldFactory,
) -> None:
    """验收 3/5/6：三类流水齐全，且 `RESERVE − RELEASE − SPEND = 0`。

    申请 1000 元 → 付款 900 元：
      - `amount_spent` 只增**实付** 900 元；
      - 该申请的预留**归零**（差额 100 元被显式释放）；
      - `amount_total` 全程不变。

    ⚠️ 付款接口（M10）还没做，所以这里直接对着 `BudgetRepository.settle()` 测 ——
    这条算术现在就得钉死，否则 M10 很容易只 `spend` 不 `release`。
    """
    world = await make_world(total_fen=100_000)
    budget_id = uuid.UUID(world.budget["id"])

    body = await _draft(world.requester, world, "结转单", 100_000)  # 1000 元
    submitted = await _submit(world.requester, body)
    assert submitted.status_code == 200, submitted.text
    request_id = uuid.UUID(submitted.json()["id"])

    async with SessionFactory() as session:
        repo = BudgetRepository(session)
        plan = await repo.settle(
            budget_id=budget_id, request_id=request_id, actual_fen=90_000
        )
        await session.commit()
    assert (plan.spend_fen, plan.release_fen) == (90_000, 10_000)

    state = await _budget_state(budget_id)
    assert state == {"total": 100_000, "reserved": 0, "spent": 90_000}

    ledger = await _ledger(request_id)
    assert ledger == [
        (TX_RESERVE, 100_000),
        (TX_SPEND, 90_000),
        (TX_RELEASE, 10_000),
    ]
    assert sum(
        amount if kind == TX_RESERVE else -amount for kind, amount in ledger
    ) == 0, "三类流水相加必须归零"


async def test_settlement_twice_is_refused(make_world: WorldFactory) -> None:
    """结清之后再结一次 → 拒绝。

    第二次结清意味着"同一笔钱被付了两遍"，而 `reserved_fen` 已经归零，
    纯函数会在这一步拦住（`BudgetError`），而不是记一笔凭空多出来的 SPEND。
    """
    world = await make_world(total_fen=100_000)
    budget_id = uuid.UUID(world.budget["id"])

    body = await _draft(world.requester, world, "重复结转", 100_000)
    submitted = await _submit(world.requester, body)
    request_id = uuid.UUID(submitted.json()["id"])

    async with SessionFactory() as session:
        repo = BudgetRepository(session)
        await repo.settle(budget_id=budget_id, request_id=request_id, actual_fen=90_000)
        await session.commit()

    async with SessionFactory() as session:
        repo = BudgetRepository(session)
        with pytest.raises(BudgetError):
            await repo.settle(
                budget_id=budget_id, request_id=request_id, actual_fen=90_000
            )
        await session.rollback()


# ================================================================ 看板接口（只读）


async def test_budget_board_is_finance_and_admin_only(
    make_world: WorldFactory,
    admin_client: AsyncClient,
    client: AsyncClient,
    user_factory: UserFactory,
) -> None:
    """看板的可见性：财务 / 管理员 200，申请人 403，未登录 401。

    `client` 是 conftest 里的**匿名**客户端；`admin_client` 是已登录的管理员。
    """
    world = await make_world(total_fen=100_000)

    # 未登录 → 401（"没登录"和"登录了但角色不够"是两件事）
    anonymous = await client.get("/api/v1/budgets")
    assert anonymous.status_code == 401, anonymous.text

    # 申请人 → 403
    forbidden = await world.requester.get("/api/v1/budgets")
    assert forbidden.status_code == 403, forbidden.text

    # 管理员 → 200，且能看到这条预算的四个数字 + 部门/品类名
    # （名字由后端带出：财务没有"列出全部部门 / 品类"的权限，不该为了看板开这个口子）
    allowed = await admin_client.get(f"/api/v1/budgets?year={budget_year()}")
    assert allowed.status_code == 200, allowed.text
    mine = [row for row in allowed.json() if row["id"] == world.budget["id"]]
    assert mine and mine[0]["amount_total_fen"] == 100_000
    assert mine[0]["department_name"] == "预算测试部"
    assert mine[0]["category_name"] == "预算测试品类"

    # 财务 → 200（这是财务的本职：审核采购预算）
    finance, _ = await world.make(["FINANCE"], None)
    finance_view = await finance.get("/api/v1/budgets")
    assert finance_view.status_code == 200, finance_view.text


async def test_budget_transactions_default_to_newest_first_and_carry_the_code(
    make_world: WorldFactory, admin_client: AsyncClient
) -> None:
    """流水接口要能直接当对账依据用：带单号、按时间新→旧。"""
    world = await make_world(total_fen=100_000)
    budget_id = uuid.UUID(world.budget["id"])

    body = await _draft(world.requester, world, "看板流水", 80_000)
    submitted = await _submit(world.requester, body)
    assert submitted.status_code == 200, submitted.text
    request_id = uuid.UUID(submitted.json()["id"])

    response = await admin_client.get(f"/api/v1/budgets/transactions?budget_id={budget_id}")
    assert response.status_code == 200, response.text
    rows = [row for row in response.json() if row["request_id"] == str(request_id)]
    assert len(rows) == 1
    assert rows[0]["kind"] == TX_RESERVE
    assert rows[0]["amount_fen"] == 80_000
    assert rows[0]["request_code"].startswith("PR-"), rows[0]

    # 释放之后流水变成两条，且**新→旧**（RELEASE 在前）
    rejected = await _reject(world.manager, request_id)
    assert rejected.status_code == 200, rejected.text
    after = await admin_client.get(
        f"/api/v1/budgets/transactions?budget_id={budget_id}"
    )
    kinds = [
        row["kind"] for row in after.json() if row["request_id"] == str(request_id)
    ]
    assert kinds == [TX_RELEASE, TX_RESERVE], kinds
