"""工具参数校验的纯函数测试（M12 增强）。

这一层守的是"**模型的输出是不可信输入**"这句话的**第一个关口**。
它错了不会报错 —— 只会让不该通过的参数通过、去调了一个真实业务动作。
所以每一条拒绝规则都要有测试钉住。
"""

from __future__ import annotations

import uuid

import pytest

from app.application.coordinators.auth import Principal
from app.domain.tooling import (
    ToolSpec,
    check_arguments,
    check_schema,
    find_tool,
    to_openai_tools,
)

pytestmark = pytest.mark.unit


def spec(**overrides: object) -> ToolSpec:
    """一个最小可用的只读工具声明。"""
    base: dict[str, object] = {
        "name": "get_request_status",
        "description": "查询采购申请的状态",
        "schema": {
            "type": "object",
            "properties": {
                "request_no": {
                    "type": "string",
                    "description": "申请编号",
                    "maxLength": 32,
                }
            },
            "required": ["request_no"],
        },
    }
    base.update(overrides)
    return ToolSpec(**base)  # type: ignore[arg-type]


# ================================================================ 声明本身要合法


def test_valid_spec_derives_required_from_schema() -> None:
    """`required` 从 schema 派生 —— 避免"两处真相"。

    两处写必填的话，迟早会出现"schema 说必填、Spec 说可选"，
    而校验按哪一处走取决于实现细节 —— 那是静默放行的典型来源。
    """
    assert spec().required == frozenset({"request_no"})


def test_tool_name_must_be_an_identifier() -> None:
    """工具名要能当标识符（日志/审计/前端按名查找）。"""
    with pytest.raises(ValueError):
        spec(name="get-request.status")


def test_description_is_required() -> None:
    """没有描述的工具是**不可用**的：模型靠描述决定何时调用它。"""
    with pytest.raises(ValueError):
        spec(description="   ")


def test_mutating_defaults_to_false_so_writes_must_be_explicit() -> None:
    """**默认只读** —— 新加工具忘了标 `mutating` 时，它会被当成只读直接执行。

    这是刻意的默认方向：让"写工具"必须显式声明。
    反过来默认 `True` 的话，忘了标的只读工具会平白多一次人工确认（体验差但安全），
    而忘了标的**写工具**会被直接执行（危险）。所以默认必须偏向安全的那一侧。
    """
    assert spec().mutating is False


@pytest.mark.parametrize(
    ("schema", "hint"),
    [
        ({"type": "string"}, "object"),  # 顶层类型错
        ({"type": "object", "properties": {}}, "非空"),  # 没有字段
        ({"type": "object", "types": {}, "properties": {"a": {"type": "string"}}}, "不认识"),
        (
            {"type": "object", "properties": {"a": {"type": "str"}}},
            "不支持的类型",
        ),
        (
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "required": ["b"],
            },
            "没在 properties 里声明",
        ),
    ],
)
def test_bad_schema_is_rejected_at_declaration_time(
    schema: dict[str, object], hint: str
) -> None:
    """**schema 写错要当场报出来**，而不是等某个字段永远不被校验。

    这条是"工具作者的错，不是模型的错"。错误信息要能直接定位到字段。
    """
    problems = check_schema(schema, path="parameters")
    assert problems, f"应当报错：{schema}"
    assert any(hint in problem.detail for problem in problems), problems


# ================================================================ 未知即拒绝


def test_unknown_argument_is_rejected_not_ignored() -> None:
    """**未知参数必须拒绝，不能忽略。**

    多余字段是最常见的提示注入载体（`{"request_no": "...", "force": true}`）。
    忽略它等于默许它存在 —— 而且下游若用 `**kwargs` 展开，那个字段会真的流进去。
    """
    cleaned, problems = check_arguments(
        spec(), {"request_no": "REQ-1", "force": True}
    )
    assert problems, "未知参数必须报问题"
    assert "force" in problems[0].path
    assert "force" not in cleaned, "未知字段不能出现在清洗后的参数里"


