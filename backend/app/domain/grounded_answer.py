"""溯源策略（`domain/grounded_answer.py`）：**没有依据就不调用模型，引用不可信要报出来**。

纯函数，不 import 任何框架。计划里把这一层叫做"相对原项目的真实增量"，
因为原项目的 `GroundedAnswerPolicy` 只保证了第一条，**没保证第二条**：

============================ 两条规则，各自防一种失败 ============================
**规则一：没有依据就不调用模型。**
    检索结果为空、或**全部低于相似度阈值**时，不调用模型，
    直接返回"制度中没有找到相关规定"。

    为什么连"低于阈值"也算没有依据：检索**永远会返回 top-k 条**（哪怕全是无关内容）。
    只判"结果为空"的话，问一个制度里根本没有的问题（比如"年假多少天"），
    检索会把最不相关的几条（采购流程）递上去，模型于是**拿着不相干的片段硬答** ——
    这就是"一本正经地编造"最常见的触发路径。

**规则二：引用必须可信（"有引用"不等于"引用是真的"）。**
    光在 Prompt 里要求"标注来源"是不够的 —— 模型完全可以：

        · 引一个**不存在的编号**（"根据《采购管理办法》第 12 条" 而只提供了 3 条来源）；
        · 正文里**一个引用标记都没有**，但通篇是断言（"无出处的结论"）。

    这两条都要**事后校验**（`check_citation_integrity`），不能只靠提示词。

============================ 发现问题的处理：第一版"不阻断，但说出来" ============================
计划里定的第一版做法：**不阻断**，把问题记进日志/`chat_sources`，
并在回答末尾追加一行"本条回答的来源校验未通过"。

理由：直接拒答会让用户觉得"明明答得挺好却用不了"，而这类问题需要一个样本量
才能决定"重试一次"还是"换成拒答"。**关键是这个校验必须存在** ——
它今天的价值不是拦截，而是**让问题可见**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 引用标记：`[1]` / `[2]`。**与拼进 Prompt 的编号格式必须是同一套** ——
#: 这里改了、Prompt 没改，校验就会把正常回答全判成"没有引用"。
CITATION_PATTERN = re.compile(r"\[(\d+)\]")

#: 相似度阈值。低于它就算"没有依据"（见规则一）。
#:
#: ⚠️ 这个数**取决于 embedding 模型**，不是普适常量：
#: 本地 `local-hash-256` 靠字面重合，同一主题的相似度普遍在 0.2–0.6；
#: 真模型（如 text-embedding-v4）同一主题通常 0.5 以上。
#: 所以它**来自配置**（`knowledge_min_score`），默认值按本地模型定。
#: 换模型时必须一起调 —— 调错的后果是"要么全部拒答，要么全部硬答"。
DEFAULT_MIN_SCORE = 0.15

#: 回答短于这个长度就认为它"没有实质内容"（例如只有一句"未找到相关规定"）。
#: 用它来区分"短回答"与"无出处的断言"：前者不该被判成问题。
SUBSTANTIVE_LENGTH = 20

#: 没找到依据时的**固定回答**。
#: 写成常量而不是散在代码里：这句话是产品承诺的一部分（"查不到就说查不到"），
#: 测试要按它断言，前端也可能要按它切换样式。
NO_GROUNDING_ANSWER = "制度中没有找到相关规定。"

#: 来源校验未通过时追加的那一行。
CITATION_WARNING = "\n\n（本条回答的来源校验未通过，请以制度原文为准。）"


@dataclass(frozen=True)
class Source:
    """一条检索到的制度片段（编号从 **1** 开始，与 Prompt 里的 `[n]` 对应）。"""

    number: int
    doc_id: str
    doc_title: str
    doc_version: int
    chunk_no: int
    content: str
    score: float


@dataclass(frozen=True)
class GroundingDecision:
    """"要不要调用模型"的判定结果。"""

    use_model: bool
    #: `use_model=False` 时给用户看的话（就是 `NO_GROUNDING_ANSWER`）
    fallback_answer: str
    #: 为什么不用模型（进日志用，用户看不到）—— 这一条是验收项
    #: "检索为空时日志里能看到『未调用模型』"的落点
    reason: str


def decide_grounding(
    sources: list[Source], *, min_score: float = DEFAULT_MIN_SCORE
) -> GroundingDecision:
    """检索结果够不够格去问模型（规则一）。

    三种"不够格"，各自的 `reason` 不同 —— 因为它们排障时要看的东西不同：
        没有检索结果      → 看切分/入库
        有结果但全低于阈值 → 看 embedding 模型与阈值（多半是换模型没调阈值）
        结果里没有词      → 看查询文本（空查询）

    最后一种其实在上游就被拦了（`embed_text` 会返回全零向量），
    这里再判一次是兜底：全零向量进 SQL 之后余弦距离是 NULL/1，排序结果没有意义。
    """
    if not sources:
        return GroundingDecision(
            use_model=False,
            fallback_answer=NO_GROUNDING_ANSWER,
            reason="检索结果为空，未调用模型",
        )
    usable = [source for source in sources if source.score >= min_score]
    if not usable:
        best = max(source.score for source in sources)
        return GroundingDecision(
            use_model=False,
            fallback_answer=NO_GROUNDING_ANSWER,
            reason=(
                f"检索到 {len(sources)} 条但全部低于相似度阈值 {min_score}"
                f"（最高 {best:.3f}），未调用模型"
            ),
        )
    return GroundingDecision(
        use_model=True,
        fallback_answer="",
        reason=f"{len(usable)} 条片段达到阈值 {min_score}，调用模型",
    )


def check_citation_integrity(answer: str, sources: list[Source]) -> list[str]:
    """校验回答里的引用是否可信。返回**问题列表**（空 = 通过）。

    三条规则（与计划里的实现一致，顺序也一致）：

        1. **引用了不存在的编号** —— 典型的"编造出处"；
        2. **有实质内容但一个引用都没有** —— "无出处的断言"；
        3. **检索到了来源却完全不引用** —— 说明答案不是基于这些片段生成的。

    规则 2 与 3 看着重叠，其实各管一段：2 管"回答是不是在断言"（另有一套常量
    `SUBSTANTIVE_LENGTH` 判"算不算有实质内容"），3 管"给了来源却没用"。
    两个都留下，是因为将来只可能加强其中一条（例如"断言必须引用"与
    "来源必须被用到"是两个可以分别收紧的口径）。

    ⚠️ **"未找到相关规定"这句话要能通过校验**：它既没有引用、也不引用来源，
    按规则 2/3 会被判成问题 —— 但那正是**正确答案**。
    所以这里把 `NO_GROUNDING_ANSWER` 作为**例外**放行：
    拒答是策略内的合法输出，不是"无出处的断言"。
    """
    if answer.strip() == NO_GROUNDING_ANSWER:
        return []

    problems: list[str] = []
    cited = {int(number) for number in CITATION_PATTERN.findall(answer)}
    valid = {source.number for source in sources}

    if cited - valid:
        problems.append(
            f"回答引用了不存在的来源编号：{sorted(cited - valid)}（可用编号 {sorted(valid)}）"
        )
    if not cited and len(answer.strip()) > SUBSTANTIVE_LENGTH:
        problems.append("回答包含实质内容但没有任何引用编号")
    if sources and not cited:
        problems.append("检索到了制度片段，但回答没有引用任何来源")
    return problems


def build_context(sources: list[Source]) -> str:
    """把来源拼成 Prompt 里的"制度片段"块。

    **编号必须与 `Source.number` 一致**，且写明文档版本 ——
    版本要写出来，是因为制度修订后模型可能把两版条文混着引，
    而用户看到"第 5 版"就知道自己在按哪一版办事。
    """
    blocks = [
        f"[{source.number}] 《{source.doc_title}》（第 {source.doc_version} 版，第 {source.chunk_no} 段）\n"
        f"{source.content}"
        for source in sources
    ]
    return "\n\n".join(blocks)


#: 系统提示词。**四句话对应四件必须守住的事**，不是随手写的：
#:   1. 只根据片段回答 → 防编造；
#:   2. 标注编号 → 让答案可校验（配合 `check_citation_integrity`）；
#:   3. 不够就说没找到 → 与规则一一致（模型这一侧也要说同一句话）；
#:   4. 它是解释工具，不能执行操作 → 这是权限边界，不能让"聊天"变成"下单"的旁路。
SYSTEM_PROMPT = """你是企业采购制度助手。规则：
1. 只根据提供的制度片段回答，不得编造制度条文、金额标准或流程。
2. 回答必须标注引用来源编号，例如「根据[1]」或「[2][3]」。
3. 如果提供的片段不足以回答，直接说"制度中没有找到相关规定"，不要猜测。
4. 你是解释工具，不能执行任何采购操作，也不能改变任何单据状态。
"""


def build_user_prompt(question: str, sources: list[Source]) -> str:
    """用户消息 = 制度片段 + 问题。

    ⚠️ 片段的**位置**很讲究：放在问题**之前**。
    反过来（先问后给材料）会让模型先形成答案再"找材料佐证"，
    更容易出现"材料里没有但答得很顺"的情况。
    """
    return (
        "以下是检索到的制度片段，请只根据它们回答：\n\n"
        f"{build_context(sources)}\n\n"
        f"---\n问题：{question}"
    )
