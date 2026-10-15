"""成本核算的纯函数测试（M11 增强）。

这一层要钉住三件事：
**① 算得对（手算例子）；② 拿不到用量时返回 `None` 而不是估算；**
**③ "不适用"与"确实是 0"必须能区分** —— 这是本项目第三次出现同一条纪律
（前两次：`support_rate` 的 `None` vs `0.0`、检索指标的"不适用"剔除）。
"""

from __future__ import annotations

import pytest

from app.domain.cost import (
    MICROS_PER_YUAN,
    ModelPrice,
    estimate_cost_micros,
    format_cost,
    total_cost_micros,
)

pytestmark = pytest.mark.unit


#: 手算基准：输入 2 元/百万、输出 8 元/百万（贴近常见国产模型量级）
PRICE = ModelPrice.from_yuan_per_million(input_yuan=2.0, output_yuan=8.0)


def test_price_is_exact_integers() -> None:
    """单价构造后**没有浮点**：2 元 → 2_000_000 微元。"""
    assert PRICE.input_micros_per_million == 2 * MICROS_PER_YUAN
    assert PRICE.output_micros_per_million == 8 * MICROS_PER_YUAN


def test_cost_matches_hand_calculation() -> None:
    """手算：输入 1000 token × 2 元/百万 = 0.002 元；输出 500 × 8 元/百万 = 0.004 元。

    合计 0.006 元 = 6000 微元。
    """
    cost = estimate_cost_micros(
        prompt_tokens=1000, completion_tokens=500, price=PRICE
    )
    assert cost == 6000
    assert cost == 0.006 * MICROS_PER_YUAN


def test_cost_is_none_when_usage_is_missing() -> None:
    """**拿不到用量就返回 `None`，不许用字符数估算。**

    估算值一旦混进真实用量，"这个月花了多少"就成了一笔算不清的账 ——
    而成本核算的意义恰恰在于能对账。
    """
    assert estimate_cost_micros(prompt_tokens=None, completion_tokens=500, price=PRICE) is None
    assert estimate_cost_micros(prompt_tokens=1000, completion_tokens=None, price=PRICE) is None


def test_cost_is_none_when_price_is_unknown() -> None:
    """模型不在价格表里 → 单价未知 → `None`。

    ⚠️ 这与"本地模型"是**两回事**：本地模型费用确实是 0，
    调用方应当给它 `ModelPrice(0, 0)`，而不是让它落到这条分支上。
    """
    assert estimate_cost_micros(prompt_tokens=1000, completion_tokens=500, price=None) is None


def test_local_model_is_zero_not_unknown() -> None:
    """**"不花钱"与"不知道花多少"必须在数据里区分开。**

    本地模型（自己跑在 GPU 上）费用确实是 0 —— 给它一个 0 价目，
    于是 `estimate_cost_micros` 返回 **0**（不是 `None`）。
    """
    free = ModelPrice(input_micros_per_million=0, output_micros_per_million=0)
    cost = estimate_cost_micros(prompt_tokens=1000, completion_tokens=500, price=free)
    assert cost == 0
    assert cost is not None


def test_negative_usage_is_rejected_as_untrusted() -> None:
    """上游给了负数 → 它的 usage 字段不可信 → 宁可不记，也不要记一个负数。"""
    assert estimate_cost_micros(prompt_tokens=-1, completion_tokens=500, price=PRICE) is None


def test_cost_uses_integer_division_by_design() -> None:
    """除以一百万用**整数除法**：宁可少算不到 1 微元，也不引入浮点。"""
    # 1 token × 1 微元/百万 = 1 微元 / 1_000_000 → 向下取整为 0
    tiny = ModelPrice(input_micros_per_million=1, output_micros_per_million=0)
    assert estimate_cost_micros(prompt_tokens=1, completion_tokens=0, price=tiny) == 0


# ================================================================ 展示与汇总


def test_format_distinguishes_unknown_from_free() -> None:
    """`未知` 与 `¥0.000000` 必须长得不一样。

    报告里把两者打成一个样子，读者会把"没量到"当成"免费"。
    """
    assert format_cost(None) == "未知"
    assert format_cost(0) == "¥0.000000"
    assert format_cost(6000) == "¥0.006000"


def test_total_reports_coverage_alongside_the_sum() -> None:
    """汇总**必须同时报"未知条数"**。

    只给一个总额会让人以为它是全部费用，而实际可能有一半调用没算进去 ——
    **一个不说明覆盖率的成本数字，比没有数字更危险。**
    """
    known, unknown = total_cost_micros([1000, None, 2000, None, 500])
    assert known == 3500
    assert unknown == 2


def test_total_of_all_unknown_is_zero_but_coverage_is_clear() -> None:
    known, unknown = total_cost_micros([None, None])
    assert known == 0
    assert unknown == 2  # 读的人一眼就知道"这 0 元不代表免费"
