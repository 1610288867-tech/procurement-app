"""认证用例（M3）：登录、刷新、登出、当前账号。

事务边界在这一层；口令与令牌的算法在 `infrastructure/security.py`；
"能看到多宽"在 `domain/permissions.py`。这里只负责把它们串起来，
并把失败翻译成 401 / 429。

============================ 三件容易做错的事 ============================
1. **登录失败不区分"用户不存在"和"口令错误"**：响应完全一样。
   区分开来等于给攻击者一个免费的账号枚举接口。
2. **冻结判定要连 token_version 一起比对**：只看 `active` 的话，
   冻结前签发的令牌能一直用到自己过期为止。
3. **限流按用户名记，成功即清零**：否则输错 4 次、过很久再错 1 次也会被锁。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.application.ports import UnitOfWork, UserRepositoryPort, UserView
from app.domain.permissions import ALL_ROLES, Visibility, resolve_visibility
from app.errors import TooManyAttemptsError, UnauthorizedError
from app.infrastructure.login_throttle import LoginThrottle
from app.infrastructure.security import (
    TokenError,
    TokenKind,
    create_token,
    decode_token,
    verify_password,
)

# 有效期写死在这里，不给它开关（配置项保持个位数）。
ACCESS_TTL_SECONDS = 900  # 15 分钟
REFRESH_TTL_SECONDS = 7 * 24 * 3600  # 7 天

# 口令错误时对外一律用这一句，别把"用户不存在"单独说出来
_LOGIN_FAILED = "用户名或密码不正确"


@dataclass(frozen=True)
class Principal:
    """已登录用户。角色已过滤成"认识的那些"，认不出的一律丢掉。"""

    id: uuid.UUID
    username: str
    full_name: str
    department_id: uuid.UUID | None
    roles: tuple[str, ...]

    def has_any(self, *allowed: str) -> bool:
        return bool(set(self.roles) & set(allowed))

    @property
    def visibility(self) -> Visibility:
        """数据级权限：这个用户能看到的行的范围（M4 起写进 WHERE）。"""
        return resolve_visibility(self.roles)


@dataclass(frozen=True)
class LoginResult:
    principal: Principal
    access_token: str
    refresh_token: str


class AuthCoordinator:
    def __init__(
        self,
        *,
        users: UserRepositoryPort,
        uow: UnitOfWork,
        secret: str,
        throttle: LoginThrottle,
    ) -> None:
        self._users = users
        self._uow = uow
        self._secret = secret
        self._throttle = throttle

    # ---------------------------------------------------------------- 登录

    async def login(self, *, username: str, password: str) -> LoginResult:
        key = username.strip().lower()
        now = time.time()

        if self._throttle.is_locked(key, now=now):
            raise TooManyAttemptsError()

        user = await self._users.find_by_username(key)
        # 三个条件写在一起：不存在 / 已冻结 / 口令不对，对外表现完全一致
        if user is None or not user.active or not verify_password(password, user.password_hash):
            self._throttle.record_failure(key, now=now)
            raise UnauthorizedError(_LOGIN_FAILED, code="INVALID_CREDENTIALS")

        self._throttle.clear(key)
        principal = await self._build_principal(user)
        return LoginResult(
            principal=principal,
            access_token=self._sign(user, kind="access", ttl=ACCESS_TTL_SECONDS),
            refresh_token=self._sign(user, kind="refresh", ttl=REFRESH_TTL_SECONDS),
        )

    # ---------------------------------------------------------------- 凭令牌

    async def principal_from_token(
        self, raw: str, *, kind: TokenKind = "access"
    ) -> Principal:
        """把 Cookie 里的令牌换成当前用户。任何一步不对都是 401。"""
        try:
            payload = decode_token(raw, secret=self._secret, kind=kind)
        except TokenError as exc:
            raise UnauthorizedError(str(exc)) from exc

        try:
            user_id = uuid.UUID(payload.subject)
        except ValueError as exc:
            raise UnauthorizedError("令牌内容不可解析") from exc

        user = await self._users.find_by_id(user_id)
        if user is None or not user.active or user.token_version != payload.token_version:
            # 冻结、删除、改过版本号 —— 旧令牌一律作废
            raise UnauthorizedError("账号已失效，请重新登录")

        return await self._build_principal(user)

    async def refresh(self, raw: str) -> tuple[Principal, str]:
        """用 refresh 换一张新的 access。**不重签 refresh** ——
        `token_version` 已经覆盖了"旧凭证立即失效"，再叠一层 rotation 只会更难调。"""
        principal = await self.principal_from_token(raw, kind="refresh")
        user = await self._users.find_by_id(principal.id)
        if user is None:  # pragma: no cover - principal 刚校验过，理论上到不了
            raise UnauthorizedError("账号已失效，请重新登录")
        return principal, self._sign(user, kind="access", ttl=ACCESS_TTL_SECONDS)

    # ---------------------------------------------------------------- 内部

    def _sign(self, user: UserView, *, kind: TokenKind, ttl: int) -> str:
        return create_token(
            subject=str(user.id),
            token_version=user.token_version,
            kind=kind,
            secret=self._secret,
            ttl_seconds=ttl,
        )

    async def _build_principal(self, user: UserView) -> Principal:
        roles: Sequence[str] = await self._users.roles_of(user.id)
        # 只留认识的角色：库里万一多出一条没人认得的记录，它不该换来任何权限
        known = tuple(sorted(role for role in roles if role in ALL_ROLES))
        return Principal(
            id=user.id,
            username=user.username,
            full_name=user.full_name,
            department_id=user.department_id,
            roles=known,
        )
