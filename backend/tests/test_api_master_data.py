"""M2 主数据接口的集成测试 —— 需要 PostgreSQL。

跑法：pytest -m integration

重点覆盖 M2 验收里那几条"**不报错但会静默出错**"的用例：
审批策略的语义校验。结构对、语义错的策略如果放进去，申请会以更低的
审批层级被批准 —— 所以这里每条都要断言 422 **和具体提示文字**。
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError

from app.database import SessionFactory
from app.infrastructure.records.catalog import (
    BudgetRecord,
    DepartmentRecord,
    PurchaseCategoryRecord,
    SupplierRecord,
)
from app.main import app
from tests.conftest import UserFactory, login_as

pytestmark = pytest.mark.integration


def _code(prefix: str) -> str:
    """生成测试用的编码。

    ============================ 前缀约定：测试数据一律 T 打头 ============================
    测试造的部门 / 品类 / 供应商都用 `TDEP-` / `TCAT-` / `TSUP-`。

    这条约定是**用一次事故换来的**：原先测试用 `CAT-`，而种子脚本的演示品类也叫
    `CAT-OFFICE` / `CAT-IT`。清理开发库里的测试数据时，"按 `CAT-%` 前缀删"
    把种子品类一起删掉了 —— 前缀撞车时，"测试数据"与"演示数据"在库里无法区分，
    任何按前缀做的清理都必然误伤。

    新写测试请沿用这个约定：**可辨认、可批量清理、绝不与种子数据重叠**。
    """
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


VALID_POLICY = {
    "steps": [
        {"order": 10, "role": "DEPT_MANAGER", "when": {"gte": ["amount_fen", 0]}},
        {"order": 20, "role": "DEPT_HEAD", "when": {"gte": ["amount_fen", 100_000]}},
    ]
}


class Cleanup:
    """按外键顺序清理本轮造的数据，不污染开发库。"""

    def __init__(self) -> None:
        self.budgets: list[uuid.UUID] = []
        self.categories: list[uuid.UUID] = []
        self.departments: list[uuid.UUID] = []
        self.suppliers: list[uuid.UUID] = []

    async def purge(self) -> None:
        async with SessionFactory() as session:
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
                    delete(DepartmentRecord).where(
                        DepartmentRecord.id.in_(self.departments)
                    )
                )
            if self.suppliers:
                await session.execute(
                    delete(SupplierRecord).where(SupplierRecord.id.in_(self.suppliers))
                )
            await session.commit()


@pytest.fixture
async def client(admin_client: AsyncClient) -> AsyncClient:
    """已登录**管理员**的客户端（M3 起 `/api/v1/admin/*` 要求 ADMIN）。

    这里沿用 `client` 这个名字，是因为本文件几乎所有请求都打管理员接口；
    公开只读接口不校验登录，用同一台 client 也照样能调。
    """
    return admin_client


@pytest.fixture
async def cleanup() -> AsyncIterator[Cleanup]:
    tracker = Cleanup()
    try:
        yield tracker
    finally:
        await tracker.purge()


async def _create_department(client: AsyncClient, cleanup: Cleanup) -> dict:
    response = await client.post(
        "/api/v1/admin/departments", json={"name": "行政部", "code": _code("TDEP")}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    cleanup.departments.append(uuid.UUID(body["id"]))
    return body


async def _create_category(
    client: AsyncClient, cleanup: Cleanup, policy: dict, *, code: str | None = None
) -> dict:
    response = await client.post(
        "/api/v1/admin/categories",
        json={
            "name": "办公用品",
            "code": code or _code("TCAT"),
            "form_schema": {"type": "object"},
            "approval_policy": policy,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    cleanup.categories.append(uuid.UUID(body["id"]))
    return body


# ---------------------------------------------------------------- 品类与策略


async def test_create_category_then_read_back(
    client: AsyncClient, cleanup: Cleanup
) -> None:
    """能创建品类、填 3 条规则，保存后重新打开能看到。"""
    policy = {
        "steps": [
            {"order": 10, "role": "DEPT_MANAGER", "when": {"gte": ["amount_fen", 0]}},
            {"order": 20, "role": "DEPT_HEAD", "when": {"gte": ["amount_fen", 100_000]}},
            {"order": 30, "role": "FINANCE", "when": {"gte": ["amount_fen", 1_000_000]}},
        ]
    }
    created = await _create_category(client, cleanup, policy)
    assert created["version"] == 1

    fetched = await client.get(f"/api/v1/categories/{created['id']}")
    assert fetched.status_code == 200
    # 存进去的是**补齐默认值后**的策略（mode 显式写 SINGLE、开关显式写 False）。
    # 这是有意的：M5 的求值器不用再去猜默认值。
    stored = fetched.json()["approval_policy"]
    assert [step["order"] for step in stored["steps"]] == [10, 20, 30]
    assert [step["role"] for step in stored["steps"]] == [
        "DEPT_MANAGER",
        "DEPT_HEAD",
        "FINANCE",
    ]
    assert all(step["mode"] == "SINGLE" for step in stored["steps"])
    assert stored["skip_when_framework_contract"] is False
    assert stored["steps"][1]["when"] == {"gte": ["amount_fen", 100_000]}


async def test_unknown_role_is_rejected_by_structure(client: AsyncClient) -> None:
    """第一层（Pydantic 结构）就该拦住 role: BOSS。"""
    response = await client.post(
        "/api/v1/admin/categories",
        json={
            "name": "办公用品",
            "code": _code("TCAT"),
            "approval_policy": {
                "steps": [{"order": 10, "role": "BOSS", "when": {"gte": ["amount_fen", 0]}}]
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_field_typo_amount_is_rejected_with_field_name(
    client: AsyncClient, cleanup: Cleanup
) -> None:
    """漏 _fen 写成 amount —— 必须 422，且提示里点名 amount_fen。"""
    bad_policy = {
        "steps": [
            {"order": 10, "role": "DEPT_MANAGER", "when": {"gte": ["amount_fen", 0]}},
            {"order": 20, "role": "DEPT_HEAD", "when": {"gte": ["amount", 100_000]}},
        ]
    }
    response = await client.post(
        "/api/v1/admin/categories",
        json={
            "name": "办公用品",
            "code": _code("TCAT"),
            "approval_policy": bad_policy,
        },
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "INVALID_APPROVAL_POLICY"
    assert "amount_fen" in error["message"]
    assert "'amount'" in error["message"]


async def test_policy_without_fallback_is_rejected(
    client: AsyncClient, cleanup: Cleanup
) -> None:
    """没有零阈值兜底 → 某些金额区间会算出空审批链 = 无人审批直接通过。"""
    response = await client.post(
        "/api/v1/admin/categories",
        json={
            "name": "办公用品",
            "code": _code("TCAT"),
            "approval_policy": {
                "steps": [
                    {
                        "order": 10,
                        "role": "DEPT_HEAD",
                        "when": {"gte": ["amount_fen", 10_000]},
                    }
                ]
            },
        },
    )
    assert response.status_code == 422
    assert "兜底" in response.json()["error"]["message"]


async def test_float_threshold_is_rejected(
    client: AsyncClient, cleanup: Cleanup
) -> None:
    response = await client.post(
        "/api/v1/admin/categories",
        json={
            "name": "办公用品",
            "code": _code("TCAT"),
            "approval_policy": {
                "steps": [
                    {"order": 10, "role": "DEPT_MANAGER", "when": {"gte": ["amount_fen", 0]}},
                    {
                        "order": 20,
                        "role": "DEPT_HEAD",
                        "when": {"gte": ["amount_fen", 10000.5]},
                    },
                ]
            },
        },
    )
    assert response.status_code == 422
    assert "浮点" in response.json()["error"]["message"]


async def test_duplicate_order_is_rejected(
    client: AsyncClient, cleanup: Cleanup
) -> None:
    response = await client.post(
        "/api/v1/admin/categories",
        json={
            "name": "办公用品",
            "code": _code("TCAT"),
            "approval_policy": {
                "steps": [
                    {"order": 10, "role": "DEPT_MANAGER", "when": {"gte": ["amount_fen", 0]}},
                    {
                        "order": 10,
                        "role": "DEPT_HEAD",
                        "when": {"gte": ["amount_fen", 100_000]},
                    },
                ]
            },
        },
    )
    assert response.status_code == 422
    assert "order=10 重复" in response.json()["error"]["message"]


async def test_update_policy_bumps_version(
    client: AsyncClient, cleanup: Cleanup
) -> None:
    created = await _create_category(client, cleanup, VALID_POLICY)
    assert created["version"] == 1

    response = await client.put(
        f"/api/v1/admin/categories/{created['id']}/approval-policy",
        json={"approval_policy": VALID_POLICY},
    )
    assert response.status_code == 200
    assert response.json()["version"] == 2


async def test_update_unknown_category_returns_404(client: AsyncClient) -> None:
    response = await client.put(
        f"/api/v1/admin/categories/{uuid.uuid4()}/approval-policy",
        json={"approval_policy": VALID_POLICY},
    )
    assert response.status_code == 404


async def test_duplicate_category_code_returns_409(
    client: AsyncClient, cleanup: Cleanup
) -> None:
    code = _code("TCAT")
    await _create_category(client, cleanup, VALID_POLICY, code=code)
    response = await client.post(
        "/api/v1/admin/categories",
        json={"name": "另一个", "code": code, "approval_policy": VALID_POLICY},
    )
    assert response.status_code == 409


# -------------------------------------------------------------------- 预算


async def test_my_budgets_requires_login_and_is_department_scoped(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    """`/api/v1/budgets/mine` 的两处收口（2026-10-03）。

    它原来是 M2 留下的公开接口，而且**部门由调用方指定** —— 两个问题叠加：
    匿名可读预算金额，且传任意部门 id 就能看别人有多少钱。

    这条同时钉住三件事：
      1. 匿名 → **401**；
      2. `department_id` 收掉：传了也不起作用（只认登录态）；
      3. 有部门的账号看到**本部门**的预算；没部门的账号看到**空列表**。

    ⚠️ 这条用例**自己按顺序清理**（预算 → 账号 → 部门），不用 `cleanup` fixture：
    那个 fixture 的拆卸顺序**早于** `user_factory`，删部门时会撞上
    `users.department_id` 的外键（实测踩过）。用例内 `finally` 是严格有序的。
    """
    # ① 匿名不可读
    anonymous = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    blocked = await anonymous.get("/api/v1/budgets/mine")
    assert blocked.status_code in (401, 403), blocked.text
    await anonymous.aclose()

    created_department = (
        await client.post(
            "/api/v1/admin/departments", json={"name": "预算可见性", "code": _code("TDEP")}
        )
    ).json()
    department_id = uuid.UUID(created_department["id"])

    # 品类 + 该部门的预算（都自己清，不用 cleanup fixture —— 理由见 docstring）
    created_category = (
        await client.post(
            "/api/v1/admin/categories",
            json={"name": "预算可见性品类", "code": _code("TCAT"), "approval_policy": VALID_POLICY},
        )
    ).json()
    category_id = uuid.UUID(created_category["id"])
    created_budget = (
        await client.post(
            "/api/v1/admin/budgets",
            json={
                "department_id": str(department_id),
                "category_id": str(category_id),
                "year": 2026,
                "amount_fen": 500_000,
            },
        )
    ).json()
    budget_id = uuid.UUID(created_budget["id"])

    try:
        insider = await user_factory(roles=["REQUESTER"], department_id=department_id)
        as_insider = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        await login_as(as_insider, insider)

        mine = await as_insider.get("/api/v1/budgets/mine")
        assert mine.status_code == 200, mine.text
        assert any(item["amount_total_fen"] == 500_000 for item in mine.json())

        # 试图用参数窥探别的部门 —— 参数已经不存在，接口只认登录态
        spoofed = await as_insider.get(
            "/api/v1/budgets/mine", params={"department_id": str(uuid.uuid4())}
        )
        assert spoofed.status_code == 200
        assert spoofed.json() == mine.json(), "department_id 不该还能由调用方指定"
        await as_insider.aclose()

        # ③ 没有部门的账号（如系统管理员）→ 空列表，而不是报错
        outsider = await user_factory(roles=["ADMIN"], department_id=None)
        as_outsider = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        await login_as(as_outsider, outsider)
        empty = await as_outsider.get("/api/v1/budgets/mine")
        assert empty.status_code == 200
        assert empty.json() == []
        await as_outsider.aclose()
    finally:
        # 严格顺序：预算 → 品类 → 部门（账号由 user_factory 自己删，它删得比这里晚）
        async with SessionFactory() as session:
            await session.execute(delete(BudgetRecord).where(BudgetRecord.id == budget_id))
            await session.execute(
                delete(PurchaseCategoryRecord).where(
                    PurchaseCategoryRecord.id == category_id
                )
            )
            await session.execute(
                # 部门上可能还挂着测试账号（t- 前缀）→ 先解绑再删，否则撞外键
                text("UPDATE users SET department_id = NULL WHERE department_id = :d"),
                {"d": department_id},
            )
            await session.execute(
                delete(DepartmentRecord).where(DepartmentRecord.id == department_id)
            )
            await session.commit()


async def test_budget_stores_fen_and_rejects_duplicate_scope(
    client: AsyncClient, cleanup: Cleanup
) -> None:
    """界面填 1000 元 → 请求体 100000 → 库里 100000（全程整数分）。"""
    department = await _create_department(client, cleanup)
    category = await _create_category(client, cleanup, VALID_POLICY)

    payload = {
        "department_id": department["id"],
        "category_id": category["id"],
        "year": 2026,
        "amount_fen": 100_000,  # 1000.00 元
    }
    response = await client.post("/api/v1/admin/budgets", json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    cleanup.budgets.append(uuid.UUID(body["id"]))

    assert body["amount_total_fen"] == 100_000
    assert body["amount_reserved_fen"] == 0
    assert body["amount_spent_fen"] == 0

    # 直接查库，确认存的就是分
    async with SessionFactory() as session:
        stored = await session.get(BudgetRecord, uuid.UUID(body["id"]))
        assert stored is not None
        assert stored.amount_total == 100_000

    # 同一 部门 × 年度 × 品类 再来一次 → 409
    duplicate = await client.post("/api/v1/admin/budgets", json=payload)
    assert duplicate.status_code == 409


async def test_budget_with_unknown_department_returns_422(
    client: AsyncClient, cleanup: Cleanup
) -> None:
    category = await _create_category(client, cleanup, VALID_POLICY)
    response = await client.post(
        "/api/v1/admin/budgets",
        json={
            "department_id": str(uuid.uuid4()),
            "category_id": category["id"],
            "year": 2026,
            "amount_fen": 1_000,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DEPARTMENT_NOT_FOUND"


# ------------------------------------------------------------------ 供应商


async def test_disabled_supplier_disappears_from_public_list(
    client: AsyncClient, cleanup: Cleanup
) -> None:
    code = _code("TSUP")
    created = await client.post(
        "/api/v1/admin/suppliers", json={"name": "待停用供应商", "code": code}
    )
    assert created.status_code == 201
    supplier_id = created.json()["id"]
    cleanup.suppliers.append(uuid.UUID(supplier_id))

    assert any(
        item["code"] == code for item in (await client.get("/api/v1/suppliers")).json()
    )

    disabled = await client.post(
        f"/api/v1/admin/suppliers/{supplier_id}/disable", json={"reason": "资质过期"}
    )
    assert disabled.status_code == 200

    # 公开列表看不到了
    assert not any(
        item["code"] == code for item in (await client.get("/api/v1/suppliers")).json()
    )
    # 管理员列表还看得到
    assert any(
        item["code"] == code
        for item in (await client.get("/api/v1/admin/suppliers")).json()
    )

    # 再停一次 → 404（条件更新没命中）
    again = await client.post(
        f"/api/v1/admin/suppliers/{supplier_id}/disable", json={"reason": "重复停用"}
    )
    assert again.status_code == 404


async def test_supplier_qualification_expiry_is_accepted(
    client: AsyncClient, cleanup: Cleanup
) -> None:
    response = await client.post(
        "/api/v1/admin/suppliers",
        json={
            "name": "有资质的供应商",
            "code": _code("TSUP"),
            "qualification_expires_at": "2027-01-01T00:00:00+08:00",
        },
    )
    assert response.status_code == 201, response.text
    cleanup.suppliers.append(uuid.UUID(response.json()["id"]))
    assert response.json()["qualification_expires_at"].startswith("2027-01-01")


async def test_bad_datetime_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/admin/suppliers",
        json={
            "name": "时间格式错",
            "code": _code("TSUP"),
            "qualification_expires_at": "2027年1月1日",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_DATETIME"


# ---------------------------------------------------------------------- 预算


async def test_budget_check_constraint_blocks_overdraft(
    client: AsyncClient, cleanup: Cleanup
) -> None:
    """数据库兜底：预留 + 已花 不允许超过总额。

    为什么这条要测到数据库层：M6 的并发预留靠应用层的条件更新挡超支，
    而"应用层总有一条路径没想到"——漏写守卫时**不会报错，只会把钱算多**。
    所以要有一层谁都绕不过去的 CHECK。

    做法：用**原始 SQL** 直接插一行"预留额 = 总额 + 1 分"（绕开应用层的所有校验），
    数据库必须拒绝。全程不 commit，最后显式回滚 —— 验证的是"数据库会拒绝"，
    而不是"先往库里塞一条脏数据看看"。
    本用例自己造部门与品类，所以不依赖库里已有的任何数据。
    """
    department = await _create_department(client, cleanup)
    category = await _create_category(client, cleanup, VALID_POLICY)

    async with SessionFactory() as session:
        params = {
            "department_id": department["id"],
            "category_id": category["id"],
        }
        try:
            # ① 预留额撑爆总额
            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        "insert into budgets (id, department_id, category_id, year, "
                        "amount_total, amount_reserved, amount_spent) values "
                        "(gen_random_uuid(), :department_id, :category_id, 2101, 1000, 1001, 0)"
                    ),
                    params,
                )
            await session.rollback()

            # ② 预留为负
            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        "insert into budgets (id, department_id, category_id, year, "
                        "amount_total, amount_reserved, amount_spent) values "
                        "(gen_random_uuid(), :department_id, :category_id, 2101, 1000, -1, 0)"
                    ),
                    params,
                )
            await session.rollback()

            # ③ 预留 + 已花 刚好等于总额 —— 边界上是**允许**的，别把合法值也挡了
            await session.execute(
                text(
                    "insert into budgets (id, department_id, category_id, year, "
                    "amount_total, amount_reserved, amount_spent) values "
                    "(gen_random_uuid(), :department_id, :category_id, 2101, 1000, 600, 400)"
                ),
                params,
            )
        finally:
            # 这一行不该落库：上面无论成功与否都回滚
            await session.rollback()

    async with SessionFactory() as session:
        leftovers = (
            await session.execute(
                text(
                    "select count(*) from budgets "
                    "where department_id = :department_id and year = 2101"
                ),
                params,
            )
        ).scalar_one()
    assert leftovers == 0, "本用例必须回滚干净，不在开发库留下测试预算"
