"""生成回答的两条路（M11）：**远端模型**（可选，走 OpenAI 兼容的流式接口）
与**本地兜底**（不联网、永远可用）。

============================ 为什么必须有一个"本地兜底" ============================
这个项目是**个人学习项目**，默认跑在本地、没有 API 密钥。如果只有远端一条路：

    · 没配密钥 → 整个助手直接不可用，连"检索到了哪些片段"都演示不了；
    · 配了密钥 → 每一次自检/回归都可能花钱，于是没人敢跑测试。

所以默认是本地兜底：它**不做生成**，只把检索到的最相关片段整理成一段
带引用的回答（"抽取式"而非"生成式"）。它显然不如模型好，但它让
**检索 → 阈值判定 → 引用 → 校验 → 流式输出** 这五件事都能被真实验证。

配置里没给密钥时用兜底，给了就自动切远端 —— 界面与流式协议完全一样，
所以"换模型"这件事不影响其它任何一层。

============================ 兜底为什么也要标引用编号 ============================
因为它产出的回答会走**同一套** `check_citation_integrity` 校验。
如果兜底不标编号，那么每次"未调用模型"的回答都会被记成"来源校验未通过"，
校验日志立刻变成噪音 —— 而这条校验存在的意义就是"让问题可见"。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from app.application.ports import (
    ChatMessage,
    ModelUsage,
    ToolCall,
    TurnResult,
)
from app.domain.grounded_answer import NO_GROUNDING_ANSWER, Source

#: 远端调用的默认超时（秒）。分两段：连上要快，读流可以慢。
_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 60.0

#: 工具调用等待的超时。**比纯文本长得多**：Agent 回合要先让模型"想"再决定调什么，
#: 而纯文本是可以边想边吐的。给短了的表现是"工具调用莫名其妙地失败"，
#: 而且失败点看起来像网络问题。
_TOOL_READ_TIMEOUT = 180.0


class LocalExtractiveChat:
    """本地兜底：把检索到的最相关片段整理成带引用的回答。**不联网。**

    它做三件事（顺序有意义）：

        1. 按相似度从高到低排片段；
        2. 取最相关的几条，**原文引用**它们的句子（不改写 ——
           改写就不是"制度原文"了，而这一层的定位是"没有模型时的可用兜底"）；
        3. 每条前面标上 `[n]`，让回答能通过 `check_citation_integrity` 校验。

    ⚠️ 它的输出**不是**对问题的回答，而是"制度里最相关的原文"。
    界面上要说明这一点（`used_model=false`），否则用户会以为模型答得很差。
    """

    name = "local-extractive"
    #: **不支持工具调用**：它是抽取式兜底，不会推理、也不会决定调工具。
    #: 边界层据此把 Agent 路径关掉（走它就是走错了路，见 stream_turn）。
    supports_tools = False

    async def stream_turn(
        self,
        *,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]],
        on_usage: Callable[[ModelUsage], None] | None = None,
    ) -> AsyncIterator[TurnResult]:
        """**本地兜底不支持工具调用** —— 显式报错，不静默返回空。

        为什么不让它"返回一句普通回答"糊过去：那会让 Agent 循环以为
        "模型决定不调工具"，于是用户看到的是"助手没有去查"，
        而真相是**这个模型根本不会用工具** —— 两者的排查方向完全相反。

        调用方（边界层 / 协调者）应该在**进入循环之前**就用 `supports_tools`
        判断掉，走到这里说明那层判断漏了，所以要让它**响亮地失败**。
        """
        raise NotImplementedError(
            "本地抽取式兜底不支持工具调用；Agent 路径需要配一个支持 tools 的模型"
            "（见 scripts/ollama.ps1）"
        )
        # 让类型检查知道这是个异步生成器（函数体里必须有 yield）
        yield TurnResult()  # pragma: no cover

    async def stream(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        sources: list[Source],
        on_usage: Callable[[ModelUsage], None] | None = None,
    ) -> AsyncIterator[str]:
        if not sources:
            yield NO_GROUNDING_ANSWER
            return

        ranked = sorted(sources, key=lambda source: source.score, reverse=True)
        lines = ["根据检索到的制度原文（未调用模型，以下为原文摘录）："]
        for source in ranked[:3]:
            lines.append(f"\n[{source.number}]《{source.doc_title}》（第 {source.doc_version} 版）")
            lines.append(_first_sentence(source.content))
        yield "\n".join(lines)
        # **不报用量**：这里没有任何"模型 token"可言（没调用模型）。
        # 报一个 0 会被误读成"这次调用消耗了 0 token"，而事实是"没有这次调用"。
        # 代价计量要看 `used_model` 字段，不能看 token 数是否为 0。


def _first_sentence(content: str, limit: int = 160) -> str:
    """取片段的第一句（到句末标点为止），太长就截断。

    只取第一句而不是整段：片段有 600 字，三条拼起来近 2000 字，
    而"兜底"的定位是让人**快速看到制度怎么说**，不是把原文倒出来。
    截断处补省略号，避免看起来像句子说完了。
    """
    cleaned = content.strip()
    for marker in ("。", "；", "！", "？", "\n"):
        position = cleaned.find(marker)
        if 0 < position < limit:
            return cleaned[: position + 1]
    return cleaned[:limit] + ("……" if len(cleaned) > limit else "")


class OpenAiCompatibleChat:
    """远端模型：**OpenAI 兼容的流式接口**（`/chat/completions` + `stream=true`）。

    为什么按"OpenAI 兼容"写而不是绑某一家：这样一套代码能对接
    DashScope 兼容模式、本地 vLLM/Ollama、以及任何同类服务，
    换供应商只改 `base_url` 与模型名两个配置。

    ============================ 关于"没测过"这件事，如实说明 ============================
    本机**没有可用的密钥**，所以这条路径的**网络调用没有被真实执行过**。
    它的正确性依据只有两条：① 协议是公开且稳定的（SSE + `data: [DONE]`）；
    ② 解析逻辑对"分片不完整""非 JSON 行""缺少 choices"这些情况都有明确处理。
    这一点写在 `PROGRESS.md` 的真债里，不假装它已经验证过。

    ⚠️ 但**上层不受它影响**：`decide_grounding` 与 `check_citation_integrity`
    都在它之外，换模型不会绕过任何一条校验。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = _READ_TIMEOUT,
    ) -> None:
        self.name = model
        root = base_url.rstrip("/")
        self._url = root + "/chat/completions"
        #: 原生 `/api/chat`（**工具调用走它** —— 见 `stream_turn` 的说明）。
        #: 从 OpenAI 兼容根推出来（`http://host:11434/v1` → `http://host:11434/api/chat`）。
        self._chat_url = (root[: -len("/v1")] if root.endswith("/v1") else root) + "/api/chat"
        self._api_key = api_key
        self._model = model
        #: 支持工具调用（走原生 /api/chat，见 stream_turn）。
        #: 注意这**不代表**它这次会调工具 —— 那由模型自己决定。
        self.supports_tools = True
        self._timeout = httpx.Timeout(timeout, connect=_CONNECT_TIMEOUT)
        #: 工具回合用**更长的读超时**（见 `_TOOL_READ_TIMEOUT` 的说明）
        self._tool_timeout = httpx.Timeout(_TOOL_READ_TIMEOUT, connect=_CONNECT_TIMEOUT)

    async def stream_turn(
        self,
        *,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]],
        on_usage: Callable[[ModelUsage], None] | None = None,
    ) -> AsyncIterator[TurnResult]:
        """走 Ollama 原生 `/api/chat` 跑一个回合（**唯一支持工具调用的路径**）。

        ============================ 为什么这里用原生端点而不是 OpenAI 兼容端点 ============================
        `stream()` 走的是 `/v1/chat/completions`（OpenAI 兼容）。
        但工具调用的**分片格式**各家差异很大：OpenAI 的 SSE 把 `tool_calls`
        按 `index` 拆到多个 delta 里、`arguments` 是**分片拼接的 JSON 字符串**；
        而 Ollama 原生 `/api/chat` 是 NDJSON、实测**一帧就给出完整调用**、
        `arguments` 是**对象**。

        走原生让聚合逻辑简单得多（不必拼接 JSON 片段），代价是"只支持 Ollama"——
        而这是本项目已经接受的取舍（本地模型就是 Ollama）。
        真接云端时应当为它写**另一个实现**，而不是把这里改成"猜格式"。

        ⚠️ **两种 `arguments` 形状都接受**：实测 Ollama 给对象，
        而文档与其它实现按约定给字符串。这里统一成"原始 JSON 字符串"，
        让下游只面对一种形状。
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "stream": True,
            "messages": [_to_ollama_message(message) for message in messages],
            "options": {"temperature": 0.2},
        }
        if tools:
            payload["tools"] = tools

        text_parts: list[str] = []
        #: `index` → 已聚合的片段。**按 index 归并而不是"只取第一个"**：
        #: 协议允许多帧分开送同一个调用（第一帧给名字、后续帧给参数），
        #: "只取第一个"在那种情况下会拿到**空参数**，而它不报错 ——
        #: 只会让工具收到空参数、然后校验失败，看起来像"模型不会用工具"。
        pending: dict[int, dict[str, Any]] = {}

        async with (
            httpx.AsyncClient(timeout=self._tool_timeout) as client,
            client.stream("POST", self._chat_url, json=payload) as response,
        ):
            response.raise_for_status()
            async for line in response.aiter_lines():
                frame = _parse_ndjson(line)
                if frame is None:
                    continue
                message = frame.get("message")
                if isinstance(message, dict):
                    piece = message.get("content")
                    if isinstance(piece, str) and piece:
                        text_parts.append(piece)
                    _merge_tool_calls(pending, message.get("tool_calls"))
                if on_usage is not None and isinstance(frame.get("usage"), dict):
                    on_usage(_usage_from_ollama(frame))

        yield TurnResult(
            text="".join(text_parts),
            tool_calls=tuple(
                ToolCall(
                    id=str(item.get("id") or f"call_{index}"),
                    name=str(item.get("name") or ""),
                    arguments=str(item.get("arguments") or "{}"),
                )
                for index, item in sorted(pending.items())
            ),
        )

    async def stream(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        sources: list[Source],
        on_usage: Callable[[ModelUsage], None] | None = None,
    ) -> AsyncIterator[str]:
        payload = {
            "model": self._model,
            "stream": True,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            # 让上游在流的**最后一帧**附带 token 用量。
            # 不传这个开关时，OpenAI 协议下 `usage` 恒为 null（流式模式默认不返回）。
            # ⚠️ 有些网关/本地推理服务不认这个字段 —— 它们要么忽略它、
            # 要么返回 400。所以拿不到用量时我们**留空**（见 `ModelUsage`），
            # 而不是伪造一个估算值。
            "stream_options": {"include_usage": True},
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        async with (
            httpx.AsyncClient(timeout=self._timeout) as client,
            client.stream("POST", self._url, json=payload, headers=headers) as response,
        ):
            response.raise_for_status()
            async for line in response.aiter_lines():
                usage = _parse_usage_line(line)
                if usage is not None:
                    if on_usage is not None:
                        on_usage(usage)
                    continue
                text = _parse_sse_line(line)
                if text:
                    yield text


def _parse_usage_line(line: str) -> ModelUsage | None:
    """从一行 SSE 里解析 token 用量，没有就返回 `None`。

    ============================ 为什么要单独一个解析函数 ============================
    用量帧的形状与正文帧**不同**，而且不同服务商给的字段名有差异：

        OpenAI 流式最后一帧：`{"choices": [], "usage": {"prompt_tokens": 12, ...}}`
        但那一帧的 `choices` 是**空数组** —— 所以正文解析器（`_parse_sse_line`）
        会因为它取不到 `choices[0]` 而返回空串，用量就这么被丢掉了。
        真实事故：只看正文解析器的话，token 数永远是 0，而"永远是 0"很容易被
        当成"模型不要钱"。

    字段缺失时填 `None`（不编造）；`usage` 整个不存在时返回 `None`（这一行不是用量帧）。
    """
    if not line or not line.startswith("data:"):
        return None
    data = line[len("data:") :].strip()
    if not data or data == "[DONE]":
        return None
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return None
    usage = parsed.get("usage")
    if not isinstance(usage, dict):
        return None

    def as_int(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return ModelUsage(
        prompt_tokens=as_int(usage.get("prompt_tokens")),
        completion_tokens=as_int(usage.get("completion_tokens")),
        model=parsed.get("model") if isinstance(parsed.get("model"), str) else None,
    )


def _parse_ndjson(line: str) -> dict[str, Any] | None:
    """一行 NDJSON → 字典。**不认识的行走 `None`**（忽略，不抛）。

    与 `_parse_sse_line` 的分工：SSE（`data: {...}`）用于 OpenAI 兼容端点，
    NDJSON（一行一个 JSON）用于 Ollama 原生 `/api/chat`。

    坏行忽略而不是抛异常，理由与 SSE 那边一致：
    **宁可少一段，也不要让整条回答消失**。真正的 HTTP 错误由
    `raise_for_status()` 在流开始前抛出，会被上层翻成明确的失败。
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _merge_tool_calls(pending: dict[int, dict[str, Any]], raw: Any) -> None:
    """把一帧里的 `tool_calls` 并进 `index → 片段` 的累积表。

    ============================ 三处必须处理的形状差异 ============================
    ① **`index` 可能缺失**：有些实现只给一个元素、不带 index。
       缺失时按 0 处理（单工具调用是最常见的情形）；
    ② **`arguments` 可能是对象**（实测 Ollama）**也可能是字符串**
       （OpenAI 约定，且会分片）。这里统一成字符串：
       对象就 `json.dumps`，字符串就原样——**并且字符串要拼接**
       （分片到达时 `"{\\"a\\":"` + `"1}"` 必须接起来，覆盖等于丢参数）；
    ③ **`name` / `id` 可能只出现在第一帧**：所以只在有值时覆盖，
       不能用后一帧的空值把已有的名字冲掉。
    """
    if not isinstance(raw, list):
        return
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        function = function if isinstance(function, dict) else {}
        index = function.get("index")
        key = index if isinstance(index, int) else position
        slot = pending.setdefault(key, {"id": "", "name": "", "arguments": ""})

        call_id = item.get("id")
        if isinstance(call_id, str) and call_id:
            slot["id"] = call_id
        name = function.get("name")
        if isinstance(name, str) and name:
            slot["name"] = name

        arguments = function.get("arguments")
        if isinstance(arguments, dict):
            # 对象形状：整体替换（它一次给全，不存在分片拼接的问题）
            slot["arguments"] = json.dumps(arguments, ensure_ascii=False)
        elif isinstance(arguments, str) and arguments:
            # 字符串形状：**拼接**而不是覆盖 —— 分片到达时覆盖会丢参数
            slot["arguments"] = str(slot["arguments"]) + arguments


