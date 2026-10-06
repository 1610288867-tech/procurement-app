"""询价与定标规则的纯函数测试（M10 / 业务规则第 3 条）。

不连数据库、不碰磁盘，毫秒级。测的是 `domain/sourcing_policy.py`。

============================ 三条最值钱的用例 ============================
1. **比价线两侧是两条不同的规则**：金额低于比价线时，一家报价也能定标 ——
   这条挡的是"把 3 家做成无条件要求"（小额直采会被无谓地卡住）；
2. **`valid_until` 为 None ≠ 已过期**：不设有效期的报价必须能定标。
   写反了会让所有"没填有效期"的报价永远定不了，而报错还说"已过期"——指错方向；
3. **临界值取闭区间**：正好等于比价线按"超过"处理，且这条只允许有一处定义。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.sourcing_policy import (
    MIN_QUOTATIONS,
    QUOTE_EXPIRED,
    QUOTE_NOT_SELECTED,
    QUOTE_SELECTED,
    QUOTE_SUBMITTED,
    QuotationFacts,
    award_plan,
    check_awardable,
    deal_unit_price_fen,
    expired_ids,
    is_effective,
    is_expired,
    usable_supplier_ids,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

#: 比价线：1 万元。测试里当**入参**用，不去读配置 ——
#: 这样规则本身与"配置成多少"解耦（改配置不用改这批用例）。
THRESHOLD = 1_000_000


def quote(
    supplier: str,
    amount_fen: int,
    *,
    status: str = QUOTE_SUBMITTED,
    valid_until: datetime | None = None,
    usable: bool = True,
) -> QuotationFacts:
    return QuotationFacts(
        supplier_id=supplier,
        amount_fen=amount_fen,
        status=status,
        valid_until=valid_until,
        supplier_usable=usable,
    )


# ---------------------------------------------------------------- 过期判定


def test_none_valid_until_means_no_expiry_not_expired() -> None:
    """**不设有效期 = 长期有效**，不是"已过期"。

    写反的后果不是报错，而是"所有没填有效期的报价永远定不了标"，
    而且提示会说"报价已过期" —— 用户按提示去改有效期，改了还是不行。
    """
    assert is_expired(None, now=NOW) is False


def test_expiry_boundary_is_the_exact_moment() -> None:
    """正好等于当前时刻**不算过期**（`<` 而不是 `<=`）。

    与"金额 >= 比价线"一样，边界取法必须有唯一一处定义并被测到 ——
    否则同一条报价在两个人眼里会得出不同结论。
    """
    assert is_expired(NOW, now=NOW) is False
    assert is_expired(NOW - timedelta(seconds=1), now=NOW) is True
    assert is_expired(NOW + timedelta(seconds=1), now=NOW) is False


def test_naive_datetime_is_treated_as_utc() -> None:
    """库里若给出 naive 时间（老数据 / 手工插入），补 UTC 再比，而不是抛 TypeError。

    "补 UTC"是刻意选择：本项目所有 `timestamptz` 都是 UTC 语义，
    把它当本地时间会得到一个差 8 小时的结论（东八区），而且是静默的。
    """
    naive = datetime(2026, 10, 4, 11, 0)  # 无 tzinfo
    assert is_expired(naive, now=NOW) is True


# ---------------------------------------------------------------- 有效性


@pytest.mark.parametrize(
    ("status", "expired", "usable", "expected"),
    [
        (QUOTE_SUBMITTED, False, True, True),
        (QUOTE_SUBMITTED, True, True, False),  # 过期
        (QUOTE_SUBMITTED, False, False, False),  # 供应商不合格
        (QUOTE_EXPIRED, False, True, False),  # 已经是过期态
        (QUOTE_SELECTED, False, True, False),  # 已定标，不该再算家数
        (QUOTE_NOT_SELECTED, False, True, False),  # 已落选
    ],
)
def test_is_effective_covers_all_four_statuses(
    status: str, expired: bool, usable: bool, expected: bool
) -> None:
    """四个状态各自的判定，一次列全。

    `SELECTED` / `NOT_SELECTED` 必须**不算有效**：否则重复定标会把上一轮的结果
    也算进家数，"三家比价"就成了"一家报价 + 上一轮的两家"。
    """
    valid_until = NOW - timedelta(days=1) if expired else None
    assert (
        is_effective(
            status=status, valid_until=valid_until, supplier_usable=usable, now=NOW
        )
        is expected
    )


# ---------------------------------------------------------------- 定标条件


def test_below_threshold_one_quote_is_enough() -> None:
    """**比价线之下，一家也能定标。**

    这条挡的是"把 3 家做成无条件要求"：几百块的单子也逼采购专员去凑三家，
    是拿制度的形式主义换真实的采购工时。
    """
    problems = check_awardable(
        [quote("s1", 500_000)], amount_fen=500_000, threshold_fen=THRESHOLD, now=NOW
    )
    assert problems == []


def test_at_threshold_needs_three() -> None:
    """**正好等于比价线**按"超过"处理（闭区间）。

    边界只允许有一处定义：这里钉住它，制度解释与代码就不会各说各话。
    """
    two = [quote("s1", THRESHOLD), quote("s2", THRESHOLD)]
    problems = check_awardable(two, amount_fen=THRESHOLD, threshold_fen=THRESHOLD, now=NOW)
    assert len(problems) == 1
    assert "至少需要 3 家" in problems[0]
    assert "当前 2 家" in problems[0]
    assert "还差 1 家" in problems[0], "报错要说清差多少，不是一句'不满足条件'"


def test_one_fen_below_threshold_needs_only_one() -> None:
    """比价线**减 1 分**就回到"不要求家数"—— 证明判定真的用的是那条线。"""
    problems = check_awardable(
        [quote("s1", 1)],
        amount_fen=THRESHOLD - 1,
        threshold_fen=THRESHOLD,
        now=NOW,
    )
    assert problems == []


def test_three_effective_quotes_can_award() -> None:
    three = [quote(f"s{index}", 900_000) for index in range(MIN_QUOTATIONS)]
    assert (
        check_awardable(three, amount_fen=1_500_000, threshold_fen=THRESHOLD, now=NOW)
        == []
    )


def test_expired_quotes_do_not_count_toward_the_three() -> None:
    """过期的报价**不算家数**—— 这是"有效"两个字唯一的实际含义。

    三家报价里有一家过期 → 仍然不满足，提示里要体现"当前 2 家"。
    """
    quotes = [
        quote("s1", 900_000),
        quote("s2", 950_000),
        quote("s3", 980_000, valid_until=NOW - timedelta(hours=1)),
    ]
    problems = check_awardable(quotes, amount_fen=1_500_000, threshold_fen=THRESHOLD, now=NOW)
    assert len(problems) == 1
    assert "当前 2 家" in problems[0]


def test_unusable_supplier_quotes_do_not_count_either() -> None:
    """供应商停用 / 资质过期的报价同样不算 —— 业务规则第 3 条的四条硬控制之一。

    "≥3 家比价"如果允许找三家已经停用的供应商来凑数，那条控制就是纸糊的。
    """
    quotes = [
        quote("s1", 900_000),
        quote("s2", 950_000),
        quote("s3", 980_000, usable=False),
    ]
    problems = check_awardable(quotes, amount_fen=1_500_000, threshold_fen=THRESHOLD, now=NOW)
    assert "当前 2 家" in problems[0]


def test_no_quotes_at_all_is_reported_even_below_threshold() -> None:
    """一家报价都没有 → 无论金额多少都不能定标（定标要有对象）。

    金额低于比价线时"家数不够"那条不会触发，所以这条提示是**唯一**的说明 ——
    少了它，低于比价线的空报价表会得到一个"可以定标"的空问题列表。
    """
    problems = check_awardable([], amount_fen=1, threshold_fen=THRESHOLD, now=NOW)
    assert problems == ["没有任何有效报价，无法定标"]


def test_no_effective_quotes_above_threshold_reports_only_the_shortfall() -> None:
    """金额超线且一家有效都没有 → **只说一条**（差 3 家），不叠一句"没有任何有效报价"。

    两条信息说同一件事，界面上会显得像两个问题。
    """
    quotes = [quote("s1", 900_000, valid_until=NOW - timedelta(days=1))]
    problems = check_awardable(quotes, amount_fen=1_500_000, threshold_fen=THRESHOLD, now=NOW)
    assert len(problems) == 1, problems
    assert "还差 3 家" in problems[0]


def test_all_selected_quotes_are_reported_as_no_effective_quote() -> None:
    """已经定过标的申请再定一次：`SELECTED` 不算有效 → 一家有效都没有。

    这条挡的是"重复定标把上一轮结果算成家数"。
    """
    quotes = [quote("s1", 900_000, status=QUOTE_SELECTED)]
    problems = check_awardable(quotes, amount_fen=1_500_000, threshold_fen=THRESHOLD, now=NOW)
    assert len(problems) == 1
    assert "还差 3 家" in problems[0]


# ---------------------------------------------------------------- 惰性结算


def test_expired_ids_only_marks_still_submitted_ones() -> None:
    """只把**还写着 SUBMITTED** 的过期报价标成 EXPIRED。

    `SELECTED` / `NOT_SELECTED` 是"定标那一刻的历史结论"：让它们随时间变成
    "过期"，事后看会像是那家自己失效了 —— 而它其实是被选中的那家。
    """
    quotes = [
        quote("s1", 1, valid_until=NOW - timedelta(days=1)),  # 该标
        quote("s2", 1, valid_until=NOW + timedelta(days=1)),  # 未过期
        quote("s3", 1, valid_until=None),  # 不设有效期
        quote("s4", 1, status=QUOTE_SELECTED, valid_until=NOW - timedelta(days=1)),
        quote("s5", 1, status=QUOTE_NOT_SELECTED, valid_until=NOW - timedelta(days=1)),
    ]
    assert expired_ids(quotes, now=NOW) == [0]


# ---------------------------------------------------------------- 定标结果


def test_award_plan_selects_one_and_marks_the_rest_not_selected() -> None:
    """定标后：选中者 `SELECTED`，**其余一律 `NOT_SELECTED`**（含过期那家）。

    过期那家也要落 `NOT_SELECTED` 而不是留着 `SUBMITTED`：
    这一轮已经结束了，其余报价不该还能被算进下一轮的家数。
    """
    quotes = [
        quote("s1", 900_000),
        quote("s2", 880_000),
        quote("s3", 970_000, valid_until=NOW - timedelta(hours=1)),
    ]
    plan = award_plan(quotes, selected_supplier_id="s2")
    assert plan == [(0, QUOTE_NOT_SELECTED), (1, QUOTE_SELECTED), (2, QUOTE_NOT_SELECTED)]


def test_deal_unit_price_comes_from_the_selected_quotation() -> None:
    """成交单价取**选中报价的原值**，不允许在定标这一步顺手改价（改价要走重新报价）。

    这条口径牵连很远：订单金额 = Σ(订单明细) = Σ(单价×数量)，
    而三单匹配的"发票 vs 订单"也以订单金额为准 —— 单价写错一次，
    发票比对上会**互相抵消**（两边都错就都不报错）。
    """
    quotes = [quote("s1", 900_000), quote("s2", 880_000)]
    assert deal_unit_price_fen(quotes, supplier_id="s2") == 880_000
    assert deal_unit_price_fen(quotes, supplier_id="unknown") is None


def test_usable_supplier_ids_filters_the_unusable() -> None:
    quotes = [quote("s1", 1), quote("s2", 1, usable=False)]
    assert usable_supplier_ids(quotes) == {"s1"}
