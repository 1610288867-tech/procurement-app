"""引用支撑性校验的纯函数测试（M11 增强）。

对应的是 RAG 评测里的 **Faithfulness（忠实度）**：
"回答里的事实，能不能从被引用的上下文里得到支持"。

============================ 这个文件要钉住的两件事 ============================
**第一件：归一是必须的，否则会满屏误报。**
"十万元" / "10 万元" / "100000 元" / "10万" 是**同一个事实**，
但写法完全不同。不做归一化的话，模型把"10 万元"说成"100000 元"
会被判成"数字不支撑" —— 那是**校验自己的 bug**，而误报多的校验没人看。

**第二件：`None` 与 `0.0` 必须分开。**
`support_rate` 返回 `None` 表示"这次回答不适用这个指标"（没有引用句），
返回 `0.0` 表示"有引用的句子全都不可信"。
把前者当成 0 分会**系统性压低**评测报告的均值 —— 这是评测里最常见的口径错误。
"""

from __future__ import annotations

import pytest

from app.domain.citation_support import (
    KIND_DISTANT,
    KIND_NEGATION,
    KIND_NUMBER,
    check_citation_support,
    extract_facts,
    support_rate,
)
from app.domain.grounded_answer import NO_GROUNDING_ANSWER, Source

pytestmark = pytest.mark.unit


def source(
    number: int = 1,
    content: str = "采购金额超过 10 万元的，应当公开招标。",
) -> Source:
    return Source(
        number=number,
        doc_id=f"doc-{number}",
        doc_title="采购管理办法",
        doc_version=1,
        chunk_no=number,
        content=content,
        score=0.8,
    )


# ================================================================ 事实抽取与归一


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("超过 10 万元", ("100000", "元")),
        ("超过十万元", ("100000", "元")),
        ("超过 100000 元", ("100000", "元")),
        # ⚠️ "10万"（省略"元"）也必须是同一个事实 ——
        # 早先版本它得到 `("100000", None)`，于是同一个金额在回答与来源之间
        # 被算成"不支撑"，**那是校验自己误报**（见 `_normalize` 的说明）。
        ("超过 10万", ("100000", "元")),
        ("超过 100,000 元", ("100000", "元")),
        ("3 个工作日", ("3", "工作日")),
        ("三个工作日", ("3", "工作日")),
        ("3 日", ("3", "天")),
        ("30%", ("30", "%")),
        ("30％", ("30", "%")),
    ],
)
def test_numbers_are_normalized_to_the_same_fact(text: str, expected: tuple[str, str]) -> None:
    """**同一个事实的不同写法必须归一到同一个键**（否则会满屏误报）。

    这条覆盖了中文数字、千分位、单位别名（万元/万、日/天、％/%）。
    """
    assert expected in extract_facts(text), extract_facts(text)


def test_different_units_are_not_conflated() -> None:
    """**"10 万" 与 "10 天" 不是同一个事实**（不替它下结论）。

    金额单位做换算（万元→元），但**时间单位只做别名统一**（日→天），不互相换算 ——
    "3 天"与"3 个工作日"在法律文本里可能不是同一件事。
    """
    facts = extract_facts("3 天")
    assert ("3", "天") in facts
    assert ("3", "工作日") not in facts


def test_cents_are_converted_to_yuan() -> None:
    """金额"分"要折成"元"，与制度文档里的"元"可比。"""
    assert ("100", "元") in extract_facts("10000 分")


# ================================================================ 规则一：数字不支撑


def test_unsupported_number_is_reported() -> None:
    """**这条是本次增强的核心**：编号真、内容假，必须报出来。

    来源说"10 万元"，回答说"50 万元" —— 编号 `[1]` 是真的，
    `check_citation_integrity` 会放行它，而本模块要拦住。
    """
    answer = "根据[1]，采购金额超过 50 万元的应当公开招标。"
    problems = check_citation_support(answer, [source(1)])
    assert problems, "编号真但数字假的回答必须被报出来"
    assert problems[0].kind == KIND_NUMBER
    assert "500000" in problems[0].detail


def test_supported_number_passes() -> None:
    """数字能在来源里找到 → 不报问题。"""
    answer = "根据[1]，采购金额超过 10 万元的应当公开招标。"
    assert check_citation_support(answer, [source(1)]) == []


def test_language_variation_of_the_same_number_passes() -> None:
    """**模型把"10 万元"说成"100000 元"不算编造** —— 这是归一化存在的全部理由。"""
    answer = "根据[1]，采购金额超过 100000 元的应当公开招标。"
    assert check_citation_support(answer, [source(1)]) == []


def test_number_from_another_source_is_unsupported() -> None:
    """回答引用了 `[1]`，但数字只出现在 `[2]` 里 → **仍算不支撑**。

    支撑关系是"这一句"与"它引用的那几条"之间的关系。
    数字在别的片段里有，不等于这句话有依据。
    """
    sources = [
        source(1, "采购金额超过 10 万元的，应当公开招标。"),
        source(2, "验收应当在 3 个工作日内完成。"),
    ]
    answer = "根据[1]，验收应当在 3 个工作日内完成。"
    problems = check_citation_support(answer, sources)
    assert problems and problems[0].kind == KIND_NUMBER


