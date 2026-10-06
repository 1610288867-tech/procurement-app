"""三单匹配的纯函数测试（M10 / 8.3）。

不连数据库、不碰 HTTP，毫秒级。测的是 `domain/settlement_policy.py`。

============================ 五条最值钱的用例 ============================
1. **金额基准是订单金额而不是申请金额**（拿申请金额比会一律判成不匹配）；
2. **容差取"比例与绝对值的较大者"**（小额单只按 2% 永远匹配不上）；
3. **容差边界用整数万分比算，没有浮点误差**（`0.02 × 2500` 在浮点下是
   `50.000000000000007`，一个边界用例就能翻车）；
4. **多张发票的供应商一致性要"全部一致"**（写成"看第一张"会放走"三张里有一张是别家"）；
5. **问题列表带 `expected` / `actual`**（前端靠它渲染差异高亮，光有文案不够）。
"""

from __future__ import annotations

import pytest

from app.domain.settlement_policy import (
    INVOICE_ACTIVE,
    INVOICE_VOIDED,
    ISSUE_AMOUNT_MISMATCH,
    ISSUE_NO_INVOICE,
    ISSUE_NO_ORDER,
    ISSUE_OVER_RECEIPT,
    ISSUE_SUPPLIER_MISMATCH,
    TOLERANCE_ABS_FEN,
    TOLERANCE_BP,
    InvoiceError,
    can_void_invoice,
    document_supplier_consistent,
    match_three_way,
    precheck_three_way,
    tolerance_fen,
    voidable,
)

pytestmark = pytest.mark.unit

ORDER_SUPPLIER = "sup-甲"


def match(**overrides: object) -> list:
    """默认是一笔"三单全对得上"的单子（12000 元），用例只改要考的那一项。"""
    params: dict = {
        "ordered_qty": 100,
        "ordered_amount_fen": 1_200_000,
        "order_supplier_id": ORDER_SUPPLIER,
        "accepted_qty": 100,
        "invoice_amount_fen": 1_200_000,
        "invoice_supplier_id": ORDER_SUPPLIER,
    }
    params.update(overrides)
    return match_three_way(**params)


# ---------------------------------------------------------------- 通过面


def test_a_matching_triple_passes_with_no_issues() -> None:
    """三单全对得上 → **空列表**（能不能付款就看它是不是空的）。"""
    assert match() == []


def test_exact_tolerance_boundary_passes() -> None:
    """差异**正好等于**容差 → 通过（闭区间）。

    订单 100 万元 → 2% = 20000 分；差值正好 20000 分算通过。
    边界取闭区间是刻意的：容差是"允许差多少"，不是"必须小于多少"。
    """
    assert tolerance_fen(1_000_000) == 20_000
    assert match(ordered_amount_fen=1_000_000, invoice_amount_fen=1_020_000) == []


def test_over_tolerance_by_one_fen_is_reported() -> None:
    """超出容差**哪怕 1 分** → 报出来。

    这是"容差是硬线"的证据：不因为差得少就放过。
    """
    issues = match(ordered_amount_fen=1_000_000, invoice_amount_fen=1_020_001)
    assert [issue.code for issue in issues] == [ISSUE_AMOUNT_MISMATCH]
    assert issues[0].expected == 1_000_000
    assert issues[0].actual == 1_020_001


# ---------------------------------------------------------------- 容差算法


def test_small_amount_uses_the_absolute_floor() -> None:
    """小额单靠**绝对值下限** 50 元兜住（比例只有 4 元时用小值会永远匹配不上）。

    订单 200 元 → 2% = 4 元，取下限 50 元 → 差异 30 元仍然通过。
    这条正是"比例和绝对值取较大者"的原因。
    """
    assert tolerance_fen(20_000) == TOLERANCE_ABS_FEN == 5_000
    assert match(ordered_amount_fen=20_000, invoice_amount_fen=23_000) == []


def test_tolerance_arithmetic_is_integer_and_has_no_float_error() -> None:
    """**整数万分比的关键用例**：`2500 × 2%` 必须是**正好** 50 元。

    浮点写法 `int(2500 * 0.02)` 里 `0.02 * 2500 = 50.000000000000007`，
    恰好落在边界上 —— 它今天"看起来对"，但换个金额就会差 1 分，
    而"差 1 分算不算超容差"是没法向用户解释的。所以全程整数。
    """
    assert tolerance_fen(2_500) == TOLERANCE_ABS_FEN  # 比例 50 分 < 下限 5000 分
    # 比例恰好等于下限的那一刻：2% 的分界在 25 万分（2500 元）
    assert tolerance_fen(250_000) == 5_000, "25 万分的 2% 正好是 5000 分，不该有浮点尾巴"
    assert TOLERANCE_BP == 200, "万分比常量本身也是断言的一部分：改成 2% 就是 200"