def test_missing_required_is_reported() -> None:
    _, problems = check_arguments(spec(), {})
    assert problems
    assert problems[0].path == "request_no"
    assert "必填" in problems[0].detail


def test_non_object_arguments_are_rejected() -> None:
    """模型可能给出字符串或数组（协议实现不一致时很常见）。"""
    for bad in ("REQ-1", ["REQ-1"], 42, None):
        _, problems = check_arguments(spec(), bad)
        assert problems, f"应当拒绝：{bad!r}"


# ================================================================ 类型必须严格（bool 陷阱）


def test_bool_is_not_accepted_as_integer() -> None:
    """**`bool` 是 `int` 的子类** —— 不显式排除的话 `{"limit": true}` 会被当成 1。

    这是 Python 里最经典的一处静默放行：`isinstance(True, int)` 是 `True`。
    """
    tool = spec(
        schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
            "required": ["limit"],
        }
    )
    _, problems = check_arguments(tool, {"limit": True})
    assert problems, "true 不该被当成整数 1"
    assert "boolean" in problems[0].detail


def test_string_is_not_coerced_to_integer() -> None:
    """**不做宽松转换**：`"10"` 不该被当成 `10`。

    宽松转换会让模型给出的字符串悄悄变成数字用，
    而"契约是数字、实际传了字符串"这类不一致在排查时极难联想。
    """
    tool = spec(
        schema={
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
            "required": ["limit"],
        }
    )
    _, problems = check_arguments(tool, {"limit": "10"})
    assert problems
    assert "期望 integer" in problems[0].detail


# ================================================================ 边界值


def test_max_length_blocks_a_whole_table_dump_attempt() -> None:
    """`maxLength` 这类上界是**防滥用**的，不是装饰。

    没有上界时，模型可以要求返回一整张表（"所有申请"），
    而那不是"答得多了点"，是把数据库拖出来。
    """
    _, problems = check_arguments(spec(), {"request_no": "R" * 33})
    assert problems
    assert "长度超过" in problems[0].detail


def test_enum_is_enforced() -> None:
    tool = spec(
        schema={
            "type": "object",
            "properties": {"status": {"type": "string", "enum": ["DRAFT", "SUBMITTED"]}},
            "required": ["status"],
        }
    )
    assert check_arguments(tool, {"status": "DRAFT"})[1] == []
    _, problems = check_arguments(tool, {"status": "DELETED"})
    assert problems and "之一" in problems[0].detail


def test_minimum_and_maximum_are_enforced() -> None:
    tool = spec(
        schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
            "required": ["limit"],
        }
    )
    assert check_arguments(tool, {"limit": 5})[1] == []
    assert check_arguments(tool, {"limit": 0})[1], "小于最小值要报"
    assert check_arguments(tool, {"limit": 999})[1], "大于最大值要报"


def test_optional_field_may_be_absent() -> None:
    """不在 `required` 里的字段可以不给 —— 而给了就必须合法。"""
    tool = spec(
        schema={
            "type": "object",
            "properties": {
                "request_no": {"type": "string"},
                "verbose": {"type": "boolean"},
            },
            "required": ["request_no"],
        }
    )
    cleaned, problems = check_arguments(tool, {"request_no": "REQ-1"})
    assert problems == []
    assert cleaned == {"request_no": "REQ-1"}


def test_cleaned_arguments_only_contain_declared_fields() -> None:
    """返回值**只含已声明字段** —— 未知键不会顺着 `**kwargs` 漏进下游用例。"""
    tool = spec(
        schema={
            "type": "object",
            "properties": {"request_no": {"type": "string"}},
        }
    )
    cleaned, problems = check_arguments(tool, {"request_no": "REQ-1", "extra": 1})
    assert set(cleaned) == {"request_no"}
    assert problems, "未知字段仍然要报出来（拒绝≠忽略）"


# ================================================================ 与模型之间的契约


