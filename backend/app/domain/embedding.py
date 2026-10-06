"""本地 embedding：**带符号的哈希投射**（signed hashing trick）。纯函数，不 import 任何框架。

============================ 它是干什么的，以及它**不是**干什么的 ============================
它把一段文本变成一个固定长度的稠密向量，好让整条 RAG 链路（切分 → 存向量 →
按距离检索 → 拼 Prompt）能**离线跑通**，不用花 API 费用、也不需要密钥。

    ⚠️ **它没有语义能力。** 它靠的是**字面重合**：
       "多少钱要招标" 与 "采购金额超过 10 万元须公开招标"
       几乎没有共同词，所以**大概率检索不到**。

    所以：**别用它判断 RAG 设计好不好，它只能验证链路通不通。**
    真要评估效果必须换真模型（换的时候维度会变，见下面那段）。

============================ 维度必须与数据库列定义一致 ============================
`VECTOR_DIM = 256`，而 `knowledge_chunks.embedding` 是 `vector(256)` ——
**这两个数字必须一起改**：`vector(N)` 里的 N 是列定义的一部分，
换 embedding 模型几乎必然换维度，而 PostgreSQL 改维度只能新建列、重灌数据、再建索引。
所以每份文档还要在 `knowledge_docs.embedding_model` 上记下用的是哪个模型
（如 `local-hash-256`）——同一张表里混着两种模型的向量，
检索结果会**静默变差**，而且极难排查。

============================ 算法（为什么是"带符号"） ============================
    1. 切词：英文按非字母数字切、中文按**字**切（不做分词，不引词典）；
    2. 每个词用稳定哈希（`blake2b`，**不用 Python 内置 `hash()`** ——
       它每个进程的盐不同，重启后同一个词会落到不同位置，检索会莫名其妙失效）；
    3. 落到 `[0, 256)` 的某一维，按哈希的另一个 bit 决定 **+1 / -1**；
    4. 最后做 L2 归一化。

带符号这一步不能省：全部取 +1 的话，任何两段文本的向量都在同一个卦限里，
**余弦相似度普遍偏高且区分度很差**（相当于只比"词频总量"）。
带符号让不同词的贡献互相抵消，这才有点"方向"的含义。
"""

from __future__ import annotations

import hashlib
import math
import re

#: 向量维度。**与 `knowledge_chunks.embedding vector(256)` 必须一致**（见文件头）。
VECTOR_DIM = 256

#: 本地模型的标识，写进 `knowledge_docs.embedding_model`。
LOCAL_MODEL_NAME = f"local-hash-{VECTOR_DIM}"

#: 英文/数字词；中文按单字走 `_CJK`。
_WORD = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """切词：英文数字按词、中文按**字**。

    不引分词器的理由：这一层的目的是"把链路跑通"，而分词器会带来词典、
    版本、以及"同一段文本两次切分结果不同"的风险。按字切最稳，
    而且对"制度条文"这种短句文本，单字的字面重合已经能给出可用的信号。

    **大小写不敏感**（统一小写）：`Amount` 与 `amount` 应当是同一个词，
    否则同一份文档里两种写法会被当成两个不同的特征。
    """
    lowered = text.lower()
    return _WORD.findall(lowered) + _CJK.findall(lowered)


def _bucket(token: str) -> tuple[int, int]:
    """一个词 → `(维度下标, 符号)`。

    用 `blake2b` 而不是内置 `hash()`：后者带**每进程随机盐**，
    重启之后同一个词会落到不同维度上 —— 表现为"昨天还检索得到，今天就检索不到了"，
    而且不报任何错。这类"跨进程不稳定"是哈希类实现最常见的坑。

    `digest_size=8` 足够：我们是把它当**两个 32 位整数**用（下标 + 符号位），
    不是当密码学摘要。
    """
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    index = value % VECTOR_DIM
    # 用**另一段 bit** 决定符号，而不是复用 index 的奇偶 ——
    # 复用的话"下标"和"符号"会相关，抵消效果变差。
    sign = 1 if (value >> 32) & 1 else -1
    return index, sign


def embed_text(text: str) -> list[float]:
    """文本 → 长度 `VECTOR_DIM` 的**已归一化**向量。

    空文本（或全是标点）返回**全零向量**。调用方要把它与"检索不到"区分开：
    全零向量与任何向量的余弦距离都是未定义的（数据库返回 NULL 或 1），
    所以 `application` 层在检索前就会把"没有词"的查询拒掉，不让它走到 SQL。
    """
    vector = [0.0] * VECTOR_DIM
    for token in tokenize(text):
        index, sign = _bucket(token)
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]


def embedding_model_name() -> str:
    """这份向量是谁算的。写进 `knowledge_docs.embedding_model`，用于将来重灌判断。"""
    return LOCAL_MODEL_NAME
