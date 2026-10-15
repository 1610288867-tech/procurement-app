"""真 embedding 模型（Ollama）的接入。**与 `chat_models.py` 同一类：只负责搬运数据。**

============================ 为什么这里要写"不做静默降级" ============================
任何"模型不可用就退回哈希"的写法都是**错的**，两个理由：

    ① 向量空间会**被污染**。哈希与真模型的向量落进同一列，
       而余弦距离在它们之间没有意义 —— 检索结果会静默变差，
       表现是"有时候答得对、有时候答得离谱"，极难定位。
    ② 检索是按 `knowledge_docs.embedding_model` **过滤**的
       （见 `knowledge_repo.current_doc_ids`），所以"退回哈希"之后
       真模型建的文档会**一条都检索不到**，而不是"检索得差一点"。

所以模型不可用时**抛错**，由协调者翻成明确的失败（503 + 原因）。
**明确的失败比静默的劣化好。**

============================ 为什么走 `/api/embed` 而不是 `/v1/embeddings` ============================
两个端点在 Ollama 上都可用，但 `/api/embed` 支持**一次传多个输入**
（`input` 是数组，返回 `embeddings` 二维数组）——
入库时一份文档几十个片段，批量一次请求比逐条发几十次快得多。
`/v1/embeddings`（OpenAI 兼容）也支持批量，但它对 `input` 的形状更挑剔，
而这里不需要"与非 Ollama 服务通用"（那是 `OpenAiCompatibleChat` 的职责）。
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from app.domain.embedding import (
    LOCAL_MODEL_NAME,
    VECTOR_DIM,
    embed_text,
    resolve_dim,
)

#: 一次请求最多送多少段文本。
#: 取 32：段均 600 字，32 段约 2 万字，在模型上下文之内；
#: 再大一些收益有限（瓶颈在网络往返次数，不在单次体积），
#: 而万一某段异常变长，batch 越大越容易整批失败（整批失败就要重试全部）。
DEFAULT_BATCH_SIZE = 32


class EmbeddingError(RuntimeError):
    """embedding 模型调用失败。**刻意与"检索不到"区分开**。

    调用方据此给出 503 而不是"查不到相关规定"——
    后者会让用户以为知识库里没有这条制度，而事实是**服务坏了**。
    """


class LocalHashEmbedding:
    """把 `domain/embedding.embed_text` 包成 `EmbeddingPort`。

    ============================ 它存在，但**不该被静默使用** ============================
    哈希实现是纯函数、离线可用，所以它是"没装模型时也能把链路跑通"的退路。
    但要注意两件事：

      ① **它没有语义能力**（见 `domain/embedding.py` 文件头）：靠字面重合，
         "多少钱要招标"与"采购金额超过十万元须公开招标"几乎检索不到；
      ② 它的向量与真模型的向量**不可比**，混用会让检索静默变差。

    所以它由**配置显式选择**，而不是在"模型调用失败"时自动顶上 ——
    见 `OllamaEmbedding` 模块头的说明。
    """

    def __init__(self) -> None:
        self.name = LOCAL_MODEL_NAME
        self.dim = VECTOR_DIM

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        # 纯函数、瞬间返回；写成 async 只是为了满足协议（换实现不改调用方）
        return [embed_text(text) for text in texts]


class OllamaEmbedding:
    """通过 Ollama 的 `/api/embed` 算向量。

    与 `OpenAiCompatibleChat` 一样，**把 URL 拼装与超时收在这里**，
    上层只认 `EmbeddingPort` 协议。
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout: float = 120.0,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        #: `base_url` 传的是 OpenAI 兼容根（`http://127.0.0.1:11434/v1`），
        #: 而这里要用的是 `/api/embed` —— 所以把结尾的 `/v1` 去掉再拼。
        #: 不这么做的话会请求 `/v1/api/embed`，报 404，而错误信息看不出是拼装问题。
        root = base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[: -len("/v1")]
        self._url = f"{root}/api/embed"
        self._model = model
        self._timeout = timeout
        self._batch_size = batch_size
        #: 向量空间标识：用模型名本身。
        #: **不带维度**也可以，因为模型名已经唯一确定了一个向量空间；
        #: 但换模型时旧向量必须被视为失效 —— 这一点由按名过滤保证。
        self.name = model
        self.dim = resolve_dim(model)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量算向量。返回顺序与入参**一一对应**。

        空输入直接返回空列表，**不发请求**：检索里"问题一个词都没有"是常见分支，
        为它发一次网络请求纯属浪费（而且上游对空 input 的行为各不一样）。
        """
        if not texts:
            return []

        vectors: list[list[float]] = []
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for start in range(0, len(texts), self._batch_size):
                batch = list(texts[start : start + self._batch_size])
                vectors.extend(await self._embed_batch(client, batch))
        return vectors

    async def _embed_batch(
        self, client: httpx.AsyncClient, batch: list[str]
    ) -> list[list[float]]:
        try:
            response = await client.post(
                self._url, json={"model": self._model, "input": batch}
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # **把模型名与地址带进错误信息**：这两样是排查时最先要确认的，
            # 而原始异常（ConnectError / ReadTimeout）里一个都没有。
            raise EmbeddingError(
                f"调用 embedding 模型失败（{self._model} @ {self._url}）：{exc}"
            ) from exc

        payload = response.json()
        embeddings = payload.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(batch):
            raise EmbeddingError(
                f"embedding 模型返回的形状不对（{self._model}）："
                f"期望 {len(batch)} 条向量，实际 {type(embeddings).__name__}"
                f"{len(embeddings) if isinstance(embeddings, list) else ''}"
            )

        # 维度校验**在这里做**，而不是等数据库报错：
        # `vector(N)` 的 N 是列定义的一部分，维度不符时 PostgreSQL 的报错
        # 不会告诉我们"是哪个模型给的什么维度"，排查要多绕好几步。
        for index, vector in enumerate(embeddings):
            if not isinstance(vector, list) or len(vector) != self.dim:
                got = len(vector) if isinstance(vector, list) else type(vector).__name__
                raise EmbeddingError(
                    f"embedding 维度不符（{self._model}）：第 {index} 条是 {got} 维，"
                    f"而模型登记的是 {self.dim} 维。"
                    f"登记表见 domain/embedding.py 的 MODEL_DIMENSIONS。"
                )
        return embeddings