def test_tolerance_floors_instead_of_rounding_up() -> None:
    """比例部分**向下取整**（容差只可能偏小、不可能偏大）。

    订单 12345 分 → 2% = 246.9 分 → 取 246（不是 247）。
    兜底方向选严的一侧：宁可多拦一次，也不要放过一次。
    """
    assert tolerance_fen(12_345) == TOLERANCE_ABS_FEN, "它比 5000 分小，所以被下限接管"
    # 用一个"比例大于下限"的金额来看取整：100 万分 → 正好 20000；再加 49 分
    assert tolerance_fen(1_000_049) == 20_000  # 20000.98 → 向下取整 20000


# ---------------------------------------------------------------- 三条规则


def test_over_receipt_is_reported_with_both_numbers() -> None:
    issues = match(ordered_qty=100, accepted_qty=101)
    assert [issue.code for issue in issues] == [ISSUE_OVER_RECEIPT]
    assert issues[0].expected == 100
    assert issues[0].actual == 101


def test_receipt_equal_to_order_passes() -> None:
    """收货 = 订单数量是**允许**的（闭区间）。"""
    assert match(ordered_qty=100, accepted_qty=100) == []


def test_under_receipt_is_not_a_mismatch() -> None:
    """**少收不算不匹配。**

    收货没齐的单子走不到"已收货"（8.2 是整单收货），所以这里正常不会出现；
    但规则本身只拦"超收" —— 少收在业务上是"还没收完"，不是"对不上账"。
    把少收也判成不匹配的话，正常链路会被自己的规则挡住。
    """
    assert match(ordered_qty=100, accepted_qty=60) == []


def test_supplier_mismatch_is_reported_without_numbers() -> None:
    """供应商不一致 → 报出来，且**不给 expected/actual**（没什么可比数字）。"""
    issues = match(invoice_supplier_id="sup-乙")
    assert [issue.code for issue in issues] == [ISSUE_SUPPLIER_MISMATCH]
    assert issues[0].expected is None and issues[0].actual is None


def test_three_problems_are_all_reported_at_once() -> None:
    """三条都不满足 → **一次列出三条**，不是只报第一条。

    前端要一次把三列都高亮出来；只报第一条会逼用户改一次、提交一次、再被拒一次。
    """
    issues = match(
        ordered_qty=100,
        accepted_qty=120,
        ordered_amount_fen=1_000_000,
        invoice_amount_fen=2_000_000,
        invoice_supplier_id="sup-乙",
    )
    assert {issue.code for issue in issues} == {
        ISSUE_OVER_RECEIPT,
        ISSUE_AMOUNT_MISMATCH,
        ISSUE_SUPPLIER_MISMATCH,
    }


def test_amount_basis_is_the_order_not_the_request() -> None:
    """**金额基准是订单金额**：订单通常小于申请（询价压价），拿申请金额比会一律不匹配。

    这条用例把它写成"订单 12000 元 / 发票 12000 元 → 通过"，
    而同一个申请当初报的是 16500 元 —— 如果实现里误用申请金额，这条就红了。
    """
    assert match(ordered_amount_fen=1_200_000, invoice_amount_fen=1_200_000) == []


def test_both_directions_of_amount_difference_are_checked() -> None:
    """发票**少于**订单金额超容差也要报（不是只拦"开多了"）。"""
    issues = match(ordered_amount_fen=1_000_000, invoice_amount_fen=900_000)
    assert [issue.code for issue in issues] == [ISSUE_AMOUNT_MISMATCH]


# ---------------------------------------------------------------- 多张发票的供应商一致性


def test_all_invoices_must_match_the_order_supplier() -> None:
    """**三张发票里只有第二张是别家 → 判不一致。**

    写成 `supplier_ids[0] == order_supplier_id`（看第一张）就会放它过去 ——
    而那一张恰恰是要拦下来的。领域设计 1372 行专门点了这条。
    """
    assert document_supplier_consistent(
        order_supplier_id=ORDER_SUPPLIER,
        supplier_ids=[ORDER_SUPPLIER, "sup-乙", ORDER_SUPPLIER],
    ) is False
    assert document_supplier_consistent(
        order_supplier_id=ORDER_SUPPLIER,
        supplier_ids=[ORDER_SUPPLIER, ORDER_SUPPLIER],
    ) is True


def test_no_invoice_is_never_consistent() -> None:
    """没有发票 → **False**（谈不上"供应商一致"）。"""
    assert document_supplier_consistent(order_supplier_id=ORDER_SUPPLIER, supplier_ids=[]) is False


def test_precheck_reports_supplier_mismatch_when_one_of_many_differs() -> None:
    """预检入口上也验一次：一张不一致 → `SUPPLIER_MISMATCH`。"""
    issues = precheck_three_way(
        ordered_qty=100,
        ordered_amount_fen=1_200_000,
        order_supplier_id=ORDER_SUPPLIER,
        accepted_qty=100,
        invoice_amount_fen=1_200_000,
        invoice_count=3,
        all_suppliers_match=False,
    )
    assert [issue.code for issue in issues] == [ISSUE_SUPPLIER_MISMATCH]


