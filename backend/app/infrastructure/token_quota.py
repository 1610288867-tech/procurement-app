"""问答的 token 配额（M11 增强）。

============================ 为什么 AI 接口的限流与登录限流不同 ============================
`login_throttle.py` 管的是"**多少次尝试**"，因为密码爆破靠次数。
问答接口的滥用成本完全在另一处：**token**。

    一个人 1 秒问 10 次、每次带 6000 token 上下文 → 账单按 token 涨，而"请求数"看不出来。

所以这里按 **token 配额**限流，而不是按请求数。请求数只是一个**次要**的防抖
（`min_interval_seconds`），它抓的是"脚本式连点"，那种连点会让首字延迟整体变差。

============================ 为什么仍然是进程内字典 ============================
与 `login_throttle` 同一个理由，不重复引 Redis：本项目是单进程 uvicorn 本地跑，
不存在多实例共享状态的问题。**代价要写清楚**：多 worker 或多实例部署时
每个进程各算各的，配额会被放大（"N 个进程 × 每日上限"）。
真要多实例，这一层要换成共享存储 —— 那是部署形态变化时要处理的事，
不是现在该为一个用不上的活动部件付的复杂度。

============================ 时间由调用方传入 ============================
与 `login_throttle` 一样：`now` 是参数。这样测试里"跨过一天"是瞬间完成的，
不需要 `sleep` 也不需要打桩时钟。

============================ 两个容易算错的地方 ============================
① **窗口是"自然日"还是"滑动 24 小时"**：这里用**滑动窗口**（`window_seconds`），
   因为它不需要处理时区与"跨零点重置"的边界，行为在测试里也更好描述。
   代价是"一个人在 23:59 与次日 00:01 各用一半"会被算进两个不同的窗口 ——
   但这对**防滥用**没有影响（滥用者不会挑零点）。
② **用量未知时怎么记账**：流式接口拿不到 token 用量时，**必须按估算上限记账**。
   留 0 会让"拿不到用量"变成"绕过配额"的一条免费通道 ——
   而那正是最该被限制的路径（上游不支持 usage 时用量无法核对）。
"""

from __future__ import annotations

import math

#: 估算一个中文字符的 token 数。
#: 取值理由：主流分词器对中文大致是 1 字 ≈ 0.6–1 token（英文 1 词 ≈ 1.3 token，
#: 而制度文本以中文为主）。取 **1.0**（偏保守、偏大）是**刻意**的：
#: 配额是防滥用的护栏，宁可比实际多算一点，也不要让实际用量突破上限。
#: ⚠️ 这个数**只用于配额判断**，绝不写进 `chat_messages.prompt_tokens`
#: （那里必须是真实用量，见 `domain/cost.py` 的口径说明）。
_CHARS_TO_TOKENS = 1.0

#: 字典超过这个规模时做一次全扫（见 `_sweep_if_bloated`）。
#: 取 512：正常使用下远达不到，而一旦达到说明用户很多、那次扫描的代价可以忽略。
#: 它决定了内存占用的**上界量级** —— 不是"永不清理"，而是"最多多留 512 个过期用户"。
_SWEEP_THRESHOLD = 512


def estimate_tokens(text: str) -> int:
    """按字符数估算 token 数（**上界**，用于配额判断）。

    **这不是精确计数**，也不该被当成精确计数用：它只回答"这次调用大概会吃掉多少配额"。
    真实用量以模型返回的 `usage` 为准；拿不到时**沿用这个估算**去记账，
    免得"拿不到用量"变成绕过配额的通道。
    """
    return max(1, math.ceil(len(text) * _CHARS_TO_TOKENS))


