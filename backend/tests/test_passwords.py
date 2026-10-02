"""口令哈希的单元测试（M3）。

验收里最硬的一条：**库里存的东西不能包含原始口令**。
这一文件不连数据库，只证明哈希函数本身是对的。
"""

import pytest

from app.infrastructure.security import hash_password, verify_password

pytestmark = pytest.mark.unit

RAW = "Dev-Passw0rd!"


def test_hash_does_not_contain_the_raw_password() -> None:
    hashed = hash_password(RAW)
    assert RAW not in hashed
    assert hashed.startswith("$argon2")


def test_verify_accepts_the_correct_password() -> None:
    assert verify_password(RAW, hash_password(RAW)) is True


def test_verify_rejects_a_wrong_password() -> None:
    assert verify_password("wrong-password", hash_password(RAW)) is False


def test_same_password_hashes_differently_each_time() -> None:
    """每次加新的盐 —— 否则相同口令在库里长得一样，一眼就能看出谁和谁同密码。"""
    assert hash_password(RAW) != hash_password(RAW)


def test_verify_returns_false_on_a_malformed_hash() -> None:
    """库里的哈希被写坏时返回 False，而不是抛异常把 500 抛给调用方。"""
    assert verify_password(RAW, "not-a-valid-argon2-hash") is False
