"""测试共用的 fixture。

分档纪律（见 pyproject.toml）：

    pytest -m unit          纯函数，不连数据库，毫秒级（**默认档**）
    pytest -m integration   需要 PostgreSQL 在跑
    pytest -m ""            全量

M0 阶段只有一个健康检查冒烟测试，用来证明"pytest 能跑"。
从 M2 起开始放真正的东西：test_rules.py / test_approval_engine.py /
test_state_machine.py / test_budget_concurrency.py / test_settlement_policy.py。

M3 起多了一个 `user_factory`：集成测试要一个"登录态"才能打受保护的接口，
而这个账号必须是**测试自己造的** —— 依赖 seed 数据会让测试互相干扰。
"""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.database import SessionFactory
from app.domain.permissions import ROLE_ADMIN
from app.infrastructure.login_throttle import login_throttle
from app.infrastructure.records.auth import UserRecord, UserRoleRecord
from app.infrastructure.security import hash_password
from app.main import app

# 测试账号统一用这个口令。它是测试数据，不是任何真实环境的凭证。
TEST_PASSWORD = "Dev-Passw0rd!"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _reset_login_throttle() -> None:
    """限流器是进程内单例，用例之间必须隔离，否则会互相锁死账号。"""
    login_throttle.reset()


# 名字刻意不叫 TestXxx：pytest 会把它当成待收集的测试类，报 PytestCollectionWarning。
@dataclass
class Account:
    id: uuid.UUID
    username: str
    password: str
    roles: list[str] = field(default_factory=list)


UserFactory = Callable[..., Awaitable[Account]]


@pytest.fixture
async def user_factory() -> AsyncIterator[UserFactory]:
    """造一个可用账号，用例结束后连角色一起删掉。"""
    created: list[uuid.UUID] = []

    async def make(
        *,
        roles: list[str],
        department_id: uuid.UUID | None = None,
        active: bool = True,
        password: str = TEST_PASSWORD,
    ) -> Account:
        username = f"t-{uuid.uuid4().hex[:10]}"
        async with SessionFactory() as session:
            user = UserRecord(
                username=username,
                password_hash=hash_password(password),
                full_name=f"{roles[0] if roles else 'USER'} 测试账号",
                department_id=department_id,
                active=active,
            )
            session.add(user)
            await session.flush()
            for role in roles:
                session.add(UserRoleRecord(user_id=user.id, role=role))
            await session.commit()
            created.append(user.id)
            return Account(
                id=user.id, username=username, password=password, roles=list(roles)
            )

    try:
        yield make
    finally:
        async with SessionFactory() as session:
            if created:
                await session.execute(
                    delete(UserRoleRecord).where(UserRoleRecord.user_id.in_(created))
                )
                await session.execute(delete(UserRecord).where(UserRecord.id.in_(created)))
            await session.commit()


async def login_as(http: AsyncClient, account: Account) -> None:
    """让这台 client 带上该账号的 Cookie。"""
    response = await http.post(
        "/api/v1/auth/login",
        json={"username": account.username, "password": account.password},
    )
    assert response.status_code == 200, response.text


@pytest.fixture
async def admin_client(user_factory: UserFactory) -> AsyncIterator[AsyncClient]:
    """**已登录管理员**的客户端。

    M3 起 `/api/v1/admin/*` 要求 ADMIN 角色，主数据的集成测试都得用它。
    公开只读接口（/api/v1/suppliers 等）不需要登录，用同一台 client 也能调。
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        await login_as(http, await user_factory(roles=[ROLE_ADMIN]))
        yield http


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """匿名客户端。每个用例一台独立的"浏览器"，Cookie 互不串门。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
