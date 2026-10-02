"""JWT 双令牌的单元测试（M3）。

设计要点：
    - access / refresh 用同一个密钥，但靠 `typ` 声明区分，**互相不能混用**
    - 令牌里带 `tv`（token_version）：冻结账号时把它 +1，旧令牌立刻失效
    - 时间与密钥都由调用方传入，所以这里能直接测"过期"而不用等 15 分钟
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.infrastructure.security import TokenError, create_token, decode_token

pytestmark = pytest.mark.unit

# 32 字节以上 —— HS256 的密钥短于 32 字节，PyJWT 会发 InsecureKeyLengthWarning
SECRET = "test-secret-that-is-long-enough-32b"
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def test_access_token_round_trips() -> None:
    raw = create_token(
        subject="user-1", token_version=3, kind="access", secret=SECRET,
        ttl_seconds=900, now=NOW,
    )
    payload = decode_token(raw, secret=SECRET, kind="access", now=NOW)
    assert payload.subject == "user-1"
    assert payload.token_version == 3
    assert payload.expires_at == NOW + timedelta(seconds=900)


def test_refresh_token_cannot_be_used_as_access_token() -> None:
    """拿 refresh 去调业务接口必须失败 —— 否则 7 天效期的凭证就等于 15 分钟的。"""
    raw = create_token(
        subject="user-1", token_version=0, kind="refresh", secret=SECRET,
        ttl_seconds=604800, now=NOW,
    )
    with pytest.raises(TokenError):
        decode_token(raw, secret=SECRET, kind="access", now=NOW)


def test_expired_token_is_rejected() -> None:
    raw = create_token(
        subject="user-1", token_version=0, kind="access", secret=SECRET,
        ttl_seconds=900, now=NOW,
    )
    with pytest.raises(TokenError):
        decode_token(raw, secret=SECRET, kind="access", now=NOW + timedelta(seconds=901))


def test_token_signed_with_another_secret_is_rejected() -> None:
    """手改 Cookie 里的令牌 → 签名对不上 → 401（验收项）。"""
    raw = create_token(
        subject="user-1", token_version=0, kind="access", secret=SECRET,
        ttl_seconds=900, now=NOW,
    )
    with pytest.raises(TokenError):
        decode_token(
            raw, secret="another-secret-that-is-also-32-bytes", kind="access", now=NOW
        )


def test_garbage_string_is_rejected() -> None:
    with pytest.raises(TokenError):
        decode_token("not.a.jwt", secret=SECRET, kind="access", now=NOW)


def test_payload_carries_the_token_version() -> None:
    """冻结账号靠它：校验时比对 users.token_version，对不上就当令牌无效。"""
    raw = create_token(
        subject="user-1", token_version=7, kind="access", secret=SECRET,
        ttl_seconds=900, now=NOW,
    )
    assert decode_token(raw, secret=SECRET, kind="access", now=NOW).token_version == 7
