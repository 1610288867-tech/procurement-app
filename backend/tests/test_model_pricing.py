"""模型价格表解析的纯函数测试（M11 增强）。

这一层要钉住两件事：
**① 错误配置必须"报出来"而不是静默丢弃** —— 一条写错的价格会让该模型的调用
全部记成"成本未知"，而没人知道为什么；
**② 模型名里可以带冒号**（`qwen2.5:7b`）—— 所以分隔符必须是 `=`。
"""

from __future__ import annotations

import pytest

from app.domain.cost import ModelPrice
from app.domain.model_pricing import parse_model_prices

pytestmark = pytest.mark.unit


def test_parses_a_single_local_model() -> None:
    """本地模型显式配 `0,0` —— **这是"确实不花钱"，不是"没配"**。"""
    prices, problems = parse_model_prices("qwen2.5:7b=0,0")
    assert problems == []
    assert prices["qwen2.5:7b"] == ModelPrice(0, 0)


def test_model_name_may_contain_a_colon() -> None:
    """**`qwen2.5:7b` 里的冒号不能被当成分隔符。**

    这正是为什么用 `=` 而不是 `:`：用冒号的话这个最常见的模型名根本解析不了，
    而报错信息还会指向一个看起来"格式对"的配置。
    """
    prices, problems = parse_model_prices("qwen2.5:7b=0.5,1.5")
    assert problems == []
    assert "qwen2.5:7b" in prices


def test_parses_multiple_models() -> None:
    prices, problems = parse_model_prices("qwen2.5:7b=0,0;gpt-4o-mini=0.15,0.6")
    assert problems == []
    assert set(prices) == {"qwen2.5:7b", "gpt-4o-mini"}
    # 0.15 元/百万 token → 150000 微元/百万
    assert prices["gpt-4o-mini"].input_micros_per_million == 150_000


def test_newlines_work_as_separators() -> None:
    """换行也是分隔符 —— `.env` 里多行写价格是常见写法。"""
    prices, problems = parse_model_prices("a=1,2\nb=3,4")
    assert problems == []
    assert set(prices) == {"a", "b"}


def test_empty_config_is_not_an_error() -> None:
    """没配价格不是错误（默认空字符串）—— 后果只是成本记"未知"。"""
    prices, problems = parse_model_prices("")
    assert prices == {}
    assert problems == []


# ================================================================ 坏配置必须报出来


@pytest.mark.parametrize(
    ("raw", "hint"),
    [
        ("bad", "缺少"),          # 没有 `=`
        ("x=abc,1", "不是数字"),   # 价格不是数字
        ("y=1", "格式不对"),       # 只给了一个价格
        ("z=-1,2", "不能为负"),    # 负数
    ],
)
def test_bad_entries_are_reported_not_dropped(raw: str, hint: str) -> None:
    """**坏配置必须报出来。**

    静默跳过一条写错的价格，后果是"该模型的调用全部记成成本未知"，
    而排查的人只能看到一堆 `NULL`、看不出为什么 —— 这正是要避免的。
    """
    prices, problems = parse_model_prices(raw)
    assert prices == {}
    assert len(problems) == 1
    assert hint in problems[0]


def test_one_bad_entry_does_not_kill_the_whole_table() -> None:
    """一条坏配置不该毁掉整张表：好的照常解析，坏的报出来。"""
    prices, problems = parse_model_prices("good=1,2;alsobad;better=3,4")
    assert set(prices) == {"good", "better"}
    assert len(problems) == 1
    assert "alsobad" in problems[0]
