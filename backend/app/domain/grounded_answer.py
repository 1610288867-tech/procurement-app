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
    """一条检索到的制度片段（编号从 **1** 开始，与 Prompt 里的 `[n]` 对应）。

    `chunk_id` 是**片段的稳定标识**（数据库主键）。它不进 Prompt（Prompt 里用编号），
    但它是**评测的锚点**：检索层指标（Recall@K / MRR / NDCG）判的是
    "期望命中的那一片有没有被检索到"，而那一判据必须落在
    **与切分无关的标识**上 —— 用 `chunk_no` 判的话，
    改一次切分参数（片数变了）整份评测集就失效了。
    """

    number: int
    doc_id: str
    doc_title: str
    doc_version: int
    chunk_no: int
    content: str
    score: float
    #: 片段主键（评测与排障用）。**默认空串**是为了不让老调用点全炸 ——
    #: 生产路径一定会填（协调者从记录里带出来）。
    chunk_id: str = ""


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


def has_confident_grounding(
    sources: list[Source], *, strong_score: float
) -> bool:
    """检索到的依据**够不够硬**（够硬才直接用它回答，不再试 Agent）。

    ============================ 为什么需要"第二道线" ============================
    实测（2026-10-15，10 份种子制度入库后）业务数据问题与制度问题的
    top_score **是重叠的**：

        业务数据："我提过哪些采购申请？"   0.624   ← 比下面那条制度问题还高
        制度问题："廉洁从业禁止哪些行为？"  0.584

    所以**不存在**一个能分开这两类的点。既然分不开，就改看**答错的代价**：

        用制度片段回答业务数据问题 → 答非所问，而且**看起来像回答**（最糟）
        让 Agent 去处理制度问题     → 它没有查制度的工具 → **诚实地拒答**

    后者是安全的一侧（本项目的铁律："没有来源就不回答"）。所以
    "够硬"取**高**的那条线：只有分很高时才直接答，其余先给 Agent 一次机会。

    ============================ 与 `decide_grounding` 的分工 ============================
        `decide_grounding`      → "**能不能**用检索结果回答"（有依据吗）
        `has_confident_grounding` → "**要不要**直接用它回答"（依据够硬吗）

    两个判据分开，是因为它们的失效方向不同：前者放宽会让系统"什么都敢答"，
    后者放宽会让系统"该答的不答"。合成一个函数就再也说不清是哪一个被调了。

    ⚠️ **`strong_score <= 0` 会让它恒为 True**（任何分数都 ≥ 0）。
    这是退化配置，**在这里不兜底**：判据本体保持"就是比大小"这一件事，
    配置合法性由装配处校验（见 `build_knowledge_coordinator` 对两个阈值的检查）——
    把配置校验混进判据，会让"这个函数为什么返回 False"有两个可能的原因。
    """
    if not sources:
        return False
    return max(source.score for source in sources) >= strong_score


#: 第一层校验（引用完整性）里"与来源相关"的两条问题消息。
#:
#: **提成常量是因为它们会被别处读**：`KnowledgeCoordinator._finish` 在
#: **Agent 型回答**上要把这两条筛掉（Agent 查的是业务数据，那种回答本来就没有制度引用，
#: 给它套这条规则会得到一个假问题 + 一句**误导性**的"请以制度原文为准"）。
#:
#: ⚠️ 之前那里写的是**字符串字面量**，而我只抄了其中一个（"没有任何引用编号"）——
#: 于是另一条（"没有引用任何来源"）照样贴在业务数据的回答后面。
#: 症状是"看着改了、行为没变"，属于**同一份事实存了两个字面量**的典型后果。
PROBLEM_NO_CITATION = "回答包含实质内容但没有任何引用编号"
PROBLEM_SOURCES_UNUSED = "检索到了制度片段，但回答没有引用任何来源"

#: 上面两条里，**在 Agent 型回答上属于假问题**的那两条。
#: 第三条（引用了不存在的编号）**任何情况下都要保留** —— 那是真违规。
AGENT_IRRELEVANT_PROBLEMS = (PROBLEM_NO_CITATION, PROBLEM_SOURCES_UNUSED)


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
        problems.append(PROBLEM_NO_CITATION)
    if sources and not cited:
        problems.append(PROBLEM_SOURCES_UNUSED)
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