class TokenQuota:
    """按用户统计 token 用量，超出窗口上限就拒绝。

    与 `LoginThrottle` 一样是进程内、有界的（过期记录会在读取时被丢掉）。
    """

    def __init__(
        self,
        *,
        max_tokens_per_window: int = 200_000,
        window_seconds: float = 86_400.0,
        min_interval_seconds: float = 0.35,
    ) -> None:
        #: 窗口内允许的 token 上限。默认 20 万 ≈ 一次 7B 本地模型几十轮问答；
        #: 规模上够日常用，又能拦住"跑脚本刷"。**部署时按预算调**。
        #: ⚠️ 它才是**成本护栏的主力** —— 下面那个最小间隔只管连点。
        self._max_tokens = max_tokens_per_window
        self._window_seconds = window_seconds
        #: 两次提问之间的最小间隔（秒）。抓的是"脚本式连点"。
        #:
        #: ============================ 这个值被实测打回过一次 ============================
        #: 第一版定 **1.0 秒**，理由是"手动点也要一秒" —— 实测**太紧**：
        #: 页面实测里"换个会话紧接着追问"是**正常操作**，
        #: 于是它撞上限流、拿不到回答，被当成功能坏了（实测踩到）。
        #:
        #: 改成 0.35 秒：真人**连续追问**（打字 + 点发送）通常也在 1 秒上下，
        #: 而脚本连点会在几十毫秒内连发 —— 0.35 秒足够把后者挡掉，
        #: 又不至于把"换个会话马上再问"这种正常操作误伤。
        #: 成本护栏交给上面的 token 上限，**不要用这个值去承担它做不到的事**。
        self._min_interval = min_interval_seconds
        #: 用户 id → [(时间戳, token 数)]，只保留窗口内的
        self._usage: dict[str, list[tuple[float, int]]] = {}
        #: 用户 id → 上一次提问的时间戳（只用于最小间隔判断）
        self._last_seen: dict[str, float] = {}

    def configure(
        self,
        *,
        max_tokens_per_window: int | None = None,
        window_seconds: float | None = None,
        min_interval_seconds: float | None = None,
    ) -> None:
        """改配额参数（只改传入的那几项）。

        ============================ 为什么需要它 ============================
        参数要有两个来源：

            **生产** → 构造时的默认值（部署时按预算调）
            **测试** → 每个用例按需收紧，否则"配额用完"这条路径**根本没法验证**
                       （默认 20 万 token，测试里不可能真跑满）

        没有这个入口，"超配额返回 429"就只能靠改默认值来测，
        而那会把生产默认值一起改掉 —— 典型的"用测试把生产配置搅乱"。
        """
        if max_tokens_per_window is not None:
            self._max_tokens = max_tokens_per_window
        if window_seconds is not None:
            self._window_seconds = window_seconds
        if min_interval_seconds is not None:
            self._min_interval = min_interval_seconds

    def _recent(self, user_id: str, now: float) -> list[tuple[float, int]]:
        """取窗口内的记录，顺手丢掉过期的（"字典有界"就是这样做到的）。"""
        kept = [
            (stamp, tokens)
            for stamp, tokens in self._usage.get(user_id, ())
            if now - stamp < self._window_seconds
        ]
        if kept:
            self._usage[user_id] = kept
        else:
            self._usage.pop(user_id, None)
            # 用量记录清空之后，`last_seen` 也该清掉 ——
            # 否则它会随着"只问过一次的用户"一直累积（那正是字典变大的原因）。
            self._last_seen.pop(user_id, None)
        return kept

    def used_tokens(self, user_id: str, *, now: float) -> int:
        """窗口内已用 token。给界面与排障看。"""
        return sum(tokens for _, tokens in self._recent(user_id, now))

    def check(self, user_id: str, *, now: float) -> str | None:
        """能不能提问？返回 `None` 表示可以；否则返回**拒绝原因**（给人看的）。

        ⚠️ 返回原因字符串而不是布尔值：调用方要把它翻成给用户的错误信息，
        而"被限流了"与"问得太快"要说不同的话 —— 否则用户只知道"失败了"。
        """
        last = self._last_seen.get(user_id)
        if last is not None and now - last < self._min_interval:
            wait = self._min_interval - (now - last)
            return f"提问过于频繁，请等 {wait:.1f} 秒后再试"
        used = self.used_tokens(user_id, now=now)
        if used >= self._max_tokens:
            return (
                f"本窗口内的 token 配额已用完（已用 {used} / 上限 {self._max_tokens}），"
                f"请稍后再试"
            )
        return None

    def record(self, user_id: str, *, tokens: int, now: float) -> None:
        """记一次用量。**拿不到真实用量时要传估算上界**，不能传 0（见模块说明）。"""
        stamps = self._recent(user_id, now)
        stamps.append((now, max(0, tokens)))
        self._usage[user_id] = stamps
        self._last_seen[user_id] = now
        self._sweep_if_bloated(now)

    def _sweep_if_bloated(self, now: float) -> None:
        """字典变大时做一次**全扫**，把所有过期用户清掉。

        ============================ 为什么需要这一步（实测踩到）============================
        惰性清理只在**读某个用户时**清**那个用户**。于是一个人如果只问过一次、
        再也不来，他的记录会**永远留着** —— 实测：造 50 个用户、只读其中一个，
        字典里还剩 49 个。

        `LoginThrottle` 也有同样的毛病，但登录失败是**少数事件**、影响小；
        而问答是**每个正常用户**都会留下一条记录，长期跑下来字典会跟着用户总数长大。

        做法是"机会式"的：只在**超过阈值**时才全扫一次，而不是每次写入都扫
        （每次扫描是 O(用户数)，那会把一次提问变成随用户数变慢的操作）。
        摊销下来接近 O(1)，而内存占用有确定上界。
        """
        if len(self._usage) <= _SWEEP_THRESHOLD:
            return
        expired = [
            user
            for user, records in self._usage.items()
            if not any(now - stamp < self._window_seconds for stamp, _ in records)
        ]
        for user in expired:
            self._usage.pop(user, None)
            self._last_seen.pop(user, None)

    def remaining_tokens(self, user_id: str, *, now: float) -> int:
        return max(0, self._max_tokens - self.used_tokens(user_id, now=now))

    def tracked_users(self) -> int:
        """当前被跟踪的用户数。给测试用，顺便证明字典不会无限长大。"""
        return len(self._usage)

    def reset(self) -> None:
        """清空全部记录。**只给测试用** —— 生产代码没有清空入口
        （清空等于给滥用者一个重置配额的机会）。"""
        self._usage.clear()
        self._last_seen.clear()


#: 应用级单例。测试要隔离状态时自己 new 一个。
token_quota = TokenQuota()