def test_openai_tools_uses_the_same_schema_as_validation() -> None:
    """**发给模型的契约与校验用的契约必须同源。**

    分成两份的话，迟早会出现"模型以为正确的调用被自己的校验拒掉"，
    而那种失败看起来像模型不会用工具，实际是契约不一致。
    """
    tool = spec()
    payload = to_openai_tools([tool])
    assert payload[0]["function"]["parameters"] is tool.schema
    assert payload[0]["function"]["name"] == tool.name


def test_find_tool_returns_none_for_unknown_name() -> None:
    """未知工具返回 `None`（而不是抛异常），调用方要能报"可用工具有哪些"。"""
    tools = [spec()]
    assert find_tool(tools, "get_request_status") is tools[0]
    assert find_tool(tools, "delete_everything") is None


# ================================================================ 确认令牌（写工具的第二段）


def principal(**overrides: object) -> object:
    """一个最小可用的调用者身份（只用到 id 与角色）。"""
    base: dict[str, object] = {
        "id": uuid.UUID("11111111-1111-1111-1111-111111111111"),
        "username": "u",
        "full_name": "用户",
        "department_id": uuid.UUID("22222222-2222-2222-2222-222222222222"),
        "roles": ("REQUESTER",),
    }
    base.update(overrides)
    return Principal(**base)  # type: ignore[arg-type]


def draft(**overrides: object) -> dict:
    base = {
        "title": "买打印机",
        "category_id": "33333333-3333-3333-3333-333333333333",
        "reason": None,
    }
    base.update(overrides)
    return base


def test_confirmation_accepts_the_exact_proposal() -> None:
    """原样回传就能通过（正常路径）。"""
    from app.application.tools import _confirmation_token, verify_confirmation

    actor = principal()
    token = _confirmation_token(actor=actor, draft=draft())
    cleaned = verify_confirmation(token=token, draft=draft(), actor=actor)
    assert cleaned["title"] == "买打印机"


def test_confirmation_rejects_changed_content() -> None:
    """**内容改一个字就必须拒绝。**

    这是"确认的是提案本身"那句话的落点：令牌是**可推算的**
    （`uuid5(actor+标题+品类+备注)`），所以只验令牌等于"知道内容就能建单"。
    把标题换掉之后令牌就对不上了 —— 而那次拒绝正是我们要的行为。
    """
    from app.application.tools import (
        ConfirmationError,
        _confirmation_token,
        verify_confirmation,
    )

    actor = principal()
    token = _confirmation_token(actor=actor, draft=draft())
    with pytest.raises(ConfirmationError) as excinfo:
        verify_confirmation(
            token=token, draft=draft(title="买十台打印机"), actor=actor
        )
    assert excinfo.value.code == "CONFIRM_TOKEN_MISMATCH"


def test_confirmation_rejects_another_actor() -> None:
    """**别人的令牌不能用**（令牌里含 actor）。"""
    import uuid

    from app.application.tools import (
        ConfirmationError,
        _confirmation_token,
        verify_confirmation,
    )

    owner = principal()
    token = _confirmation_token(actor=owner, draft=draft())
    other = principal(id=uuid.UUID("99999999-9999-9999-9999-999999999999"))
    with pytest.raises(ConfirmationError) as excinfo:
        verify_confirmation(token=token, draft=draft(), actor=other)
    assert excinfo.value.code == "CONFIRM_TOKEN_MISMATCH"


def test_confirmation_rejects_unknown_fields() -> None:
    """**未知字段一律拒绝**（与工具参数同一条纪律：`**kwargs` 是注入的通道）。"""
    from app.application.tools import ConfirmationError, verify_confirmation

    actor = principal()
    with pytest.raises(ConfirmationError) as excinfo:
        verify_confirmation(
            token="x" * 16,
            draft={**draft(), "urgency": "HIGH"},
            actor=actor,
        )
    assert excinfo.value.code == "CONFIRM_UNKNOWN_FIELD"


