"""采购申请业务规则的纯函数测试（M5）。

对应 M5 验收里最要紧的两条：
    "能建草稿、改草稿、加三行明细，**amount_total 由后端算出来且正确**"
    "同一个幂等键但改成不同金额 → 409"
后者能成立的前提，正是这里把"金额是算出来的"钉死。

不碰数据库、不碰 HTTP。
"""

import pytest

from app.domain.enums import RequestStatus
from app.domain.purchase import (
    MAX_ITEMS,
    LineDraft,
    PurchaseError,
    check_submittable,
    parse_lines,
    parse_urgency,
    total_amount_fen,
)

pytestmark = pytest.mark.unit


def _line(name: str, quantity: int, unit_price_fen: int) -> dict:
    return {"name": name, "quantity": quantity, "unit_price_fen": unit_price_fen}


# ---------------------------------------------------------------- 明细与金额


def test_three_lines_total_is_the_sum_of_line_amounts() -> None:
    """验收原话：加三行明细，amount_total 由后端算出来且正确。

    100.00 元 × 2 + 0.01 元 × 1 + 33.33 元 × 3
    = 20000 + 1 + 9999 = 30000 分（300.00 元）
    """
    lines = parse_lines(
        [
            _line("打印纸", 2, 10_000),
            _line("回形针", 1, 1),
            _line("文件夹", 3, 3_333),
        ]
    )
    assert [line.amount_fen for line in lines] == [20_000, 1, 9_999]
    assert total_amount_fen(lines) == 30_000


def test_line_numbers_are_assigned_by_the_backend() -> None:
    """客户端给的 line_no 不作数，后端按顺序重排。

    否则客户端能造出重复或乱序的行号，明细顺序就没有意义了。
    """
    lines = parse_lines(
        [
            {"name": "甲", "quantity": 1, "unit_price_fen": 100, "line_no": 99},
            {"name": "乙", "quantity": 1, "unit_price_fen": 100, "line_no": 99},
        ]
    )
    assert [line.line_no for line in lines] == [1, 2]


def test_client_supplied_amount_is_ignored() -> None:
    """**前端传的 amount 一律不信任**：算出来是多少就是多少。

    这条是"账目对不对"的问题，不是"更严谨一点"的问题。
    """
    (line,) = parse_lines(
        [{"name": "被篡改的行", "quantity": 2, "unit_price_fen": 500, "amount_fen": 1}]
    )
    assert line.amount_fen == 1_000


@pytest.mark.parametrize(
    ("raw", "message_part"),
    [
        ({"name": "", "quantity": 1, "unit_price_fen": 100}, "名称"),
        ({"name": "甲", "quantity": 0, "unit_price_fen": 100}, "数量必须大于 0"),
        ({"name": "甲", "quantity": -1, "unit_price_fen": 100}, "数量必须大于 0"),
        ({"name": "甲", "quantity": 1, "unit_price_fen": 0}, "单价必须大于 0"),
        ({"name": "甲", "quantity": 1, "unit_price_fen": -5}, "单价必须大于 0"),
        ({"name": "甲", "quantity": 1.5, "unit_price_fen": 100}, "数量必须是整数"),
        ({"name": "甲", "quantity": 1, "unit_price_fen": 1.5}, "单价必须是整数"),
        ({"name": "甲", "quantity": True, "unit_price_fen": 100}, "数量必须是整数"),
    ],
)
def test_bad_lines_are_rejected(raw: dict, message_part: str) -> None:
    """非法明细要 422，且**提示里说清是哪一行、哪个字段**。

    `1.5` 与 `True` 这两条容易被放过：前者是金额禁止浮点，
    后者是 Python 里 `True == 1`，不显式挡就会静默当成 1 件。
    """
    with pytest.raises(PurchaseError) as exc:
        parse_lines([raw])
    assert message_part in str(exc.value)


def test_empty_items_are_rejected() -> None:
    with pytest.raises(PurchaseError):
        parse_lines([])


def test_too_many_items_are_rejected() -> None:
    raw = [_line(f"行{i}", 1, 100) for i in range(MAX_ITEMS + 1)]
    with pytest.raises(PurchaseError) as exc:
        parse_lines(raw)
    assert str(MAX_ITEMS) in str(exc.value)


# ---------------------------------------------------------------- 紧急程度


def test_urgency_defaults_to_normal() -> None:
    assert parse_urgency(None) == "NORMAL"
    assert parse_urgency("") == "NORMAL"


def test_urgency_accepts_urgent() -> None:
    assert parse_urgency("URGENT") == "URGENT"


def test_misspelled_urgency_is_rejected_not_silently_downgraded() -> None:
    """`URGENTT` 拼错时必须报错。

    宽松处理（认不出就按 NORMAL）会把一笔加急申请**静默降级**成普通件，
    而且没有任何人会发现 —— 这正是"不会报错、只会算错"那一类问题。
    """
    with pytest.raises(PurchaseError):
        parse_urgency("URGENTT")
    with pytest.raises(PurchaseError):
        parse_urgency("urgent")


# ---------------------------------------------------------------- 提交前置


def _three_lines() -> list[LineDraft]:
    return parse_lines([_line("甲", 1, 100), _line("乙", 2, 200)])


def test_submittable_draft_passes() -> None:
    lines = _three_lines()
    check_submittable(
        status=RequestStatus.DRAFT,
        lines=lines,
        amount_total_fen=total_amount_fen(lines),
    )


def test_only_draft_can_be_submitted() -> None:
    lines = _three_lines()
    total = total_amount_fen(lines)
    for status in (RequestStatus.IN_APPROVAL, RequestStatus.WITHDRAWN):
        with pytest.raises(PurchaseError):
            check_submittable(status=status, lines=lines, amount_total_fen=total)


def test_no_items_cannot_be_submitted() -> None:
    with pytest.raises(PurchaseError):
        check_submittable(status=RequestStatus.DRAFT, lines=[], amount_total_fen=0)


def test_zero_amount_cannot_be_submitted() -> None:
    """业务上不存在 0 元采购：金额为 0 的申请不许进审批流。"""
    with pytest.raises(PurchaseError) as exc:
        check_submittable(status=RequestStatus.DRAFT, lines=[], amount_total_fen=0)
    assert "明细" in str(exc.value) or "金额为 0" in str(exc.value)


def test_total_must_match_the_lines() -> None:
    """总额与明细对不上 = 有人绕过重算写了值，一律拒绝。"""
    lines = _three_lines()
    with pytest.raises(PurchaseError) as exc:
        check_submittable(
            status=RequestStatus.DRAFT,
            lines=lines,
            amount_total_fen=total_amount_fen(lines) + 1,
        )
    assert "不一致" in str(exc.value)
