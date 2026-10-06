"""订单与收货规则的纯函数测试（M10 / 8.2）。

不连数据库、不碰 HTTP，毫秒级。测的是 `domain/fulfillment.py`。

============================ 四条最值钱的用例 ============================
1. **单价来源按采购方式分**：询价取成交单价（整单统一）、直采取申请单价 ——
   这条写错就会把订单金额悄悄变回申请金额，而三单匹配会跟着一起错（两边都错就抵消）；
2. **订单金额只由后端按明细重算**：`order_amount_fen` 是唯一入口；
3. **超收 422**：多收的货没有合同依据，而"收货 ≤ 订单"正是三单匹配的前提；
4. **"收齐"才算收到**：写成"收了一行就算收到"会让仓库签一半货就进入待付款。
"""

from __future__ import annotations

import pytest

from app.domain.fulfillment import (
    SourceLine,
    SourcingOrderError,
    build_order_lines,
    check_receipt,
    is_fully_received,
    order_amount_fen,
    receipt_plan,
    receipt_problems,
    short_lines,
)

pytestmark = pytest.mark.unit


def line(
    item_id: str = "i1",
    name: str = "A4 复印纸",
    quantity: int = 10,
    unit_price_fen: int = 2_500,
    spec: str | None = None,
) -> SourceLine:
    return SourceLine(
        item_id=item_id,
        name=name,
        spec=spec,
        quantity=quantity,
        unit_price_fen=unit_price_fen,
    )


# ---------------------------------------------------------------- 单价来源


def test_inquiry_uses_the_awarded_unit_price_for_every_line() -> None:
    """**询价场景：整单统一取定标成交单价**（通常低于申请单价）。

    这条就是"订单总额可能小于申请总额"的来源。写成"逐行取申请单价"的话，
    订单金额会变回申请金额，而**发票按订单金额比对时也会一起错**——
    两边都错就互相抵消，谁都不会报错。
    """
    lines = [
        line("i1", "A4 复印纸", quantity=10, unit_price_fen=2_500),  # 申请 25 元
        line("i2", "订书机", quantity=2, unit_price_fen=4_000),  # 申请 40 元
    ]
    drafts = build_order_lines(lines, deal_unit_price_fen=2_000)  # 成交 20 元
    assert [d.unit_price_fen for d in drafts] == [2_000, 2_000]
    # 10×20 + 2×20 = 240 元 = 24000 分（申请是 250+80 = 330 元）
    assert order_amount_fen(drafts) == 24_000


def test_direct_purchase_uses_each_lines_own_price() -> None:
    """**直采场景（没走询价）：逐行取申请单价**（`deal_unit_price_fen=None`）。"""
    lines = [
        line("i1", quantity=10, unit_price_fen=2_500),
        line("i2", quantity=2, unit_price_fen=4_000),
    ]
    drafts = build_order_lines(lines, deal_unit_price_fen=None)
    assert [d.unit_price_fen for d in drafts] == [2_500, 4_000]
    assert order_amount_fen(drafts) == 10 * 2_500 + 2 * 4_000


def test_order_lines_copy_the_source_instead_of_pointing_at_it() -> None:
    """订单明细是**复制**（快照）：数量、名称、规格都带走，与原行无关。

    订单一旦生成就是独立凭证 —— 指向申请明细的话，
    将来任何能改申请明细的流程都会把历史订单的金额一起改掉，而订单可能已经付过款。
    """
    source = line("i1", "A4 复印纸", quantity=10, unit_price_fen=2_500, spec="70g")
    drafts = build_order_lines([source], deal_unit_price_fen=None)
    draft = drafts[0]
    assert (draft.item_id, draft.name, draft.spec, draft.quantity) == (
        "i1",
        "A4 复印纸",
        "70g",
        10,
    )
    # 行金额也是算出来的（数量 × 单价），不是抄来的
    assert draft.amount_fen == 25_000


# ---------------------------------------------------------------- 拒绝面


def test_empty_lines_are_refused() -> None:
    """没有明细 → 拒绝。

    空订单不是"金额为 0"而是**没有采购内容**：放行的话，
    付款时会走到"订单金额 0 与发票金额比对"这种荒唐分支。
    """
    with pytest.raises(SourcingOrderError) as exc:
        build_order_lines([], deal_unit_price_fen=None)
    assert exc.value.code == "ORDER_LINES_EMPTY"


@pytest.mark.parametrize("quantity", [0, -1])
def test_non_positive_quantity_is_refused(quantity: int) -> None:
    with pytest.raises(SourcingOrderError) as exc:
        build_order_lines([line(quantity=quantity)], deal_unit_price_fen=None)
    assert exc.value.code == "ORDER_QUANTITY_INVALID"


def test_non_positive_unit_price_is_refused() -> None:
    """单价 0 → 拒绝：它会生成一张"金额为 0 但确实有货"的订单。"""
    with pytest.raises(SourcingOrderError) as exc:
        build_order_lines([line(unit_price_fen=0)], deal_unit_price_fen=None)
    assert exc.value.code == "ORDER_PRICE_INVALID"


# ---------------------------------------------------------------- 收货校验


def test_receipt_equal_to_ordered_is_accepted() -> None:
    """实收 = 订单数量：边界取**闭区间**（正好收齐是合法且常见的）。"""
    assert check_receipt(ordered_quantity=10, accepted_quantity=10) == []