def test_sentence_without_citation_is_not_checked_here() -> None:
    """**没有引用的句子不在这里扣分** —— 那由 `check_citation_integrity` 负责。

    两个模块各管一段，重复计分会让评测指标互相污染。
    """
    answer = "采购金额超过 50 万元的应当公开招标。"
    assert check_citation_support(answer, [source(1)]) == []


# ================================================================ 规则二：否定翻转


def test_negation_flip_is_reported() -> None:
    """"**不得**"被说成"**可以**"是制度问答里最危险的错误，单独一类报出来。"""
    policy = source(1, "没有预算的采购申请，一律不得提交。")
    answer = "根据[1]，没有预算的采购申请可以先提交，事后再补预算。"
    problems = check_citation_support(answer, [policy])
    assert any(problem.kind == KIND_NEGATION for problem in problems), problems


def test_negation_preserved_passes() -> None:
    """否定没被翻转 → 不报。"""
    policy = source(1, "没有预算的采购申请，一律不得提交。")
    answer = "根据[1]，没有预算的采购申请不得提交。"
    assert not [
        p for p in check_citation_support(answer, [policy]) if p.kind == KIND_NEGATION
    ]


# ================================================================ 规则三：引用错位


def test_answer_that_drifts_away_from_its_citations_is_reported() -> None:
    """**引用集中在开头、后面整段自由发挥** —— 后半部分要报"错位"。

    ============================ 这条规则的阈值是 3 句，说明为什么 ============================
    合法的制度问答常有这样的形态（本地兜底回答就是）：
        「根据检索到的制度原文（以下为原文摘录）：」
        「[1]《采购管理办法》（第 1 版）」
        「采购金额超过 10 万元的应当公开招标。」
    第三句**不带编号但属于上一个引用块** —— 判它错位就是误报。
    所以规则允许"离最近的引用最多 3 句"。

    而真正要抓的是这里的形态：开头引了一次，之后连着说了四句没有任何出处的断言。
    """
    answer = (
        "根据[1]，采购金额超过 10 万元的应当公开招标。"
        "除此之外还有若干配套要求需要一并遵守。"
        "供应商必须在合格清单之内并保持资质有效。"
        "验收应当由使用部门与仓储部门共同完成。"
        "付款前还必须完成订单、验收单与发票的三单核对。"
    )
    problems = check_citation_support(answer, [source(1)])
    assert any(problem.kind == KIND_DISTANT for problem in problems), problems


def test_excerpt_after_a_citation_header_is_not_flagged() -> None:
    """**摘录句紧跟引用块头 → 不报错位**（这是本地兜底回答的真实形态）。

    没有这条豁免的话，本地兜底回答（原文摘录）会被整段判成"无依据"——
    而它恰恰是**最忠实**的一种回答。**误报的校验没人看**，所以这条必须有测试钉住。
    """
    answer = (
        "根据检索到的制度原文（未调用模型，以下为原文摘录）：\n"
        "[1]《采购管理办法》（第 1 版）\n"
        "采购金额超过 10 万元的应当公开招标。"
    )
    problems = check_citation_support(answer, [source(1)])
    assert [p for p in problems if p.kind == KIND_DISTANT] == [], problems


# ================================================================ support_rate 的口径


def test_support_rate_is_none_when_not_applicable() -> None:
    """**`None`（不适用）绝不能与 `0.0`（全都不可信）混为一谈。**

    没有引用句时返回 `None` —— 否则评测均值会被"不适用"当成 0 分系统性压低。
    """
    assert support_rate("", [source(1)]) is None
    assert support_rate("根据[1]，超过 10 万元应当招标。", []) is None
    assert support_rate("这句话没有引用。", [source(1)]) is None


def test_support_rate_is_one_when_everything_supported() -> None:
    answer = "根据[1]，采购金额超过 10 万元的应当公开招标。"
    assert support_rate(answer, [source(1)]) == 1.0


def test_support_rate_counts_sentences_not_problems() -> None:
    """**同一句报了两个问题只算这一句不通过**（按句统计，不按问题条数）。

    这句同时命中"数字不支撑"与"否定翻转"，但它只是**一句**；
    而另一句是完全支撑的 —— 所以通过率应当是 1/2，不是 1/3。
    """
    policy = source(1, "没有预算的采购申请一律不得提交，金额超过 10 万元的应当公开招标。")
    answer = (
        "根据[1]，没有预算的申请可以先提交，而且金额超过 50 万元的应当公开招标。"  # 两个问题
        "根据[1]，金额超过 10 万元的应当公开招标。"  # 这句没问题
    )
    rate = support_rate(answer, [policy])
    assert rate == 0.5, f"应当按句统计（1/2），实际 {rate}"


def test_refusal_answer_has_no_support_problems() -> None:
    """**"未找到相关规定"是策略内的合法输出**，不该被支撑性校验报问题。

    （它连来源都没有，而本模块在没有来源时直接返回空列表。）
    """
    assert check_citation_support(NO_GROUNDING_ANSWER, []) == []
    assert support_rate(NO_GROUNDING_ANSWER, []) is None
