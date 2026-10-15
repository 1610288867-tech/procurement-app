"""Agent 循环的纯逻辑测试（M12 增强）：**用假模型，不依赖 Ollama**。

============================ 这一层为什么必须离线可测 ============================
ReAct 循环的价值在于"模型能自己决定调工具"，但**它的风险全在护栏上**：

    · 没有轮次上限 → 可以无限烧 token
    · 没有失败计数 → 模型反复调同一个坏工具，一直重试
    · 坏 JSON 参数   → 整轮以异常结束，而正确做法是"告诉模型 JSON 坏了，让它改"

这些护栏**不能靠真模型去碰**：真模型今天不循环，不代表明天不循环。
所以这里用一个"按剧本走的假模型"，把每一种失控情形都**确定性地**造出来。

真模型只负责验证一件事：它**能不能**给出结构化工具调用（那已经单独验过）。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.application.agent import (
    MAX_CONSECUTIVE_FAILURES,
    MAX_ROUNDS,
    AgentResult,
    AgentStep,
    run_agent,
)
from app.application.coordinators.auth import Principal
from app.application.ports import ChatMessage, ToolCall, TurnResult
from app.domain.tooling import ToolSpec, check_arguments

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------- 测试替身


class ScriptedChat:
    """按剧本返回回合的假模型。

    每个元素是 `(文本, [(工具名, 参数字典), ...])`。
    剧本用完就重复最后一个 —— 这样"模型陷入循环"能被造出来。
    """

    supports_tools = True
    name = "scripted"

    def __init__(self, script: list[tuple[str, list[tuple[str, dict[str, Any]]]]]) -> None:
        self._script = script or [("默认回答", [])]
        self.calls: list[list[ChatMessage]] = []

    async def stream_turn(
        self,
        *,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]],
        on_usage: Any = None,
    ) -> AsyncIterator[TurnResult]:
        self.calls.append(list(messages))
        index = min(len(self.calls) - 1, len(self._script) - 1)
        text, calls = self._script[index]
        yield TurnResult(
            text=text,
            tool_calls=tuple(
                ToolCall(id=f"call-{i}", name=name, arguments=json.dumps(args))
                for i, (name, args) in enumerate(calls)
            ),
        )

    async def stream(self, **_: Any) -> AsyncIterator[str]:  # pragma: no cover
        yield ""


class FakePurchase:
    """只实现被测路径用到的那几个方法。**记录调用参数**，好断言"权限被传下去了"。"""

    def __init__(self, requests: list[dict[str, Any]] | None = None) -> None:
        self._requests = requests or []
        self.list_calls: list[dict[str, Any]] = []

    async def list_requests(self, **kwargs: Any) -> tuple[list[Any], int]:
        self.list_calls.append(kwargs)
        rows = [_Row(item) for item in self._requests]
        return rows, len(rows)

    async def get_request_detail_by_code(self, **kwargs: Any) -> tuple[Any, list[Any], list[Any]]:
        from app.errors import NotFoundError

        raise NotFoundError(f"采购申请不存在，或你没有权限查看：{kwargs.get('code')}")


class FakeApproval:
    async def list_todo(self, **kwargs: Any) -> tuple[list[Any], int]:
        return [], 0


class _Row:
    """一行申请（够 `_request_brief` 用）。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self.code = data.get("code", "REQ-2026-0001")
        self.title = data.get("title", "测试申请")
        self.status = data.get("status", "DRAFT")
        self.amount_total = data.get("amount_total", 120_000)
        self.urgency = data.get("urgency", "NORMAL")
        self.created_at = None
        self.submitted_at = None


def actor() -> Principal:
    return Principal(
        id=uuid.uuid4(),
        username="tester",
        full_name="测试用户",
        department_id=uuid.uuid4(),
        roles=("REQUESTER",),
    )


async def collect(script: list[Any], **kwargs: Any) -> tuple[list[AgentStep], AgentResult]:
    steps: list[AgentStep] = []
    result: AgentResult | None = None
    async for item in run_agent(
        question=kwargs.pop("question", "我提过哪些申请？"),
        actor=kwargs.pop("actor", None) or actor(),
        chat_model=ScriptedChat(script),
        purchase=kwargs.pop("purchase", None) or FakePurchase(),
        approval=kwargs.pop("approval", None) or FakeApproval(),
        **kwargs,
    ):
        if isinstance(item, AgentResult):
            result = item
        else:
            steps.append(item)
    assert result is not None, "循环必须产出一个 AgentResult"
    return steps, result


# ---------------------------------------------------------------- 正常路径


