"""最小 ReAct 循环（M12 增强）：让模型自己决定要不要调工具。

============================ 为什么是"最小"，以及它刻意不做什么 ============================
做的是：**想 → 调工具 → 看结果 → 再想 → 回答**，带两个硬护栏（轮次、总耗时）。

刻意**不做**的（每一项都会让这个阶段的复杂度翻倍，而收益要等有了真实使用数据才知道）：

    · 不做并行工具调用（模型的多个调用按顺序执行，简单且好读日志）
    · 不做工具结果的缓存（同一回合内不重复调同一个工具是模型的职责）
    · 不做"反思/自我批评"（那是另一个 prompt 的活，而它需要 A/B 数据才说得清好坏）
    · 不做长期记忆（会话历史已经在库里，那够用）

============================ 两条护栏为什么必须有 ============================
**没有轮次上限的 Agent 是一个可以无限烧 token 的循环。** 而它出问题时的样子
不是"报错"，是"一直在想" —— 用户看到的是转圈，账上是一直涨。

    轮次上限：模型反复调同一个工具（很常见：它没读懂结果）
    耗时上限：某次工具调用卡住（外部依赖慢），没有总预算就会一直等

两个都要，因为它们的失败模式不同：轮次上限防"想不完"，耗时上限防"等不完"。

============================ 工具报错不是"结束"，是"给模型一次改正的机会" ============================
工具失败的常见原因恰恰**不是**模型笨，而是：单号打错一个字、参数名记错、
那张单子确实不在他的权限范围内。这些都应该把**错误信息回灌给模型**让它改一次 ——
这正是 ReAct 比"一次性生成"强的地方。

所以循环的退出条件里 **不包括"工具报错"**：只要还有轮次，就把错误给它，让它重试。
但**要计数**：如果同一轮里连续三次工具都失败，说明它卡住了，那时要停下来
（见 `_consecutive_failures`），否则它会一直重试到轮次上限，白烧时间。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.application.coordinators.approval import ApprovalCoordinator
from app.application.coordinators.auth import Principal
from app.application.coordinators.purchase import PurchaseCoordinator
from app.application.ports import ChatMessage, ChatModelPort, ModelUsage, ToolCall
from app.application.tools import ToolOutcome, run_tool, tool_specs
from app.domain.agent_run import next_step_index, should_reuse
from app.domain.tooling import to_openai_tools

logger = logging.getLogger("procurement")

#: 一次提问里最多几次"模型→工具→模型"往返。
#: 取 4：实测"查列表 → 拿单号 → 查详情 → 回答"是 3 次，留一次余量。
#: 再多就说明模型陷入了循环，继续给它轮次只会更慢更贵。
MAX_ROUNDS = 4

#: 一次提问的总耗时上限（秒）。超过就**带着已有内容收尾**，不是报错。
#: 取 180：比一次模型调用（`_TOOL_READ_TIMEOUT`=180）略宽 ——
#: 下限是"至少让第一轮跑完"，上限是"别让用户等过 3 分钟还看不到东西"。
MAX_TOTAL_SECONDS = 180.0

#: 连续这么多次工具失败就停止（见模块头的说明）。
MAX_CONSECUTIVE_FAILURES = 3

#: Agent 的系统提示。**与制度问答那个 `SYSTEM_PROMPT` 分开**：
#: 那个的职责是"只依据给定片段回答"，而这个的职责是"决定要不要调工具"。
#: 混成一个会两头不讨好：制度问答不需要知道自己有工具，
#: 而 Agent 需要知道"能查就查、不要凭记忆答"。
AGENT_SYSTEM_PROMPT = """你是一个企业内部系统的助手，可以调用工具查询**当前用户有权查看**的真实数据。

规则：
1. **能查就查，不要凭记忆或猜测回答**。凡是涉及具体单号、金额、状态、待办的问题，先调工具。
2. 一次只调一个工具；拿到结果之后再决定下一步。
3. 如果工具返回错误（例如"没有找到"或"参数不合法"），**看错误信息改正后重试一次**。
4. 工具返回的数据里没有的信息，就明确说"系统里没有查到"，**不要编造**。
5. 用中文回答，简洁、直接给结论；引用数据时带上单号。
6. 如果用户要求做写操作（例如新建申请），先准备好内容并提醒他**需要在前端确认**，
   不要声称"已经完成"。
