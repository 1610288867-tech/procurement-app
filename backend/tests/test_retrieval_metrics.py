"""检索层评测指标的纯函数测试（M11 增强）。

这一层是 RAG 评测的地基，所以测试要钉住三类东西：
**① 指标算得对（用手算得出的例子）；② `None`（不适用）与 `0.0`（坏结果）不混；
③ 去重口径** —— 同一片段被召回两次不该算"召回得更多"。
"""

from __future__ import annotations

import pytest

from app.domain.retrieval_metrics import (
    dcg_at_k,
    hit_at_k,
    mean_reciprocal_rank,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    summarize_retrieval,
)

pytestmark = pytest.mark.unit


# ================================================================ Recall@K


def test_recall_uses_a_hand_checkable_example() -> None:
    """手算例子：相关 3 条（A/B/C），前 5 召回 D/A/F/B/G → 命中 A、B → 2/3。"""
    retrieved = ["D", "A", "F", "B", "G"]
    assert recall_at_k(retrieved, ["A", "B", "C"], 5) == pytest.approx(2 / 3)


def test_recall_is_one_when_all_relevant_found() -> None:
    assert recall_at_k(["A", "B"], ["A", "B"], 5) == 1.0


def test_recall_does_not_grow_when_the_same_chunk_repeats() -> None:
    """**重复召回不等于召回得更多**（去重后再算）。

    没有去重的话，一个片段被检索回三次就能把 Recall 刷到虚高 ——
    而用户看到的上下文里其实只有一条是新的。
    """
    assert recall_at_k(["A", "A", "A"], ["A", "B"], 5) == pytest.approx(0.5)


def test_recall_is_none_when_not_applicable() -> None:
    """**相关项为空 → `None`（不适用），不是 0.0。**

    评测集里的"无答案问题"就属于这种。把它当 0 分会**系统性压低**整体 Recall，
    而那不是检索变差，是评测集里多了这类问题。
    """
    assert recall_at_k(["A", "B"], [], 5) is None


def test_recall_rejects_non_positive_k() -> None:
    with pytest.raises(ValueError):
        recall_at_k(["A"], ["A"], 0)


# ================================================================ Precision@K


def test_precision_uses_returned_count_as_denominator() -> None:
    """只返回 3 条时按 3 算分母，**不按 K 算**。

    否则只检索回 3 条会得到"Precision@10 = 0.3"这种误导性低分 ——
    那不是"结果不准"，而是"结果少"，两者要分开看。
    """
    assert precision_at_k(["A", "X"], ["A"], 10) == pytest.approx(0.5)


def test_precision_is_none_for_empty_result() -> None:
    assert precision_at_k([], ["A"], 5) is None


# ================================================================ MRR


def test_reciprocal_rank_follows_rank() -> None:
    """排第 1 → 1.0；第 2 → 0.5；第 4 → 0.25。"""
    assert reciprocal_rank(["A"], ["A"], 10) == 1.0
    assert reciprocal_rank(["X", "A"], ["A"], 10) == 0.5
    assert reciprocal_rank(["X", "Y", "Z", "A"], ["A"], 10) == 0.25


def test_reciprocal_rank_is_zero_when_nothing_hits() -> None:
    """**一个都没命中是明确的坏结果（0 分），不是"不适用"** —— 与 Recall 的差别在这里。"""
    assert reciprocal_rank(["X", "Y"], ["A"], 10) == 0.0


def test_mrr_averages_over_cases() -> None:
    """手算例子：排名 1 / 2 / 4 → (1 + 0.5 + 0.25) / 3。"""
    cases = [(["A"], ["A"]), (["X", "A"], ["A"]), (["X", "Y", "Z", "A"], ["A"])]
    assert mean_reciprocal_rank(cases, k=10) == pytest.approx(1.75 / 3)


def test_mrr_is_none_without_cases() -> None:
    assert mean_reciprocal_rank([], k=10) is None