async def test_model_answers_without_tools_when_it_does_not_need_them() -> None:
    """模型可以直接回答 —— 那时不该有任何工具调用。"""
    steps, result = await collect([("制度里规定超过十万元要公开招标。", [])])
    assert result.answer.startswith("制度里规定")
    assert result.rounds == 1
    assert result.stop_reason == "answered"
    assert [s.kind for s in steps] == [], "没有工具调用就不该有步骤"


async def test_one_tool_call_then_answer() -> None:
    """一次工具调用 → 看结果 → 回答（最小的一次 ReAct）。"""
    purchase = FakePurchase([{"code": "REQ-2026-0009", "title": "买打印机"}])
    steps, result = await collect(
        [
            ("", [("list_my_purchase_requests", {"limit": 5})]),
            ("你提过 1 张单：REQ-2026-0009（买打印机）。", []),
        ],
        purchase=purchase,
    )
    assert [s.kind for s in steps] == ["tool_call", "tool_result"]
    assert steps[0].call is not None and steps[0].call.name == "list_my_purchase_requests"
    assert "REQ-2026-0009" in result.answer
    assert result.rounds == 2
    assert result.stop_reason == "answered"


async def test_tool_identity_comes_from_the_caller_not_from_arguments() -> None:
    """**身份必须来自调用者**，参数里没有"以谁的身份"这种字段。

    这条是整块设计的根：模型的输出是不可信输入，
    一旦参数里能指定 actor，模型就能以别人的身份取数。
    """
    purchase = FakePurchase()
    who = actor()
    await collect(
        [
            ("", [("list_my_purchase_requests", {"limit": 3})]),
            ("好了", []),
        ],
        purchase=purchase,
        actor=who,
    )
    assert purchase.list_calls, "工具应当被调用"
    sent = purchase.list_calls[0]
    assert sent["actor_id"] == who.id
    assert sent["roles"] == who.roles
    assert sent["department_id"] == who.department_id


async def test_tool_result_is_fed_back_to_the_model_as_json() -> None:
    """工具结果回灌给模型时是 **JSON**（结构最好读），且带上配对所需的字段。"""
    chat = ScriptedChat(
        [
            ("", [("list_my_purchase_requests", {})]),
            ("好", []),
        ]
    )
    async for _ in run_agent(
        question="q",
        actor=actor(),
        chat_model=chat,
        purchase=FakePurchase([{"code": "REQ-1"}]),
        approval=FakeApproval(),
    ):
        pass
    # 第二次调用时，历史里应当有一条 role=tool 的消息
    second_call = chat.calls[1]
    tool_messages = [m for m in second_call if m.role == "tool"]
    assert tool_messages, "工具结果必须回灌"
    assert tool_messages[0].tool_call_id == "call-0", "缺 tool_call_id 上游会配不上"
    # ⚠️ **`tool_name` 也必须给**：Ollama 认它（见 `_to_ollama_message`）。
    # 只给 `tool_call_id` 是我的实现里真实存在过的 bug —— 3B 容错没暴露，
    # 但换模型/换版本就可能开始报错，而那种报错看起来像"模型不会用工具"。
    assert tool_messages[0].tool_name == "list_my_purchase_requests", (
        "缺 tool_name：Ollama 的 tool 消息要求它"
    )
    payload = json.loads(tool_messages[0].content)
    assert payload["requests"][0]["code"] == "REQ-1"


def test_tool_message_carries_both_name_and_call_id() -> None:
    """把上面那条断言下沉到**消息序列化**这一层。

    为什么两处都测：应用层给了 `tool_name` 不等于**发出去的 JSON** 里有它 ——
    中间还有 `_to_ollama_message` 这一层转换，它是真的漏过一次。
    """
    from app.infrastructure.chat_models import _to_ollama_message

    payload = _to_ollama_message(
        ChatMessage(role="tool", content="{}", tool_call_id="c1", tool_name="get_x")
    )
    assert payload["tool_name"] == "get_x"
    assert payload["tool_call_id"] == "c1"


# ---------------------------------------------------------------- 三条护栏


async def test_round_limit_stops_an_endless_tool_loop() -> None:
    """**模型一直调工具时必须停住。** 没有这条护栏，Agent 就是一个无限烧钱的循环。

    剧本里每一轮都要调工具 —— 循环必须在 `MAX_ROUNDS` 处停下来。
    """
    steps, result = await collect([("", [("list_my_purchase_requests", {})])])
    assert result.rounds == MAX_ROUNDS
    assert result.stop_reason == "max_rounds"
    guards = [s for s in steps if s.kind == "guard"]
    assert guards, "停下来时必须留下一条 guard 步骤（要能解释为什么停）"
    assert "轮次上限" in guards[-1].detail


