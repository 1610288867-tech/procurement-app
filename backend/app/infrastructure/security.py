"""口令哈希与 JWT 双令牌（M3）。

两件事放一个文件，因为它们同属"凭证"这一个关注点，而且都很小：
口令怎么存、令牌怎么签发与校验。

============================ 口令 ============================
用 Argon2id（`argon2-cffi` 的默认参数）。**绝不用 MD5/SHA256**，
也不要拿"反正只是练习"当借口存明文 —— 这个习惯一旦养成很危险。

`verify_password` 任何异常都返回 False：库里的哈希被写坏、格式不认，
都只应该表现为"这次登录失败"，而不是把 500 抛给登录接口。

============================ 令牌 ============================
access（15 分钟）与 refresh（7 天）用同一个密钥，靠 `typ` 声明区分，
**互相不能混用** —— 否则 7 天效期的 refresh 就等于一张 7 天的通行证。

载荷里带 `tv`（token_version）：冻结账号时把它 +1，校验时比对数据库里的值，
旧令牌立刻失效。**所以不需要黑名单，也不需要再做一遍 refresh rotation。**

时间与密钥都由调用方传入（不在这里读配置），这样测试能直接构造"已过期"，
不用真的等 15 分钟。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from argon2 import PasswordHasher

# 算法固定 HS256：单机、单密钥、没有第三方验签方，用它就够。
_ALGORITHM = "HS256"

_hasher = PasswordHasher()

TokenKind = Literal["access", "refresh"]


def hash_password(raw: str) -> str:
    """哈希口令。每次都换新的随机盐，所以同一口令两次结果不同。"""
    return _hasher.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    """校验口令。**任何异常都当作"不匹配"**，绝不把异常抛给登录接口。"""
    try:
        return _hasher.verify(hashed, raw)
    except Exception:  # noqa: BLE001 - 哈希损坏/格式不认都只等于"这次登录失败"
        return False


class TokenError(Exception):
    """令牌不可用（签名不对、已过期、类型不匹配）。边界层把它转成 401。"""


@dataclass(frozen=True)
class TokenPayload:
    subject: str
    token_version: int
    kind: str
    expires_at: datetime


def create_token(
    *,
    subject: str,
    token_version: int,
    kind: TokenKind,
    secret: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> str:
    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + timedelta(seconds=ttl_seconds)
    claims = {
        "sub": subject,
        "tv": token_version,
        "typ": kind,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(claims, secret, algorithm=_ALGORITHM)


def decode_token(
    raw: str,
    *,
    secret: str,
    kind: TokenKind,
    now: datetime | None = None,
) -> TokenPayload:
    """校验签名与有效期，并确认令牌类型。失败一律抛 TokenError。

    分工很明确：**签名与结构交给 PyJWT，时间判断由我们自己算**。
    因为 PyJWT 内部读真实时钟，测试里就没法注入"现在是几点"，
    构造"已过期"只能真的去等 15 分钟。关掉它的 exp/iat 校验、
    拿 `now` 自己比一次，既确定又照样拦得住过期令牌。
    """
    moment = now or datetime.now(UTC)
    try:
        claims = jwt.decode(
            raw,
            secret,
            algorithms=[_ALGORITHM],
            options={
                "require": ["sub", "typ", "iat", "exp"],
                "verify_exp": False,
                "verify_iat": False,
            },
        )
    except jwt.PyJWTError as exc:
        raise TokenError("令牌无效或已过期") from exc

    if claims.get("typ") != kind:
        raise TokenError(f"令牌类型不匹配：需要 {kind}")

    expires_at = datetime.fromtimestamp(int(claims["exp"]), tz=UTC)
    if expires_at <= moment:
        raise TokenError("令牌已过期")

    return TokenPayload(
        subject=str(claims["sub"]),
        token_version=int(claims.get("tv", 0)),
        kind=str(claims["typ"]),
        expires_at=expires_at,
    )