def _to_ollama_message(message: ChatMessage) -> dict[str, Any]:
    """把协议消息翻成 Ollama `/api/chat` 的形状。

    ============================ `tool` 消息必须带 `tool_name`（我漏过一次）============================
    Ollama 官方文档给的回灌形状是：

        {"role": "tool", "tool_name": "get_temperature", "content": "22°C"}

    而我的第一版只发了 `tool_call_id`。实测 3B 模型容错、没暴露出来，
    但这是**真的不合规范**：换模型或换版本就可能开始报错，
    而那种报错看起来像"模型不会用工具"，排查方向完全错。
    参考 microsoft/agent-framework 的修复 PR「send the tool name on tool result messages」，
    以及 `bridle` 的要求「both ToolCallID and ToolName must be set」。

    所以这里**两个都给**：`tool_name` 是 Ollama 认的，`tool_call_id` 是 OpenAI 认的 ——
    多给一个字段不会有害，少给一个就会在某些实现上静默失败。
    """
    if message.role == "tool":
        payload: dict[str, Any] = {
            "role": "tool",
            "content": message.content,
            "tool_call_id": message.tool_call_id or "",
        }
        if message.tool_name:
            payload["tool_name"] = message.tool_name
        return payload
    payload = {"role": message.role, "content": message.content}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": _arguments_as_object(call.arguments),
                },
            }
            for call in message.tool_calls
        ]
    return payload


