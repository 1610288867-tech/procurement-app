"""embedding 相关纯函数/纯内存测试（M11 增强）。

这一层要钉住四件事：
**① 维度注册表**：没登记的模型要**报错**，而不是让它去撞数据库；
**② 空输入不发请求**（检索里"问题没有可检索的词"是常见分支）；
**③ 形状/维度不符要报出来**，且错误信息里带模型名与地址（排查时最先要确认的两样）；
**④ 本地哈希与真模型的 `name` 必须不同** —— 名字一样就等于宣布"两个向量空间可比"，
而它们不可比：检索会静默变差。

**全程不打真网络**：用一个假的 httpx 客户端。理由不是"怕慢"，
而是"测试不该依赖外部服务的可用性"—— 那样的测试会在别人机器上红。
"""

from __future__ import annotations

import httpx
import pytest

from app.domain.embedding import (
    LOCAL_MODEL_NAME,
    MODEL_DIMENSIONS,
    VECTOR_DIM,
    resolve_dim,
)
from app.infrastructure.embeddings import (
    EmbeddingError,
    LocalHashEmbedding,
    OllamaEmbedding,
)

pytestmark = pytest.mark.unit


# ================================================================ 维度注册表


def test_local_model_is_registered_and_matches_the_column_dim() -> None:
    """本地哈希模型的维度**必须等于 `VECTOR_DIM`**（列定义用的是它）。

    这两个数一旦不一致，插入就会在数据库层报错 ——
    这条断言把"应用层常量与注册表"绑在一起。
    """
    assert MODEL_DIMENSIONS[LOCAL_MODEL_NAME] == VECTOR_DIM


def test_unregistered_model_is_rejected_with_a_helpful_message() -> None:
    """**没登记的模型要报错**，而且报错信息要告诉人去哪加。

    不报错的话错误会推迟到数据库层，而那里的报错不会说"是哪个模型给的什么维度"，
    排查要多绕好几步。
    """
    with pytest.raises(ValueError) as excinfo:
        resolve_dim("someone/unknown-model")
    message = str(excinfo.value)
    assert "MODEL_DIMENSIONS" in message, "要指出改哪里"
    assert "local-hash" in message, "要列出已登记的有哪些"


def test_registered_real_models_have_expected_dimensions() -> None:
    """真模型的维度**是模型定的**，这里钉住我们登记的值。

    钉它的理由：改错这个数（例如把 bge-m3 写成 512）不会立刻报错，
    而是在插入时才发现 —— 而那时你可能已经改了迁移。
    """
    assert resolve_dim("quentinz/bge-small-zh-v1.5") == 512
    assert resolve_dim("bge-m3") == 1024


# ================================================================ 本地哈希实现


async def test_local_hash_satisfies_the_protocol() -> None:
    """哈希实现也要满足协议（异步），这样"换实现不改调用方"。"""
    embedder = LocalHashEmbedding()
    assert embedder.name == LOCAL_MODEL_NAME
    assert embedder.dim == VECTOR_DIM
    vectors = await embedder.embed(["采购管理办法", "差旅费报销标准"])
    assert len(vectors) == 2
    assert all(len(vector) == VECTOR_DIM for vector in vectors)


async def test_local_hash_name_differs_from_real_models() -> None:
    """**哈希的名字不能与真模型相同。**

    名字是检索时的过滤条件（`knowledge_docs.embedding_model`）。
    同名就意味着"两个不可比的向量空间被当成同一个"，
    检索结果会静默变差 —— 这种 bug 没有任何报错可以依赖。
    """
    hash_name = LocalHashEmbedding().name
    assert hash_name not in {"quentinz/bge-small-zh-v1.5", "bge-m3", "nomic-embed-text"}


# ================================================================ Ollama 实现（假客户端）


class FakeResponse:
    def __init__(self, payload: dict, *, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None  # type: ignore[arg-type]
            )

    def json(self) -> dict:
        return self._payload


class FakeClient:
    """最小的假 httpx 客户端：记下所有请求，按脚本返回。

    `echo_batch=True` 时**按请求里的批量大小**返回等量向量 ——
    用来测分批（固定返回固定条数会让分批用例以"形状不对"失败，
    而那是假客户端的错、不是被测代码的错）。
    """

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = responses
        self.requests: list[dict] = []
        self.echo_batch = False

    async def post(self, url: str, json: dict) -> FakeResponse:  # noqa: A002
        self.requests.append({"url": url, "json": json})
        if self.echo_batch:
            count = len(json["input"])
            return FakeResponse({"embeddings": [[0.0] * 512 for _ in range(count)]})
        return self._responses[min(len(self.requests) - 1, len(self._responses) - 1)]

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def make_embedder(monkeypatch: pytest.MonkeyPatch, client: FakeClient) -> OllamaEmbedding:
    """造一个"用假客户端"的 OllamaEmbedding，**不打真网络**。"""
    embedder = OllamaEmbedding(
        base_url="http://127.0.0.1:11434/v1", model="quentinz/bge-small-zh-v1.5"
    )
    monkeypatch.setattr(
        "app.infrastructure.embeddings.httpx.AsyncClient", lambda **_: client
    )
    return embedder


