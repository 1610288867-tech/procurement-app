"""token 配额的纯函数/纯内存测试（M11 增强）。

这一层要钉住四件事：
**① 窗口过期后配额确实恢复；② 用量未知时按估算记账（不能记 0）；
③ 字典有界（不会随用户数无限长大）；④ 两种拒绝给的原因不同** ——
"被限流了"与"问得太快"要说不同的话，否则用户只知道"失败了"。
"""

from __future__ import annotations

import pytest

from app.infrastructure.token_quota import TokenQuota, estimate_tokens

pytestmark = pytest.mark.unit


# ================================================================ 估算


def test_estimate_is_an_upper_bound_and_never_zero() -> None:
    """估算**只用于配额**，而且永远 ≥ 1 —— 空文本也不该算成 0。

    算成 0 的话，"每次带一个空问题"就能无限刷（虽然业务上问题不能为空，
    但配额层不该依赖上游一定校验过）。
    """
    assert estimate_tokens("") == 1
    assert estimate_tokens("采购管理办法") == 6
    assert estimate_tokens("a" * 100) == 100


# ================================================================ 配额


def test_under_quota_is_allowed() -> None:
    quota = TokenQuota(max_tokens_per_window=1000, window_seconds=100.0, min_interval_seconds=0.0)
    assert quota.check("u1", now=0.0) is None
    quota.record("u1", tokens=400, now=0.0)
    assert quota.check("u1", now=1.0) is None
    assert quota.remaining_tokens("u1", now=1.0) == 600


def test_over_quota_is_rejected_with_a_reason() -> None:
    """**返回的是原因字符串，不是布尔值** —— 调用方要拿它当错误信息给用户。"""
    quota = TokenQuota(max_tokens_per_window=1000, window_seconds=100.0, min_interval_seconds=0.0)
    quota.record("u1", tokens=1000, now=0.0)
    reason = quota.check("u1", now=1.0)
    assert reason is not None
    assert "配额" in reason
    assert "1000" in reason, "要把已用与上限说清楚，否则用户不知道等到什么时候"


def test_quota_recovers_after_the_window_expires() -> None:
    """**窗口一过，配额恢复。** 这是"限流"与"封禁"的分界：本功能是限流。"""
    quota = TokenQuota(max_tokens_per_window=1000, window_seconds=100.0, min_interval_seconds=0.0)
    quota.record("u1", tokens=1000, now=0.0)
    assert quota.check("u1", now=50.0) is not None, "窗口内应当仍被限"
    assert quota.check("u1", now=101.0) is None, "窗口过了应当恢复"
    assert quota.used_tokens("u1", now=101.0) == 0


def test_min_interval_rejects_rapid_fire_with_a_different_reason() -> None:
    """"问得太快"与"配额用完"必须是**两条不同的提示**。

    否则用户只知道"失败了"，会一直重试 —— 而重试正是要拦住的行为。
    """
    quota = TokenQuota(max_tokens_per_window=10_000, window_seconds=100.0, min_interval_seconds=2.0)
    quota.record("u1", tokens=10, now=0.0)
    reason = quota.check("u1", now=0.5)
    assert reason is not None
    assert "频繁" in reason
    assert "配额" not in reason, "这不是配额问题，不该说成配额"
    assert quota.check("u1", now=2.1) is None


def test_quota_is_per_user() -> None:
    """**按用户隔离**：一个人的用量不该扣到别人头上。"""
    quota = TokenQuota(max_tokens_per_window=100, window_seconds=100.0, min_interval_seconds=0.0)
    quota.record("u1", tokens=100, now=0.0)
    assert quota.check("u1", now=1.0) is not None
    assert quota.check("u2", now=1.0) is None


def test_usage_is_cumulative_within_the_window() -> None:
    """窗口内**累加**：单次很小也拦得住"刷很多次"。"""
    quota = TokenQuota(max_tokens_per_window=1000, window_seconds=100.0, min_interval_seconds=0.0)
    for index in range(10):
        assert quota.check("u1", now=float(index)) is None
        quota.record("u1", tokens=100, now=float(index))
    assert quota.used_tokens("u1", now=10.0) == 1000
    assert quota.check("u1", now=11.0) is not None


def test_unknown_usage_must_be_recorded_as_an_estimate_not_zero() -> None:
    """**拿不到用量时要按估算记账，不能记 0。**

    记 0 会让"上游不返回 usage"变成一条**绕过配额的免费通道** ——
    而那恰恰是最该被限制的路径（用量无法核对）。
    这条测试把"调用方必须传估算值"这个约定钉住：
    传 0 是允许的（真用了 0 token 不可能），但**传估算值才会被计入**。
    """
    quota = TokenQuota(max_tokens_per_window=1000, window_seconds=100.0, min_interval_seconds=0.0)
    estimate = estimate_tokens("采购金额超过十万元的应当公开招标。" * 10)
    quota.record("u1", tokens=estimate, now=0.0)
    assert quota.used_tokens("u1", now=1.0) == estimate
    assert estimate > 0


# ================================================================ 字典有界


def test_expired_entries_are_dropped_so_the_dict_stays_bounded() -> None:
    """**字典必须有界**：窗口一过，记录被丢掉（不只是被读到的那一个）。

    ============================ 这条测试抓到过一个真实缺陷 ============================
    第一版的惰性清理只在**读某个用户时清那个用户**。于是：
    造 50 个用户、只读其中一个 → 字典里还剩 **49** 个（实测）。
    也就是说"字典有界"只对**被读到的**用户成立；一个人只问过一次、
    再也不来，他的记录会永远留着。

    `LoginThrottle` 也有同样毛病，但登录失败是少数事件；
    问答是**每个正常用户**都会留一条，所以这里必须补上"变大时全扫"。
    """
    quota = TokenQuota(max_tokens_per_window=1000, window_seconds=10.0, min_interval_seconds=0.0)
    # 超过 `_SWEEP_THRESHOLD` 之后再写一次，触发全扫
    from app.infrastructure.token_quota import _SWEEP_THRESHOLD

    for index in range(_SWEEP_THRESHOLD + 5):
        quota.record(f"u{index}", tokens=10, now=0.0)
    # 窗口过了之后随便写一条 —— 全扫会把所有过期的清掉
    quota.record("trigger", tokens=1, now=100.0)
    assert quota.tracked_users() == 1, "过期用户应当被全扫清掉，只剩刚写的那个"
    assert quota.used_tokens("u0", now=100.0) == 0


def test_negative_tokens_are_clamped_to_zero() -> None:
    """上游给了负数说明它的 usage 不可信 → 按 0 记（**不抵消**已用量）。"""
    quota = TokenQuota(max_tokens_per_window=100, window_seconds=100.0, min_interval_seconds=0.0)
    quota.record("u1", tokens=-500, now=0.0)
    assert quota.used_tokens("u1", now=1.0) == 0


def test_reset_clears_everything_and_is_test_only() -> None:
    """`reset` 只给测试用：生产没有清空入口（那等于给滥用者重置配额的机会）。"""
    quota = TokenQuota(max_tokens_per_window=100, window_seconds=100.0, min_interval_seconds=0.0)
    quota.record("u1", tokens=100, now=0.0)
    quota.reset()
    assert quota.tracked_users() == 0
    assert quota.check("u1", now=0.0) is None
