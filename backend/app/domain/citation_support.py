"""引用支撑性校验（M11 增强）：**"编号真"不等于"内容撑得住"**。

纯函数，不 import 任何框架。

============================ 这个文件补的是一个真实盲区 ============================
`grounded_answer.check_citation_integrity` 只校验三件事：编号是否存在、
有无零引用、检索到来源是否引用。**它校验的是"引用的编号真不真"，
不是"引用的内容撑不撑得住这句话"。** 所以下面这种情况会被放行：

    来源 [1]：《采购管理办法》第十二条 —— "采购金额超过 10 万元的，应当公开招标。"
    回答    ："根据[1]，采购金额超过 50 万元的应当公开招标。"

编号是真的（`[1]` 存在），来源也确实被引用了 —— 但**内容是错的**。
这在 RAG 评测里对应的指标叫 **Faithfulness（忠实度）**：
"回答里的事实，能不能从被引用的上下文里得到支持"。

============================ 为什么用规则法而不是再调一个模型 ============================
1. **不花钱、不依赖密钥**：能进回归入口，每次提交都跑；
2. **确定性**：同一段文本永远给同一个结论，测试可以钉住；
3. **这一层的目标是"发现明显不支撑"**，不是"判定语义等价" ——
   制度问答里出问题的事实绝大多数是**数字、期限、比例、否定词**这四类，
   而它们恰好都是可以用规则精确比对的。
   语义级的判断留给"生成指标"那一层（可选接 LLM-as-a-Judge）。

============================ 校验的三件事（各自防一种错） ============================
    ① **数字不支撑**：回答里的金额/期限/比例，在被引用的片段里找不到对应值
       （"10 万"写成"50 万"、"三个工作日"写成"七个工作日"）；
    ② **否定翻转**：来源说"不得/无需/禁止"，回答却说"可以/应当"（或反之）；
    ③ **引用错位**：`[n]` 出现在离它该支撑的那句话很远的地方 ——
       典型表现是"整段回答只在末尾挂一个 [1]"，
       读者无法判断哪句话来自哪个片段。

**这三件事都查不出"意思对但换了个说法"**，所以本模块的结论是
"**发现的问题**"而不是"通过率"——它只减分、不加分。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.grounded_answer import CITATION_PATTERN, Source

#: 中文数字 → 阿拉伯数字。只覆盖制度文本里真会出现的量级。
_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
#: 数量级后缀：万 / 亿（"十万" = 100000）
_CN_SCALES = {"万": 10_000, "亿": 100_000_000, "千": 1_000, "百": 100}

#: 数值 + 可选单位。单位参与比对，因为"10 万元"与"10 天"不是同一件事。
_NUMBER_UNITS = (
    "元", "万元", "亿元", "万", "亿", "块", "分", "角",
    "天", "日", "个工作日", "工作日", "周", "星期", "个月", "月", "年", "小时", "分钟",
    "%", "％", "百分之", "折", "倍", "次", "人", "家",
)
_UNIT_GROUP = "|".join(re.escape(unit) for unit in sorted(_NUMBER_UNITS, key=len, reverse=True))

#: **一个正则同时认阿拉伯数字与中文数字**，并允许带上数量级后缀。
#:
#: 为什么必须放在一个正则里：早期版本用两个正则分别抽，
#: 于是 `"10 万元"` 被拆成 `("10", None)`（来自阿拉伯正则）与
#: `("10000", "元")`（来自中文正则的"万"）**两个错的事实** ——
#: 前者会被判成"数字不支撑"，**是校验自己在误报**。
#:
#: 形状（按长度从长到短匹配，避免"十万"被拆成"十"+"万"）：
#:     十万 / 十 / 10000 / 100,000   可选数量级（万|亿）   可选单位（万元|元|天|%…）
#: 数字与单位之间允许空白 —— ⚠️ **必须包含全角空格 `\u3000`**：
#: 制度文档里"10　万元"是常见写法，而 Python 的 `\s` **不匹配全角空格**
#: （实测踩到：`"10　万元"` 只抽出 `("10", None)`，单位丢了，于是同一个事实
#: 在回答与来源里被算成两个 —— 校验自己误报）。
_NUMBER_PATTERN = re.compile(
    r"([零一二两三四五六七八九十百千万亿]{1,6}|\d[\d,，]*(?:\.\d+)?)"
    r"[\s\u3000]*"
    r"([万亿])?"
    r"[\s\u3000]*"
    rf"({_UNIT_GROUP})?"
)

#: 否定词。用来做"否定翻转"检测。
_NEGATION = ("不得", "不能", "不可", "不应", "不予", "无需", "无须", "禁止", "严禁", "不允许")
_AFFIRMATION = ("可以", "应当", "必须", "允许", "需要", "应")

#: 引用标记出现在句子末尾多远之内，算"就近"。
#: 取 1 句：制度回答的自然写法是"根据[1]，……"或"……[1]。"，
#: 要求"同一句"最严格也最合理；跨句引用往往意味着整段挂一个编号。
_SENTENCE_SPLIT = re.compile(r"[。！？；\n]+")

#: 判定"这句话算不算一条独立断言"的最短长度。
#: 太短的句子（"好的。"、"另外。"）不算断言，不该报"无依据"。
_MIN_ASSERTION_LENGTH = 20

#: 没有引用的句子，**离最近的引用句最多允许多远**（按句数）。
#: 取 3 而不是 1：制度问答有几种合法写法会天然隔开一两句 ——
#:     「块头：[1]《采购管理办法》第 N 条」（引用句）
#:     「采购金额超过 10 万元的应当公开招标」（摘录，不带编号但属于上一块）
#:     「[2]《差旅费管理办法》第 M 条」（下一个块头）
#: 只允许同句引用会把上面第二种判成"错位"（实测踩到：本地兜底回答被误报）。
#: 而隔到 4 句以上，基本就是"引用集中在开头、后面自由发挥"了。
_MAX_CITATION_DISTANCE = 3

#: **结构性说明句**的前缀 —— 它们在说"这段回答是什么"，不是在断言制度内容，
#: 所以不需要出处。按前缀匹配（不是包含）：见 `_is_structural_line`。
_STRUCTURAL_PREFIXES = (
    "根据检索到的制度原文",
    "以下为制度原文",
    "以下内容摘自",
    "本条回答",
    "（本条回答",
)

#: 取否定词后面多少个字符当作"它否定的对象"（"不得**提交**" → "提交"）。
#: 取 4：中文里"不得提交""不允许报销""无需审批"这类动宾结构通常是 2–4 字。
_NEGATION_OBJECT_LEN = 4


@dataclass(frozen=True)
class SupportProblem:
    """一条"引用不支撑"的问题。

    `kind` 是**机器可判的类别**（落库与统计用），`detail` 是给人看的话。
    分开的理由：统计"哪一类问题最多"要靠 `kind`，
    而 `detail` 的文案将来会改（改文案不该动统计口径）。

    `sentence_index` 是**这句话在回答里的序号**（`None` 表示这条问题不针对具体某句）。
    它存在的唯一理由：让 `support_rate` 能按"**句**"统计而不是按"问题条数"统计 ——
    同一句报了两个问题时，它只该算"这一句不通过"，而不是扣两次分。
    """

    kind: str
    detail: str
    sentence_index: int | None = None


#: 问题类别（落库与统计用，**改这些字符串要一起改测试与报表**）
KIND_NUMBER = "CITATION_NUMBER_UNSUPPORTED"
KIND_NEGATION = "CITATION_NEGATION_FLIPPED"
KIND_DISTANT = "CITATION_MISPLACED"


def _cn_to_int(text: str) -> int | None:
    """中文数字 → 整数。认不出返回 None（**不猜**）。

    只处理制度文本里真会出现的形态：
        "十" = 10、"十五" = 15、"二十" = 20、"三" = 3、"两百" = 200。

    ⚠️ **"万 / 亿" 不在这里处理**：它们是**数量级**，由 `extract_facts` 的
    数量级组负责乘上去。早先版本把它们混在一起判，
    于是 `"十万"` 会走进下面的逐字拼接分支、得到 `None` ——
    表现为"整条事实被丢掉"（而丢掉比误报更危险：它让校验静默失效）。

    **认不出就返回 None 而不是抛异常**：这里的目标是"少报错"，
    一个认不出的数字不该让整条校验失败（调用方会跳过它）。
    """
    if not text:
        return None
    if len(text) == 1:
        return _CN_DIGITS.get(text)
    if text.startswith("十"):
        tail = _CN_DIGITS.get(text[1:]) if len(text) == 2 else None
        return 10 + tail if tail is not None else None
    if "十" in text:
        head, _, tail_text = text.partition("十")
        head_value = _CN_DIGITS.get(head) if head else 1
        tail_value = _CN_DIGITS.get(tail_text) if tail_text else 0
        if head_value is None or tail_value is None:
            return None
        return head_value * 10 + tail_value
    # "两百" / "三千" 这类：数值 + 倍率
    if len(text) == 2 and text[1] in _CN_SCALES:
        head_value = _CN_DIGITS.get(text[0])
        return head_value * _CN_SCALES[text[1]] if head_value is not None else None
    # 纯数字的逐字拼接（"一二三" 这种极少见，但不难）
    total = 0
    for char in text:
        value = _CN_DIGITS.get(char)
        if value is None:
            return None
        total = total * 10 + value if total else value
    return total


def _to_number(text: str) -> float | None:
    """数字文本 → 数值。认不出返回 `None`（**不猜、不抛异常**）。

    认不出的代价只是"这个数字不参与校验"，而抛异常会让整条回答的校验失败 ——
    校验工具本身不该成为故障源。
    """
    cleaned = text.replace(",", "").replace("，", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        pass
    value = _cn_to_int(cleaned)
    return float(value) if value is not None else None


def extract_facts(text: str) -> set[tuple[str, str | None]]:
    """抽出这段文本里的**硬事实**：`(归一化后的数值, 单位)` 集合。

    ============================ 抽之前先去掉引用标记 ============================
    ⚠️ **`[1]` 里的 "1" 会被数字正则当成一个数字** ——
    而 `[1]` 是引用标记、不是事实。实测踩到：加了"裸数字按元处理"的门槛之后，
    `"根据[1]，……"` 抽出了 `("1", None)`，于是**每一句带引用的回答都被判成
    "数字 1 不支撑"**（校验自己制造误报）。所以这里先剥掉引用标记再抽。

    ============================ 归一化（必须做，否则满屏误报）============================
    "十万元" / "10 万元" / "100000 元" / "10万" **是同一个事实**，
    但写法完全不同。所以统一归一成 `(数值, 归一化单位)`：
        · 中文数字转成阿拉伯数字（"十万" → 100000）；
        · 金额单位统一成"元"（"万元" → ×10000）；
        · 去掉千分位与空白。
    **只做这几件确定的事**，不做语义推断。

    返回**集合**（同一个数字出现两次不是两个事实）。
    """
    facts: set[tuple[str, str | None]] = set()
    cleaned_text = CITATION_PATTERN.sub(" ", text)

    for match in _NUMBER_PATTERN.finditer(cleaned_text):
        raw, scale, unit = match.group(1), match.group(2), match.group(3)
        # 中文数字里的"万/亿"是**数量级**，不是数值的一部分：
        # 正则的数字组允许含"万/亿"（否则"十万"匹配不上），
        # 所以这里要把结尾的数量级字符摘出来单独乘 —— 见下面 `_split_scale`。
        raw, inline_scale = _split_scale(raw)
        effective_scale = inline_scale or scale
        number = _to_number(raw)
        if number is None:
            continue
        if effective_scale == "万":
            number *= 10_000
        elif effective_scale == "亿":
            number *= 100_000_000
        facts.add(_normalize(number, unit))

    return facts


def _split_scale(raw: str) -> tuple[str, str | None]:
    """把中文数字结尾的数量级字符摘出来：`"十万"` → `("十", "万")`。

    为什么要摘：看数字组的取值顺序 —— 先 `_to_number("十万")`
    （它认不出，返回 `None`），再把"万"乘上去。**反过来的话整条事实会被丢掉**，
    而"丢掉"比"误报"危险得多：它让校验静默失效。

    只摘**一个**字符，且只在它确实是"万/亿"、且前面还有数字时摘 ——
    `"万"` 单独出现（比如"万元"被切出来的残片）时原样返回，让 `_to_number` 判 None。
    """
    if len(raw) >= 2 and raw[-1] in ("万", "亿"):
        return raw[:-1], raw[-1]
    return raw, None


#: 没有单位、但数值大到"只可能是金额"的门槛。
#: 用于把"10万"与"10 万元"归一成同一个事实（见 `_normalize` 的最后一段）。
#: 取 100：制度文本里"3 天""5 人""20%"都远小于它，
#: 而"10万""5000""20000"这类裸数字几乎必然是金额。
_BARE_AMOUNT_THRESHOLD = 100


def _normalize(number: float, unit: str | None) -> tuple[str, str | None]:
    """把 `(数值, 单位)` 归一到可比较的形态。

    金额一律折算成**元**（"万元"→×10000、"亿元"→×1e8、"分"→÷100），
    这样"10 万元"与"100000 元"会落到同一个事实。
    其它单位（天/工作日/%）只做**别名统一**、不做换算 ——
    "3 天"与"3 个工作日"在法律文本里可能不是同一件事，**不替它下结论**。

    ============================ 最后那段"裸数字补元"为什么必要 ============================
    `"10万"`（省略单位）与 `"10 万元"` 是**同一个事实**，但早先版本
    分别得到 `("100000", None)` 与 `("100000", "元")` —— 两个不同的键，
    于是同一个金额在回答与来源之间被算成"不支撑"。**那是校验自己误报。**
    所以：**没有单位、且数值 ≥ 100** 时按"元"处理（保守门槛，见常量说明）。
    """
    if unit in ("万元", "万"):
        number, unit = number * 10_000, "元"
    elif unit in ("亿元", "亿"):
        number, unit = number * 100_000_000, "元"
    elif unit == "分":
        number, unit = number / 100, "元"
    elif unit == "角":
        number, unit = number / 10, "元"
    elif unit == "％":
        unit = "%"
    elif unit in ("个工作日", "工作日"):
        unit = "工作日"
    elif unit == "日":
        unit = "天"
    elif unit is None and number >= _BARE_AMOUNT_THRESHOLD:
        unit = "元"
    return (_fmt(number), unit)


def _fmt(number: float) -> str:
    """数字归一成字符串：整数去掉小数点，小数保留原值。

    为什么用字符串而不是 float 作 key：`0.1 + 0.2` 的浮点表示问题会让
    `30.0 == 30` 这类比较看起来没问题，但 `0.1*3 != 0.3` ——
    写成规范化字符串就绕开了这一类比较陷阱（**与"金额用整数分"是同一条纪律**）。
    """
    if number == int(number):
        return str(int(number))
    return f"{number:.6f}".rstrip("0").rstrip(".")


def _sentences_with_citations(answer: str) -> list[tuple[str, set[int]]]:
    """把回答按句切分，返回每句与它引用的编号集合。"""
    result: list[tuple[str, set[int]]] = []
    for sentence in _SENTENCE_SPLIT.split(answer):
        stripped = sentence.strip()
        if not stripped:
            continue
        numbers = {int(number) for number in CITATION_PATTERN.findall(stripped)}
        result.append((stripped, numbers))
    return result


def check_citation_support(answer: str, sources: list[Source]) -> list[SupportProblem]:
    """校验"回答是否被它引用的片段支撑"。返回问题列表（空 = 没发现问题）。

    ============================ 三条规则与它们各自的误报边界 ============================
    ① **数字不支撑**：回答里出现了一个数字，而**它引用的那些片段里都没有**这个数字。
       ⚠️ 误报边界：模型可能做了**合理换算**（"10 万元"说成"100000 元"）——
       所以归一化必须做全（见 `_normalize`）；
       也可能是**来源之外的常识**（"一年 12 个月"）—— 这类会误报，
       所以 `detail` 里要写清"在哪句、什么数字、被引来源里没有"，
       **让人能一眼判断是不是误报**，而不是直接判死。

    ② **否定翻转**：同一句里，来源有否定词而回答只有肯定词（或反之）。
       制度问答里"不得"写成"可以"是**最危险的错误**，值得单独一类。

    ③ **引用错位**：整段回答里有引用，但**某一句有断言却没有任何引用，
       且它离最近的引用超过一句**。这条比前两条弱，所以判定条件写得保守
       （只在"整段有引用、这一句明显孤立"时报）。

    ⚠️ **本函数的结论只用于"提示与统计"，不用于拦截**（与既有的
    `check_citation_integrity` 同一策略）：规则法有误报，
    直接拒答会让用户觉得"明明答得挺好却用不了"。
    它的价值是**让问题可见**，并为 `Faithfulness` 指标提供分子/分母。
    """
    problems: list[SupportProblem] = []
    if not answer.strip() or not sources:
        return problems

    by_number = {source.number: source for source in sources}
    # 全部来源的硬事实（用于判断"这个数字是不是来自某个来源"）
    source_facts_by_number = {
        number: extract_facts(source.content) for number, source in by_number.items()
    }

    sentences = _sentences_with_citations(answer)

    # ---- 规则 ① / ②：逐句做，因为"支撑"是句级关系 ----
    for index, (sentence, cited) in enumerate(sentences):
        if not cited:
            continue
        cited_sources = [by_number[number] for number in cited if number in by_number]
        if not cited_sources:
            continue  # 编号不存在的问题由 check_citation_integrity 报，这里不重复

        # ① 数字：回答里的每个数字，至少要能在"它引用的任一片段"里找到
        sentence_facts = extract_facts(sentence)
        supported: set[tuple[str, str | None]] = set()
        for source in cited_sources:
            supported |= source_facts_by_number[source.number]
        missing = {
            fact
            for fact in sentence_facts
            # 单位不同但数值相同，也算不支撑（"10 万"≠"10 天"）
            if fact not in supported
        }
        if missing:
            shown = "、".join(
                f"{value}{unit or ''}" for value, unit in sorted(missing, key=lambda x: x[0])
            )
            problems.append(
                SupportProblem(
                    kind=KIND_NUMBER,
                    detail=(
                        f"这句话引用了{[*cited]}，但其中的数字「{shown}」"
                        f"在被引用的片段里找不到（可能是模型改写、也可能是来源里确实没有）"
                    ),
                    sentence_index=index,
                )
            )

        # ② 否定翻转（**必须作用域匹配**，见 `_negation_flip`）
        cited_text = " ".join(source.content for source in cited_sources)
        if _negation_flip(sentence, cited_text):
            problems.append(
                SupportProblem(
                    kind=KIND_NEGATION,
                    detail=(
                        f"被引用的片段里有否定表述（如「{_first_negation(cited_text)}」），"
                        f"而这句话用肯定语气说了**同一个对象** —— "
                        f"否定翻转是最危险的引用错误（「不得」写成「可以」），请人工确认"
                    ),
                    sentence_index=index,
                )
            )

    # ---- 规则 ③：引用错位（"离最近的引用太远"才算）----
    total_cited = {number for _, cited in sentences for number in cited}
    if total_cited:
        cited_indexes = [i for i, (_, cited) in enumerate(sentences) if cited]
        for index, (sentence, cited) in enumerate(sentences):
            if cited or len(sentence) <= _MIN_ASSERTION_LENGTH:
                continue
            if _is_structural_line(sentence):
                continue
            # 到**最近的**引用句的距离。取"最近"而不是"之后" ——
            # 制度问答的常见形态是「块头：[1]《办法》第 N 条」+ 紧跟一段原文摘录，
            # 摘录句本身不带编号但**属于上一个引用块**，那是合法写法。
            distance = min(abs(index - c) for c in cited_indexes)
            if distance > _MAX_CITATION_DISTANCE:
                problems.append(
                    SupportProblem(
                        kind=KIND_DISTANT,
                        detail=(
                            f"这句话有实质内容，但**上方 {distance} 句之内没有任何引用**："
                            f"「{sentence[:30]}…」—— 无法判断它依据哪一段制度"
                        ),
                        sentence_index=index,
                    )
                )

    return problems


def _is_structural_line(sentence: str) -> bool:
    """这句话是不是**结构性说明**（不是断言）。

    本地兜底回答的第一句是"根据检索到的制度原文（未调用模型，以下为原文摘录）："，
    它是在**说明这段回答的性质**，不是一条需要出处的断言。
    把它算成"无依据的断言"是误报 —— 所以按前缀识别并跳过。

    刻意用**前缀匹配**而不是"包含"：含"以下为"的句子若出现在中间，
    那多半是真的断言（"以下为不得报销的情形：……"），不该放过。
    """
    return any(sentence.startswith(prefix) for prefix in _STRUCTURAL_PREFIXES)


def _first_negation(text: str) -> str:
    """找出文本里第一个否定词（给报错信息用，找不到返回"否定表述"）。"""
    for word in _NEGATION:
        if word in text:
            return word
    return "否定表述"


#: 中文句读与断句字符。用于把"否定词的对象"切干净
#: （实测踩到：4 字窗口把 `"提交。"` 连句号一起取出来，于是"提交"匹配不上）。
_PUNCTUATION = "，。！？；：、,.!?;: \t\n　（）()「」【】《》"


def _negation_object(text: str, negation: str) -> str:
    """取否定词**后面**那个对象，**并在第一个标点处截断**（"不得提交。" → "提交"）。

    为什么要它：见 `_negation_flip` 的作用域说明。
    为什么要在标点处截断：否定对象是"动词/宾语"（提交、报销、审批），
    它不会跨过句读。不截断的话窗口会把标点一起吞进去，导致永远匹配不上
    （而那会让**规则静默失效** —— 比误报更糟，因为没人知道它没在工作）。
    """
    position = text.find(negation)
    if position < 0:
        return ""
    tail = text[position + len(negation) : position + len(negation) + _NEGATION_OBJECT_LEN]
    cut = len(tail)
    for index, char in enumerate(tail):
        if char in _PUNCTUATION:
            cut = index
            break
    return tail[:cut]


def _negation_flip(sentence: str, cited_text: str) -> bool:
    """这句话是否把被引用片段里的否定**翻成了肯定**。

    ============================ 为什么必须做作用域匹配 ============================
    早先的规则是"来源里**任意位置**有否定词 + 这句话是肯定语气 → 报问题"。
    实测立刻误报：来源是
        "没有预算的采购申请一律**不得**提交，金额超过 10 万元的应当公开招标。"
    而回答里有一句**正确复述**"金额超过 10 万元的应当公开招标" ——
    它跟"不得提交"毫无关系，却被判成否定翻转。

    **误报的校验没人看**，所以这里收紧成：**否定必须针对同一个对象** ——
    即"否定词 + 它的对象"（如"不得提交"）里那个**对象**也要出现在这句话里。
    这样"不得提交"→"可以提交"会被抓住，而"不得提交"→"10 万元应当招标"不会。

    已知边界：对象是**字符串包含**匹配，所以同义改写（"不得提交" → "不允许申报"）
    抓不到 —— 那是语义判断，本层的目标是"发现明显不支撑"，不越界。
    """
    answer_negated = any(word in sentence for word in _NEGATION)
    answer_asserted = any(word in sentence for word in _AFFIRMATION)
    if answer_negated or not answer_asserted:
        return False  # 这句话自己也是否定，或没有肯定语气 → 不算翻转

    for negation in _NEGATION:
        if negation not in cited_text:
            continue
        target = _negation_object(cited_text, negation)
        if target and target in sentence:
            return True
    return False


def support_rate(answer: str, sources: list[Source]) -> float | None:
    """**Faithfulness 的规则法近似**：有引用的句子中，**没有被发现问题**的比例。

    返回 `None` 表示**无法判定**（没有引用句，或没有来源）——
    ⚠️ 一定要区分 `None` 与 `0.0`：
    前者是"这次回答不适用这个指标"，后者是"有引用的句子全都不可信"。
    混在一起会让评测报告里的平均值失去意义（把"不适用"当成 0 分会系统性压低均值）。

    ============================ 分母与分子（口径必须能一句话说清）============================
        分母 = **有引用标记的句子数**
        分子 = 其中**没有任何支撑性问题**的句子数
        记 `KIND_DISTANT`（引用错位）**不计入** —— 它针对的是"没有引用的句子"，
        用它去扣"有引用的句子"的分是口径错位。

    这个近似有两处已知偏差，写清楚以免被当成精确指标：
      · **只检查句级引用关系**，不做语义蕴含判断（"意思对但换了说法"会被算作通过）；
      · 分母是"有引用的句子数"，所以"**整段不引用**"的回答在这里不扣分 ——
        那由 `check_citation_integrity` 的第三条规则负责，不要重复计分。
    """
    if not answer.strip() or not sources:
        return None
    sentences = _sentences_with_citations(answer)
    cited_indexes = {index for index, (_, cited) in enumerate(sentences) if cited}
    if not cited_indexes:
        return None

    problems = check_citation_support(answer, sources)
    bad_indexes = {
        problem.sentence_index
        for problem in problems
        if problem.kind != KIND_DISTANT and problem.sentence_index is not None
    }
    bad_cited = cited_indexes & bad_indexes
    return (len(cited_indexes) - len(bad_cited)) / len(cited_indexes)
