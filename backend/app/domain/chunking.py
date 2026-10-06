"""文档切分（`domain/chunking.py`）。纯函数，不 import 任何框架。

============================ 切分粒度：500–800 字、带重叠 ============================
计划里给的起点就是"500–800 字带重叠"，这里照做，并把三个数字**变成常量**：

    CHUNK_SIZE = 600        每片目标长度（落在 500–800 之间）
    CHUNK_OVERLAP = 100     相邻两片的重叠字数
    CHUNK_MIN_SIZE = 80     短于这个长度的**尾片**并入上一片

============================ 为什么要有重叠 ============================
制度条文的关键信息经常**跨段**："金额超过 10 万元的采购，" 在一段末尾，
"须经总经理审批" 在下一段开头。不重叠的话这两句会落进不同的片，
而检索只命中其中一片 —— 模型看到的依据就残缺了，答出来的结论要么少一半条件，
要么干脆说"制度中没有找到"。

============================ 为什么尾片要并进去而不是单独留一片 ============================
切到末尾常剩一小截（比如 40 个字）。**小片在向量检索里天然吃亏**：
它的向量由很少的词构成，与查询的余弦相似度波动大，
容易挤不进 top-k，或者在 top-k 里占掉一个本该属于完整片的位置。
把过短的尾片并入上一片，比让它单独存在更稳。

============================ 切分点是"尽量落在段落/句号处" ============================
硬切会把一句话从中间截断。所以切的时候先在 `[start + CHUNK_SIZE * 0.6, start + CHUNK_SIZE]`
这个窗口里**往回找**最近的段落分隔或句末标点；找不到才硬切。
`* 0.6` 是下限：再往回找就会让片太短，反而降低信息密度。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 每片目标长度（字符）。计划给的区间是 500–800，取中值。
CHUNK_SIZE = 600
#: 相邻片的重叠长度。不重叠会让跨段的条文条件被切断（见文件头）。
CHUNK_OVERLAP = 100
#: 短于这个长度的尾片并入上一片（小片在检索里天然吃亏）。
CHUNK_MIN_SIZE = 80
#: 往回找切分点的窗口下界（占 `CHUNK_SIZE` 的比例）。
_BREAK_SEARCH_RATIO = 0.6

#: 优先在这些字符**之后**断开：段落 > 句末 > 分号。
#: 顺序有意义：段落边界比句号更"硬"，先找段落。
_BREAK_CHARS = ("\n\n", "\n", "。", "；", "！", "？", ".", ";", "!", "?")


@dataclass(frozen=True)
class Chunk:
    """一片切分结果。

    `start_offset` / `end_offset` 是**原文字符下标**（`[start, end)`）——
    计划里特别点了要存它们："出问题时你能一眼看出是『没检索到』还是『切碎了』"。
    所以这两个字段不是装饰，是排障入口。

    字段名与 `knowledge_chunks` 的两列**逐字相同**：从切分到入库要经过
    领域 → 协调者 → 仓库三层，中间任何一层换个名字（`start` / `begin` / `offset`）
    都会让读代码的人多一次心算，而这类"同一件东西三个名字"正是最容易接错的地方。
    """

    chunk_no: int
    content: str
    start_offset: int
    end_offset: int

    @property
    def length(self) -> int:
        return self.end_offset - self.start_offset


def _find_break(text: str, start: int, hard_end: int, limit: int) -> int:
    """在 `[limit, hard_end)` 里往回找最近的断点，返回**断点之后**的位置。

    找不到就返回 `hard_end`（硬切）。返回的位置一定 `> start`（否则会死循环）。
    """
    for marker in _BREAK_CHARS:
        position = text.rfind(marker, limit, hard_end)
        if position != -1:
            return position + len(marker)
    return hard_end


def chunk_text(
    text: str,
    *,
    size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
    min_size: int = CHUNK_MIN_SIZE,
) -> list[Chunk]:
    """把文本切成带重叠的片。

    三条必须成立的约束（都有测试钉着）：

        1. **片与片之间按原文顺序、首尾相接**（`chunks[i].end == chunks[i+1].start` 只在无重叠时成立，
           有重叠时 `chunks[i+1].start < chunks[i].end` —— 这是刻意的）；
        2. **拼不回原文**：重叠部分会出现两次，所以**不要试图用它重建全文** ——
           要原文请按 `start`/`end` 回查（这也是存下标的原因之一）；
        3. `size <= overlap` 会让每片都原地打转（步进为 0 或负），**必须拦住** ——
           否则这是一个死循环，而不是一个错误结果。
    """
    if size <= overlap:
        raise ValueError(f"片长 {size} 必须大于重叠 {overlap}，否则切分会原地打转")
    if overlap < 0 or size <= 0:
        raise ValueError("片长必须为正、重叠不能为负")

    stripped = text.strip()
    if not stripped:
        return []

    step = size - overlap
    chunks: list[Chunk] = []
    start = 0
    total = len(stripped)

    while start < total:
        hard_end = min(start + size, total)
        if hard_end < total:
            limit = start + int(size * _BREAK_SEARCH_RATIO)
            end = _find_break(stripped, start, hard_end, limit)
        else:
            end = total

        content = stripped[start:end]
        chunks.append(
            Chunk(
                chunk_no=len(chunks),
                content=content,
                start_offset=start,
                end_offset=end,
            )
        )

        if end >= total:
            break
        start = max(end - overlap, start + step - overlap, start + 1)

    # 尾片太短就并进上一片（并完就不再有"小片吃亏"的问题）
    if len(chunks) >= 2 and chunks[-1].length < min_size:
        tail = chunks.pop()
        previous = chunks[-1]
        merged = Chunk(
            chunk_no=previous.chunk_no,
            content=stripped[previous.start_offset : tail.end_offset],
            start_offset=previous.start_offset,
            end_offset=tail.end_offset,
        )
        chunks[-1] = merged
    return chunks


def normalize_text(raw: str) -> str:
    """入库前的文本归一化：统一换行、压掉过多空行。

    **不做**的事，以及为什么：
      - 不删空行：段落边界是切分点的主要依据，删了就只能硬切；
      - 不做全角/半角转换：制度文档里的全角标点（`。`、`（）`）是切分点的一部分，
        转成半角反而会让 `_BREAK_CHARS` 失效；
      - 不折叠空格：只把 `\\r\\n` 统一成 `\\n`、把 3 个以上连续换行压成 2 个
        （PDF 抽出来的文本经常带一长串空行，那会让切分点全是空行）。
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()
