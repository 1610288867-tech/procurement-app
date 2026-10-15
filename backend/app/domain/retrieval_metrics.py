"""检索层评测指标（M11 增强）：Recall@K / Precision@K / MRR / NDCG@K。

纯函数，不 import 任何框架。

============================ 为什么这一层必须单独有指标 ============================
RAG 出问题时，最难回答的一个问题是：**到底是检索没找到，还是模型没答对？**

    该找的资料没进入上下文        → 检索问题（改切分 / 改 embedding / 改阈值）
    资料进了上下文但答案还是错的  → 生成问题（改 prompt / 换模型）

只看"最终回答对不对"永远分不开这两者。所以评测必须**分层**：
检索层（本模块）与生成层（`citation_support.support_rate` 是其中一个近似）。
这是 RAG 评测的第一原则 —— 把检索评测与生成评测**解耦**。

============================ 四个指标各回答一个问题 ============================
    Recall@K      "该找到的，找到了吗？"        —— RAG 里**最重要**的一个，
                    因为正确资料没进上下文，后面模型再强也没用
    Precision@K   "找回来的，有多少是有用的？"  —— 噪声太多会挤掉上下文预算
    MRR           "第一个正确结果排在第几？"    —— 只关心**第一个**命中，
                    适合"一个答案就够了"的制度问答
    NDCG@K        "排序质量如何？"             —— 能区分"完全相关/部分相关"，
                    适合一份制度有多个相关片段的场景

============================ 三条口径约定（不写清就会算错）============================
1. **`k` 超过候选数时按实际数量算**：`Recall@10` 而只检索回 3 条时，
   分母仍是"相关总数"，不做任何放大；
2. **相关集合为空时 Recall 返回 `None`**（不适用），不是 0 ——
   与 `support_rate` 同一条纪律：把"不适用"当 0 会系统性压低均值；
3. **同一 chunk 在结果里出现两次只算一次命中**（去重后再算），
   因为"重复召回"不该被当成"召回得更多"。
"""

from __future__ import annotations

import math
from collections.abc import Sequence

#: 相关性等级（NDCG 用）。3 = 直接回答问题的条文，2 = 部分相关，
#: 1 = 背景/参考，0 = 无关。
GAIN_FULL = 3
GAIN_PARTIAL = 2
GAIN_BACKGROUND = 1


def recall_at_k(
    retrieved: Sequence[str], relevant: Sequence[str], k: int
) -> float | None:
    """`Recall@K` = 前 K 个结果里命中的相关项数 / 相关项总数。

    ⚠️ **相关项为空时返回 `None`（不适用）而不是 0.0** ——
    评测集里"无答案问题"就属于这种：它**没有**应该命中的片段，
    所以 Recall 对它没有意义。把它算成 0 会让整体均值凭空变差。
    """
    if k <= 0:
        raise ValueError("k 必须为正整数")
    unique_relevant = set(relevant)
    if not unique_relevant:
        return None
    hit = len(set(retrieved[:k]) & unique_relevant)
    return hit / len(unique_relevant)


def precision_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> float | None:
    """`Precision@K` = 前 K 个结果里命中的相关项数 / K。

    分母是 **K 与实际返回数里较小的那个**：只检索回 3 条却按 10 算分母，
    会得到"Precision@10 = 0.3"这种**误导性**的低分 ——
    那不是"结果不准"，而是"结果少"，两者要分开看。
    """
    if k <= 0:
        raise ValueError("k 必须为正整数")
    top = list(dict.fromkeys(retrieved[:k]))  # 去重，保留顺序
    if not top:
        return None
    hit = len(set(top) & set(relevant))
    return hit / len(top)