def test_over_receipt_is_reported_with_both_numbers() -> None:
    """超收 → 问题列表里同时给出"收了多少、订单多少"（不是一句"不合法"）。"""
    problems = check_receipt(ordered_quantity=10, accepted_quantity=11, line_name="A4 纸")
    assert len(problems) == 1
    assert "11" in problems[0] and "10" in problems[0]
    assert "A4 纸" in problems[0]


def test_negative_receipt_is_refused() -> None:
    problems = check_receipt(ordered_quantity=10, accepted_quantity=-1)
    assert problems and "负数" in problems[0]


def test_zero_receipt_is_allowed_per_line() -> None:
    """**某一行一件都没收**是合法的（整单收货里"这行没到货"很常见）。

    它不合法的地方在于**整单没收齐**，那由主单状态表达，不由这一行的校验表达 ——
    把 0 也拒掉的话，用户没法登记"这一行缺货"，只能干脆不提交收货。
    """
    assert check_receipt(ordered_quantity=10, accepted_quantity=0) == []


# ---------------------------------------------------------------- 整单收货计划


def test_receipt_plan_defaults_missing_lines_to_zero() -> None:
    """没填的行按 **0** 处理（不是"跳过"）：整单收货要求每一行都有个结论。"""
    order_lines = [("l1", 10, "A4 纸"), ("l2", 2, "订书机")]
    plan = receipt_plan(order_lines=order_lines, accepted={"l1": 10})
    assert plan == [("l1", 10), ("l2", 0)]


def test_receipt_plan_reports_all_problems_at_once() -> None:
    """两行都填错时**一次说完**（界面要能一次标出所有出错的行）。"""
    order_lines = [("l1", 10, "A4 纸"), ("l2", 2, "订书机")]
    with pytest.raises(SourcingOrderError) as exc:
        receipt_plan(order_lines=order_lines, accepted={"l1": 11, "l2": 3})
    message = str(exc.value)
    assert "A4 纸" in message and "订书机" in message
    assert exc.value.code == "RECEIPT_INVALID"


def test_receipt_plan_refuses_lines_from_another_order() -> None:
    """拿**别的订单**的行 id 来收货 → 拒绝（不然会把货记到不相干的单子上）。"""
    with pytest.raises(SourcingOrderError) as exc:
        receipt_plan(order_lines=[("l1", 10, "A4 纸")], accepted={"other-line": 1})
    assert exc.value.code == "RECEIPT_LINE_UNKNOWN"


# ---------------------------------------------------------------- 收齐判定


def test_fully_received_requires_every_line() -> None:
    """**每一行都收齐**才算收到 —— 这是主单能否进 `RECEIVED` 的唯一判据。"""
    ordered = {"l1": 10, "l2": 2}
    assert is_fully_received([("l1", 10), ("l2", 2)], ordered=ordered) is True
    assert is_fully_received([("l1", 10), ("l2", 0)], ordered=ordered) is False
    assert is_fully_received([("l1", 9), ("l2", 2)], ordered=ordered) is False


def test_partial_receipt_does_not_count_as_received() -> None:
    """只收了一部分**不能**算收到。

    写成"只要收了一行就算收到"的话，仓库签收一半货单子就进入待付款，
    而付款那边"未收货不能付款"的闸门会被绕过 —— **不报错，只是账做错了**。
    """
    ordered = {"l1": 10, "l2": 2}
    assert is_fully_received([("l1", 10), ("l2", 1)], ordered=ordered) is False
    assert is_fully_received([("l1", 0), ("l2", 2)], ordered=ordered) is False


# ---------------------------------------------------------------- 整单收货的"拒绝面"


def test_receipt_problems_names_the_short_lines_with_numbers() -> None:
    """没收齐 → 说清**差哪几行、差多少**（`明细「订书机」收 1 / 订 2`）。

    这条挡的是"只回一句『未收齐』"：用户只能一行一行试，
    而每次试都是一次被拒的请求。
    """
    problems = receipt_problems(
        [("l1", 10), ("l2", 1)],
        ordered={"l1": 10, "l2": 2},
        names={"l1": "A4 纸", "l2": "订书机"},
    )
    assert problems, "没收齐必须有问题"
    joined = "；".join(problems)
    assert "整单收货" in joined, "要说清规则本身（第一版只支持整单收货）"
    assert "订书机" in joined and "1" in joined and "2" in joined
    assert "A4 纸" not in joined, "已经收齐的行不该出现在问题清单里"


def test_receipt_problems_is_empty_when_every_line_is_full() -> None:
    assert (
        receipt_problems(
            [("l1", 10), ("l2", 2)],
            ordered={"l1": 10, "l2": 2},
            names={"l1": "A4 纸", "l2": "订书机"},
        )
        == []
    )


def test_receipt_problems_refuses_an_empty_order() -> None:
    """没有明细的订单不能收货（否则会建出一张空的收货单）。"""
    problems = receipt_problems([], ordered={}, names={})
    assert problems and "没有明细" in problems[0]


def test_short_lines_ignores_full_lines() -> None:
    assert short_lines(
        [("l1", 10), ("l2", 1), ("l3", 0)],
        ordered={"l1": 10, "l2": 2, "l3": 5},
        names={"l1": "A", "l2": "B", "l3": "C"},
    ) == ["明细「B」收 1 / 订 2", "明细「C」收 0 / 订 5"]


def test_short_lines_is_empty_when_everything_is_full() -> None:
    assert (
        short_lines(
            [("l1", 10), ("l2", 2)],
            ordered={"l1": 10, "l2": 2},
            names={"l1": "A", "l2": "B"},
        )
        == []
    )
