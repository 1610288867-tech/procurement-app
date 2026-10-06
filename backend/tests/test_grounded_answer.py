"""溯源策略的纯函数测试（M11）。不连数据库、不调模型，毫秒级。

对应验收项：

    问文档里没有的问题 → 明确回答"制度中没有找到相关规定"，不编造
    检索为空时，日志里能看到"未调用模型"
    构造一段引用了不存在编号的回答 → `check_citation_integrity` 能报出问题

============================ 最值钱的两组用例 ============================
**"有结果但全低于阈值"必须等同于"没有依据"。**
检索永远返回 top-k（哪怕全是无关内容），只判"结果为空"的话，
问一个制度里根本没有的问题会把最不相关的片段递上去 —— 模型于是拿着
不相干的片段硬答，这正是"一本正经地编造"最常见的触发路径。

**"未找到相关规定"这句话必须能通过引用校验。**
它既没有引用、也不引用来源，按规则 2/3 会被判成问题 —— 但那正是正确答案。
拒答是策略内的合法输出，不是"无出处的断言"。这条如果写错，
每一次正确的拒答都会被记成"校验失败"，校验本身也就没人看了。
"""

from __future__ import annotations

import pytest

from app.domain.grounded_answer import (
    CITATION_WARNING,
    NO_GROUNDING_ANSWER,
    SYSTEM_PROMPT,
    Source,
    build_context,
    build_user_prompt,
    check_citation_integrity,
    decide_grounding,
)

pytestmark = pytest.mark.unit


def source(number: int = 1, score: float = 0.8, title: str = "采购管理办法") -> Source:
    return Source(
        number=number,
        doc_id=f"doc-{number}",
        doc_title=title,
        doc_version=1,
        chunk_no=number,
        content="采购金额超过 10 万元的，应当报总经理审批。",
        score=score,
    )


# ---------------------------------------------------------------- 规则一：没有依据不调模型


def test_no_sources_means_no_model_call() -> None:
    """检索为空 → 不调模型，直接回答"未找到"。

    这是"不编造"的第一道闸门：**没有材料就不问模型**，
    而不是"问了之后祈祷它说没找到"。
    """
    decision = decide_grounding([])
    assert decision.use_model is False
    assert decision.fallback_answer == NO_GROUNDING_ANSWER
    assert "未调用模型" in decision.reason, "日志里要能看出「没调模型」（验收项）"


def test_sources_below_the_threshold_also_mean_no_model_call() -> None:
    """**有结果但全部低于阈值 = 没有依据**（这条是最容易被漏掉的）。

    检索永远返回 top-k，哪怕全是无关内容。只判"结果为空"的话，
    问"年假多少天"会把采购流程的片段递上去，模型拿着不相干的材料硬答 ——
    这就是"一本正经地编造"最常见的触发路径。
    """
    unrelated = [source(1, score=0.02), source(2, score=0.05), source(3, score=0.01)]
    decision = decide_grounding(unrelated, min_score=0.15)
    assert decision.use_model is False
    assert decision.fallback_answer == NO_GROUNDING_ANSWER
    # 理由要说清"检索到了几条、最高多少" —— 否则排障时分不清是
    # "没检索到"还是"阈值太高"（这两件事的修法完全不同）
    assert "3 条" in decision.reason
    assert "0.050" in decision.reason or "0.05" in decision.reason


def test_at_least_one_source_above_the_threshold_allows_the_model() -> None:
    """只要有一条够格就调用模型（阈值是**兜底闸门**，不是"每条都要达标"）。"""
    decision = decide_grounding([source(1, 0.02), source(2, 0.9)], min_score=0.15)
    assert decision.use_model is True
    assert decision.fallback_answer == ""


def test_threshold_boundary_is_closed() -> None:
    """相似度**正好等于**阈值 → 够格（闭区间）。

    边界取闭区间是刻意的：阈值是"最低可接受"，不是"必须高于"。
    """
    decision = decide_grounding([source(1, 0.15)], min_score=0.15)
    assert decision.use_model is True


# ---------------------------------------------------------------- 规则二：引用校验


def test_a_fabricated_citation_number_is_reported() -> None:
    """**引用了不存在的编号 → 报出来**（验收项的核心）。

    这是"编造出处"的典型形态：只提供了 2 条来源，回答却说"根据[5]"。
    光在提示词里要求"标注来源"是拦不住它的 —— 必须事后校验。
    """
    answer = "根据[5]，金额超过 10 万元须报总经理审批。"
    problems = check_citation_integrity(answer, [source(1), source(2)])
    assert problems
    assert "不存在的来源编号" in problems[0]
    assert "[5]" in problems[0]