def test_mrr_excludes_cases_that_returned_nothing() -> None:
    """**"没返回任何结果"的用例必须从 MRR 里剔除** —— 它不是"排得靠后"。

    ============================ 为什么这条必须钉住（实测踩到）============================
    MRR 问的是"第一个正确结果排在第几"。一次**根本没返回结果**的查询
    （被阈值拒答、或检索为空）在这条指标上**没有位置可言**。
    把它按 `RR = 0` 计入，MRR 就退化成"命中率"的另一种写法，
    而且**系统性压低**它 —— 实测同一份数据：算进去 0.879，剔除后 0.977。

    这与"召回为空时 `recall_at_k` 返回 `None`"是同一条纪律：**不适用不能当成 0**。
    """
    cases = [
        (["A"], ["A"]),      # 排第 1 → RR = 1.0
        ([], ["A"]),         # **没返回结果** → 应当剔除，而不是算 0
        ([], ["B"]),         # 同上
    ]
    assert mean_reciprocal_rank(cases, k=10) == pytest.approx(1.0)


def test_mrr_is_none_when_every_case_returned_nothing() -> None:
    """全都"没返回结果"时 MRR 是 `None`（不适用），不是 0 —— 与"全都排得很靠后"要能区分。"""
    assert mean_reciprocal_rank([([], ["A"]), ([], ["B"])], k=10) is None


# ================================================================ NDCG


def test_dcg_discounts_lower_ranks() -> None:
    """`DCG` 用 `2^gain − 1` 并除以 `log2(rank+1)`：排得越靠后贡献越小。"""
    assert dcg_at_k([3], 5) == pytest.approx(7.0)  # (2^3-1)/log2(2)
    assert dcg_at_k([3, 3], 5) < 2 * dcg_at_k([3], 5)


def test_ndcg_is_one_for_the_ideal_order() -> None:
    """理想排序（高收益在前）→ NDCG = 1.0。"""
    gains = {"A": 3, "B": 2, "C": 1}
    assert ndcg_at_k(["A", "B", "C"], gains, 3) == pytest.approx(1.0)


def test_ndcg_punishes_putting_the_best_result_late() -> None:
    """**同一个"都召回了"的集合，排序不同 → NDCG 不同。**

    这正是 NDCG 存在的理由：Recall 会对两种排序都给满分，
    而 NDCG 会指出"最相关的那条被排到了最后"。
    """
    gains = {"A": 3, "B": 2, "C": 1}
    good = ndcg_at_k(["A", "B", "C"], gains, 3)
    bad = ndcg_at_k(["C", "B", "A"], gains, 3)
    assert bad is not None and good is not None
    assert bad < good


def test_ndcg_is_none_without_positive_gains() -> None:
    assert ndcg_at_k(["A"], {"A": 0}, 5) is None


# ================================================================ 命中率与汇总


def test_hit_rate_is_coarse_but_user_facing() -> None:
    """"它在不在前几条里" —— 比 Recall 粗，但更贴近体感。"""
    assert hit_at_k(["X", "A"], ["A"], 5) is True
    assert hit_at_k(["X", "Y"], ["A"], 5) is False
    assert hit_at_k(["A"], [], 5) is None


def test_summarize_excludes_not_applicable_and_reports_how_many() -> None:
    """**"不适用"要从均值里剔除，并把剔除条数报出来。**

    这条是评测报告可信度的关键：`cases_scored` 与 `recall_skipped` 一起说明
    "这个均值算在几条上" —— 只报一个均值而不说样本量，是评测里最常见的含混。
    """
    cases = [
        (["A", "B"], ["A", "B"]),  # Recall = 1.0
        (["X", "Y"], ["A"]),       # Recall = 0.0
        (["X"], []),               # 无答案问题 → 不计入
    ]
    report = summarize_retrieval(cases, k=5)
    assert report["cases_total"] == 3
    assert report["cases_scored"] == 2
    assert report["recall_skipped"] == 1
    assert report["recall"] == pytest.approx(0.5)
    assert report["hit_rate"] == pytest.approx(0.5)
    # MRR 只在"有相关项 **且** 真的返回了结果"的用例上算
    assert report["mrr"] == pytest.approx(0.5)
    assert report["mrr_cases"] == 2


def test_summarize_returns_none_when_nothing_is_scorable() -> None:
    """全是无答案问题时，指标为 `None` 而不是 0 —— 与"全都答错"要能区分。"""
    report = summarize_retrieval([(["X"], []), (["Y"], [])], k=5)
    assert report["recall"] is None
    assert report["recall_skipped"] == 2
