"""Agent 运行状态与断点复用的纯函数测试（M12++）。

============================ 为什么这批必须有 ============================
`domain/agent_run.py` 判的是"**这一步跑过了吗**"。它判错**不会报错**，
只会让 Agent **拿着上一次的数据回答这一次的问题** —— 也就是"串味的依据"。
那比"重新查一次"危险得多，而且从输出上完全看不出来。

所以这里逐条钉住四件事：

    ① 步数怎么数（只数 `tool_call`，不数 `tool_result`）
    ② 工具名不同**必须不复用**（防串味 —— 这是本文件最重要的一条）
    ③ 没记录过就正常执行（别把"首次跑"当成"分叉"）
    ④ 什么算"卡住的 RUNNING"

全部是纯函数，毫秒级，不碰数据库也不碰模型。
"""

from __future__ import annotations

import pytest

from app.domain.agent_run import (
    RUN_FINISHED,
    RUN_INTERRUPTED,
    RUN_RUNNING,
    is_stale,
    next_step_index,
    recorded_result_for,
    should_reuse,
)

#: 纯函数档：不连数据库、不调模型，毫秒级（`pytest` 默认只跑这一档）
pytestmark = pytest.mark.unit


def _call(tool: str, detail: str, call_id: str = "c1", arguments: str = "{}") -> list[dict]:
    """造一对 `tool_call` + `tool_result` 记录。"""
    return [
        {
            "round": 1,
            "kind": "tool_call",
            "detail": f"调用 {tool}",
            "call_id": call_id,
            "tool": tool,
            "arguments": arguments,
        },
        {"round": 1, "kind": "tool_result", "detail": detail, "call_id": None, "tool": None,
         "arguments": None},
    ]


# ------------------------------------------------------------------ ① 步数怎么数


def test_step_index_counts_only_tool_calls() -> None:
    """`tool_result` **不算**一步 —— 它总是紧跟 `tool_call`、成对出现。

    两个都数会让下标翻倍，而"第几步"在人的理解里就是"调了几次工具"。
    """
    assert next_step_index([]) == 0
    assert next_step_index(_call("list_my_purchase_requests", "列出申请 3/3 条")) == 1
    assert (
        next_step_index(
            _call("list_my_purchase_requests", "列出申请 3/3 条")
            + _call("get_purchase_request", "查到 1 条", call_id="c2")
        )
        == 2
    )


def test_step_index_ignores_guard_and_answer_steps() -> None:
    """护栏步（`guard`）与回答步不算工具调用，不该让下标前进。"""
    recorded = [
        {"round": 1, "kind": "guard", "detail": "超过总耗时上限", "tool": None},
        *_call("list_my_purchase_requests", "列出申请 3/3 条"),
        {"round": 2, "kind": "answer", "detail": "回答", "tool": None},
    ]
    assert next_step_index(recorded) == 1


# ------------------------------------------------------------------ ② 防串味（最重要）


def test_different_tool_at_same_index_must_not_be_reused() -> None:
    """**同一步序、不同工具 → 绝不复用。**

    这是本文件最重要的一条。若只按步序判重，模型这次在第 1 步调了另一个工具，
    我们会把**上一个工具的结果**喂给它 —— 它就会拿 A 的数据回答 B 的问题，
    而且不报错。表现是"答案莫名其妙对不上问题"，极难归因。
    """
    recorded = _call("list_my_purchase_requests", "列出申请 3/3 条")
    decision = should_reuse(recorded, step_index=0, tool="list_my_todo")
    assert decision.reuse is False
    assert decision.detail is None
    assert "list_my_purchase_requests" in decision.reason
    assert "list_my_todo" in decision.reason


def test_recorded_result_for_returns_none_when_tool_differs() -> None:
    recorded = _call("list_my_purchase_requests", "列出申请 3/3 条")
    assert (
        recorded_result_for(recorded, step_index=0, tool="list_my_purchase_requests")
        == "列出申请 3/3 条"
    )
    assert recorded_result_for(recorded, step_index=0, tool="list_my_todo") is None


# ------------------------------------------------------------------ ③ 首次执行


def test_unrecorded_step_executes_normally() -> None:
    """没记录过 = 正常的首次执行，**不是**分叉（两者要能区分）。"""
    decision = should_reuse([], step_index=0, tool="list_my_purchase_requests")
    assert decision.reuse is False
    assert "还没跑过" in decision.reason


def test_step_beyond_recorded_executes_normally() -> None:
    """已经记了 2 步，模型现在要调第 3 步 → 正常执行（续跑的正常路径）。"""
    recorded = _call("a_tool", "结果 A") + _call("b_tool", "结果 B", call_id="c2")
    decision = should_reuse(recorded, step_index=2, tool="c_tool")
    assert decision.reuse is False
    assert "还没跑过" in decision.reason


def test_same_tool_at_recorded_index_is_reused() -> None:
    """记录过、工具也对得上 → **复用，不重新执行**。"""
    recorded = _call("list_my_purchase_requests", "列出申请 3/3 条")
    decision = should_reuse(recorded, step_index=0, tool="list_my_purchase_requests")
    assert decision.reuse is True
    assert decision.detail == "列出申请 3/3 条"


def test_reuse_picks_the_right_step_among_several() -> None:
    """多步时按**下标**取对应那一步的结果，不能拿错。"""
    recorded = _call("a_tool", "结果 A") + _call("b_tool", "结果 B", call_id="c2")
    assert should_reuse(recorded, step_index=0, tool="a_tool").detail == "结果 A"
    assert should_reuse(recorded, step_index=1, tool="b_tool").detail == "结果 B"


def test_call_without_result_is_not_reused() -> None:
    """只有 `tool_call`、没有配对的 `tool_result` → 不能复用。

    这正是"进程在工具执行到一半被杀"留下的形状：那一步**没跑完**，
    必须重新执行。把它当成"跑过了"会让 Agent 拿到一个空结果，
    而它会基于空结果继续推理 —— 又是一次静默的错误依据。
    """
    orphan = [
        {
            "round": 1,
            "kind": "tool_call",
            "detail": "调用 list_my_purchase_requests",
            "call_id": "c1",
            "tool": "list_my_purchase_requests",
            "arguments": "{}",
        }
    ]
    decision = should_reuse(orphan, step_index=0, tool="list_my_purchase_requests")
    assert decision.reuse is False
    assert recorded_result_for(orphan, step_index=0, tool="list_my_purchase_requests") is None


# ------------------------------------------------------------------ ④ 卡住的 RUNNING


def test_running_within_threshold_is_not_stale() -> None:
    """刚开跑的 RUNNING **不是**残留 —— 误判它会让两个进程同时续跑同一次运行。"""
    assert is_stale(status=RUN_RUNNING, age_seconds=5, stale_after_seconds=600) is False
    assert is_stale(status=RUN_RUNNING, age_seconds=599, stale_after_seconds=600) is False


def test_running_beyond_threshold_is_stale() -> None:
    assert is_stale(status=RUN_RUNNING, age_seconds=601, stale_after_seconds=600) is True


def test_finished_and_interrupted_are_never_stale() -> None:
    """终态与"已经中断"都不该被 sweep 再动一次。"""
    for status in (RUN_FINISHED, RUN_INTERRUPTED):
        assert is_stale(status=status, age_seconds=99999, stale_after_seconds=1) is False