def test_a_substantive_answer_without_any_citation_is_reported() -> None:
    """有实质内容却一个引用都没有 → 报"无出处的断言"。"""
    answer = "金额超过十万元的采购必须报总经理审批，并附上三家比价记录。"
    problems = check_citation_integrity(answer, [source(1)])
    assert any("没有任何引用编号" in problem for problem in problems)
    assert any("没有引用任何来源" in problem for problem in problems)


def test_a_correct_answer_with_valid_citations_passes() -> None:
    answer = "根据[1]，金额超过 10 万元的采购须报总经理审批。"
    assert check_citation_integrity(answer, [source(1)]) == []


def test_the_refusal_answer_passes_the_check() -> None:
    """**"未找到相关规定"必须能通过校验**（拒答是策略内的合法输出）。

    它既没有引用、也不引用来源，按规则 2/3 会被判成问题 —— 但那正是正确答案。
    这条如果写错，每一次正确的拒答都会被记成"校验失败"，校验也就没人看了。
    """
    assert check_citation_integrity(NO_GROUNDING_ANSWER, [source(1)]) == []
    assert check_citation_integrity(NO_GROUNDING_ANSWER, []) == []


def test_a_short_answer_without_citation_is_not_flagged() -> None:
    """**短回答**不按"无出处的断言"处理（阈值 `SUBSTANTIVE_LENGTH` 的作用）。

    否则"好的。"这种承接句也会被记成问题，日志里全是噪音。
    """
    problems = check_citation_integrity("好的。", [source(1)])
    # 仍然会命中"检索到了来源却完全不引用"（这条不设长度门槛），
    # 但不该命中"包含实质内容但没有任何引用编号"
    assert not any("实质内容" in problem for problem in problems)


def test_sources_exist_but_answer_cites_nothing_is_reported_even_if_short() -> None:
    """检索到了来源却完全不用 → 报出来（不设长度门槛）。

    含义是"这段回答不是基于这些片段生成的" —— 哪怕它很短，也说明拼进 Prompt 的
    材料没起作用，属于链路问题。
    """
    problems = check_citation_integrity("我看看啊", [source(1), source(2)])
    assert any("没有引用任何来源" in problem for problem in problems)


def test_multiple_valid_citations_pass() -> None:
    answer = "根据[1]与[2]，金额标准与审批层级如下……"
    assert check_citation_integrity(answer, [source(1), source(2), source(3)]) == []


def test_citation_warning_is_a_constant() -> None:
    """提示语是常量：前端/测试要按它断言，散在代码里就会漂。"""
    assert "来源校验未通过" in CITATION_WARNING


# ---------------------------------------------------------------- Prompt 组装


def test_context_numbers_match_sources_and_show_version() -> None:
    """片段块里的编号与 `Source.number` 一致，且**写明版本**。

    版本必须出现在 Prompt 里：制度修订后模型可能把两版条文混着引，
    而"第几版"是判断"它在按哪一版说话"的唯一线索。
    """
    context = build_context([source(1), source(2, title="差旅费管理办法")])
    assert "[1]" in context
    assert "[2]" in context
    assert "《采购管理办法》" in context
    assert "《差旅费管理办法》" in context
    assert "第 1 版" in context


def test_user_prompt_puts_the_material_before_the_question() -> None:
    """**材料在问题之前**。

    反过来（先问后给材料）会让模型先形成答案再"找材料佐证"，
    更容易出现"材料里没有但答得很顺"的情况。
    """
    prompt = build_user_prompt("多少钱要招标？", [source(1)])
    assert prompt.index("采购金额超过") < prompt.index("多少钱要招标？")


def test_system_prompt_covers_the_four_rules() -> None:
    """四句话对应四件必须守住的事（写死断言，防止有人"顺手改简洁"）。

    尤其是第 4 条（不能执行操作）：那是权限边界 ——
    不能因为多了个聊天入口，就出现一条绕过审批的旁路。
    """
    assert "只根据提供的制度片段回答" in SYSTEM_PROMPT
    assert "标注引用来源编号" in SYSTEM_PROMPT
    assert "制度中没有找到相关规定" in SYSTEM_PROMPT
    assert "不能执行任何采购操作" in SYSTEM_PROMPT