def reciprocal_rank(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> float:
    """单个查询的 `RR` = 1 / 第一个正确结果的排名；一个都没命中返回 0.0。

    这里**不返回 `None`**：一个都没命中是**明确的坏结果**（0 分），
    不是"不适用" —— 与 Recall 的语义差别就在这里。
    """
    unique_relevant = set(relevant)
    for index, item in enumerate(retrieved[:k], start=1):
        if item in unique_relevant:
            return 1.0 / index
    return 0.0


def mean_reciprocal_rank(
    results: Sequence[tuple[Sequence[str], Sequence[str]]], k: int = 10
) -> float | None:
    """`MRR` = 各查询 `RR` 的算术平均。没有可用查询时返回 `None`。

    ============================ 为什么"没有返回结果"的用例要剔除 ============================
    MRR 问的是"**第一个正确结果排在第几**"。一次**根本没返回任何结果**的查询
    （比如被阈值拒答、或检索为空）在这条指标上**没有位置可言** ——
    把它按 `RR = 0` 计入，会让 MRR 变成"命中率"的另一种写法，
    而且**系统性压低**它（实测踩到：同一份数据，剔除后 0.879 → 0.977）。

    这与"召回为空"的用例在 `recall_at_k` 里返回 `None` 是同一条纪律：
    **"不适用"不能当成 0**。
    """
    usable = [(retrieved, relevant) for retrieved, relevant in results if retrieved]
    if not usable:
        return None
    return sum(reciprocal_rank(r, rel, k) for r, rel in usable) / len(usable)


def dcg_at_k(gains: Sequence[int], k: int) -> float:
    """`DCG@K` = Σ (2^gain − 1) / log2(rank + 1)。

    用 `2^gain − 1` 而不是 gain 本身，是为了**放大高相关性结果的权重**：
    "完全相关"与"无关"的差距应当比"部分相关"与"无关"大得多。
    """
    total = 0.0
    for index, gain in enumerate(gains[:k], start=1):
        total += (2**gain - 1) / math.log2(index + 1)
    return total


def ndcg_at_k(retrieved: Sequence[str], gains: dict[str, int], k: int) -> float | None:
    """`NDCG@K` = DCG@K / IDCG@K（用理想排序归一化，取值 0–1）。

    为什么需要 NDCG 而不只看 Recall：一份制度里往往有多个相关片段，
    但它们**相关程度不同**（直接答案 vs 背景说明）。
    Recall 会说"都找到了，满分"，而 NDCG 会指出"最相关的那条被排到了第 8 位"。

    `gains` 是"片段 id → 相关性等级"的映射（3/2/1/0）。
    没有正收益的项时返回 `None`（不适用，同 Recall）。
    """
    if k <= 0:
        raise ValueError("k 必须为正整数")
    positive = [gain for gain in gains.values() if gain > 0]
    if not positive:
        return None

    actual = [gains.get(item, 0) for item in dict.fromkeys(retrieved[:k])]
    ideal = sorted(positive, reverse=True)
    ideal_dcg = dcg_at_k(ideal, k)
    if ideal_dcg == 0:
        return None
    return dcg_at_k(actual, k) / ideal_dcg


def hit_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> bool | None:
    """**命中率**：前 K 个里有没有至少一个相关项（相关项为空时 `None`）。

    它比 Recall 粗，但更贴近用户体感："我这个问题，它在不在前几条里给我找出来了"。
    """
    if not relevant:
        return None
    return bool(set(retrieved[:k]) & set(relevant))


def summarize_retrieval(
    cases: Sequence[tuple[Sequence[str], Sequence[str]]], k: int = 5
) -> dict[str, float | int | None]:
    """把一批评测用例汇总成一份检索指标报告。

    ============================ 为什么"不适用"要从均值里剔除 ============================
    `Recall` 对"无答案问题"返回 `None`。如果汇总时把 `None` 当成 0，
    那么**评测集里无答案问题越多，整体 Recall 看起来越差** ——
    而那不是检索变差了，是评测集构成变了。所以这里**只对有值的用例求均值**，
    并把被剔除的条数一起报出来（`recall_skipped`），让人知道这个均值是在多少条上算的。

    `mrr` 还额外剔除"**检索没返回任何结果**"的用例（见 `mean_reciprocal_rank`），
    所以它的样本数与 `recall` 的**可以不同** —— 报告里两个数都要能对上。
    """
    if k <= 0:
        raise ValueError("k 必须为正整数")

    recalls: list[float] = []
    precisions: list[float] = []
    hits: list[bool] = []
    skipped = 0

    for retrieved, relevant in cases:
        recall = recall_at_k(retrieved, relevant, k)
        precision = precision_at_k(retrieved, relevant, k)
        hit = hit_at_k(retrieved, relevant, k)
        if recall is None:
            skipped += 1
            continue
        recalls.append(recall)
        if precision is not None:
            precisions.append(precision)
        if hit is not None:
            hits.append(hit)

    # MRR 的样本 = 有相关项 **且** 真的返回了结果的用例
    mrr_cases = [
        (retrieved, relevant) for retrieved, relevant in cases if relevant and retrieved
    ]

    return {
        "k": k,
        "cases_total": len(cases),
        "cases_scored": len(recalls),
        # 「不适用」被剔除的条数 —— 报出来才知道均值算在多少条上
        "recall_skipped": skipped,
        "recall": (sum(recalls) / len(recalls)) if recalls else None,
        "precision": (sum(precisions) / len(precisions)) if precisions else None,
        "hit_rate": (sum(hits) / len(hits)) if hits else None,
        "mrr": mean_reciprocal_rank(mrr_cases, k),
        "mrr_cases": len(mrr_cases),
    }