def test_confirmation_rejects_empty_title_and_bad_category() -> None:
    """这里比模型给的输入**更严格**，不是更松 —— 它是最后一道关口。"""
    from app.application.tools import ConfirmationError, verify_confirmation

    actor = principal()
    with pytest.raises(ConfirmationError) as excinfo:
        verify_confirmation(token="x" * 16, draft=draft(title="   "), actor=actor)
    assert excinfo.value.code == "CONFIRM_TITLE_EMPTY"

    with pytest.raises(ConfirmationError) as excinfo:
        verify_confirmation(
            token="x" * 16, draft=draft(category_id="不是uuid"), actor=actor
        )
    assert excinfo.value.code == "CONFIRM_BAD_CATEGORY"


def test_confirmation_token_is_stable_for_the_same_proposal() -> None:
    """同一人同一内容 → **同一个令牌**。

    它同时是幂等键，所以"稳定性"是"重复点确认不会建两张单"的前提。
    """
    from app.application.tools import _confirmation_token

    actor = principal()
    assert _confirmation_token(actor=actor, draft=draft()) == _confirmation_token(
        actor=actor, draft=draft()
    )


# ================================================================ Agent 轨迹的判据


def test_agent_used_tools_is_false_for_a_no_tool_answer() -> None:
    """**没有工具调用的轨迹 → `False`。**

    这条守的是一个**恒真条件**（实测踩到）：`_finish` 要用
    "**有没有真的调过工具**"决定要不要豁免"有断言必须带引用"这条引用规则，
    而它一开始写的是 `bool(agent_steps)` —— 但 `agent_steps` 在**走 Agent 路径时必然非空**
    （至少一条 `answer` 步），于是那个条件恒为真。

    后果：一个"**没调任何工具**、直接答了制度问题"的回答也被豁免，
    它"通篇断言却零引用"**没被标出来**。实测表现是模型回一句半截的
    "系统里没有查到"却带着合法状态 —— 而那正是这条检查存在的意义。
    """
    from app.application.tools import agent_used_tools

    # 走了 Agent 路径，但一个工具都没调（只有 `answer` 步）
    assert agent_used_tools([{"kind": "answer", "detail": "系统里没有查到"}]) is False
    # 空 / None 同样是"没用工具"
    assert agent_used_tools([]) is False
    assert agent_used_tools(None) is False


def test_agent_used_tools_is_true_when_a_tool_was_called() -> None:
    """**有工具调用 → `True`**（这才是该豁免引用规则的场景）。"""
    from app.application.tools import agent_used_tools

    assert agent_used_tools([{"kind": "tool_call", "detail": "调用 X", "tool": "X"}]) is True
    assert agent_used_tools([{"kind": "tool_result", "detail": "查到 3 条"}]) is True
    # 混合轨迹里只要有一次工具调用就算
    assert (
        agent_used_tools(
            [
                {"kind": "answer", "detail": "先看看"},
                {"kind": "tool_call", "detail": "调用 X"},
                {"kind": "tool_result", "detail": "查到"},
                {"kind": "answer", "detail": "结论"},
            ]
        )
        is True
    )


def test_agent_used_tools_ignores_guard_steps() -> None:
    """护栏步骤（超时 / 轮次用尽）**不算**用工具 —— 它没拿到任何数据。

    这条有实际含义：一个"因为超时被护栏打断"的回答，如果被当成数据型回答而豁免引用检查，
    就等于**用护栏掩盖了引用问题**。
    """
    from app.application.tools import agent_used_tools

    assert agent_used_tools([{"kind": "guard", "detail": "达到轮次上限 4"}]) is False
    assert (
        agent_used_tools([{"kind": "answer", "detail": "…"}, {"kind": "guard", "detail": "超时"}])
        is False
    )


def test_agent_used_tools_tolerates_malformed_steps() -> None:
    """轨迹形状异常时**不抛异常**（它是收尾路径上的判据，崩了会让整轮回答丢掉）。"""
    from app.application.tools import agent_used_tools

    assert agent_used_tools([{"no_kind": 1}]) is False  # type: ignore[list-item]
    assert agent_used_tools(["不是字典"]) is False  # type: ignore[list-item]
    assert agent_used_tools([{"kind": "tool_call"}]) is True