"""


@dataclass
class AgentStep:
    """循环里的一步（给界面/日志看，也用于测试断言）。"""

    round_no: int
    kind: str  # "tool_call" | "tool_result" | "answer" | "guard"
    detail: str
    #: 工具调用的原始信息（只在 kind == "tool_call" 时有意义）
    call: ToolCall | None = None
    #: **待人工确认的提案**（M12+ 起用于写工具的第一段）。
    #: 只在写工具预检成功时非空，形如：
    #:     {"action": "create_purchase_draft", "draft": {...}, "confirmation_token": "..."}
    #:
    #: ============================ 为什么必须给前端 ============================
    #: 写工具是**两段式**：模型只能准备，落库要人点一下。
    #: 而"点一下"需要一个界面 —— 界面需要 `draft`（显示要建什么）
    #: 与 `confirmation_token`（确认时原样回传）。
    #: 只给一句摘要文本（"待确认：建草稿「X」"）的话，用户**找不到确认的地方**，
    #: Agent 就会说"请去确认"而系统里没有任何可确认的入口（实测就是这个表现）。
    confirmation: dict[str, Any] | None = None


@dataclass
class AgentResult:
    """一次 Agent 提问的完整结果。"""

    answer: str
    steps: list[AgentStep] = field(default_factory=list)
    #: 用掉的轮次（模型回合数）。用于成本与可观测。
    rounds: int = 0
    #: 为什么停下来：`answered` / `max_rounds` / `timeout` / `too_many_failures`
    stop_reason: str = "answered"
    usage: ModelUsage | None = None
    #: 这次跑**复用了多少步**已记录的结果（0 = 全新执行）。
    #: 要发出去：续跑的成功与否，判据就是"它真的没重新执行那几步"。
    reused_steps: int = 0


def rebuild_messages(
    *,
    recorded: list[dict[str, Any]],
    history: list[ChatMessage] | None,
    question: str,
) -> list[ChatMessage]:
    """把**已记录的轨迹**还原成模型能接着跑的消息序列。

    ============================ 为什么不能只给"部分结果" ============================
    模型的每一步都依赖前一步的上下文。只把"已查到什么"拼成一段文本塞给它，
    它会**不知道那是什么时候查的、以什么参数查的**，于是可能重复查、
    也可能基于错误的前提继续。还原成原样的 `assistant`(tool_calls) + `tool` 消息，
    模型看到的就是"它自己刚才跑过的那几步"，没有解释成本。

    `detail` 里存的是**给人看的一句话**（如"列出申请 3/3 条"），
    而回灌给模型的必须是**原始 JSON**。所以这里从 `detail` 重建一个最小结构 ——
    它够模型接着推理（知道"这个工具调过了、成功了"），
    但**不是原始载荷**。要原始载荷得把它一起存进 trace，代价是 trace 体积翻几倍；
    当前所有工具都是只读的、重查一次也便宜，所以刻意选轻的这一头。
    """
    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=AGENT_SYSTEM_PROMPT),
        *(history or []),
        ChatMessage(role="user", content=question),
    ]
    calls = [item for item in recorded if item.get("kind") == "tool_call"]
    results = [item for item in recorded if item.get("kind") == "tool_result"]
    for index, call in enumerate(calls):
        call_id = str(call.get("call_id") or f"replay-{index}")
        tool = str(call.get("tool") or "")
        messages.append(
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=(
                    ToolCall(
                        id=call_id,
                        name=tool,
                        arguments=str(call.get("arguments") or "{}"),
                    ),
                ),
            )
        )
        detail = results[index].get("detail") if index < len(results) else ""
        messages.append(
            ChatMessage(
                role="tool",
                content=json.dumps(
                    {"replayed": True, "tool": tool, "result": detail},
                    ensure_ascii=False,
                ),
                tool_call_id=call_id,
                tool_name=tool,
            )
        )
    return messages


async def run_agent(
    *,
    question: str,
    actor: Principal,
    chat_model: ChatModelPort,
    purchase: PurchaseCoordinator,
    approval: ApprovalCoordinator,
    history: list[ChatMessage] | None = None,
    resume_from: list[dict[str, Any]] | None = None,
    on_step: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
) -> AsyncIterator[AgentStep | AgentResult]:
    """跑一轮 Agent：产出若干 `AgentStep`，最后产出**一个** `AgentResult`。

    之所以是"产出步骤"而不是"返回结果"：步骤要能**实时推给前端**
    （用户会想看到"它正在查 XX"）。而"最后一个是 AgentResult"这个约定
    让调用方既能逐步展示，又能一次拿到结论。

    ============================ M12++：断点续跑 ============================
    两个新参数，都是可选的（不传时行为与加它之前**完全一致**）：

        `resume_from`  上一次中断时已记录的轨迹。给了就**不重新执行**
                       已经跑过的那几步，直接把记录的结果回灌给模型接着跑。
        `on_step`      每跑完一步的回调（调用方用它把轨迹落盘）。
                       **必须在工具执行之后、下一步之前**被调用 ——
                       "断点"这个词的全部意义就在这里。

    调用方要负责的事（这里**不做**，因为它们是边界层的职责）：
        · 判断模型是否支持工具（`chat_model.supports_tools`）
        · 额度与限流、落库、引用校验
    """
    specs = tool_specs()
    tools = to_openai_tools(specs) if chat_model.supports_tools else []
    recorded = list(resume_from or [])
    # 有记录就按记录重建消息（模型的上下文要能接上），否则从头开始。
    messages = rebuild_messages(recorded=recorded, history=history, question=question)

    started = time.perf_counter()
    consecutive_failures = 0
    usage: ModelUsage | None = None
    collected: list[str] = []
    #: 已记录轨迹的**可持久化形式**（与 `steps` 同源，但字段更全）
    trace: list[dict[str, Any]] = [
        {**item} for item in recorded
    ]
    #: 这次执行里第几次工具调用（**续跑时从已记录的次数接着数**）
    calls_seen = next_step_index(recorded)
    reused_steps = 0

    def capture(reported: ModelUsage) -> None:
        nonlocal usage
        usage = reported

    def record(step: AgentStep) -> dict[str, Any]:
        """把一步落进 trace。`tool_call` 要带 `call_id` / `tool` / `arguments` —— 
        续跑时靠它们判断"这一步是不是就是模型这次想调的"。"""
        return {
            "round": step.round_no,
            "kind": step.kind,
            "detail": step.detail,
            "call_id": step.call.id if step.call else None,
            "tool": step.call.name if step.call else None,
            "arguments": step.call.arguments if step.call else None,
        }

    async def persist() -> None:
        """把轨迹交给调用方落盘。**没有回调就什么都不做**（可选特性）。"""
        if on_step is not None:
            await on_step([dict(item) for item in trace])

    stop_reason = "answered"
    answer = ""
    round_no = 0

    for round_no in range(1, MAX_ROUNDS + 1):
        if time.perf_counter() - started > MAX_TOTAL_SECONDS:
            # **带着已有内容收尾**，不是报错：用户宁可看到"我查到一半，还没查完"，
            # 也不愿看到一个 500。这一条与"不静默截断"并不矛盾 ——
            # 我们**明确告诉**他超时了，而不是把半截答案伪装成完整答案。
            stop_reason = "timeout"
            step = AgentStep(round_no, "guard", f"超过总耗时上限 {MAX_TOTAL_SECONDS:.0f} 秒")
            trace.append(record(step))
            await persist()
            yield step
            break

        turn_text = ""
        calls: tuple[ToolCall, ...] = ()
        async for turn in chat_model.stream_turn(
            messages=messages, tools=tools, on_usage=capture
        ):
            turn_text += turn.text
            calls = calls + turn.tool_calls

        messages.append(ChatMessage(role="assistant", content=turn_text, tool_calls=calls))

        if not calls:
            answer = turn_text.strip()
            break

        if turn_text.strip():
            # 模型既说了话又要调工具：把那句话留着，最后回答时一起给用户
            collected.append(turn_text.strip())

        for call in calls:
            step = AgentStep(round_no, "tool_call", f"调用 {call.name}", call=call)
            trace.append(record(step))
            yield step

            # ---- 断点续跑：这一步跑过了就不再执行 ----
            decision = should_reuse(trace[: -1], step_index=calls_seen, tool=call.name)
            if decision.reuse:
                reused_steps += 1
                outcome = ToolOutcome(
                    ok=True,
                    payload={"reused_from_checkpoint": True, "result": decision.detail},
                    summary=f"{decision.detail}（复用记录，未重新执行）",
                )
            else:
                outcome = await _execute(
                    call=call,
                    specs=specs,
                    actor=actor,
                    purchase=purchase,
                    approval=approval,
                )
            calls_seen += 1
            consecutive_failures = 0 if outcome.ok else consecutive_failures + 1
            result_step = AgentStep(
                round_no,
                "tool_result",
                outcome.summary or ("成功" if outcome.ok else "失败"),
                # 写工具预检成功时把**待确认的提案**带上：前端靠它渲染确认按钮，
                # 而"点了按钮要回传什么"就是这一份 `draft` 与令牌。
                confirmation=(
                    {
                        "action": outcome.payload.get("action"),
                        "draft": outcome.payload.get("draft"),
                        "confirmation_token": outcome.payload.get("confirmation_token"),
                    }
                    if outcome.payload.get("status") == "PENDING_CONFIRMATION"
                    else None
                ),
            )
            trace.append(record(result_step))
            # **落盘必须在下一步之前** —— 这里就是"断点"本身。
            await persist()
            yield result_step
            # 结果回灌给模型。`content` 用 JSON —— 模型对 JSON 的结构理解最好，
            # 而纯文本拼接会让它分不清"哪个字段是哪个"。
            # ⚠️ `tool_name` 与 `tool_call_id` **都要给**：Ollama 认前者、
            # OpenAI 认后者，少给一个会在某些实现上静默失败（见 `_to_ollama_message`）。
            messages.append(
                ChatMessage(
                    role="tool",
                    content=json.dumps(outcome.payload, ensure_ascii=False, default=str),
                    tool_call_id=call.id,
                    tool_name=call.name,
                )
            )

        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            stop_reason = "too_many_failures"
            step = AgentStep(
                round_no,
                "guard",
                f"连续 {consecutive_failures} 次工具调用失败，停止重试",
            )
            trace.append(record(step))
            await persist()
            yield step
            break
    else:
        # 循环跑满还没 break：说明模型一直在调工具
        stop_reason = "max_rounds"
        step = AgentStep(round_no, "guard", f"达到轮次上限 {MAX_ROUNDS}")
        trace.append(record(step))
        await persist()
        yield step

    if not answer:
        # 没能拿到模型的最终回答：用**已经查到的内容**给一个诚实的收尾。
        # 为什么不由这里编一句话：那会让"没查完"看起来像"查完了"。
        answer = _fallback_answer(collected=collected, stop_reason=stop_reason)

    yield AgentResult(
        answer=answer,
        rounds=round_no,
        stop_reason=stop_reason,
        usage=usage,
        reused_steps=reused_steps,
    )


async def _execute(
    *,
    call: ToolCall,
    specs: list[Any],
    actor: Principal,
    purchase: PurchaseCoordinator,
    approval: ApprovalCoordinator,
) -> Any:
    """执行一个工具调用，**把"参数不是合法 JSON"也变成一次可回灌的失败**。

    模型给出坏 JSON 是常见情形（尤其小模型）。如果这里让它抛异常，
    整个 Agent 就会以一个 500 结束 —— 而正确做法是告诉模型"你的 JSON 坏了"，
    让它重来一次。
    """
    try:
        arguments = json.loads(call.arguments or "{}")
    except json.JSONDecodeError as exc:
        from app.application.tools import ToolOutcome

        return ToolOutcome(
            ok=False,
            payload={
                "error": f"arguments 不是合法 JSON：{exc}",
                "hint": "请只输出符合 schema 的 JSON 对象。",
            },
            summary="参数不是合法 JSON",
        )
    return await run_tool(
        name=call.name,
        arguments=arguments,
        specs=specs,
        actor=actor,
        purchase=purchase,
        approval=approval,
    )


def _fallback_answer(*, collected: list[str], stop_reason: str) -> str:
    """没能拿到最终回答时的收尾话术。

    **要能区分"没查完"与"什么都没有"**：前者给用户"我已经查到这些"，
    后者说清"没查到"。两种情况都**不许**编一个结论。
    """
    reason_text = {
        "timeout": "这次查询超时了",
        "max_rounds": "这次查询轮次太多，先停在这里",
        "too_many_failures": "连续几次查询都失败了",
    }.get(stop_reason, "没能给出完整回答")
    head = f"{reason_text}。"
    if collected:
        return head + "已经查到的部分：" + "；".join(collected)
    return head + "请换个说法再问一次，或直接在系统页面里查看。"
