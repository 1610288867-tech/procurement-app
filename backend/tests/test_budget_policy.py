"""预算可用额与结转口径的纯函数测试（M7）。

对应 M7 验收里三条最容易"算错但不报错"的：
    "预算总额 1000 元时，同时提交两笔 800 元的申请，只有一笔成功"
    "申请 1000 元、实际付款 900 元之后：已花 +900、该申请预留归零、总额不变"
    "该申请的三类流水相加（RESERVE − RELEASE − SPEND）= 0"

最后一条能成立的前提，就是这里把"差额必须显式释放"钉死。
不碰数据库、不碰 HTTP。
"""

from datetime import UTC, datetime

import pytest

from app.domain.budget_policy import (
    BudgetError,
    available_fen,
    check_reservable,
    resolve_budget_year,
    settlement,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------- 可用余额


def test_available_is_total_minus_reserved_minus_spent() -> None:
    """1000 元总额、预留 800 元、已花 100 元 → 可用 100 元。"""
    assert (
        available_fen(
            amount_total=100_000, amount_reserved=80_000, amount_spent=10_000
        )
        == 10_000
    )


def test_available_can_be_zero_but_never_silently_negative() -> None:
    """刚好占满 → 0。三个字段都是合法的非负数，公式本身不抛异常。"""
    assert (
        available_fen(amount_total=100_000, amount_reserved=100_000, amount_spent=0) == 0
    )


def test_available_rejects_negative_fields() -> None:
    """库里出现负数说明有东西绕过守卫写了值 —— 宁可报错，也不要把负数算进余额。"""
    with pytest.raises(BudgetError, match="已预留不能为负数"):
        available_fen(amount_total=100_000, amount_reserved=-1, amount_spent=0)


def test_available_rejects_bool() -> None:
    """`True` 在 Python 里能当 1 用，金额字段必须把它挡在外面。"""
    with pytest.raises(BudgetError, match="必须是整数"):
        available_fen(amount_total=100_000, amount_reserved=True, amount_spent=0)


# ---------------------------------------------------------------- 够不够


def test_check_reservable_passes_when_exactly_enough() -> None:
    """可用 800 元、要预留 800 元 → 通过（边界必须放行，否则永远差一分钱）。"""
    check_reservable(
        amount_total=100_000, amount_reserved=20_000, amount_spent=0, amount_fen=80_000
    )


def test_check_reservable_reports_the_shortfall_in_fen() -> None:
    """不足时把"可用 / 需要 / 还差"三个数都写出来 —— 这是用户唯一能自救的线索。"""
    with pytest.raises(BudgetError) as excinfo:
        check_reservable(
            amount_total=100_000,
            amount_reserved=80_000,
            amount_spent=0,
            amount_fen=80_000,
        )
    message = str(excinfo.value)
    assert "可用 20000 分" in message
    assert "需要 80000 分" in message
    assert "还差 60000 分" in message


def test_check_reservable_rejects_zero_or_negative_amount() -> None:
    """0 元预留没有意义：它会写出一条金额为 0 的流水，账上凭空多一行。"""
    for bad in (0, -1):
        with pytest.raises(BudgetError):
            check_reservable(
                amount_total=100_000,
                amount_reserved=0,
                amount_spent=0,
                amount_fen=bad,
            )


# ---------------------------------------------------------------- 结转（差额必须释放）


def test_settlement_releases_the_difference() -> None:
    """1000 元预留、900 元实付 → SPEND 900 元 + RELEASE 100 元。

    这是 M7 最要紧的一条算术：少了 RELEASE，那 100 元会永久挂在预留上。
    """
    plan = settlement(reserved_fen=100_000, actual_fen=90_000)
    assert plan.spend_fen == 90_000
    assert plan.release_fen == 10_000
    assert plan.total_fen == 100_000


def test_settlement_with_equal_amounts_releases_nothing() -> None:
    """实付 = 预留 → 只写 SPEND，不写差额流水（金额为 0 的流水没有意义）。"""
    plan = settlement(reserved_fen=50_000, actual_fen=50_000)
    assert plan.spend_fen == 50_000
    assert plan.release_fen == 0


def test_settlement_refuses_to_pay_more_than_reserved() -> None:
    """实付大于预留 → 拒绝。账面宁可停下来，也不要记一笔对不上的钱。"""
    with pytest.raises(BudgetError, match="超过预留"):
        settlement(reserved_fen=90_000, actual_fen=100_000)


def test_settlement_refuses_when_there_is_no_reservation() -> None:
    """没有预留就结转 = 某个释放入口被漏掉了，必须报错而不是把账弄乱。"""
    with pytest.raises(BudgetError, match="没有任何预留"):
        settlement(reserved_fen=0, actual_fen=100)


def test_settlement_rejects_zero_actual_amount() -> None:
    """付款金额为 0 不是"免费"，是数据不对。"""
    with pytest.raises(BudgetError, match="实付金额必须大于 0"):
        settlement(reserved_fen=10_000, actual_fen=0)


def test_settlement_keeps_the_ledger_balanced() -> None:
    """三类流水相加（RESERVE − RELEASE − SPEND）= 0，对任意实付金额都成立。"""
    reserved = 123_456
    for actual in (1, 50_000, 100_000, reserved):
        plan = settlement(reserved_fen=reserved, actual_fen=actual)
        assert reserved - plan.release_fen - plan.spend_fen == 0


# ---------------------------------------------------------------- 年度口径


def test_budget_year_comes_from_the_submission_moment() -> None:
    """草稿可以跨年躺着，占哪一年的额度看**提交**那一刻。"""
    assert resolve_budget_year(datetime(2026, 1, 1, 0, 0, tzinfo=UTC)) == 2026
    assert resolve_budget_year(datetime(2026, 12, 31, 23, 59, tzinfo=UTC)) == 2026
    assert resolve_budget_year(datetime(2027, 1, 1, 0, 0, tzinfo=UTC)) == 2027
