"""边界层依赖：当前用户与角色校验（M3）。

这一层回答的是"**能不能用这个接口**"；"**能看到哪些行**"是另一件事，
在 `domain/permissions.py` 算出来、由各仓库写进 `WHERE`（M4 落地）。
两者缺一不可：只做前者，接口调得通但数据越权；只做后者，谁都进得来。

越权语义（贯穿全项目）：
    没登录 / 令牌失效      → 401
    可见范围内但角色不够   → 403
    不在可见范围内         → 404（不告诉他这东西存在）
"""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.coordinators.auth import AuthCoordinator, Principal
from app.config import get_settings
from app.database import get_session
from app.errors import PermissionDeniedError, UnauthorizedError
from app.infrastructure.login_throttle import login_throttle
from app.infrastructure.repositories.user_repo import UserRepository

# Cookie 名字只在这里定义一次，边界层其他文件都从这里取
ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def auth_coordinator(session: AsyncSession) -> AuthCoordinator:
    """装配依赖。仓库与会话在结构上满足 application/ports.py 里的协议。"""
    return AuthCoordinator(
        users=UserRepository(session),
        uow=session,
        secret=get_settings().jwt_secret,
        throttle=login_throttle,
    )


async def get_current_user(request: Request, session: SessionDep) -> Principal:
    """从 httpOnly Cookie 里取令牌并换成当前用户。失败一律 401。"""
    raw = request.cookies.get(ACCESS_COOKIE)
    if not raw:
        raise UnauthorizedError("请先登录")
    return await auth_coordinator(session).principal_from_token(raw)


CurrentUser = Annotated[Principal, Depends(get_current_user)]


def require_roles(*allowed: str):
    """角色依赖工厂。路由上这样用：

        router = APIRouter(dependencies=[Depends(require_roles(ROLE_ADMIN))])

    `allowed` 为空表示"只要登录就行"。
    """

    async def dependency(user: CurrentUser) -> Principal:
        if allowed and not user.has_any(*allowed):
            raise PermissionDeniedError(f"需要以下角色之一：{', '.join(allowed)}")
        return user

    return dependency
