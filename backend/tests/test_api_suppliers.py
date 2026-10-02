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
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.database import SessionFactory
from app.infrastructure.records.catalog import SupplierRecord

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
    code = _code("SUP-DUP")
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
    code = _code("SUP-OFF")
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
