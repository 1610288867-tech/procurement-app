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
    """M5 + M6 + M10 全部子阶段：一共只该有**十二个**状态（履约链至此闭环）。

    一次把 12 个状态全写完，测试会非常难写，而且大部分分支没有触发路径 ——
    写了也测不到，等于给自己一个"覆盖过了"的错觉。

    所以这条断言是**逐阶段往上加**的，每次加都必须同时改这里：
    它挡的正是"顺手把履约链六个状态一次全写上"。
    到 8.4 为止，状态机里**每一个状态都有真实代码会走到它**：
    `SourcingCoordinator` / `FulfillmentCoordinator` / `SettlementCoordinator`
    各负责一段，而 `PAID → CLOSED` 是 `close` 走的。

    **`PARTIALLY_RECEIVED` 仍然不在里面**，而且**永远不该加**：
    第一版只做整单收货（没收齐直接 422），所以没有"部分收货"这个状态；
    要做它得按实施计划 5.5 节 E 的四步把状态、转移、主单判定、验收项**一起**加回来。
    """
    assert set(TRANSITIONS) == {
        RequestStatus.DRAFT,
        RequestStatus.IN_APPROVAL,
        RequestStatus.DISCARDED,
        RequestStatus.WITHDRAWN,
        RequestStatus.SOURCING,
        RequestStatus.REJECTED,
        # M10 / 8.1：定标的出口（`SourcingCoordinator.award` 真的会走到它）
        RequestStatus.AWARDED,
        # M10 / 8.2：下单与收货（`FulfillmentCoordinator` 真的会走到它们）
        RequestStatus.ORDERED,
        RequestStatus.RECEIVED,
        # M10 / 8.3：登记发票（`SettlementCoordinator.add_invoice` 真的会走到它）
        RequestStatus.INVOICED,
        # M10 / 8.4：执行付款与归档（`SettlementCoordinator.pay` / `.close`）
        RequestStatus.PAID,
        RequestStatus.CLOSED,
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
        # M6：审批的两个出口
        (RequestStatus.IN_APPROVAL, RequestStatus.SOURCING),
        (RequestStatus.IN_APPROVAL, RequestStatus.REJECTED),
        # M10 / 8.1：定标
        (RequestStatus.SOURCING, RequestStatus.AWARDED),
        # M10 / 8.2：下单与收货
        (RequestStatus.AWARDED, RequestStatus.ORDERED),
        (RequestStatus.ORDERED, RequestStatus.RECEIVED),
        # M10 / 8.3：登记发票
        (RequestStatus.RECEIVED, RequestStatus.INVOICED),
        # M10 / 8.4：付款与归档
        (RequestStatus.INVOICED, RequestStatus.PAID),
        (RequestStatus.PAID, RequestStatus.CLOSED),
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