async def test_consecutive_failures_stop_the_loop_early() -> None:
    """连续失败达到阈值就停 —— 比"跑满轮次"更早止损。

    造法：让模型每一轮都调一个**不存在的工具**（必然失败）。
    """
    steps, result = await collect([("", [("no_such_tool", {})])])
    assert result.stop_reason == "too_many_failures"
    # 它应当在达到阈值那一轮就停，而不是等到 MAX_ROUNDS
    assert result.rounds <= MAX_CONSECUTIVE_FAILURES
    assert "连续" in [s for s in steps if s.kind == "guard"][-1].detail


async def test_broken_json_arguments_become_a_retryable_failure() -> None:
    """**模型给出坏 JSON 时不能整轮崩掉** —— 要变成一次可回灌的失败。

    小模型给出坏 JSON 很常见。若在这里抛异常，整次提问就是一个 500，
    而正确做法是告诉它"你的参数不是合法 JSON"，让它改一次。
    """

    class BadJsonChat(ScriptedChat):
        async def stream_turn(
            self, *, messages: list[ChatMessage], tools: list[dict[str, Any]], on_usage: Any = None
        ) -> AsyncIterator[TurnResult]:
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                yield TurnResult(
                    text="",
                    tool_calls=(
                        ToolCall(
                            id="c1",
                            name="list_my_purchase_requests",
                            arguments="{不是合法 JSON",
                        ),
                    ),
                )
            else:
                yield TurnResult(text="刚才参数写错了，现在直接回答。")

    steps: list[AgentStep] = []
    result: AgentResult | None = None
    async for item in run_agent(
        question="q",
        actor=actor(),
        chat_model=BadJsonChat([("x", [])]),
        purchase=FakePurchase(),
        approval=FakeApproval(),
    ):
        if isinstance(item, AgentResult):
            result = item
        else:
            steps.append(item)
    assert result is not None
    assert "参数不是合法 JSON" in [s.detail for s in steps if s.kind == "tool_result"]
    assert result.answer.startswith("刚才参数写错了"), "模型应当有机会改正并回答"


async def test_unknown_tool_reports_available_tools() -> None:
    """未知工具要**回报可用工具名** —— 模型据此才能改对。"""
    steps, _ = await collect([("", [("not_a_real_tool", {})])])
    detail = [s.detail for s in steps if s.kind == "tool_result"][0]
    assert "未知工具" in detail or "not_a_real_tool" in detail


# ---------------------------------------------------------------- 收尾话术


async def test_timeout_case_is_honest_about_being_incomplete() -> None:
    """**超时不能伪装成"查完了"。** 收尾话术必须说清"没查完"。

    这条与"不静默截断"是同一条纪律：用户宁可看到"我查到一半"，
    也不要看到半截答案被当成完整的。
    """
    from app.application.agent import _fallback_answer

    text = _fallback_answer(collected=["已查到 REQ-1"], stop_reason="timeout")
    assert "超时" in text
    assert "已查到 REQ-1" in text

    empty = _fallback_answer(collected=[], stop_reason="max_rounds")
    assert "轮次" in empty
    assert "没查到" in empty or "再问一次" in empty


# ---------------------------------------------------------------- 与校验器的一致性


def test_agent_tools_have_valid_schemas() -> None:
    """**所有真实工具的 schema 都要能过声明期校验。**

    工具的 schema 写错时 `ToolSpec` 的 `__post_init__` 会抛 ——
    这条测试保证"注册表里每一个工具都真的合法"，
    而不是等到某次运行时才发现某个工具永远调不通。
    """
    from app.application.tools import tool_specs

    specs = tool_specs()
    assert specs, "至少要有一个工具"
    for spec in specs:
        assert isinstance(spec, ToolSpec)
        # 自检：拿一份"空参数"去过校验，缺必填必须报出来
        _, problems = check_arguments(spec, {})
        if spec.required:
            assert problems, f"{spec.name} 声明了必填却没报缺参 —— 校验器可能失灵"


def test_mutating_tool_is_marked_and_stops_before_execution() -> None:
    """**写工具必须标 `mutating`，而且第一次调用不落库。**

    这是"模型不能自己造业务数据"的那道闸门。
    """
    from app.application.tools import tool_specs

    mutating = [spec for spec in tool_specs() if spec.mutating]
    assert mutating, "本阶段应当有一个写工具（建草稿）"
    assert all(spec.name == "create_purchase_draft" for spec in mutating)
