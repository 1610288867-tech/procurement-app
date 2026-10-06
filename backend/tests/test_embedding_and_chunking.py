"""切分与本地 embedding 的纯函数测试（M11）。毫秒级。

============================ 三组最值钱的用例 ============================
**切分点要落在句号/段落处**：硬切会把一句话从中间截断。制度条文的关键信息经常
跨段（"金额超过 10 万元的采购，" + "须经总经理审批"），切断之后模型看到的依据
就是残缺的 —— 答出来的结论要么少一半条件，要么干脆说"没找到"。

**`size <= overlap` 必须拦住**：那不是"结果难看"，而是**死循环**
（步进为 0 或负）。这类参数错误一定要在入口拒绝。

**哈希要跨进程稳定**：用 Python 内置 `hash()` 的话，同一个词在重启后会落到
不同维度上 —— 表现为"昨天检索得到，今天就检索不到"，且不报任何错。
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from app.domain.chunking import (
    CHUNK_MIN_SIZE,
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    chunk_text,
    normalize_text,
)
from app.domain.embedding import (
    LOCAL_MODEL_NAME,
    VECTOR_DIM,
    embed_text,
    embedding_model_name,
    tokenize,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------- 切分


def test_short_text_is_a_single_chunk() -> None:
    chunks = chunk_text("金额超过十万元的采购须报总经理审批。")
    assert len(chunks) == 1
    assert chunks[0].chunk_no == 0
    assert chunks[0].content == "金额超过十万元的采购须报总经理审批。"


def test_empty_text_yields_no_chunks() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_long_text_is_split_into_overlapping_chunks() -> None:
    """长文本切成多片，且**相邻两片有重叠**（`start < 上一片 end`）。"""
    text = "".join(f"第{i}条 采购金额超过十万元的须报总经理审批。" for i in range(60))
    chunks = chunk_text(text)
    assert len(chunks) > 1
    assert chunks[0].start_offset == 0
    assert chunks[-1].end_offset == len(text.strip())
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert current.start_offset < previous.end_offset, "相邻片必须有重叠（否则跨段条件会被切断）"
        assert current.chunk_no == previous.chunk_no + 1


def test_chunk_boundaries_prefer_sentence_ends() -> None:
    """切分点**尽量落在句末标点上**（不硬切到句子中间）。

    断言方式：每一片（除最后一片）都以断点字符结尾。
    """
    text = "。".join(f"第{i}条 采购金额标准如下所述并且还有补充说明" for i in range(80)) + "。"
    chunks = chunk_text(text, size=200, overlap=40)
    assert len(chunks) > 3
    for chunk in chunks[:-1]:
        assert chunk.content.rstrip().endswith(("。", "\n", "；", "！", "？", ".", ";", "!", "?")), (
            f"第 {chunk.chunk_no} 片没有落在断点上：…{chunk.content[-12:]!r}"
        )


def test_offsets_allow_recovering_the_original_text() -> None:
    """用 `start`/`end` 能**从原文取回**每一片（这是存下标的意义）。"""
    text = "。".join(f"第{i}条 说明" for i in range(100)) + "。"
    stripped = text.strip()
    for chunk in chunk_text(text, size=120, overlap=20):
        assert stripped[chunk.start_offset : chunk.end_offset] == chunk.content


def test_a_short_tail_is_merged_into_the_previous_chunk() -> None:
    """过短的尾片**并入上一片**，不单独成片。

    小片在向量检索里天然吃亏：它的向量由很少的词构成，相似度波动大，
    容易挤掉一个本该属于完整片的位置。
    """
    # 构造一个"刚好剩一点点"的长度：120 字的片长 + 一点点尾巴
    text = "甲" * 130
    chunks = chunk_text(text, size=120, overlap=20)
    assert all(chunk.length >= CHUNK_MIN_SIZE for chunk in chunks), [c.length for c in chunks]


@pytest.mark.parametrize(("size", "overlap"), [(100, 100), (100, 150), (0, 0), (100, -1)])
def test_invalid_size_and_overlap_are_refused(size: int, overlap: int) -> None:
    """`size <= overlap` 会**死循环**，所以必须拦住（不是"结果难看"）。

    `(0, 0)` 与 `(100, -1)` 同样拒绝：片长必须为正、重叠不能为负。
    """
    with pytest.raises(ValueError):
        chunk_text("任意文本", size=size, overlap=overlap)


def test_defaults_match_the_documented_range() -> None:
    """默认参数落在计划给的区间（500–800 字带重叠）。"""
    assert 500 <= CHUNK_SIZE <= 800
    assert 0 < CHUNK_OVERLAP < CHUNK_SIZE


def test_normalize_unifies_newlines_and_collapses_blank_runs() -> None:
    """归一化只做三件事：统一换行、压掉连续空行、去掉首尾空白。

    **不删空行**：段落边界是切分点的主要依据，删了就只能硬切。
    """
    raw = "第一段\r\n\r\n\r\n\r\n第二段\r\n"
    normalized = normalize_text(raw)
    assert "\r" not in normalized
    assert "\n\n\n" not in normalized
    assert normalized.startswith("第一段")
    assert normalized.endswith("第二段")
    assert normalized == "第一段\n\n第二段"


# ---------------------------------------------------------------- 本地 embedding


def test_vector_has_the_declared_dimension() -> None:
    """维度必须等于 `VECTOR_DIM` —— 它同时是**数据库列定义**（`vector(256)`）。

    两个数字不一致时不会报错，只会让插入失败或检索静默变差。
    """
    assert len(embed_text("采购金额超过十万元须报总经理审批")) == VECTOR_DIM


def test_vector_is_l2_normalized() -> None:
    """归一化之后模长为 1，于是**余弦相似度就等于点积**（省一次开方）。"""
    vector = embed_text("采购管理办法 第十二条")
    norm = sum(value * value for value in vector) ** 0.5
    assert abs(norm - 1.0) < 1e-9


def test_empty_text_gives_a_zero_vector() -> None:
    """空文本 → **全零向量**（不是抛异常）。

    调用方要把它与"检索不到"区分开：全零向量进 SQL 之后余弦距离没有意义，
    所以上层在检索前就会把"没有词"的查询拒掉。
    """
    vector = embed_text("！？。，")
    assert len(vector) == VECTOR_DIM
    assert all(value == 0.0 for value in vector)


def test_overlapping_text_scores_higher_than_unrelated_text() -> None:
    """**字面重合**高的文本相似度更高（这是它唯一的本事）。

    ⚠️ 这条用例**不代表它能做语义检索**：它靠的是共同词，
    所以"多少钱要招标"与"采购金额超过 10 万元须公开招标"几乎不重合 ——
    那种情况要真模型。这里只验证"链路里的距离是有意义的"。
    """
    base = embed_text("采购金额超过十万元的须报总经理审批")
    same = embed_text("采购金额超过十万元的须报总经理审批")
    other = embed_text("差旅费报销标准与住宿费上限")

    def dot(left: list[float], right: list[float]) -> float:
        return sum(a * b for a, b in zip(left, right, strict=True))

    assert dot(base, same) > 0.99
    assert dot(base, same) > dot(base, other)


def test_hash_is_stable_across_processes() -> None:
    """**哈希必须跨进程稳定**（这是不用内置 `hash()` 的全部理由）。

    内置 `hash()` 带每进程随机盐：重启之后同一个词落到不同维度，
    表现为"昨天检索得到，今天就检索不到"，而且不报任何错。
    所以这里**另起一个进程**算同一个词的向量，逐位比对。
    """
    script = (
        "import json;"
        "from app.domain.embedding import embed_text;"
        "print(json.dumps(embed_text('采购金额超过十万元')))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=".",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-400:]
    import json

    other_process = json.loads(completed.stdout)
    assert other_process == embed_text("采购金额超过十万元"), (
        "同一个词在两个进程里算出的向量不一致 —— 哈希带了随机盐"
    )


def test_tokenize_splits_english_words_and_chinese_characters() -> None:
    """英文按词、中文按**字**（不引分词器：词典与版本会带来额外的不确定性）。"""
    tokens = tokenize("采购 PO2026 金额 100 万")
    assert "采购" not in tokens, "中文按字切，不出现双字词"
    assert "采" in tokens and "购" in tokens
    assert "po2026" in tokens, "英文数字整体成词，且统一小写"
    assert "100" in tokens


def test_model_name_is_recorded_with_its_dimension() -> None:
    """模型名里带维度：换维度时**一眼能看出**哪些文档的向量是旧的。"""
    assert str(VECTOR_DIM) in LOCAL_MODEL_NAME
    assert embedding_model_name() == LOCAL_MODEL_NAME
