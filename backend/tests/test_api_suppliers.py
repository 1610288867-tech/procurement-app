"""供应商接口的集成测试 —— 需要 PostgreSQL。

跑法：
    pytest -m integration

M2 起**创建/停用走 `/api/v1/admin/suppliers`**，公开的 `/api/v1/suppliers` 只读
（与实施计划 M2 的接口清单一致）。

用 httpx 的 ASGITransport 直接打应用，不起真实端口；断言里会**直接查库**，
这样才能证明数据真的落盘了，而不是停在内存里（M1 的硬验收就是这一条）。

每次用随机 code，用完按 code 清掉，不污染开发库。
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.database import SessionFactory
from app.infrastructure.records.catalog import SupplierRecord
from app.main import app

pytestmark = pytest.mark.integration


def _code(prefix: str = "SUP-T") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


async def _purge(codes: list[str]) -> None:
    async with SessionFactory() as session:
        await session.execute(
            delete(SupplierRecord).where(SupplierRecord.code.in_(codes))
        )
        await session.commit()


@pytest.fixture
async def client(admin_client: AsyncClient) -> AsyncClient:
    """已登录**管理员**的客户端（M3 起 `/api/v1/admin/*` 要求 ADMIN）。

    本文件既有公开只读用例（不需要登录），也有创建/停用用例（需要 ADMIN），
    所以统一用管理员客户端。
    """
    return admin_client


async def test_list_suppliers_requires_login() -> None:
    """**2026-10-03 收口**：供应商列表不再是公开接口。

    原先它完全公开（未登录可读供应商名与编码），而"合格供应商清单"是采购/管理职能的资产
    （内控指引第 7 号第七条："建立科学的供应商评估和准入制度，确定合格供应商清单"，
    前提是"授予相应的请购权"）。所以改为**要求登录**。

    这里刻意**自带一个匿名客户端**（不用本文件那个 admin 的 `client` fixture）——
    用后者的话这条用例永远是 200，测不出任何东西。
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as anon:
        response = await anon.get("/api/v1/suppliers")
    assert response.status_code in (401, 403), response.text


async def test_only_active_parameter_is_gone(client: AsyncClient) -> None:
    """`only_active` 不再是客户端能拨的开关。

    原先传 `?only_active=false` 就能拿到**已停用**的供应商 —— 最窄的角色不该有
    "要不要过滤"的决定权（OWASP 最小权限 + 默认拒绝）。

    这条断言的是"这个参数不再有任何提权效果"：带上它，返回的还是合格清单。
    为了让断言有区分度，先造一个**已停用**的供应商，它不能出现在结果里。
    """
    code = _code("TSUP-PARAM")
    try:
        created = await client.post(
            "/api/v1/admin/suppliers", json={"name": "参数化试探供应商", "code": code}
        )
        assert created.status_code == 201
        await client.post(
            f"/api/v1/admin/suppliers/{created.json()['id']}/disable",
            json={"reason": "参数收口测试"},
        )

        plain = await client.get("/api/v1/suppliers")
        probed = await client.get("/api/v1/suppliers", params={"only_active": "false"})
        assert plain.status_code == 200 and probed.status_code == 200
        assert not any(item["code"] == code for item in plain.json())
        assert not any(item["code"] == code for item in probed.json()), (
            "带上 only_active=false 仍不该拿到已停用供应商"
        )
    finally:
        await _purge([code])


async def test_list_suppliers_returns_list(client: AsyncClient) -> None:
    response = await client.get("/api/v1/suppliers")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


