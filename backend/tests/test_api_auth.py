"""M3 认证与权限接口的集成测试 —— 需要 PostgreSQL。

跑法：pytest -m integration

覆盖 M3 验收里那几条"看起来通过、其实没生效"的用例：
    1. 手改 Cookie 里的令牌 → 401
    2. 连错 5 次后第 6 次 → 429（不是 401）
    3. 冻结账号后旧令牌立即失效（靠 token_version，不是黑名单）
    4. refresh Cookie 的 Path 限定到 refresh 接口
    5. 库里存的哈希不含原始口令
    6. REQUESTER 打管理员接口 → 403（不是 404，也不是 200）
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from app.database import SessionFactory
from app.infrastructure.records.auth import UserRecord
from tests.conftest import TEST_PASSWORD, Account, UserFactory

pytestmark = pytest.mark.integration


async def _login(client: AsyncClient, user: Account) -> dict:
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": user.username, "password": user.password},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _set_cookie_header(response, name: str) -> str:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{name}="):
            return header
    raise AssertionError(f"响应里没有 {name} 这个 Cookie：{response.headers.get_list('set-cookie')}")


# ------------------------------------------------------------------ 登录


async def test_login_returns_roles_and_sets_both_cookies(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    user = await user_factory(roles=["REQUESTER"])
    body = await _login(client, user)

    assert body["username"] == user.username
    assert body["roles"] == ["REQUESTER"]
    assert "password_hash" not in body, "口令哈希绝不能出现在响应里"
    assert client.cookies.get("access_token")
    assert client.cookies.get("refresh_token")


async def test_wrong_password_returns_401(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    user = await user_factory(roles=["REQUESTER"])
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": user.username, "password": "definitely-wrong"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_unknown_user_returns_the_same_401_as_a_wrong_password(
    client: AsyncClient,
) -> None:
    """不能靠响应区分"用户不存在"和"密码错"——那是在帮别人枚举账号。"""
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": "no-such-user-at-all", "password": "whatever"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_sixth_attempt_after_five_failures_returns_429(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    """验收：连错 5 次后第 6 次返回 429（而不是 401）。"""
    user = await user_factory(roles=["REQUESTER"])
    payload = {"username": user.username, "password": "wrong"}

    for _ in range(5):
        assert (await client.post("/api/v1/auth/login", json=payload)).status_code == 401

    locked = await client.post("/api/v1/auth/login", json=payload)
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "TOO_MANY_ATTEMPTS"


async def test_correct_password_after_a_few_failures_still_works(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    """窗口内没到 5 次就不该锁；而且成功一次要把计数清零。"""
    user = await user_factory(roles=["REQUESTER"])
    for _ in range(4):
        await client.post(
            "/api/v1/auth/login",
            json={"username": user.username, "password": "wrong"},
        )
    assert (await client.post(
        "/api/v1/auth/login",
        json={"username": user.username, "password": TEST_PASSWORD},
    )).status_code == 200

    for _ in range(4):
        await client.post(
            "/api/v1/auth/login",
            json={"username": user.username, "password": "wrong"},
        )
    assert (await client.post(
        "/api/v1/auth/login",
        json={"username": user.username, "password": TEST_PASSWORD},
    )).status_code == 200


async def test_frozen_account_cannot_log_in(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    user = await user_factory(roles=["REQUESTER"], active=False)
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": user.username, "password": TEST_PASSWORD},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------- 当前账号


async def test_me_requires_a_cookie(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_me_returns_the_logged_in_account(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    user = await user_factory(roles=["DEPT_MANAGER"])
    await _login(client, user)
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 200
    assert response.json()["roles"] == ["DEPT_MANAGER"]


async def test_tampered_cookie_is_rejected(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    """验收：手改 Cookie 里的令牌 → 401。"""
    user = await user_factory(roles=["REQUESTER"])
    await _login(client, user)
    client.cookies.set("access_token", "eyJhbGciOiJIUzI1NiJ9.tampered.signature")
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_freezing_an_account_kills_its_existing_token(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    """验收：冻结账号后旧令牌立即失效。

    实现是 `token_version += 1`，不是维护一张黑名单。
    """
    user = await user_factory(roles=["REQUESTER"])
    await _login(client, user)
    assert (await client.get("/api/v1/auth/me")).status_code == 200

    async with SessionFactory() as session:
        await session.execute(
            update(UserRecord)
            .where(UserRecord.id == user.id)
            .values(active=False, token_version=UserRecord.token_version + 1)
        )
        await session.commit()

    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_users_password_hash_never_holds_the_raw_password(
    user_factory: UserFactory,
) -> None:
    """验收：库里 `users.password_hash` 不含原始密码。"""
    user = await user_factory(roles=["REQUESTER"])
    async with SessionFactory() as session:
        stored = (
            await session.scalars(
                select(UserRecord.password_hash).where(UserRecord.id == user.id)
            )
        ).one()
    assert TEST_PASSWORD not in stored
    assert stored.startswith("$argon2")


# ------------------------------------------------------------------ Cookie


async def test_refresh_cookie_is_scoped_to_the_refresh_path(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    """验收：refresh_token 的 Path 是 /api/v1/auth/refresh。

    这一行的性价比最高 —— refresh 从此不会跟着每个请求到处跑。
    """
    user = await user_factory(roles=["REQUESTER"])
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": user.username, "password": user.password},
    )
    header = _set_cookie_header(response, "refresh_token")
    assert "Path=/api/v1/auth/refresh" in header
    assert "HttpOnly" in header
    assert "samesite=lax" in header.lower()

    access_header = _set_cookie_header(response, "access_token")
    assert "Path=/;" in access_header or access_header.rstrip().endswith("Path=/")
    assert "HttpOnly" in access_header


async def test_refresh_issues_a_new_access_token(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    user = await user_factory(roles=["REQUESTER"])
    await _login(client, user)
    response = await client.post("/api/v1/auth/refresh")
    assert response.status_code == 200
    assert client.cookies.get("access_token")


async def test_refresh_without_a_cookie_returns_401(client: AsyncClient) -> None:
    assert (await client.post("/api/v1/auth/refresh")).status_code == 401


async def test_logout_clears_both_cookies(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    user = await user_factory(roles=["REQUESTER"])
    await _login(client, user)
    assert (await client.post("/api/v1/auth/logout")).status_code == 204
    assert (await client.get("/api/v1/auth/me")).status_code == 401


# ---------------------------------------------------------------- 权限边界


async def test_admin_route_rejects_anonymous(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/admin/suppliers")).status_code == 401


async def test_admin_route_rejects_a_requester_with_403(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    """验收：用 REQUESTER 的令牌打管理员接口 → 403（不是 404、不是 200）。"""
    user = await user_factory(roles=["REQUESTER"])
    await _login(client, user)
    response = await client.get("/api/v1/admin/suppliers")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


async def test_admin_route_allows_an_admin(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    user = await user_factory(roles=["ADMIN"])
    await _login(client, user)
    assert (await client.get("/api/v1/admin/suppliers")).status_code == 200


async def test_catalog_endpoints_all_require_login(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    """主数据接口的公开边界（2026-10-03 全部收口）。

    这三个接口**原先都是公开的**，理由写的是"申请表单要用的选择项"。但它们
    只在**登录后的界面**里用得到，公开没有任何收益，反而暴露了：

    - `/api/v1/categories/{id}` → **整份 `approval_policy`**（金额阈值与各级角色），
      即"多少钱需要谁批"这条内控结构；
    - `/api/v1/departments` → 部门树；
    - `/api/v1/categories`、`/api/v1/suppliers` → 各类清单。

    顺带收掉了 `only_active` 这个**客户端可控**的过滤器（传 `false` 可索取停用项）——
    与供应商那次同样的理由：最窄的角色不该有"要不要过滤"的决定权。

    这条同时钉住两个方向：**匿名一律 401**、**登录后照常可用**。
    少了后半句，把接口整个关掉也能"通过"。
    """
    for path in (
        "/api/v1/suppliers",
        "/api/v1/departments",
        "/api/v1/categories",
    ):
        response = await client.get(path)
        assert response.status_code in (401, 403), f"{path} 不该匿名可读：{response.text}"

    # 登录之后三个都能用
    user = await user_factory(roles=["REQUESTER"])
    await _login(client, user)
    for path in (
        "/api/v1/suppliers",
        "/api/v1/departments",
        "/api/v1/categories",
    ):
        assert (await client.get(path)).status_code == 200, path

    # `only_active` 不再是客户端能拨的开关：带上它结果必须完全一致
    plain = (await client.get("/api/v1/departments")).json()
    probed = (
        await client.get("/api/v1/departments", params={"only_active": "false"})
    ).json()
    assert plain == probed, "only_active 不该还能由调用方指定"
