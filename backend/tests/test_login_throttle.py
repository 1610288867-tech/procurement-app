"""登录限流的单元测试（M3）。

刻意用进程内字典，不引 Redis —— 单进程本地跑没有共享状态的问题。
时间由调用方传入，所以"等 5 分钟"在测试里是瞬间完成的。
"""

import pytest

from app.infrastructure.login_throttle import LoginThrottle

pytestmark = pytest.mark.unit


@pytest.fixture
def throttle() -> LoginThrottle:
    return LoginThrottle(max_failures=5, window_seconds=300)


def test_not_locked_before_the_threshold(throttle: LoginThrottle) -> None:
    for index in range(4):
        throttle.record_failure("alice", now=1000.0 + index)
    assert throttle.is_locked("alice", now=1010.0) is False


def test_locked_on_the_sixth_attempt_after_five_failures(throttle: LoginThrottle) -> None:
    """验收：连错 5 次后第 6 次返回 429（而不是 401）。"""
    for index in range(5):
        throttle.record_failure("alice", now=1000.0 + index)
    assert throttle.is_locked("alice", now=1010.0) is True


def test_successful_login_clears_the_counter(throttle: LoginThrottle) -> None:
    for index in range(5):
        throttle.record_failure("alice", now=1000.0 + index)
    throttle.clear("alice")
    assert throttle.is_locked("alice", now=1010.0) is False


def test_window_expiry_unlocks_the_account(throttle: LoginThrottle) -> None:
    """验收：窗口过期后恢复。"""
    for index in range(5):
        throttle.record_failure("alice", now=1000.0 + index)
    assert throttle.is_locked("alice", now=1400.0) is False


def test_failures_of_other_users_do_not_lock_me(throttle: LoginThrottle) -> None:
    for index in range(5):
        throttle.record_failure("mallory", now=1000.0 + index)
    assert throttle.is_locked("alice", now=1010.0) is False


def test_stale_usernames_are_dropped_to_keep_the_dict_bounded(
    throttle: LoginThrottle,
) -> None:
    """只有失败的账号会进字典；窗口一过就要被清掉，否则字典无限长大。"""
    throttle.record_failure("alice", now=1000.0)
    assert throttle.is_locked("alice", now=2000.0) is False
    assert throttle.tracked_users() == 0


def test_reset_clears_every_account(throttle: LoginThrottle) -> None:
    """测试之间的隔离靠它 —— 单例状态不清，一个用例能把另一个用例的账号锁死。"""
    throttle.record_failure("alice", now=1000.0)
    throttle.record_failure("bob", now=1000.0)
    throttle.reset()
    assert throttle.tracked_users() == 0
