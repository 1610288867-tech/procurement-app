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
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.database import SessionFactory
from app.domain.permissions import ROLE_ADMIN
from app.infrastructure.login_throttle import login_throttle
from app.infrastructure.records.auth import UserRecord, UserRoleRecord
from app.infrastructure.records.purchase import (
    IdempotencyRecord,
    PurchaseItemRecord,
    PurchaseRequestRecord,
    RequestEventRecord,
)
from app.infrastructure.security import hash_password
from app.infrastructure.token_quota import token_quota
from app.main import app

# 测试账号统一用这个口令。它是测试数据，不是任何真实环境的凭证。
TEST_PASSWORD = "Dev-Passw0rd!"

#: 集成测试默认配的预算额度：100 万元（分）。
#: 定得比任何一条用例的申请都大 —— 这些用例验的是审批与权限，不是预算不足；
#: 真要撞预算边界时，用例自己指定金额（见 `tests/test_api_budget.py`）。
DEFAULT_BUDGET_FEN = 100_000_000


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
                # 先删引用这些账号的业务数据，再删账号本身。
                # 为什么放在这里而不是各用例自己的清理器里：pytest 按相反顺序拆 fixture，
                # 而这个工厂的清理**总是最后一个跑**；只要有任何一张表引用 users.id，
                # 删账号就会撞外键，而且报错发生在 teardown 里，看不出是清理顺序的问题。
                # M5 起 purchase_requests / request_events / idempotency_records 都引用它。
                await session.execute(
                    delete(RequestEventRecord).where(
                        RequestEventRecord.actor_id.in_(created)
                    )
                )
                await session.execute(
                    delete(PurchaseItemRecord).where(
                        PurchaseItemRecord.request_id.in_(
                            select(PurchaseRequestRecord.id).where(
                                PurchaseRequestRecord.requester_id.in_(created)
                            )
                        )
                    )
                )
                await session.execute(
                    delete(PurchaseRequestRecord).where(
                        PurchaseRequestRecord.requester_id.in_(created)
                    )
                )
                await session.execute(
                    delete(IdempotencyRecord).where(
                        IdempotencyRecord.actor_id.in_(created)
                    )
                )
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


def budget_year() -> int:
    """预算按「提交时刻所在年度」认领（M7），测试造预算时用同一个口径。"""
    return datetime.now(UTC).year