def _arguments_as_object(raw: str) -> Any:
    """回灌时把参数字符串还原成对象（Ollama 期望对象）。

    解析不了就**原样给字符串**：让上游自己去报错，
    比在这里静默换成 `{}` 好 —— 后者会让模型以为"参数是空的"，
    然后基于空参数继续推理，错误会一路传下去。
    """
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def _usage_from_ollama(frame: dict[str, Any]) -> ModelUsage:
    """从 Ollama 的帧里取用量。字段名与 OpenAI 不同，所以单独一个函数。"""

    def as_int(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return ModelUsage(
        prompt_tokens=as_int(frame.get("prompt_eval_count")),
        completion_tokens=as_int(frame.get("eval_count")),
        model=frame.get("model") if isinstance(frame.get("model"), str) else None,
    )


def _parse_sse_line(line: str) -> str:
    """把一行 SSE 变成要吐出的文本。**不认识的行走空字符串**（忽略，不抛异常）。

    三种必须处理好、否则会中途崩掉上游流的情况：

        `data: [DONE]`        结束标记，没有 JSON 内容；
        `: keep-alive`        注释行（有些网关会发），以冒号开头；
        `data: {...}` 坏 JSON 网关出错时会发一段 HTML/纯文本 ——
                              这时**忽略它**比让整条回答崩掉好（用户至少拿到了前半段）。

    最后一条是刻意的取舍：宁可少一段，也不要"整条回答消失"。
    真正的错误（HTTP 4xx/5xx）由 `raise_for_status()` 在流开始前抛出，
    会被上层翻成 `event: error`，**不静默截断**。
    """
    if not line or line.startswith(":"):
        return ""
    if not line.startswith("data:"):
        return ""
    data = line[len("data:") :].strip()
    if not data or data == "[DONE]":
        return ""
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return ""
    choices = parsed.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    content = delta.get("content")
    return content if isinstance(content, str) else ""
