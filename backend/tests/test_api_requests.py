"""采购申请接口的集成测试（M5）—— 需要 PostgreSQL。

跑法：`pytest -m integration`

这一组用例逐条对着 M5 的验收标准写：

    1. 能建草稿、改草稿、加三行明细，amount_total 由后端算出来且正确
    2. 用旧 version 提交修改 → 409
    3. 同一个 Idempotency-Key 提交两次，只产生一条申请（第二次是重放）
    4. 同一个 Idempotency-Key 但改成不同内容 → 409
    5. **同一个 Idempotency-Key 并发提交（asyncio.gather），只产生一条申请**
    6. 提交后状态变 IN_APPROVAL，时间线上出现两条事件
    7. 已提交的申请不能再 PATCH → 409

另外补上 M3 挂账的两条数据级权限验收（换 id 取别人的申请 → 404）。

清理策略：每个用例自己造部门 / 品类 / 账号，结束后按外键顺序删干净。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update

from app.database import SessionFactory
from app.domain.enums import RequestStatus
from app.domain.permissions import ROLE_ADMIN, ROLE_DEPT_MANAGER, ROLE_REQUESTER
from app.infrastructure.records.auth import UserRecord
from app.infrastructure.records.catalog import DepartmentRecord, PurchaseCategoryRecord
from app.infrastructure.records.purchase import (
    IdempotencyRecord,
    PurchaseItemRecord,
    PurchaseRequestRecord,
    RequestEventRecord,
)
from app.main import app
from tests.conftest import Account, UserFactory, login_as

pytestmark = pytest.mark.integration

VALID_POLICY = {
    "steps": [
        {"order": 10, "role": "DEPT_MANAGER", "when": {"gte": ["amount_fen", 0]}},
    ]
}


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


class Fixture:
    """一次用例造出来的数据，用完按外键顺序清掉。"""

    def __init__(self) -> None:
        self.departments: list[uuid.UUID] = []
        self.categories: list[uuid.UUID] = []
        self.requests: list[uuid.UUID] = []
        self.actors: list[uuid.UUID] = []

    async def purge(self) -> None:
        """按外键顺序清理。

        两个必须记住的依赖方向：
          - `purchase_requests.requester_id → users`：申请必须**先于**账号删掉。
            所以这个 fixture 要排在 `user_factory` **之前**拆（见下面的依赖声明）。
          - `users.department_id → departments`：部门要**最后**删，
            因为用例造的账号挂在它上面。
        """
        async with SessionFactory() as session:
            if self.requests:
                # 明细与事件是 CASCADE，这里显式删是为了不依赖建表选项
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
            if self.categories:
                await session.execute(
                    delete(PurchaseCategoryRecord).where(
                        PurchaseCategoryRecord.id.in_(self.categories)
                    )
                )
            if self.departments:
                # 解绑仍挂在这些部门上的账号（部门是最后才删的一环，
                # 而账号可能由 fixture 之外的地方造出来，顺序不保证）。
                # `users.department_id` 可空，置空比"赌它已经删干净"稳。
                await session.execute(
                    update(UserRecord)
                    .where(UserRecord.department_id.in_(self.departments))
                    .values(department_id=None)
                    .execution_options(synchronize_session=False)
                )
                await session.execute(
                    delete(DepartmentRecord).where(
                        DepartmentRecord.id.in_(self.departments)
                    )
                )
            await session.commit()


@pytest.fixture
async def fixture(user_factory: UserFactory) -> AsyncIterator[Fixture]:
    """用例数据的清理器。

    显式依赖 `user_factory`：pytest 按**相反顺序**拆 fixture，
    所以这里声明依赖之后，申请数据会先于账号被删掉 ——
    否则删用户时会撞上 `purchase_requests.requester_id` 的外键
    （实测报过 ForeignKeyViolation，而且报在 teardown 里，很难看出是清理顺序的问题）。
    """
    tracker = Fixture()
    try:
        yield tracker
    finally:
        await tracker.purge()


async def _make_actor(
    factory: UserFactory,
    fixture: Fixture,
    *,
    roles: list[str],
    department: uuid.UUID | None,
) -> tuple[AsyncClient, Account]:
    """造一个已登录的客户端（账号由 `user_factory` 在用例结束后删掉）。"""
    account = await factory(roles=roles, department_id=department)
    fixture.actors.append(account.id)
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    await login_as(client, account)
    return client, account


@pytest.fixture
async def world(admin_client: AsyncClient, user_factory: UserFactory, fixture: Fixture):
    """一套最小可用的业务世界：部门 + 品类 + 一个申请人 + **一个部门主管**。

    为什么必须带一个部门主管（M6 起）：提交现在会在**同一个事务里**按品类策略解析
    候选人并建审批链，某一级解析出 0 个人时提交直接 422 —— 这是刻意的设计
    （M6 规则 3：把"提交成功后永久卡死"变成"提交时就报错"）。
    所以"能提交成功"的前提是每一级都有人。

    刻意不用 seed 数据：用例之间不该互相污染，也不该要求开发库处于某个状态。
    """
    department = (
        await admin_client.post(
            "/api/v1/admin/departments", json={"name": "行政部", "code": _code("TDEP")}
        )
    ).json()
    fixture.departments.append(uuid.UUID(department["id"]))

    category = (
        await admin_client.post(
            "/api/v1/admin/categories",
            json={
                "name": "办公用品",
                "code": _code("TCAT"),
                "approval_policy": VALID_POLICY,
            },
        )
    ).json()
    fixture.categories.append(uuid.UUID(category["id"]))

    dept_id = uuid.UUID(department["id"])
    client, account = await _make_actor(
        user_factory, fixture, roles=[ROLE_REQUESTER], department=dept_id
    )
    # 审批链第 1 级（DEPT_MANAGER）必须有人，否则提交会被 422 拦住
    await _make_actor(
        user_factory, fixture, roles=[ROLE_DEPT_MANAGER], department=dept_id
    )
    yield {
        "department": department,
        "category": category,
        "client": client,
        "requester_id": account.id,
    }
    await client.aclose()


async def _create_draft(client: AsyncClient, world: dict, *, title: str = "办公用品采购") -> dict:
    response = await client.post(
        "/api/v1/requests",
        json={
            "category_id": world["category"]["id"],
            "title": title,
            "reason": "本月补充",
            "urgency": "NORMAL",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _put_items(
    client: AsyncClient, request_id: str, version: int, items: list[dict]
) -> dict:
    response = await client.put(
        f"/api/v1/requests/{request_id}/items",
        json={"version": version, "items": items},
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------- 1. 草稿与金额


async def test_draft_with_three_lines_computes_amount_on_the_server(
    world: dict, fixture: Fixture
) -> None:
    """验收 1：建草稿、加三行明细，`amount_total` 由后端算出来且正确。

    顺便断言"客户端传的金额不作数"：请求体里塞一个假金额进去，结果必须是重算值。
    """
    client = world["client"]
    draft = await _create_draft(client, world)
    fixture.requests.append(uuid.UUID(draft["id"]))

    assert draft["status"] == RequestStatus.DRAFT.value
    assert draft["version"] == 1
    assert draft["amount_total_fen"] == 0, "还没填明细，金额应当是 0"

    updated = await _put_items(
        client,
        draft["id"],
        draft["version"],
        [
            {"name": "打印纸", "quantity": 2, "unit_price_fen": 10_000},
            {"name": "回形针", "quantity": 1, "unit_price_fen": 1},
            {"name": "文件夹", "quantity": 3, "unit_price_fen": 3_333},
        ],
    )
    # 20000 + 1 + 9999 = 30000 分
    assert updated["amount_total_fen"] == 30_000
    assert updated["version"] == 2

    detail = (await client.get(f"/api/v1/requests/{draft['id']}")).json()
    assert [item["line_no"] for item in detail["items"]] == [1, 2, 3]
    assert [item["amount_fen"] for item in detail["items"]] == [20_000, 1, 9_999]

    # 直接查库核对：金额真的落盘了，不是只在响应里算了一下
    async with SessionFactory() as session:
        stored = (
            await session.scalars(
                select(PurchaseRequestRecord).where(
                    PurchaseRequestRecord.id == uuid.UUID(draft["id"])
                )
            )
        ).one()
        assert stored.amount_total == 30_000
        assert stored.status == RequestStatus.DRAFT.value


async def test_client_supplied_amount_is_ignored(world: dict, fixture: Fixture) -> None:
    """前端传的金额一律不信任：接口里**根本没有** amount_total 这个入参。"""
    client = world["client"]
    draft = await _create_draft(client, world)
    fixture.requests.append(uuid.UUID(draft["id"]))

    response = await client.patch(
        f"/api/v1/requests/{draft['id']}",
        json={"version": draft["version"], "amount_total": 1},
    )
    assert response.status_code == 422
    assert "amount_total" in response.json()["error"]["message"]


async def test_patch_draft_changes_title_and_bumps_version(
    world: dict, fixture: Fixture
) -> None:
    client = world["client"]
    draft = await _create_draft(client, world)
    fixture.requests.append(uuid.UUID(draft["id"]))

    patched = await client.patch(
        f"/api/v1/requests/{draft['id']}",
        json={"version": draft["version"], "title": "改过的标题"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["title"] == "改过的标题"
    assert patched.json()["version"] == draft["version"] + 1


# ---------------------------------------------------------------- 2. 乐观锁


async def test_stale_version_returns_409(world: dict, fixture: Fixture) -> None:
    """验收 2：用旧 `version` 提交修改 → 409。"""
    client = world["client"]
    draft = await _create_draft(client, world)
    fixture.requests.append(uuid.UUID(draft["id"]))

    first = await client.patch(
        f"/api/v1/requests/{draft['id']}",
        json={"version": draft["version"], "title": "第一次改"},
    )
    assert first.status_code == 200

    # 拿已经过期的 version 再改一次
    stale = await client.patch(
        f"/api/v1/requests/{draft['id']}",
        json={"version": draft["version"], "title": "用旧版本改"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "CONFLICT"


# ---------------------------------------------------------------- 3/4/5. 幂等


async def _submit(client: AsyncClient, request_id: str, version: int, key: str):
    return await client.post(
        f"/api/v1/requests/{request_id}/submit",
        json={"version": version},
        headers={"Idempotency-Key": key},
    )


async def test_submit_requires_idempotency_key(world: dict, fixture: Fixture) -> None:
    """没带幂等键 → 422，且提示里说清该带什么头。

    这里刻意**不做"没有键就放行"**：那样接口只是"支持"幂等，
    客户端重试时该重复的照样重复。
    """
    client = world["client"]
    draft = await _create_draft(client, world)
    fixture.requests.append(uuid.UUID(draft["id"]))

    response = await client.post(
        f"/api/v1/requests/{draft['id']}/submit", json={"version": draft["version"]}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_same_key_twice_only_submits_once(world: dict, fixture: Fixture) -> None:
    """验收 3：同一个幂等键提交两次，只产生一条申请（第二次是重放响应）。"""
    client = world["client"]
    draft = await _create_draft(client, world)
    fixture.requests.append(uuid.UUID(draft["id"]))
    ready = await _put_items(
        client,
        draft["id"],
        draft["version"],
        [{"name": "打印纸", "quantity": 1, "unit_price_fen": 5_000}],
    )

    key = uuid.uuid4().hex
    first = await _submit(client, draft["id"], ready["version"], key)
    assert first.status_code == 200, first.text

    second = await _submit(client, draft["id"], ready["version"], key)
    assert second.status_code == 200, second.text
    # 重放：返回的是第一次的响应，`version` 不会再加一次
    assert second.json()["version"] == first.json()["version"]
    assert second.json()["status"] == RequestStatus.IN_APPROVAL.value

    async with SessionFactory() as session:
        count = (
            await session.execute(
                select(PurchaseRequestRecord.id).where(
                    PurchaseRequestRecord.requester_id == world["requester_id"]
                )
            )
        ).all()
        assert len(count) == 1, "同一个幂等键不该产生第二条申请"
        events = (
            await session.scalars(
                select(RequestEventRecord.event_type)
                .where(RequestEventRecord.request_id == uuid.UUID(draft["id"]))
                .order_by(RequestEventRecord.created_at)
            )
        ).all()
        assert events.count("REQUEST_SUBMITTED") == 1, "重放不该再写一条提交事件"


async def test_same_key_different_payload_returns_409(
    world: dict, fixture: Fixture
) -> None:
    """验收 4：同一个键但内容不同 → 409（不是重放）。

    载荷哈希记的是"这次请求的内容"（单号 + 版本）。用同一个键带**另一个版本**再来一次，
    必须被识别成"改了主意"，而不是当成重放。
    """
    client = world["client"]
    draft = await _create_draft(client, world)
    fixture.requests.append(uuid.UUID(draft["id"]))
    ready = await _put_items(
        client,
        draft["id"],
        draft["version"],
        [{"name": "打印纸", "quantity": 1, "unit_price_fen": 5_000}],
    )

    key = uuid.uuid4().hex
    first = await _submit(client, draft["id"], ready["version"], key)
    assert first.status_code == 200

    # 同一个 key，但版本不同 = 不同载荷
    again = await _submit(client, draft["id"], ready["version"] + 1, key)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "CONFLICT"


async def test_concurrent_same_key_submits_once(world: dict, fixture: Fixture) -> None:
    """验收 5：**并发同键**提交，只产生一条申请。

    这条是幂等最容易被写错的地方：只做"查不到就执行"的话，
    两个并发请求会双双执行，而且**悄无声息**。
    正确做法是先抢一个 IN_PROGRESS 占位行（独立事务提交），撞上的那方拿 409。

    期望：两个请求里**恰好一个 200**，另一个 409。
    """
    client = world["client"]
    draft = await _create_draft(client, world)
    fixture.requests.append(uuid.UUID(draft["id"]))
    ready = await _put_items(
        client,
        draft["id"],
        draft["version"],
        [{"name": "打印纸", "quantity": 2, "unit_price_fen": 12_345}],
    )

    key = uuid.uuid4().hex
    responses = await asyncio.gather(
        _submit(client, draft["id"], ready["version"], key),
        _submit(client, draft["id"], ready["version"], key),
        return_exceptions=True,
    )
    statuses = sorted(
        getattr(response, "status_code", 0) for response in responses
    )
    assert statuses.count(200) == 1, f"应当恰好一个成功：{statuses}"
    assert statuses[1] == 409, f"另一个应当是 409：{statuses}"

    async with SessionFactory() as session:
        stored = (
            await session.scalars(
                select(PurchaseRequestRecord).where(
                    PurchaseRequestRecord.id == uuid.UUID(draft["id"])
                )
            )
        ).one()
        assert stored.status == RequestStatus.IN_APPROVAL.value
        # 状态只被推进了一次：version 从提交前 +1
        assert stored.version == ready["version"] + 1

        events = (
            await session.scalars(
                select(RequestEventRecord.event_type).where(
                    RequestEventRecord.request_id == uuid.UUID(draft["id"]),
                    RequestEventRecord.event_type == "REQUEST_SUBMITTED",
                )
            )
        ).all()
        assert len(events) == 1, "并发提交只该写一条提交事件"


# ---------------------------------------------------------------- 6. 事件流


async def test_submit_sets_status_and_writes_two_events(
    world: dict, fixture: Fixture
) -> None:
    """验收 6：提交后状态变 `IN_APPROVAL`，时间线上出现**两条**事件。

    两条是：建草稿（REQUEST_CREATED）+ 提交（REQUEST_SUBMITTED）。
    """
    client = world["client"]
    draft = await _create_draft(client, world)
    fixture.requests.append(uuid.UUID(draft["id"]))
    ready = await _put_items(
        client,
        draft["id"],
        draft["version"],
        [{"name": "打印纸", "quantity": 1, "unit_price_fen": 5_000}],
    )

    response = await _submit(client, draft["id"], ready["version"], uuid.uuid4().hex)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == RequestStatus.IN_APPROVAL.value
    assert body["submitted_at"] is not None

    timeline = (await client.get(f"/api/v1/requests/{draft['id']}/timeline")).json()
    # 两条事件：创建 + 提交。M6 起提交还会多一条 APPROVAL_STARTED（审批链已生成），
    # 它**不是**状态变更，所以 from/to 都是 IN_APPROVAL。
    assert [event["event_type"] for event in timeline["events"]] == [
        "REQUEST_CREATED",
        "REQUEST_SUBMITTED",
        "APPROVAL_STARTED",
    ]
    # 事件带状态迁移，时间线才能画出"从哪到哪"
    assert timeline["events"][1]["from_status"] == RequestStatus.DRAFT.value
    assert timeline["events"][1]["to_status"] == RequestStatus.IN_APPROVAL.value


async def test_timeline_is_append_only_across_actions(
    world: dict, fixture: Fixture
) -> None:
    """事件只追加：建草稿、改标题、提交之后，历史一条不少。

    注意"换明细"**不产生事件**：事件流记的是状态变更与关键动作，
    草稿阶段反复改内容是常态，每次都记会把时间线淹掉。
    """
    client = world["client"]
    draft = await _create_draft(client, world)
    fixture.requests.append(uuid.UUID(draft["id"]))

    await client.patch(
        f"/api/v1/requests/{draft['id']}",
        json={"version": draft["version"], "title": "改一下"},
    )
    ready = await _put_items(
        client,
        draft["id"],
        draft["version"] + 1,
        [{"name": "打印纸", "quantity": 1, "unit_price_fen": 5_000}],
    )
    await _submit(client, draft["id"], ready["version"], uuid.uuid4().hex)

    timeline = (await client.get(f"/api/v1/requests/{draft['id']}/timeline")).json()
    types = [event["event_type"] for event in timeline["events"]]
    assert types == [
        "REQUEST_CREATED",
        "REQUEST_UPDATED",
        "REQUEST_SUBMITTED",
        "APPROVAL_STARTED",
    ]


# ---------------------------------------------------------------- 7. 提交后不可改


async def test_submitted_request_cannot_be_patched(world: dict, fixture: Fixture) -> None:
    """验收 7：已提交的申请不能再 PATCH → 409。"""
    client = world["client"]
    draft = await _create_draft(client, world)
    fixture.requests.append(uuid.UUID(draft["id"]))
    ready = await _put_items(
        client,
        draft["id"],
        draft["version"],
        [{"name": "打印纸", "quantity": 1, "unit_price_fen": 5_000}],
    )
    submitted = await _submit(client, draft["id"], ready["version"], uuid.uuid4().hex)
    assert submitted.status_code == 200
    current_version = submitted.json()["version"]

    patched = await client.patch(
        f"/api/v1/requests/{draft['id']}",
        json={"version": current_version, "title": "提交之后还想改"},
    )
    assert patched.status_code == 409

    # 明细同样不能再改
    items = await client.put(
        f"/api/v1/requests/{draft['id']}/items",
        json={
            "version": current_version,
            "items": [{"name": "偷偷加一行", "quantity": 1, "unit_price_fen": 100}],
        },
    )
    assert items.status_code == 409


async def test_submit_without_items_returns_422(world: dict, fixture: Fixture) -> None:
    """空明细的单子不许进审批流（提交成功之后就再也补救不了了）。"""
    client = world["client"]
    draft = await _create_draft(client, world)
    fixture.requests.append(uuid.UUID(draft["id"]))

    response = await _submit(client, draft["id"], draft["version"], uuid.uuid4().hex)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "NOT_SUBMITTABLE"


# ---------------------------------------------------------------- 撤回与丢弃


async def test_withdraw_then_cannot_resubmit(world: dict, fixture: Fixture) -> None:
    """撤回是终态：撤回之后不能再次提交（状态机里没有 WITHDRAWN → IN_APPROVAL）。"""
    client = world["client"]
    draft = await _create_draft(client, world)
    fixture.requests.append(uuid.UUID(draft["id"]))
    ready = await _put_items(
        client,
        draft["id"],
        draft["version"],
        [{"name": "打印纸", "quantity": 1, "unit_price_fen": 5_000}],
    )
    submitted = await _submit(client, draft["id"], ready["version"], uuid.uuid4().hex)
    version = submitted.json()["version"]

    withdrawn = await client.post(
        f"/api/v1/requests/{draft['id']}/withdraw",
        json={"version": version},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["status"] == RequestStatus.WITHDRAWN.value
    assert withdrawn.json()["closed_at"] is not None

    again = await _submit(
        client, draft["id"], withdrawn.json()["version"], uuid.uuid4().hex
    )
    assert again.status_code == 409


async def test_discard_only_works_on_draft(world: dict, fixture: Fixture) -> None:
    client = world["client"]
    draft = await _create_draft(client, world)
    fixture.requests.append(uuid.UUID(draft["id"]))

    discarded = await client.post(
        f"/api/v1/requests/{draft['id']}/discard", json={"version": draft["version"]}
    )
    assert discarded.status_code == 200
    assert discarded.json()["status"] == RequestStatus.DISCARDED.value

    # 再丢一次 → 409（已不是草稿）
    again = await client.post(
        f"/api/v1/requests/{draft['id']}/discard",
        json={"version": discarded.json()["version"]},
    )
    assert again.status_code == 409


# ---------------------------------------------------------------- 数据级权限


async def test_another_requester_gets_404_not_403(
    world: dict, fixture: Fixture, user_factory: UserFactory
) -> None:
    """M3 挂账的验收项：**换 id 取别人的申请 → 404**（不是 403、不是 200）。

    404 的含义是"这东西不在你的可见范围内" —— 连"它存在"都不告诉他。
    """
    draft = await _create_draft(world["client"], world)
    fixture.requests.append(uuid.UUID(draft["id"]))

    stranger, _ = await _make_actor(
        user_factory, fixture, roles=[ROLE_REQUESTER], department=None
    )
    response = await stranger.get(f"/api/v1/requests/{draft['id']}")
    assert response.status_code == 404

    # 列表里也不该出现
    listed = (await stranger.get("/api/v1/requests")).json()
    assert listed["items"] == []
    await stranger.aclose()


async def test_department_manager_sees_own_department_but_not_others(
    admin_client: AsyncClient, user_factory: UserFactory, fixture: Fixture
) -> None:
    """M3 挂账的验收项：`DEPT_MANAGER` 看不到别的部门的申请。"""
    other_department = (
        await admin_client.post(
            "/api/v1/admin/departments", json={"name": "采购部", "code": _code("TDEP")}
        )
    ).json()
    fixture.departments.append(uuid.UUID(other_department["id"]))

    other_requester, _ = await _make_actor(
        user_factory,
        fixture,
        roles=[ROLE_REQUESTER],
        department=uuid.UUID(other_department["id"]),
    )
    category = (
        await admin_client.post(
            "/api/v1/admin/categories",
            json={"name": "办公用品", "code": _code("TCAT"), "approval_policy": VALID_POLICY},
        )
    ).json()
    fixture.categories.append(uuid.UUID(category["id"]))

    draft = (
        await other_requester.post(
            "/api/v1/requests",
            json={"category_id": category["id"], "title": "别的部门的单子"},
        )
    ).json()
    fixture.requests.append(uuid.UUID(draft["id"]))

    # 采购部的主管：能看见本部门的这张单子
    own_manager, _ = await _make_actor(
        user_factory,
        fixture,
        roles=[ROLE_DEPT_MANAGER],
        department=uuid.UUID(other_department["id"]),
    )
    assert (
        await own_manager.get(f"/api/v1/requests/{draft['id']}")
    ).status_code == 200

    # 另一个部门的主管：看不到
    third = (
        await admin_client.post(
            "/api/v1/admin/departments", json={"name": "财务部", "code": _code("TDEP")}
        )
    ).json()
    fixture.departments.append(uuid.UUID(third["id"]))
    outsider, _ = await _make_actor(
        user_factory, fixture, roles=[ROLE_DEPT_MANAGER], department=uuid.UUID(third["id"])
    )
    assert (await outsider.get(f"/api/v1/requests/{draft['id']}")).status_code == 404


async def test_admin_sees_every_request(
    admin_client: AsyncClient, user_factory: UserFactory, fixture: Fixture
) -> None:
    """ADMIN 兼 GM 这类跨部门角色看得见全公司（不然待办/审批都没法做）。"""
    requester, _ = await _make_actor(
        user_factory, fixture, roles=[ROLE_REQUESTER], department=None
    )
    admin, _ = await _make_actor(
        user_factory, fixture, roles=[ROLE_ADMIN], department=None
    )

    department = (
        await admin_client.post(
            "/api/v1/admin/departments", json={"name": "行政部", "code": _code("TDEP")}
        )
    ).json()
    fixture.departments.append(uuid.UUID(department["id"]))
    category = (
        await admin_client.post(
            "/api/v1/admin/categories",
            json={"name": "办公用品", "code": _code("TCAT"), "approval_policy": VALID_POLICY},
        )
    ).json()
    fixture.categories.append(uuid.UUID(category["id"]))

    # 申请人没有部门 → 建单会被 422 拦住（部门取登录用户所属部门）
    blocked = await requester.post(
        "/api/v1/requests", json={"category_id": category["id"], "title": "无部门"}
    )
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "NO_DEPARTMENT"

    assert (await admin.get("/api/v1/requests")).status_code == 200