async def create_budget(
    admin: AsyncClient,
    *,
    department_id: str,
    category_id: str,
    amount_fen: int = DEFAULT_BUDGET_FEN,
    year: int | None = None,
) -> dict:
    """给"部门 × 品类 × 年度"配一条预算。

    M7 起**提交必须有预算**：没有预算行 = 计划外 → 422（业务规则第 1 条）。
    所以凡是会走到"提交"的集成测试，都得先有这一条 ——
    这恰恰是那条口径在测试里的体现，而不是测试的累赘。
    """
    response = await admin.post(
        "/api/v1/admin/budgets",
        json={
            "department_id": department_id,
            "category_id": category_id,
            "year": year if year is not None else budget_year(),
            "amount_fen": amount_fen,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
async def admin_client(user_factory: UserFactory) -> AsyncIterator[AsyncClient]:
    """**已登录管理员**的客户端。

    M3 起 `/api/v1/admin/*` 要求 ADMIN 角色，主数据的集成测试都得用它。
    M5 起业务接口（/api/v1/requests、/api/v1/approvals）也要求登录，
    所以"随便挑一个已登录客户端"就用它。
    （`/api/v1/suppliers` 已于 2026-10-03 从公开改为要求登录；department / category 仍公开。）
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


@pytest.fixture(autouse=True)
def _reset_token_quota() -> None:
    """每个用例前重置问答的 token 配额，并把 embedding 强制成**离线实现**。

    ============================ 为什么必须有这个夹具 ============================
    配额是**应用级单例**（进程内一份），所以它会**跨用例累积**。
    实测踩到：`test_follow_up_question_reuses_the_session` 连问两次，
    第二次撞上 1 秒最小间隔，直接 429 失败 —— 而那条用例跟限流毫无关系。

    ============================ 为什么测试要用 `local` embedding ============================
    默认配置是 `ollama`（真模型、效果更好），但**测试不该依赖外部服务在跑**：
    那样的测试会在没装 Ollama 的机器上、以及 CI 里全红，
    而且每次上传文档都要等一次 HTTP 往返（实测：不设它时集成测试直接 500，
    报的是"配置矛盾：选了 ollama 但没配地址"）。

    这一层要验的是**装配与流程**（切分、入库、检索、引用校验、落库），
    不是"这个 embedding 模型好不好"—— 后者由 `scripts\knowledge_eval.py`
    用真模型在专门的评测里量（见 `docs/AI-评测baseline.md`）。

    所以这里强制成 `local`：离线、毫秒级，结论只关于代码、不关于环境。
    """
    from app.config import get_settings

    token_quota.reset()
    token_quota.configure(
        max_tokens_per_window=200_000, window_seconds=86_400.0, min_interval_seconds=0.0
    )
    # `get_settings` 带缓存，所以这里是"改那个缓存对象"，对后续请求立即生效；
    # 用完恢复，免得影响别的测试文件（它们可能断言默认值）。
    settings = get_settings()
    original_provider = settings.knowledge_embedding_provider
    settings.knowledge_embedding_provider = "local"
    yield
    settings.knowledge_embedding_provider = original_provider
    # 用完再重置一次：留下状态会让"下一条用例为什么失败"变得难以解释
    token_quota.reset()
    token_quota.configure(min_interval_seconds=0.0)


#: 非本测试套件造的、但会污染**检索类**用例的制度文档前缀。#:
#: 为什么需要这份清单：`test_api_knowledge.py` 里有几条用例断言
#: "**空库时不该有来源**"、"兜底回答不该有支撑性问题" ——
#: 它们的成立前提是"库里只有本用例上传的那份制度"。
#: 而下面这些前缀的文档是**别的工具造的**：
#:
#:     M11PAGE-   scripts\m11-知识库实测.mjs（页面实测）
#:     EVAL-      backend\scripts\knowledge_eval.py（评测集）
#:     PROBE      scripts\regression_probes.py
#:
#: `testdata_cleanup.py` 平时会清它们，但**执行顺序不保证**：
#: 实测踩到 —— 页面实测（回归第 9 步）造的 `M11PAGE-*` 文档，
#: 被第 12 步的清理删掉之前，第 2 步的 pytest 已经跑过了；
#: 而手动重跑 pytest 时它还在，于是两条用例失败。
#: **检索类测试必须自己保证起点干净，不能依赖别处恰好清过。**
POLLUTING_DOC_PREFIXES = ("M11PAGE-%", "M11TEST-%", "EVAL-%", "PROBE%")


@pytest.fixture(scope="session", autouse=True)
async def _clean_polluting_knowledge_docs() -> AsyncIterator[None]:
    """整个测试会话开始前，清掉别的工具留下的、会污染检索的制度文档。

    **只动上面那份前缀清单里的东西** —— 不碰任何手工上传的制度文档，
    也不碰任何业务数据。所以它是安全的：清掉的都是"可再生"的测试数据
    （页面实测与评测脚本每次运行都会重新造）。
    """
    from app.infrastructure.records.knowledge import (
        KnowledgeChunkRecord,
        KnowledgeDocRecord,
    )

    async with SessionFactory() as session:
        from sqlalchemy import or_

        conditions = [
            KnowledgeDocRecord.source_key.like(prefix)
            for prefix in POLLUTING_DOC_PREFIXES
        ]
        doc_ids = select(KnowledgeDocRecord.id).where(or_(*conditions))
        await session.execute(
            delete(KnowledgeChunkRecord).where(KnowledgeChunkRecord.doc_id.in_(doc_ids))
        )
        await session.execute(delete(KnowledgeDocRecord).where(or_(*conditions)))
        await session.commit()
    yield