def test_url_drops_the_openai_suffix() -> None:
    """`base_url` 传的是 OpenAI 兼容根（`.../v1`），而这里要用 `/api/embed`。

    不剥掉 `/v1` 就会请求 `/v1/api/embed` → 404，
    而那个 404 的报错完全看不出是 URL 拼装问题。
    """
    embedder = OllamaEmbedding(base_url="http://127.0.0.1:11434/v1", model="bge-m3")
    assert embedder._url == "http://127.0.0.1:11434/api/embed"  # noqa: SLF001


async def test_empty_input_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """**空输入不发请求。**

    检索里"问题里一个可检索的词都没有"是常见分支（哈希实现会返回全零向量），
    为它发一次网络请求纯属浪费，而且不同上游对空 `input` 的行为不一致。
    """
    client = FakeClient([])
    embedder = make_embedder(monkeypatch, client)
    assert await embedder.embed([]) == []
    assert client.requests == [], "不该发任何请求"


async def test_happy_path_returns_vectors_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient([FakeResponse({"embeddings": [[0.1] * 512, [0.2] * 512]})])
    embedder = make_embedder(monkeypatch, client)
    vectors = await embedder.embed(["甲", "乙"])
    assert len(vectors) == 2
    assert vectors[0][0] == 0.1 and vectors[1][0] == 0.2, "顺序必须与入参一一对应"
    assert client.requests[0]["json"]["model"] == "quentinz/bge-small-zh-v1.5"


async def test_batching_splits_large_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    """超过 batch 上限时要**分批**，而不是一次全塞进去。"""
    client = FakeClient([])
    # 按**每次请求实际的批量大小**返回等量向量 —— 假客户端如果固定返回固定条数，
    # 这条测试会以"形状不对"失败，而那是假客户端的错、不是被测代码的错。
    client.echo_batch = True
    embedder = OllamaEmbedding(
        base_url="http://127.0.0.1:11434", model="quentinz/bge-small-zh-v1.5", batch_size=2
    )
    monkeypatch.setattr(
        "app.infrastructure.embeddings.httpx.AsyncClient", lambda **_: client
    )
    vectors = await embedder.embed(["1", "2", "3"])
    assert len(client.requests) == 2, "3 条文本、batch=2 → 应当分两次请求"
    assert [len(request["json"]["input"]) for request in client.requests] == [2, 1]
    assert len(vectors) == 3, "分批之后仍要返回 3 条、顺序对应"


async def test_http_failure_becomes_embedding_error_with_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 失败要翻成 `EmbeddingError`，**并把模型名与地址带进去**。

    原始异常（ConnectError / ReadTimeout）里一个都看不出"是哪个模型、哪个地址"，
    而这两样正是排查时最先要确认的。
    """
    client = FakeClient([FakeResponse({}, status=500)])
    embedder = make_embedder(monkeypatch, client)
    with pytest.raises(EmbeddingError) as excinfo:
        await embedder.embed(["甲"])
    message = str(excinfo.value)
    assert "quentinz/bge-small-zh-v1.5" in message
    assert "/api/embed" in message


async def test_shape_mismatch_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """返回的条数与入参不符 → 报错（否则会静默错位：把甲的向量当成乙的）。"""
    client = FakeClient([FakeResponse({"embeddings": [[0.1] * 512]})])
    embedder = make_embedder(monkeypatch, client)
    with pytest.raises(EmbeddingError) as excinfo:
        await embedder.embed(["甲", "乙"])
    assert "形状不对" in str(excinfo.value)


async def test_dimension_mismatch_is_reported_with_both_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """维度不符要在**应用层**报出来，并说清"实得多少、期望多少"。

    等数据库报错的话，PostgreSQL 只会说 `expected 512 dimensions, not 256`，
    不会告诉我们"是哪个模型"。而这条错误信息里两样都有。
    """
    client = FakeClient([FakeResponse({"embeddings": [[0.1] * 256]})])
    embedder = make_embedder(monkeypatch, client)
    with pytest.raises(EmbeddingError) as excinfo:
        await embedder.embed(["甲"])
    message = str(excinfo.value)
    assert "256" in message and "512" in message
    assert "MODEL_DIMENSIONS" in message
