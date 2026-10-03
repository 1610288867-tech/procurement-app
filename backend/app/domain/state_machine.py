"""主单状态迁移表（M5）。

这一层是纯 Python，**不 import 任何框架**。

============================ 只写当前阶段用得到的转移 ============================
M5 只做"草稿到提交"这半段，所以 `TRANSITIONS` 里**只有四个状态**：

    DRAFT ──> IN_APPROVAL ──> WITHDRAWN
      └────> DISCARDED

不要一次把 12 个状态全写完。两个理由，第二个更重要：

1. 一次写完，测试会非常难写；
2. **写完却没有任何触发路径的状态，会让状态机测试失去意义** ——
   你测的那个转移根本走不到，测试全绿也只是"没测到"。
   M6 加 `SOURCING` / `REJECTED`，M9 加履约链那几个，都是**用到时才加**。

============================ 为什么要有 can_transition ============================
迁移表本身只是数据；真正会出事的写法是各处自己 `if status == "DRAFT"`。
只留一个判定入口，加新状态时改这里一处，漏不掉。
"""

from __future__ import annotations

from app.domain.enums import RequestStatus

# 值用集合：判断"能不能去"是 O(1)，而且写出来的形状一眼能看出全貌。
TRANSITIONS: dict[RequestStatus, frozenset[RequestStatus]] = {
    RequestStatus.DRAFT: frozenset(
        {RequestStatus.IN_APPROVAL, RequestStatus.DISCARDED}
    ),
    # M6：审批的两个出口 —— 全通过进 SOURCING，任一级驳回进 REJECTED
    RequestStatus.IN_APPROVAL: frozenset(
        {RequestStatus.WITHDRAWN, RequestStatus.SOURCING, RequestStatus.REJECTED}
    ),
    RequestStatus.DISCARDED: frozenset(),
    RequestStatus.WITHDRAWN: frozenset(),
    # 履约链的出口（AWARDED / ORDERED / …）留到 M10 用到时再加
    RequestStatus.SOURCING: frozenset(),
    RequestStatus.REJECTED: frozenset(),
}


def can_transition(current: str, target: str) -> bool:
    """`current` 能不能走到 `target`。

    认不出的状态一律返回 False（**兜底只能往"不允许"的方向兜**）：
    库里万一有一条来路不明的状态，它不该换来任何可执行的转移。
    """
    try:
        source = RequestStatus(current)
        destination = RequestStatus(target)
    except ValueError:
        return False
    return destination in TRANSITIONS.get(source, frozenset())


def allowed_targets(current: str) -> frozenset[RequestStatus]:
    """当前状态可以往哪走。给测试与报错信息用。"""
    try:
        return TRANSITIONS.get(RequestStatus(current), frozenset())
    except ValueError:
        return frozenset()
