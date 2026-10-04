"""审批引擎的集成测试（M6）—— 需要 PostgreSQL。

跑法：`pytest -m integration`

逐条对着 M6 的验收标准写：

    1. 四笔基准金额 → 任务数 1 / 2 / 4 / 5
    2. 500 元的申请只有部门主管能看到待办
    3. **没轮到第 2–5 级时，它们的审批人看不到；第 1 级通过后第 2 级才出现**
    4. **越级审批 → 403，且任务仍是 PENDING**
    5. **两人同时通过同一级的两个会签任务，current_step_order 只推进一级**
    6. 两人同时通过同一个任务 → 一个 200、一个 409
    7. 驳回 → 主单 REJECTED，时间线有驳回意见
    8. 待办列表一次 JOIN 查完
    9. 全部通过 → 主单 SOURCING + APPROVAL_COMPLETED
   10. 改策略不改代码，链路跟着变
   11. 改完策略后历史申请仍能复现当时的策略（policy_snapshot）
   12. 某级缺人 → 422 并说明哪一级
   13. `ALL` 会签：3 人 → 3 条任务、预分配、全部通过才算完成
   14. `SINGLE` 或签：3 人 → 1 条任务，任一人认领通过即完成
   15. 没兜底 / 有死步骤的策略 → 422

清理策略：自己造部门 / 品类 / 账号与各角色审批人，结束后按外键顺序删干净。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.database import SessionFactory
from app.domain.enums import RequestStatus
from app.domain.permissions import ROLE_REQUESTER
from app.infrastructure.records.approval import ApprovalFlowRecord, ApprovalTaskRecord
from app.infrastructure.records.auth import UserRecord
from app.infrastructure.records.catalog import (
    BudgetRecord,
    DepartmentRecord,
    PurchaseCategoryRecord,
)
from app.infrastructure.records.purchase import (
    IdempotencyRecord,
    PurchaseItemRecord,
    PurchaseRequestRecord,
    RequestEventRecord,
)
from app.main import app
from tests.conftest import Account, UserFactory, create_budget, login_as

pytestmark = pytest.mark.integration

#: 与《金额-审批链对照表》一致的策略：500/5000/50000/200000 元 → 1/2/4/5 级
FULL_POLICY = {
    "steps": [
        {"order": 10, "role": "DEPT_MANAGER", "mode": "SINGLE", "when": {"gte": ["amount_fen", 0]}},
        {"order": 20, "role": "DEPT_HEAD", "mode": "SINGLE", "when": {"gte": ["amount_fen", 100_000]}},
        {"order": 30, "role": "PROCUREMENT", "mode": "SINGLE", "when": {"gte": ["amount_fen", 1_000_000]}},
        {"order": 40, "role": "FINANCE", "mode": "SINGLE", "when": {"gte": ["amount_fen", 1_000_000]}},
        {"order": 50, "role": "GM", "mode": "SINGLE", "when": {"gte": ["amount_fen", 10_000_000]}},
    ],
    "skip_when_framework_contract": False,
}


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


class World:
    """一次用例的"世界"：造出来的数据 + 已登录的各角色客户端。

    自己管清理（`purge`），并且**在用例结束时一定跑** ——
    否则开发库里会堆满测试残留（M5 期间真踩过：120 个账号、86 个品类）。
    """

    def __init__(self) -> None:
        self.departments: list[uuid.UUID] = []
        self.categories: list[uuid.UUID] = []
        self.budgets: list[uuid.UUID] = []
        self.requests: list[uuid.UUID] = []
        self.actors: list[uuid.UUID] = []
        # 下面这些在 fixture 里填
        self.department: dict = {}
        self.category: dict = {}
        self.requester: AsyncClient
        self.requester_account: Account
        self.manager: AsyncClient
        self.manager_account: Account
        self.admin_client: AsyncClient
        self._factory: UserFactory

    async def make(
        self, roles: list[str], department_id: uuid.UUID | None
    ) -> tuple[AsyncClient, Account]:
        """再造一个已登录的客户端（账号由 `user_factory` 负责最终清理）。"""
        account = await self._factory(roles=roles, department_id=department_id)
        self.actors.append(account.id)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        await login_as(client, account)
        return client, account

    async def budget_for(
        self, category_id: str, *, amount_fen: int = 100_000_000
    ) -> dict:
        """给某个品类补一条今年的预算。

        M7 起**提交必须有预算**（没有预算行 = 计划外 → 422），
        所以用例临时造的新品类也要配一条，否则提交会被预算拦住 ——
        那条用例验的就不是审批了。
        """
        budget = await create_budget(
            self.admin_client,
            department_id=self.department["id"],
            category_id=category_id,
            amount_fen=amount_fen,
        )
        self.budgets.append(uuid.UUID(budget["id"]))
        return budget

    async def purge(self) -> None:
        """按外键顺序清理。

        `approval_tasks` / `approval_flows` 由 `purchase_requests` 级联，
        但这里显式删：清理顺序不依赖建表选项，读起来也更清楚。
        """
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
                # 预算要在**品类与部门之前**删：budgets 对这两张表都是 RESTRICT
                # （M7 起用例会造预算；流水则由上面删申请时级联带走）。
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
                # 删部门前要把**所有** RESTRICT 引用清掉，否则删部门会静默 0 行、
                # 留下孤儿部门（实测踩过：跑多轮后库里多了 TDEP-* 残骸）。
                # 引用 departments 的一共四处：
                #   users.department_id / budgets.department_id /
                #   purchase_requests.department_id / departments.parent_id
                # （预算那一条已在上面按 id 删掉）
                # 解绑仍挂在待删部门上的测试账号（顺序不保证，置空比赌它删干净稳）
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
    admin_client: AsyncClient, user_factory: UserFactory
) -> AsyncIterator[World]:
    """最小可用的审批世界：一个部门 + 一个品类 + 一个申请人 + 一个部门主管。

    刻意**不用 seed 数据**：用例之间不该互相污染，也不该要求开发库处于某个状态。
    需要别的角色（负责人 / 采购 / 财务 / GM）时，用 `world.make([...], dept)` 现场造 ——
    这样每条用例只会拥有它真正需要的那几个人。
    """
    state = World()
    state.admin_client = admin_client
    state._factory = user_factory
    try:
        department = (
            await admin_client.post(
                "/api/v1/admin/departments", json={"name": "行政部", "code": _code("TDEP")}
            )
        ).json()
        state.departments.append(uuid.UUID(department["id"]))
        state.department = department

        category = (
            await admin_client.post(
                "/api/v1/admin/categories",
                json={
                    "name": "办公用品",
                    "code": _code("TCAT"),
                    "approval_policy": FULL_POLICY,
                },
            )
        ).json()
        state.categories.append(uuid.UUID(category["id"]))
        state.category = category

        dept_id = uuid.UUID(department["id"])
        state.requester, state.requester_account = await state.make([ROLE_REQUESTER], dept_id)
        state.manager, state.manager_account = await state.make(["DEPT_MANAGER"], dept_id)
        # M7 起提交必须有预算（没有预算行 = 计划外 → 422）。
        # 额度定得比任何一条用例的申请都大：这里验的是审批，不是预算不足 ——
        # 预算边界由 `tests/test_api_budget.py` 专门去撞。
        budget = await create_budget(
            admin_client,
            department_id=department["id"],
            category_id=category["id"],
            amount_fen=100_000_000,
        )
        state.budgets.append(uuid.UUID(budget["id"]))
        yield state
    finally:
        await state.purge()


# ------------------------------------------------------------------ 辅助


async def _submit(
    client: AsyncClient, category_id: str, title: str, amount_fen: int, *, key: str | None = None
) -> dict:
    """建草稿 → 填一行明细 → 提交。返回申请详情。"""
    draft = await client.post(
        "/api/v1/requests", json={"category_id": category_id, "title": title}
    )
    assert draft.status_code == 201, draft.text
    body = draft.json()
    items = await client.put(
        f"/api/v1/requests/{body['id']}/items",
        json={
            "version": body["version"],
            "items": [{"name": "办公用品", "quantity": 1, "unit_price_fen": amount_fen}],
        },
    )
    assert items.status_code == 200, items.text

    submitted = await client.post(
        f"/api/v1/requests/{body['id']}/submit",
        json={"version": items.json()["version"]},
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
    )
    assert submitted.status_code == 200, submitted.text
    return submitted.json()


#: 常量：审批任务状态
PENDING = "PENDING"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
CANCELLED = "CANCELLED"


async def _tasks_of(request_id: str) -> list[ApprovalTaskRecord]:
    """查任务 —— **只读列，不读对象**。

    为什么不用 `select(ApprovalTaskRecord)`：接口那边是用 bulk UPDATE 改的库
    （`synchronize_session=False`），如果这个会话里恰好已经加载过同一个任务对象，
    再查就会拿回**身份映射里缓存的那份旧值**，于是测试会得出
    "接口成功了但数据没变"的假结论 —— 这个坑在本项目里踩过两次
    （M5 的条件更新回读、M6 的步骤完成判定）。

    只选列就绕开了身份映射，读到的必然是库里的当前值。
    """
    async with SessionFactory() as session:
        rows = await session.execute(
            select(
                ApprovalTaskRecord.id,
                ApprovalTaskRecord.request_id,
                ApprovalTaskRecord.step_order,
                ApprovalTaskRecord.role,
                ApprovalTaskRecord.mode,
                ApprovalTaskRecord.status,
                ApprovalTaskRecord.assignee_id,
                ApprovalTaskRecord.decided_by,
                ApprovalTaskRecord.comment,
                ApprovalTaskRecord.version,
            )
            .where(ApprovalTaskRecord.request_id == uuid.UUID(request_id))
            .order_by(ApprovalTaskRecord.step_order, ApprovalTaskRecord.created_at)
        )
        return [_TaskRow(row) for row in rows.all()]


class _TaskRow:
    """把列查询结果包成"有属性"的小对象，方便测试里按 `task.role` 那样读。"""

    def __init__(self, row: Any) -> None:
        (
            self.id,
            self.request_id,
            self.step_order,
            self.role,
            self.mode,
            self.status,
            self.assignee_id,
            self.decided_by,
            self.comment,
            self.version,
        ) = row


async def _flow_of(request_id: str):
    """查审批流 —— 同样只读列（理由见 `_tasks_of`）。"""
    async with SessionFactory() as session:
        rows = await session.execute(
            select(
                ApprovalFlowRecord.id,
                ApprovalFlowRecord.request_id,
                ApprovalFlowRecord.status,
                ApprovalFlowRecord.current_step_order,
                ApprovalFlowRecord.policy_snapshot,
                ApprovalFlowRecord.policy_version,
                ApprovalFlowRecord.started_at,
                ApprovalFlowRecord.completed_at,
            ).where(ApprovalFlowRecord.request_id == uuid.UUID(request_id))
        )
        row = rows.one_or_none()
        if row is None:
            return None
        flow = _FlowRow()
        (
            flow.id,
            flow.request_id,
            flow.status,
            flow.current_step_order,
            flow.policy_snapshot,
            flow.policy_version,
            flow.started_at,
            flow.completed_at,
        ) = row
        return flow


class _FlowRow:
    pass


async def _todo(client: AsyncClient) -> dict:
    response = await client.get("/api/v1/approvals/tasks")
    assert response.status_code == 200, response.text
    return response.json()


async def _todo_for(client: AsyncClient, request_id: str) -> list[dict]:
    """待办里**属于这一条申请**的条目。

    ⚠️ 为什么不用 `todo["total"]`：那是这个账号的**全部**待办，
    开发库里只要还有别的单子（例如你手动建的、或浏览器实测留下的），
    断言"总数 == 1"就会莫名其妙地失败 —— 而功能其实是对的。
    这类"依赖库是空的"断言是测试脆弱性的常见来源，所以统一改成
    只看**本次用例自己造的那些申请**。
    """
    todo = await _todo(client)
    return [item for item in todo["items"] if item["request"]["id"] == request_id]


async def _submit_expect(
    world: World,
    client: AsyncClient,
    category_id: str,
    title: str,
    amount_fen: int,
) -> tuple[int, dict, uuid.UUID]:
    """建草稿 → 填明细 → 提交，返回 `(状态码, 响应体, 草稿 id)`，**不吞错误**。

    与 `_submit` 的区别：`_submit` 断言必须成功，这里要能拿到失败的响应体
    （SoD 用例正是要断言它失败）。**第三个返回值是草稿 id** ——
    提交失败时响应体里只有 `error`，没有 `id`，但回滚断言仍然需要申请 id。

    ⚠️ 草稿**一建出来就登记进 `world.requests`**，不管后面提交成不成功 ——
    否则提交失败时草稿会留在库里，teardown 删品类时撞外键
    （`purchase_requests.category_id` → `purchase_categories`）。
    """
    draft = await client.post(
        "/api/v1/requests", json={"category_id": category_id, "title": title}
    )
    assert draft.status_code == 201, draft.text
    body = draft.json()
    request_id = uuid.UUID(body["id"])
    world.requests.append(request_id)
    items = await client.put(
        f"/api/v1/requests/{body['id']}/items",
        json={
            "version": body["version"],
            "items": [{"name": "办公用品", "quantity": 1, "unit_price_fen": amount_fen}],
        },
    )
    response = await client.post(
        f"/api/v1/requests/{body['id']}/submit",
        json={"version": items.json()["version"]},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    return response.status_code, response.json(), request_id


# ================================================================ 0. 职责分离（SoD）


async def test_sole_approver_cannot_submit_own_request(world: World) -> None:
    """**提交人不能审批自己的申请（2026-10-03 补）。**

    世界里的部门主管只有 `world.manager` 一人，让他提交 5000 元（要两级）——
    第 1 级唯一符合条件的人就是他自己。此时应当**在提交时**拒绝，
    而不是生成一条"自己审自己"的任务。

    对应依据：内控指引第 7 号第五条"不得安排同一机构办理采购业务全过程"；
    上市公司制度范本不相容岗位第（一）条"请购与审批"。
    """
    department_id = uuid.UUID(world.department["id"])
    await world.make(["DEPT_HEAD"], department_id)

    status, body, request_id = await _submit_expect(
        world, world.manager, world.category["id"], "自提单", 500_000
    )
    assert status == 422, body
    error = body["error"]
    assert error["code"] == "SELF_APPROVAL_NOT_ALLOWED", error
    assert "order=10" in error["message"] and "DEPT_MANAGER" in error["message"]

    # 失败要整体回滚：主单仍是草稿，且**没有**留下半截审批流
    detail = (await world.manager.get(f"/api/v1/requests/{request_id}")).json()
    assert detail["request"]["status"] == "DRAFT"
    assert await _flow_of(str(request_id)) is None


async def test_requester_is_excluded_and_others_can_approve(world: World) -> None:
    """部门有**两个**主管时：提交人被剔除，另一个主管接手，流程照常走完第 1 级。

    这条证明"排除提交人"不是简单地把整级砍掉，而是**顺延给同级其他人**
    （Oracle Prohibit User Self-Approval 的 skip 语义）。
    """
    department_id = uuid.UUID(world.department["id"])
    other_manager, _ = await world.make(["DEPT_MANAGER"], department_id)
    await world.make(["DEPT_HEAD"], department_id)

    status, body, request_id = await _submit_expect(
        world, world.manager, world.category["id"], "有人接手", 500_000
    )
    assert status == 200, body
    request_id = str(request_id)

    # 提交人自己的待办里**不能**有这一条
    mine = await _todo(world.manager)
    assert all(item["request"]["id"] != request_id for item in mine["items"]), (
        "提交人自己的待办里出现了自己的申请"
    )

    # 另一个主管看得到，并且能批掉它 → 推进到第 2 级
    theirs = await _todo(other_manager)
    rows = [item for item in theirs["items"] if item["request"]["id"] == request_id]
    assert len(rows) == 1
    task = rows[0]["task"]
    approved = await other_manager.post(
        f"/api/v1/approvals/tasks/{task['id']}/approve",
        json={"version": task["version"], "comment": "代审"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["current_step_order"] == 20


async def test_all_mode_does_not_assign_a_task_to_the_requester(world: World) -> None:
    """会签（ALL）也要剔除提交人：否则"全部通过"里混进了申请人自己那一票。"""
    department_id = uuid.UUID(world.department["id"])
    policy = {
        "steps": [
            {"order": 10, "role": "DEPT_MANAGER", "mode": "ALL", "when": {"gte": ["amount_fen", 0]}},
        ]
    }
    category = (
        await world.admin_client.post(
            "/api/v1/admin/categories",
            json={"name": "SoD会签", "code": _code("TCAT"), "approval_policy": policy},
        )
    ).json()
    world.categories.append(uuid.UUID(category["id"]))
    await world.budget_for(category["id"])

    others = [await world.make(["DEPT_MANAGER"], department_id) for _ in range(2)]

    status, body, request_uuid = await _submit_expect(
        world, world.manager, category["id"], "会签剔除提交人", 50_000
    )
    assert status == 200, body
    request_id = str(request_uuid)

    tasks = [
        task for task in await _tasks_of(request_id) if task.role == "DEPT_MANAGER"
    ]
    # 3 个主管里剔掉提交人（world.manager）→ 2 条任务，且都不是他
    assert len(tasks) == 2, f"应当只给另外两个人各建一条：{tasks}"
    submitter_id = world.manager_account.id
    assert all(task.assignee_id != submitter_id for task in tasks)
    for client, _ in others:
        assert len(await _todo_for(client, request_id)) == 1


# ================================================================ 1. 任务数


@pytest.mark.parametrize(
    ("amount_fen", "expected_tasks", "roles"),
    [
        (50_000, 1, ["DEPT_MANAGER"]),
        (500_000, 2, ["DEPT_MANAGER", "DEPT_HEAD"]),
        (5_000_000, 4, ["DEPT_MANAGER", "DEPT_HEAD", "PROCUREMENT", "FINANCE"]),
        (
            20_000_000,
            5,
            ["DEPT_MANAGER", "DEPT_HEAD", "PROCUREMENT", "FINANCE", "GM"],
        ),
    ],
)
async def test_task_count_matches_the_amount_table(
    world: World, amount_fen: int, expected_tasks: int, roles: list[str]
) -> None:
    """验收 1：500 / 5000 / 50000 / 200000 元 → 任务数 1 / 2 / 4 / 5。

    这一条同时证明"审批链是数据驱动的"：任务的级数完全由**品类策略**算出来。
    """
    # 这个用例只关心链路，先把各角色审批人补齐
    department_id = uuid.UUID(world.department["id"])
    for role in roles:
        await world.make([role], department_id if role.startswith("DEPT_") else None)

    submitted = await _submit(
        world.requester, world.category["id"], f"{amount_fen} 分", amount_fen
    )
    world.requests.append(uuid.UUID(submitted["id"]))

    tasks = await _tasks_of(submitted["id"])
    assert len(tasks) == expected_tasks
    assert [task.role for task in tasks] == roles
    assert all(task.status == "PENDING" for task in tasks)


# ================================================================ 2/3. 可见性


async def test_only_current_step_is_visible(world: World) -> None:
    """验收 2 + 3：只有当前这一级的审批人能看到待办；高级别看不到。

    5000 元 → 两级（主管 → 负责人）。第 1 级没批完之前，
    负责人**必须看不到**这条待办；主管通过之后，它才出现在负责人的待办里。
    """
    department_id = uuid.UUID(world.department["id"])
    head, _ = await world.make(["DEPT_HEAD"], department_id)

    submitted = await _submit(world.requester, world.category["id"], "两级审批", 500_000)
    world.requests.append(uuid.UUID(submitted["id"]))

    # 第 1 级：主管看得到
    manager_rows = await _todo_for(world.manager, submitted["id"])
    assert len(manager_rows) == 1

    # 第 2 级：负责人看不到（还没轮到）
    assert await _todo_for(head, submitted["id"]) == [], "还没轮到的级别不该出现在待办里"

    # 主管通过 → 轮到第 2 级
    manager_task = manager_rows[0]["task"]
    approved = await world.manager.post(
        f"/api/v1/approvals/tasks/{manager_task['id']}/approve",
        json={"version": manager_task["version"]},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["current_step_order"] == 20

    assert len(await _todo_for(head, submitted["id"])) == 1
    # 上一级的任务自然退出待办
    assert await _todo_for(world.manager, submitted["id"]) == []


async def test_500_yuan_is_invisible_to_procurement_and_finance(world: World) -> None:
    """验收 2：500 元的申请只有部门主管能看到；采购与财务看不到。"""
    procurement, _ = await world.make(["PROCUREMENT"], None)
    finance, _ = await world.make(["FINANCE"], None)

    submitted = await _submit(world.requester, world.category["id"], "小额", 50_000)
    world.requests.append(uuid.UUID(submitted["id"]))

    assert len(await _todo_for(world.manager, submitted["id"])) == 1
    assert await _todo_for(procurement, submitted["id"]) == []
    assert await _todo_for(finance, submitted["id"]) == []


# ================================================================ 4. 越级审批


async def test_approving_out_of_turn_is_forbidden(world: World) -> None:
    """验收 4：拿第 4 级任务 id 直接调 approve → 403，且任务仍是 PENDING。

    关键在"**不是靠界面藏按钮**"：待办列表不显示它，但接口是可以被直接调用的
    （Postman 拿 id 打一下就能绕过界面）。所以决定接口自己也要校验。
    """
    department_id = uuid.UUID(world.department["id"])
    for role in ("DEPT_HEAD", "PROCUREMENT", "FINANCE"):
        await world.make([role], department_id if role == "DEPT_HEAD" else None)
    finance_client, _ = await world.make(["FINANCE"], None)

    submitted = await _submit(world.requester, world.category["id"], "越级", 5_000_000)
    world.requests.append(uuid.UUID(submitted["id"]))

    tasks = await _tasks_of(submitted["id"])
    finance_task = next(task for task in tasks if task.role == "FINANCE")
    assert finance_task.step_order == 40

    response = await finance_client.post(
        f"/api/v1/approvals/tasks/{finance_task.id}/approve",
        json={"version": finance_task.version},
    )
    assert response.status_code == 403, response.text

    # 任务必须原封不动
    after = await _tasks_of(submitted["id"])
    still = next(task for task in after if task.role == "FINANCE")
    assert still.status == "PENDING"
    assert still.decided_by is None


# ================================================================ 5/13/14. 会签与或签


async def test_all_mode_creates_one_task_per_person_and_needs_all(
    world: World,
) -> None:
    """验收 13：`ALL` 会签 —— 3 人 → 3 条任务、都预分配、全部通过才算完成。"""
    department_id = uuid.UUID(world.department["id"])
    # 把第 2 级改成会签，并造 3 个部门负责人
    policy = {
        "steps": [
            {"order": 10, "role": "DEPT_MANAGER", "mode": "SINGLE", "when": {"gte": ["amount_fen", 0]}},
            {"order": 20, "role": "DEPT_HEAD", "mode": "ALL", "when": {"gte": ["amount_fen", 100_000]}},
        ]
    }
    category = (
        await world.admin_client.post(
            "/api/v1/admin/categories",
            json={"name": "会签品类", "code": _code("TCAT"), "approval_policy": policy},
        )
    ).json()
    world.categories.append(uuid.UUID(category["id"]))
    await world.budget_for(category["id"])

    heads = [await world.make(["DEPT_HEAD"], department_id) for _ in range(3)]

    submitted = await _submit(world.requester, category["id"], "会签", 500_000)
    world.requests.append(uuid.UUID(submitted["id"]))

    # 第 1 级先批掉，才能轮到会签那一级
    manager_todo = await _todo(world.manager)
    manager_task = manager_todo["items"][0]["task"]
    await world.manager.post(
        f"/api/v1/approvals/tasks/{manager_task['id']}/approve",
        json={"version": manager_task["version"]},
    )

    head_tasks = [task for task in await _tasks_of(submitted["id"]) if task.role == "DEPT_HEAD"]
    assert len(head_tasks) == 3
    assert all(task.assignee_id is not None for task in head_tasks), "会签必须预分配"

    # 每人只看到自己那一条
    for client, _ in heads:
        todo = await _todo(client)
        assert len(todo["items"]) == 1

    # 前两个人通过：该级还没完成，指针不许动
    for client, _ in heads[:2]:
        task = (await _todo(client))["items"][0]["task"]
        response = await client.post(
            f"/api/v1/approvals/tasks/{task['id']}/approve",
            json={"version": task["version"]},
        )
        assert response.status_code == 200
        assert response.json()["step_completed"] is False
    assert (await _flow_of(submitted["id"])).current_step_order == 20

    # 第三个人通过：该级完成，全部通过 → 主单 SOURCING
    task = (await _todo(heads[2][0]))["items"][0]["task"]
    response = await heads[2][0].post(
        f"/api/v1/approvals/tasks/{task['id']}/approve",
        json={"version": task["version"]},
    )
    assert response.status_code == 200
    assert response.json()["request_status"] == RequestStatus.SOURCING.value


async def test_single_mode_creates_one_task_anyone_can_claim(world: World) -> None:
    """验收 14：`SINGLE` 或签 —— 3 人 → **1 条**任务，3 人都能看到，任一人认领通过即完成。"""
    department_id = uuid.UUID(world.department["id"])
    # 注意 `make` 返回 `(client, account)`：`world.manager` 在 fixture 里已经解包过，
    # 这里为了统一形状补一个占位第二项（本用例用不到那两个 account）。
    managers: list[tuple[AsyncClient, Any]] = [(world.manager, None)]
    for _ in range(2):
        managers.append(await world.make(["DEPT_MANAGER"], department_id))

    submitted = await _submit(world.requester, world.category["id"], "或签", 50_000)
    world.requests.append(uuid.UUID(submitted["id"]))

    tasks = [task for task in await _tasks_of(submitted["id"]) if task.role == "DEPT_MANAGER"]
    assert len(tasks) == 1
    assert tasks[0].assignee_id is None

    for client, _ in managers:
        assert len(await _todo_for(client, submitted["id"])) == 1

    # 第一个人认领
    task_id = tasks[0].id
    claimed = await managers[1][0].post(
        f"/api/v1/approvals/tasks/{task_id}/claim", json={"version": tasks[0].version}
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["assignee_id"] is not None

    # 第二个人再认领 → **403**：任务已分配给他人 —— "可见但无权"是 403；
    # 409 留给"版本过期 / 已被处理"这类状态冲突。两种语义不能混。
    second = await managers[2][0].post(
        f"/api/v1/approvals/tasks/{task_id}/claim",
        json={"version": claimed.json()["version"]},
    )
    assert second.status_code == 403, second.text

    # 认领人通过 → 单级链走完 → SOURCING
    done = await managers[1][0].post(
        f"/api/v1/approvals/tasks/{task_id}/approve",
        json={"version": claimed.json()["version"]},
    )
    assert done.status_code == 200, done.text
    assert done.json()["request_status"] == RequestStatus.SOURCING.value


# ================================================================ 6. 并发同任务


async def test_two_people_approving_the_same_task_only_one_wins(
    world: World,
) -> None:
    """验收 6：两个人同时通过**同一个**任务，只有一个成功，另一个 409。"""
    department_id = uuid.UUID(world.department["id"])
    other_manager, _ = await world.make(["DEPT_MANAGER"], department_id)

    submitted = await _submit(world.requester, world.category["id"], "抢批", 50_000)
    world.requests.append(uuid.UUID(submitted["id"]))

    task = (await _todo(world.manager))["items"][0]["task"]
    responses = await asyncio.gather(
        world.manager.post(
            f"/api/v1/approvals/tasks/{task['id']}/approve",
            json={"version": task["version"]},
        ),
        other_manager.post(
            f"/api/v1/approvals/tasks/{task['id']}/approve",
            json={"version": task["version"]},
        ),
        return_exceptions=True,
    )
    codes = sorted(getattr(response, "status_code", 0) for response in responses)
    assert codes.count(200) == 1, f"应当恰好一个成功：{codes}"
    assert codes[1] == 409, f"另一个应当是 409：{codes}"


# ================================================================ 5. 指针推进


async def test_concurrent_approvals_of_one_step_advance_only_one_step(
    world: World,
) -> None:
    """验收 5：两人同时通过同一级的两个会签任务 → 指针**只推进一级**。

    这一条专测"先读再写"的坑：如果用读-改-写推进 `current_step_order`，
    两边都读到旧值、都写"下一级"，指针会被推两格，**跳过一整级**，
    而那一级的审批人永远等不到待办 —— 而且不会有人发现。
    """
    department_id = uuid.UUID(world.department["id"])
    policy = {
        "steps": [
            {"order": 10, "role": "DEPT_MANAGER", "mode": "ALL", "when": {"gte": ["amount_fen", 0]}},
            {"order": 20, "role": "DEPT_HEAD", "mode": "SINGLE", "when": {"gte": ["amount_fen", 100_000]}},
            {"order": 30, "role": "FINANCE", "mode": "SINGLE", "when": {"gte": ["amount_fen", 100_000]}},
        ]
    }
    category = (
        await world.admin_client.post(
            "/api/v1/admin/categories",
            json={"name": "并发品类", "code": _code("TCAT"), "approval_policy": policy},
        )
    ).json()
    world.categories.append(uuid.UUID(category["id"]))
    await world.budget_for(category["id"])

    second_manager, _ = await world.make(["DEPT_MANAGER"], department_id)
    await world.make(["DEPT_HEAD"], department_id)
    await world.make(["FINANCE"], None)

    submitted = await _submit(world.requester, category["id"], "并发推进", 5_000_000)
    world.requests.append(uuid.UUID(submitted["id"]))

    # 第 1 级是**两个**主管的会签
    manager_tasks = [
        task for task in await _tasks_of(submitted["id"]) if task.role == "DEPT_MANAGER"
    ]
    assert len(manager_tasks) == 2

    fetched = [
        (await _todo(world.manager))["items"][0]["task"],
        (await _todo(second_manager))["items"][0]["task"],
    ]
    responses = await asyncio.gather(
        world.manager.post(
            f"/api/v1/approvals/tasks/{fetched[0]['id']}/approve",
            json={"version": fetched[0]["version"]},
        ),
        second_manager.post(
            f"/api/v1/approvals/tasks/{fetched[1]['id']}/approve",
            json={"version": fetched[1]["version"]},
        ),
        return_exceptions=True,
    )
    assert all(getattr(r, "status_code", 0) == 200 for r in responses), [
        getattr(r, "text", r) for r in responses
    ]

    flow = await _flow_of(submitted["id"])
    assert flow is not None
    # 只推进到第 2 级；如果被推两格就会变成 30（跳过部门负责人）
    if flow.current_step_order != 20:
        # 排查用：把两次响应体与库里的任务状态一起打出来
        detail = await _tasks_of(submitted["id"])
        raise AssertionError(
            "指针没停在 20："
            f"current_step_order={flow.current_step_order}，"
            f"流状态={flow.status}；"
            f"两次响应={[getattr(r, 'text', r) for r in responses]}；"
            "库里任务="
            + str([(t.step_order, t.role, t.status) for t in detail])
        )


# ================================================================ 7. 驳回


async def test_reject_sets_request_rejected_and_records_comment(
    world: World,
) -> None:
    """验收 7：主管驳回 → 主单 `REJECTED`，申请人时间线上能看到驳回意见。"""
    department_id = uuid.UUID(world.department["id"])
    await world.make(["DEPT_HEAD"], department_id)

    submitted = await _submit(world.requester, world.category["id"], "被驳回", 500_000)
    world.requests.append(uuid.UUID(submitted["id"]))

    task = (await _todo(world.manager))["items"][0]["task"]
    rejected = await world.manager.post(
        f"/api/v1/approvals/tasks/{task['id']}/reject",
        json={"version": task["version"], "comment": "预算不够，下季度再报"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["request_status"] == RequestStatus.REJECTED.value

    # 申请人看得到驳回意见
    detail = (await world.requester.get(f"/api/v1/requests/{submitted['id']}")).json()
    assert detail["request"]["status"] == RequestStatus.REJECTED.value
    assert detail["request"]["closed_at"] is not None
    rejection_events = [
        event for event in detail["timeline"] if event["event_type"] == "APPROVAL_REJECTED"
    ]
    assert len(rejection_events) == 1
    assert "预算不够" in rejection_events[0]["payload"]["comment"]

    # 后面的任务被取消，不该继续挂在别人待办里
    tasks = await _tasks_of(submitted["id"])
    assert all(task.status != "PENDING" for task in tasks)
    # 被驳回的那条保持 REJECTED（与"被取消"含义不同）
    assert any(task.status == "REJECTED" for task in tasks)


async def test_reject_requires_a_comment(world: World) -> None:
    """驳回意见必填：它是申请人唯一能看到的理由。"""
    submitted = await _submit(world.requester, world.category["id"], "没意见", 50_000)
    world.requests.append(uuid.UUID(submitted["id"]))

    task = (await _todo(world.manager))["items"][0]["task"]
    response = await world.manager.post(
        f"/api/v1/approvals/tasks/{task['id']}/reject",
        json={"version": task["version"], "comment": ""},
    )
    assert response.status_code == 422


# ================================================================ 9. 完成推进


async def test_all_steps_approved_pushes_request_to_sourcing(world: World) -> None:
    """验收 9：全部通过后主单自动变 `SOURCING`，时间线有 `APPROVAL_COMPLETED`。"""
    submitted = await _submit(world.requester, world.category["id"], "单级通过", 50_000)
    world.requests.append(uuid.UUID(submitted["id"]))

    task = (await _todo(world.manager))["items"][0]["task"]
    response = await world.manager.post(
        f"/api/v1/approvals/tasks/{task['id']}/approve",
        json={"version": task["version"], "comment": "同意"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["flow_status"] == "APPROVED"
    assert body["request_status"] == RequestStatus.SOURCING.value

    detail = (await world.requester.get(f"/api/v1/requests/{submitted['id']}")).json()
    types = [event["event_type"] for event in detail["timeline"]]
    assert "APPROVAL_COMPLETED" in types
    assert detail["request"]["status"] == RequestStatus.SOURCING.value


# ================================================================ 12. 缺人


async def test_missing_approver_for_a_step_returns_422(world: World) -> None:
    """验收 12：把该级的审批人全删掉再提交 → 422 并说明是哪一级缺人。

    这一条把"提交成功、永久卡死、无人知情"变成"提交时就报错、立刻能修"。
    """
    # 这个品类需要部门负责人，但世界里没有 DEPT_HEAD
    draft = await world.requester.post(
        "/api/v1/requests",
        json={"category_id": world.category["id"], "title": "缺人", "reason": None},
    )
    body = draft.json()
    world.requests.append(uuid.UUID(body["id"]))
    items = await world.requester.put(
        f"/api/v1/requests/{body['id']}/items",
        json={
            "version": body["version"],
            "items": [{"name": "办公用品", "quantity": 1, "unit_price_fen": 500_000}],
        },
    )

    response = await world.requester.post(
        f"/api/v1/requests/{body['id']}/submit",
        json={"version": items.json()["version"]},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "NO_APPROVER_FOR_STEP"
    assert "DEPT_HEAD" in error["message"]
    assert "order=20" in error["message"]

    # 失败要整体回滚：主单仍在草稿，且没有留下半截审批流
    detail = (await world.requester.get(f"/api/v1/requests/{body['id']}")).json()
    assert detail["request"]["status"] == RequestStatus.DRAFT.value
    assert await _flow_of(body["id"]) is None


# ================================================================ 10/11. 策略快照


async def test_policy_change_changes_chain_and_history_keeps_snapshot(
    world: World,
) -> None:
    """验收 10 + 11：改策略不改代码，链路跟着变；**历史申请仍能复现当时的策略**。"""
    department_id = uuid.UUID(world.department["id"])
    await world.make(["DEPT_HEAD"], department_id)

    # ① 用原策略（1,000 元起走负责人）提交 5,000 元 → 2 级
    first = await _submit(world.requester, world.category["id"], "改策略前", 500_000)
    world.requests.append(uuid.UUID(first["id"]))
    assert len(await _tasks_of(first["id"])) == 2

    # ② 把阈值从 1,000 元改成 10,000 元（**只改数据**）
    patched_policy = {
        "steps": [
            {"order": 10, "role": "DEPT_MANAGER", "when": {"gte": ["amount_fen", 0]}},
            {"order": 20, "role": "DEPT_HEAD", "when": {"gte": ["amount_fen", 1_000_000]}},
        ]
    }
    updated = await world.admin_client.put(
        f"/api/v1/admin/categories/{world.category['id']}/approval-policy",
        json={"approval_policy": patched_policy},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2

    # ③ 同样 5,000 元再提交一笔 → 现在只有 1 级（阈值涨上去了）
    second = await _submit(world.requester, world.category["id"], "改策略后", 500_000)
    world.requests.append(uuid.UUID(second["id"]))
    assert len(await _tasks_of(second["id"])) == 1

    # ④ 历史那笔仍能复现它当时的策略原件与版本
    flow = await _flow_of(first["id"])
    assert flow is not None
    assert flow.policy_version == 1
    snapshot = flow.policy_snapshot
    snapshot_threshold = snapshot["steps"][1]["when"]["gte"][1]
    assert snapshot_threshold == 100_000, "快照必须是提交那一刻的原件，不是现查的结果"
    # 快照是**品类那份完整策略**（5 步），不是"算出来的链"（那 2 级在任务表里）
    assert len(snapshot["steps"]) == 5
    assert [step["order"] for step in snapshot["steps"]] == [10, 20, 30, 40, 50]

    # ⑤ 通过进度接口也能读到这份快照
    progress = await world.requester.get(
        f"/api/v1/approvals/requests/{first['id']}/flow"
    )
    assert progress.status_code == 200, progress.text
    assert progress.json()["policy_version"] == 1
    assert progress.json()["current_step_order"] == 10


# ================================================================ 8. 待办 JOIN


async def test_todo_list_is_one_query(world: World) -> None:
    """验收 8：待办列表**一次 JOIN 查完**，不是"查 20 条任务再逐条查申请"。

    做法很直接：造 3 条待办，数该请求期间执行的 SELECT 条数。
    如果每条任务都去补查申请与品类，就是 1 + 3×2 = 7 条以上。
    """
    submitted_ids: list[str] = []
    for index in range(3):
        submitted = await _submit(
            world.requester, world.category["id"], f"待办 {index}", 50_000
        )
        world.requests.append(uuid.UUID(submitted["id"]))
        submitted_ids.append(submitted["id"])

    import logging

    logger = logging.getLogger("sqlalchemy.engine")
    records: list[str] = []

    class _Counter(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            message = record.getMessage()
            if "SELECT" in message.upper():
                records.append(message)

    handler = _Counter()
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        todo = await _todo(world.manager)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    # 只数**本次造的**那几条：开发库里可能还有别的待办（手工建的、实测留的），
    # 断言 `total` 会让这条用例依赖"库是空的"。
    mine = [item for item in todo["items"] if item["request"]["id"] in submitted_ids]
    assert len(mine) == len(submitted_ids)
    # 一次 count + 一次数据查询 = 2；留一点余量给会话层的杂项查询
    assert len(records) <= 4, f"待办查询条数过多（疑似 N+1）：{len(records)}\n" + "\n".join(
        records[:8]
    )


# ================================================================ 15. 策略校验


async def test_policy_without_fallback_is_rejected(world: World) -> None:
    """验收 15（一）：没有零阈值兜底 → 422。"""
    response = await world.admin_client.post(
        "/api/v1/admin/categories",
        json={
            "name": "无兜底",
            "code": _code("TCAT"),
            "approval_policy": {
                "steps": [
                    {"order": 10, "role": "DEPT_MANAGER", "when": {"gte": ["amount_fen", 10_000]}}
                ]
            },
        },
    )
    assert response.status_code == 422
    assert "兜底" in response.json()["error"]["message"]


async def test_policy_with_unreachable_step_is_rejected(world: World) -> None:
    """验收 15（二）：有步骤永远不会被触发 → 同样 422。

    这一条是 M6 新增的**第四层校验**：引擎能算了，就顺手反推策略是否处处可达。
    死步骤 = 管理员以为配了、实际永远不生效 = 审批层级**静默降低**。
    """
    response = await world.admin_client.post(
        "/api/v1/admin/categories",
        json={
            "name": "死步骤",
            "code": _code("TCAT"),
            "approval_policy": {
                "steps": [
                    {"order": 10, "role": "DEPT_MANAGER", "when": {"gte": ["amount_fen", 0]}},
                    # 探测金额最大 100,000 元（10,000,000 分），这里要 200,000 元才触发
                    {"order": 60, "role": "GM", "when": {"gte": ["amount_fen", 20_000_000]}},
                ]
            },
        },
    )
    assert response.status_code == 422
    assert "order=60" in response.json()["error"]["message"]