async def test_create_supplier_persists_to_database(client: AsyncClient) -> None:
    """M1 的硬验收：建完直接查库能查到，证明真的落盘了。"""
    code = _code()
    try:
        response = await client.post(
            "/api/v1/admin/suppliers", json={"name": "集成测试供应商", "code": code}
        )
        assert response.status_code == 201

        body = response.json()
        uuid.UUID(body["id"])  # 是合法 UUID
        assert body["code"] == code
        assert body["active"] is True

        # 换一个全新会话直接查库 —— 走的是数据库，不是请求上下文里的对象
        async with SessionFactory() as session:
            stored = (
                await session.scalars(
                    select(SupplierRecord).where(SupplierRecord.code == code)
                )
            ).one()
            assert stored.name == "集成测试供应商"

        # 列表接口也能看到
        listed = (await client.get("/api/v1/suppliers")).json()
        assert any(item["code"] == code for item in listed)
    finally:
        await _purge([code])


async def test_duplicate_code_returns_conflict(client: AsyncClient) -> None:
    code = _code("TSUP-DUP")
    payload = {"name": "重复编码供应商", "code": code}
    try:
        first = await client.post("/api/v1/admin/suppliers", json=payload)
        assert first.status_code == 201

        response = await client.post("/api/v1/admin/suppliers", json=payload)
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "CONFLICT"
        assert error["request_id"]  # 统一错误体必须带 request_id
    finally:
        await _purge([code])


async def test_invalid_payload_returns_unified_error(client: AsyncClient) -> None:
    """校验失败也必须是统一错误体 —— 前端只认 body.error.message。"""
    response = await client.post("/api/v1/admin/suppliers", json={"name": "", "code": ""})
    assert response.status_code == 422

    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["message"]
    assert error["request_id"]


async def test_inactive_supplier_is_filtered_out(client: AsyncClient) -> None:
    """停用的供应商不出现在默认列表里。"""
    code = _code("TSUP-OFF")
    try:
        created = await client.post(
            "/api/v1/admin/suppliers", json={"name": "停用供应商", "code": code}
        )
        assert created.status_code == 201

        disabled = await client.post(
            f"/api/v1/admin/suppliers/{created.json()['id']}/disable",
            json={"reason": "集成测试"},
        )
        assert disabled.status_code == 200

        default = (await client.get("/api/v1/suppliers")).json()
        assert not any(item["code"] == code for item in default)

        # 管理员列表里还看得到
        all_items = (await client.get("/api/v1/admin/suppliers")).json()
        assert any(item["code"] == code for item in all_items)
    finally:
        await _purge([code])


async def test_expired_qualification_is_not_selectable(client: AsyncClient) -> None:
    """业务规则第 4 条：资质过期的供应商**不可选**，但管理员仍能查到。

    "过期即不可选"是靠**查询时过滤**实现的，所以这里必须断言两个方向：
    公开列表里没有它（选不到），管理列表里还有它（排查得到）。
    只测前者的话，把供应商整个过滤没了也会"通过"。
    """
    code = _code("TSUP-EXP")
    try:
        created = await client.post(
            "/api/v1/admin/suppliers",
            json={
                "name": "资质已过期供应商",
                "code": code,
                "qualification_expires_at": "2020-01-01T00:00:00+08:00",
            },
        )
        assert created.status_code == 201

        public = (await client.get("/api/v1/suppliers")).json()
        assert not any(item["code"] == code for item in public), "资质过期的供应商不该可选"

        admin = (await client.get("/api/v1/admin/suppliers")).json()
        assert any(item["code"] == code for item in admin), "管理员列表必须还查得到"
    finally:
        await _purge([code])


async def test_supplier_without_expiry_stays_selectable(client: AsyncClient) -> None:
    """没填资质有效期的 = 长期有效，不能被"过期过滤"误伤。

    少了这条用例，把过滤条件写成"必须有未来有效期"也能全绿 ——
    而那会让所有没填有效期的供应商凭空消失。
    """
    code = _code("TSUP-NOEXP")
    try:
        created = await client.post(
            "/api/v1/admin/suppliers", json={"name": "长期有效供应商", "code": code}
        )
        assert created.status_code == 201
        assert created.json()["qualification_expires_at"] is None

        public = (await client.get("/api/v1/suppliers")).json()
        assert any(item["code"] == code for item in public), "没填有效期的应当长期可选"
    finally:
        await _purge([code])
