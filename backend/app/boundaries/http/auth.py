"""认证接口：登录 / 刷新 / 登出 / 当前账号（M3）。

============================ Cookie 的三个属性一次写对 ============================
    httponly=True    防 XSS 读取（JS 拿不到令牌）
    samesite="lax"   防 CSRF：跨站发起的 POST 不会自动带上它
    secure=False     本地 http 开发期必须 False；将来上 HTTPS 再改 True

`refresh_token` 多一条 `path="/api/v1/auth/refresh"` —— **这一行性价比最高**：
refresh 从此只在这一个路径下发出，别的请求根本带不上它，泄露面立刻变小。

============================ CSRF 的三条防线 ============================
    SameSite=Lax + refresh 限定 Path + （M4 起）写操作要求 Idempotency-Key 自定义头
浏览器不会跨站自动带上自定义头，所以 M4 的幂等机制白捡了一道 CSRF 防线。
前后端同源（Vite 代理）时这套够用；上公网再加 double-submit token。
"""

import uuid

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from app.application.coordinators.auth import (
    ACCESS_TTL_SECONDS,
    REFRESH_TTL_SECONDS,
    Principal,
)
from app.boundaries.deps import (
    ACCESS_COOKIE,
    REFRESH_COOKIE,
    CurrentUser,
    SessionDep,
    auth_coordinator,
)
from app.errors import UnauthorizedError

router = APIRouter(prefix="/api/v1/auth", tags=["认证"])

# refresh 只在这一个路径下发送（见模块文档）
REFRESH_PATH = "/api/v1/auth/refresh"


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    """对外的账号视图。**故意没有 password_hash** ——
    令牌哈希永远不该出现在任何响应体里。"""

    id: uuid.UUID
    username: str
    full_name: str
    department_id: uuid.UUID | None = None
    roles: list[str]


def _to_out(principal: Principal) -> UserOut:
    return UserOut(
        id=principal.id,
        username=principal.username,
        full_name=principal.full_name,
        department_id=principal.department_id,
        roles=list(principal.roles),
    )


def _set_access_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        ACCESS_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
        max_age=ACCESS_TTL_SECONDS,
    )


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=False,
        path=REFRESH_PATH,
        max_age=REFRESH_TTL_SECONDS,
    )


@router.post("/login", response_model=UserOut, summary="登录")
async def login(payload: LoginIn, response: Response, session: SessionDep) -> UserOut:
    result = await auth_coordinator(session).login(
        username=payload.username, password=payload.password
    )
    _set_access_cookie(response, result.access_token)
    _set_refresh_cookie(response, result.refresh_token)
    return _to_out(result.principal)


@router.post("/refresh", response_model=UserOut, summary="刷新访问令牌")
async def refresh(request: Request, response: Response, session: SessionDep) -> UserOut:
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise UnauthorizedError("缺少刷新令牌，请重新登录")
    principal, access_token = await auth_coordinator(session).refresh(raw)
    _set_access_cookie(response, access_token)
    return _to_out(principal)


@router.post("/logout", status_code=204, response_class=Response, summary="登出")
async def logout() -> Response:
    """清掉两张 Cookie。

    刻意**不**碰 `token_version`：登出只该结束当前这个浏览器，
    不该把用户在别处的会话一起踢掉（那是"冻结账号"才该干的事）。
    """
    response = Response(status_code=204)
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_PATH)
    return response


@router.get("/me", response_model=UserOut, summary="当前账号")
async def me(user: CurrentUser) -> UserOut:
    return _to_out(user)