# ---------------------------------------------------------------- 预检的结构性判断


def test_precheck_without_an_order_says_so() -> None:
    """还没下单 → `NO_ORDER`（而不是"金额差超容差"那种指错方向的结论）。"""
    issues = precheck_three_way(
        ordered_qty=None,
        ordered_amount_fen=None,
        order_supplier_id=None,
        accepted_qty=None,
        invoice_amount_fen=0,
        invoice_count=0,
        all_suppliers_match=False,
    )
    assert [issue.code for issue in issues] == [ISSUE_NO_ORDER]


def test_precheck_without_invoices_says_so() -> None:
    issues = precheck_three_way(
        ordered_qty=100,
        ordered_amount_fen=1_200_000,
        order_supplier_id=ORDER_SUPPLIER,
        accepted_qty=100,
        invoice_amount_fen=0,
        invoice_count=0,
        all_suppliers_match=False,
    )
    assert [issue.code for issue in issues] == [ISSUE_NO_INVOICE]


def test_precheck_passes_on_a_complete_matching_triple() -> None:
    assert (
        precheck_three_way(
            ordered_qty=100,
            ordered_amount_fen=1_200_000,
            order_supplier_id=ORDER_SUPPLIER,
            accepted_qty=100,
            invoice_amount_fen=1_200_000,
            invoice_count=2,
            all_suppliers_match=True,
        )
        == []
    )


# ---------------------------------------------------------------- 发票作废


def test_can_void_an_active_invoice_before_payment() -> None:
    """正常路径：有效发票 + 还没付款 → 可以作废（不抛异常就是可以）。"""
    assert can_void_invoice(status=INVOICE_ACTIVE, request_status="INVOICED") is None
    # 收货之后、还没登记发票时不会有发票，所以只测"能作废"的那几个状态
    assert can_void_invoice(status=INVOICE_ACTIVE, request_status="RECEIVED") is None


def test_voiding_twice_is_refused() -> None:
    """重复作废 → 拒绝。

    不是"宽容地当成成功"：作废只写一条时间线事件，第二次说"成功"会让用户
    以为做了两次（而账只变了一次），这类"看起来做了但其实没做"最难查。
    """
    with pytest.raises(InvoiceError) as exc:
        can_void_invoice(status=INVOICE_VOIDED, request_status="INVOICED")
    assert exc.value.code == "INVOICE_ALREADY_VOIDED"


@pytest.mark.parametrize("request_status", ["PAID", "CLOSED"])
def test_voiding_after_payment_is_refused(request_status: str) -> None:
    """**付过款之后不许作废**：钱已经按这张票付过了。

    作废会让"已付金额 vs 有效发票合计"在账上对不上，而系统没有退款流程 ——
    所以宁可让人工处理并留书面记录（这条是少见的"宁可人工"分支）。
    """
    with pytest.raises(InvoiceError) as exc:
        can_void_invoice(status=INVOICE_ACTIVE, request_status=request_status)
    assert exc.value.code == "INVOICE_VOID_AFTER_PAYMENT"


def test_voidable_requires_an_active_invoice_not_just_a_row() -> None:
    """**"还有没有发票"必须看状态，不能看行数。**

    写成 `len(invoices) > 0` 的话，"登记过 3 张、全作废了"仍然算有发票 ——
    于是匹配会拿着 0 元去和订单金额比，报出"金额差超容差"，
    而真正的原因是**一张有效发票都没有**（该报 `NO_INVOICE`）。
    """
    assert voidable([INVOICE_ACTIVE]) is True
    assert voidable([INVOICE_ACTIVE, INVOICE_VOIDED]) is True
    assert voidable([INVOICE_VOIDED, INVOICE_VOIDED]) is False
    assert voidable([]) is False


def test_precheck_reports_no_invoice_when_all_are_voided() -> None:
    """全部作废 → `NO_INVOICE`（**不是** `AMOUNT_MISMATCH`）。

    这条与 `test_precheck_without_invoices_says_so` 配对：两者的业务含义不同
    （一个"还没登记"，一个"登记了但都作废了"），但对匹配的意义相同 ——
    没有可对账的金额。所以报同一个码，但人话里把两种可能都说出来。
    """
    issues = precheck_three_way(
        ordered_qty=100,
        ordered_amount_fen=1_200_000,
        order_supplier_id=ORDER_SUPPLIER,
        accepted_qty=100,
        invoice_amount_fen=0,
        invoice_count=0,  # 有效发票张数为 0（全部作废）
        all_suppliers_match=False,
    )
    assert [issue.code for issue in issues] == [ISSUE_NO_INVOICE]
    assert "作废" in issues[0].message, "人话里要提到「可能是作废了」"

