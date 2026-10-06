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
from collections.abc import AsyncIterator

import httpx

from app.domain.grounded_answer import NO_GROUNDING_ANSWER, Source

#: 远端调用的默认超时（秒）。分两段：连上要快，读流可以慢。
_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 60.0


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

    async def stream(
        self, *, system_prompt: str, user_prompt: str, sources: list[Source]
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
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout = httpx.Timeout(timeout, connect=_CONNECT_TIMEOUT)

    async def stream(
        self, *, system_prompt: str, user_prompt: str, sources: list[Source]
    ) -> AsyncIterator[str]:
        payload = {
            "model": self._model,
            "stream": True,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
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
                text = _parse_sse_line(line)
                if text:
                    yield text


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
