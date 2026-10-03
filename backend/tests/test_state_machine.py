"""主单状态机的纯函数测试（M5）。

对应验收："已提交的申请不能再 PATCH" —— 它背后其实是状态机不允许
`IN_APPROVAL -> DRAFT` 这条转移。所以这一层要先把"哪些路走得通"钉死，
接口层的 409 才有依据。

不碰数据库、不碰 HTTP：`pytest -m unit` 毫秒级。
"""

import pytest

from app.domain.enums import RequestStatus
from app.domain.state_machine import TRANSITIONS, allowed_targets, can_transition

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------- 只加当前用得到的


def test_only_the_states_we_actually_use_exist() -> None:
    """M5 + M6 一共只该有六个状态。

    一次把 12 个状态全写完，测试会非常难写，而且大部分分支没有触发路径 ——
    写了也测不到，等于给自己一个"覆盖过了"的错觉。
    履约链那几个（AWARDED / ORDERED / RECEIVED / INVOICED / PAID / CLOSED）
    留到 M10 用到时再加。
    """
    assert set(TRANSITIONS) == {
        RequestStatus.DRAFT,
        RequestStatus.IN_APPROVAL,
        RequestStatus.DISCARDED,
        RequestStatus.WITHDRAWN,
        RequestStatus.SOURCING,
        RequestStatus.REJECTED,
    }


def test_every_transition_target_is_a_known_state() -> None:
    """转移表里不许出现"指向不存在状态"的边。"""
    known = set(TRANSITIONS)
    for source, targets in TRANSITIONS.items():
        assert targets <= known, f"{source} 指向了未知状态：{targets - known}"


# ---------------------------------------------------------------- 走得通的路


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (RequestStatus.DRAFT, RequestStatus.IN_APPROVAL),
        (RequestStatus.DRAFT, RequestStatus.DISCARDED),
        (RequestStatus.IN_APPROVAL, RequestStatus.WITHDRAWN),
    ],
)
def test_allowed_transitions(source: RequestStatus, target: RequestStatus) -> None:
    assert can_transition(source, target) is True


# ---------------------------------------------------------------- 走不通的路


@pytest.mark.parametrize(
    ("source", "target"),
    [
        # 已提交的申请不能改回草稿 —— "提交后不能再 PATCH" 的根据
        (RequestStatus.IN_APPROVAL, RequestStatus.DRAFT),
        (RequestStatus.IN_APPROVAL, RequestStatus.DISCARDED),
        # 终态没有出口
        (RequestStatus.DISCARDED, RequestStatus.DRAFT),
        (RequestStatus.DISCARDED, RequestStatus.IN_APPROVAL),
        (RequestStatus.WITHDRAWN, RequestStatus.DRAFT),
        (RequestStatus.WITHDRAWN, RequestStatus.IN_APPROVAL),
        # 自己到自己
        (RequestStatus.DRAFT, RequestStatus.DRAFT),
        (RequestStatus.IN_APPROVAL, RequestStatus.IN_APPROVAL),
    ],
)
def test_forbidden_transitions(source: RequestStatus, target: RequestStatus) -> None:
    assert can_transition(source, target) is False


def test_terminal_states_have_no_outgoing_edge() -> None:
    """终态必须是死的：能出去一个，就说明状态机漏了一道口子。"""
    assert allowed_targets(RequestStatus.DISCARDED) == frozenset()
    assert allowed_targets(RequestStatus.WITHDRAWN) == frozenset()


# ---------------------------------------------------------------- 兜底方向


@pytest.mark.parametrize("raw", ["", "NOPE", "draft", "APPROVED", "None"])
def test_unknown_status_is_never_allowed(raw: str) -> None:
    """认不出的状态一律"不允许转移"。

    两件事：状态值大小写敏感（`draft` 不是 `DRAFT`），
    以及未知状态**不该换来任何可执行的边** —— 兜底只能往小里兜。
    """
    assert can_transition(raw, RequestStatus.DRAFT) is False
    assert can_transition(RequestStatus.DRAFT, raw) is False
    assert allowed_targets(raw) == frozenset()
