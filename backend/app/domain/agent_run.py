"""Agent 运行的持久化状态（M12++）。

这一层是纯 Python，**不 import 任何框架**。

============================ 它解决的是什么问题 ============================
ReAct 循环的执行状态**原先只在内存里**：一次提问就是一次 `run_agent` 调用，
中途断了（进程被杀、连接掉线、用户刷新页面）就**全部丢掉**，
只能从头再问一遍。

而"从头再问一遍"在 Agent 场景里不是免费的：
  ① 模型要重新想一遍（token 再花一次，3B 在本地也要几秒）；
  ② 已经查到的事实要重新查（多几次数据库往返）；
  ③ **写操作会重复执行** —— 这一条才是真事故（重复建单、重复付款）。

所以需要一个"**跑过哪几步、每步拿到什么**"的可查记录。

============================ 判定"这一步跑过了吗" ============================
**只按"第几步"比，不按内容比。** 理由：

模型是**非确定性**的 —— 同一段对话重跑，它可能给出措辞不同的
`assistant` 文本、甚至调一个**顺序不同**的工具。若按内容判重，
"模型这次换了个说法"就会被当成"这一步没跑过" → 又执行一次。

按**步序**判重则确定：第 n 步已经记下来了，就不会再执行第 n 步。
代价是：模型重跑时若走上**不同**的路径，第 n 步对不上 →
那时**记录作废、重新开始**（见 `should_reuse`），而不是拼出半真半假的轨迹。

============================ 为什么"重复副作用"现在还不是问题 ============================
当前 4 个工具**全是只读的**，写工具走"两段式"（第一次调用只预检、不落库，
人在前端确认后才执行，且确认令牌兼作幂等键）。所以"重放已记录的步骤"
在语义上是安全的 —— 它只是把**上一次查到的结果**再拿一次。

但结构要现在就立起来：等写工具真的接进来时，
判重逻辑必须是"**已记录就不执行**"而不是"再执行一次、靠幂等键兜底"。
两者都对，但前者省一次真实的副作用窗口。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 运行状态。只有四个，且**终态无出口**。
RUN_RUNNING = "RUNNING"
#: 跑完了（含"没调工具就答了"与"被护栏拦下"两种 —— 它们都是正常结束）。
RUN_FINISHED = "FINISHED"
#: 中断中：进程被杀或连接掉线留下的状态。**这是唯一可续跑的状态。**
RUN_INTERRUPTED = "INTERRUPTED"
#: 失败：构建 Agent 之前就出错了（取不到身份、数据库不可用等）。
RUN_FAILED = "FAILED"

RUN_STATUSES: frozenset[str] = frozenset(
    {RUN_RUNNING, RUN_FINISHED, RUN_INTERRUPTED, RUN_FAILED}
)

#: 可以续跑的状态。**只有 INTERRUPTED。**
#: 为什么不把 RUNNING 也算上：一个处于 RUNNING 的行可能是**另一个进程正在跑**的，
#: 续跑它等于两个进程同时调同一个模型、同时查同一批数据。
#: 要续跑"当前正在跑"的那一次，得先由超时清理把它标成 INTERRUPTED
#: （见 `is_stale`），那一步是显式的、可审计的。
RESUMABLE_STATUSES: frozenset[str] = frozenset({RUN_INTERRUPTED})

#: 一步的记录。与 `application/agent.py` 的 `AgentStep` 一一对应，
#: 但**多带两样东西**：`call_id` 与 `arguments`。
#: 没有它俩，续跑时无法判断"这一步是否就是模型这次想调的那个"。
@dataclass(frozen=True)
class StepRecord:
    round_no: int
    kind: str  # "tool_call" | "tool_result"
    detail: str
    call_id: str | None = None
    tool: str | None = None
    arguments: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "round": self.round_no,
            "kind": self.kind,
            "detail": self.detail,
            "call_id": self.call_id,
            "tool": self.tool,
            "arguments": self.arguments,
        }


@dataclass(frozen=True)
class Reuse:
    """续跑时对某一步的处置结论。"""

    #: 能不能复用已记录的结果
    reuse: bool
    #: 复用时给出上次的结果（`tool_result` 的 `detail`）；不复用时为 None
    detail: str | None
    #: 不复用的原因（给人看的一句话，写进轨迹与日志）
    reason: str


def next_step_index(recorded: list[dict[str, Any]]) -> int:
    """已记录到第几步（= 下一个 **tool_call** 的下标）。

    只数 `tool_call`：`tool_result` 总是紧跟它、成对出现，
    两个都数会让下标翻倍，而"第几步"在人的理解里是"调了几次工具"。
    """
    return sum(1 for item in recorded if item.get("kind") == "tool_call")


def recorded_result_for(
    recorded: list[dict[str, Any]], *, step_index: int, tool: str
) -> str | None:
    """第 `step_index` 次工具调用（工具名 `tool`）**已记录的结果**，没有则 None。

    ⚠️ **工具名也要比**：只比步序的话，模型这次在第 1 步调了另一个工具，
    我们会把上一个工具的结果塞给它 —— 那是**串味的依据**，
    比"重新查一次"危险得多（模型会拿 A 的数据回答 B 的问题，而且不报错）。
    """
    calls = [item for item in recorded if item.get("kind") == "tool_call"]
    results = [item for item in recorded if item.get("kind") == "tool_result"]
    if step_index >= len(calls) or step_index >= len(results):
        return None
    if calls[step_index].get("tool") != tool:
        return None
    return str(results[step_index].get("detail") or "")


def should_reuse(
    recorded: list[dict[str, Any]], *, step_index: int, tool: str
) -> Reuse:
    """决定"模型这次想调的第 n 个工具"能不能复用已记录的结果。

    三个判断，**顺序有意**：

        ① 这一步**根本没记录过** → 执行（这是正常的首次跑）
        ② 记录了、但**工具不是同一个** → 不复用。
           轨迹已经和模型这次的路径分叉了，继续拼会造出
           "前半段是上次的、后半段是这次的"这种半真半假的记录。
        ③ 记录了、工具也对得上 → **复用**，不再执行。
    """
    detail = recorded_result_for(recorded, step_index=step_index, tool=tool)
    if detail is None:
        calls = [item for item in recorded if item.get("kind") == "tool_call"]
        if step_index >= len(calls):
            return Reuse(reuse=False, detail=None, reason="这一步还没跑过，正常执行")
        return Reuse(
            reuse=False,
            detail=None,
            reason=(
                f"第 {step_index + 1} 步记录的是 {calls[step_index].get('tool')!r}，"
                f"而模型这次要调 {tool!r} —— 轨迹分叉，不复用"
            ),
        )
    return Reuse(reuse=True, detail=detail, reason=f"第 {step_index + 1} 步已跑过，复用记录")


def is_stale(*, status: str, age_seconds: float, stale_after_seconds: float) -> bool:
    """这一行是不是"卡住的 RUNNING"（可以安全地标成 INTERRUPTED）。

    `RUNNING` 本身不能续跑（可能是另一个进程正在跑），但如果它已经卡了
    很久（远超一次正常 Agent 的总耗时上限），那只能是**被杀掉的残留**。
    阈值取 600 秒 = 3.3 倍于 `MAX_TOTAL_SECONDS`（180 秒），故意宽松 ——
    宁可让残留多躺一会儿，也不要误判一个正在跑的请求。
    """
    return status == RUN_RUNNING and age_seconds > stale_after_seconds
