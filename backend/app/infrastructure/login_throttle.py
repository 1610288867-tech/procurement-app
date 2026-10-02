"""登录限流（M3）。

**进程内字典就够了，不要为这个装 Redis。**
本项目是单进程 uvicorn 本地跑，根本不存在多实例共享状态的问题；
引 Redis 只会多一个"为什么不通"的活动部件。

时间由调用方传入，所以"等 5 分钟"在测试里是瞬间完成的。

字典有界：只有失败过的账号会进来，窗口一过就被清掉，
免得字典随着攻击者换用户名无限长大。
"""

from __future__ import annotations


class LoginThrottle:
    def __init__(self, *, max_failures: int = 5, window_seconds: float = 300.0) -> None:
        self._failures: dict[str, list[float]] = {}
        self._max_failures = max_failures
        self._window_seconds = window_seconds

    def _recent_failures(self, username: str, now: float) -> list[float]:
        """取窗口内的失败时间戳，顺手把过期的丢掉（"字典有界"就是在这里做到的）。"""
        stamps = [
            stamp
            for stamp in self._failures.get(username, ())
            if now - stamp < self._window_seconds
        ]
        if stamps:
            self._failures[username] = stamps
        else:
            self._failures.pop(username, None)
        return stamps

    def is_locked(self, username: str, *, now: float) -> bool:
        return len(self._recent_failures(username, now)) >= self._max_failures

    def record_failure(self, username: str, *, now: float) -> None:
        stamps = self._recent_failures(username, now)
        stamps.append(now)
        self._failures[username] = stamps

    def clear(self, username: str) -> None:
        """登录成功就清空 —— 否则输错 4 次之后隔很久再错 1 次也会被锁。"""
        self._failures.pop(username, None)

    def tracked_users(self) -> int:
        """当前被跟踪的账号数。给测试用，顺便证明字典不会无限长大。"""
        return len(self._failures)

    def reset(self) -> None:
        """清空全部记录。**只给测试用**，生产代码没有清空的入口
        （清空等于给攻击者一个重置计数的机会）。"""
        self._failures.clear()


# 应用级单例。测试要隔离状态时自己 new 一个。
login_throttle = LoginThrottle()
