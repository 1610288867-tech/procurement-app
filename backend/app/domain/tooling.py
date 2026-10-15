"""工具的**参数校验**（M12 增强）。纯函数，不 import 任何框架。

============================ 为什么参数校验必须逐字写清楚 ============================
模型的输出是**不可信输入**。它会给出：

    · 少字段       `{}`                      → 缺必填
    · 多字段       `{"request_no": ..., "force": true}`  → **多出来的字段可能是注入**
    · 类型不对     `{"amount_fen": "一万"}`   → 类型不符
    · 越界         `{"limit": 100000}`        → 一次把整个库拖出来
    · 幻觉出的工具  `name="delete_everything"` → **工具名不存在**

最后一条最危险：**如果注册表对未知工具"宽容处理"（忽略或当成普通回答），
模型就可能通过"编一个工具名"把控制流带偏**。所以这里的原则是
**未知即拒绝**，而且要给出"可用工具有哪些"，让上层能把它回灌给模型让它改。

============================ 为什么自己写而不引 jsonschema ============================
契约面很窄（type / required / enum / minimum / maximum / minLength / maxLength），
而这些语义我们**必须完全掌控** —— 引一个库意味着"它多支持一种写法，
我们的校验行为就跟着变"，而这条路径的失败模式是静默的（错误的参数被放行、
去调了一个写操作）。窄契约 + 显式拒绝未知关键字，比一个宽契约更安全。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 允许出现在参数 schema 里的关键字。**别的会被 `check_schema` 拒绝**。
#:
#: 为什么要限制工具作者：写 `{"type": "object", "items": {...}}` 这种
#: 语义错的 schema 时，如果校验器"看不懂就跳过"，那个字段就**永远不会被校验** ——
#: 而且不报错。所以不认识的键必须当场报出来。
_ALLOWED_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "description",
        "enum",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "items",
    }
)

#: 支持的 JSON 类型 → Python 类型。
#: ⚠️ `bool` 在 Python 里是 `int` 的子类，所以整数校验必须**显式排除 bool**
#: （`{"limit": true}` 不该被当成 `limit=1`）。
_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}


@dataclass(frozen=True)
class ArgumentProblem:
    """一条参数问题。`path` 指到具体字段，便于回灌给模型让它改。"""

    path: str
    detail: str


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的声明。

    `schema` 是 OpenAI 兼容的 JSON Schema 片段（`{"type": "object", "properties": ...}`）。
    它同时用于**两件事**：发给模型（让它知道怎么调）、校验模型给的参数。
    **同一份 schema 两用**是关键 —— 分成两份迟早会不一致，
    而"发出去的契约与校验用的契约不一致"会让模型永远调不对，且看不出原因。
    """

    name: str
    description: str
    schema: dict[str, Any]
    #: 写类工具（有副作用）。调用方据此决定要不要走"人工确认"。
    #: **默认 False 是刻意的**：新加工具时如果忘了标，它会被当成只读工具直接执行 ——
    #: 所以写工具必须**显式**声明 `mutating=True`。这条默认值在测试里钉着。
    mutating: bool = False
    #: 只读工具是否可以用"调用者身份"以外的身份（如系统账号）。
    #: 目前**没有**任何工具用它 —— 留这个字段是为了让"提权"这件事
    #: 必须**显式写出来**，而不是悄悄发生。
    runs_as_system: bool = False

    @property
    def required(self) -> frozenset[str]:
        """必填字段名。**从 `schema` 派生，而不是另一个字段。**

        早先它是个 `field(default_factory=frozenset)`，而 `__post_init__` 里
        没有真的去算它 —— 于是"必填"是空集，**模型不给任何参数也能通过校验**，
        而且不报错（实测被两条测试抓到：`test_missing_required_is_reported`）。

        这类"同一件事有两处真相"最容易出的就是这个错：默认值恰好是空集，
        读起来像"这个工具没有必填字段"，完全不可疑。改成属性之后**不可能不同步**。
        """
        declared = self.schema.get("required", [])
        return frozenset(declared) if isinstance(declared, list) else frozenset()

    def __post_init__(self) -> None:
        if not self.name or not self.name.isidentifier():
            # 工具名要当标识符用（日志、审计、前端），所以限制成标识符形状。
            # 含点或连字符的名字会让"按名字查工具"在各处写法不一。
            raise ValueError(f"工具名必须是合法标识符：{self.name!r}")
        if not self.description.strip():
            raise ValueError(f"工具 {self.name} 缺少描述 —— 模型靠它决定何时调用")
        schema_problems = check_schema(self.schema, path=f"{self.name}.parameters")
        if schema_problems:
            details = "；".join(f"{p.path}: {p.detail}" for p in schema_problems)
            raise ValueError(f"工具 {self.name} 的 schema 不合法：{details}")


def check_schema(schema: Any, *, path: str) -> list[ArgumentProblem]:
    """检查一份参数 schema 本身是否合法（**工具作者的错，不是模型的错**）。

    它挡住的是"schema 写错了但没人发现"：例如 `type` 拼成 `types`、
    `required` 写成本该有的字段名之外的字符串。这类错不报的话，
    校验器会跳过那些字段，而**跳过等于不校验**。
    """
    problems: list[ArgumentProblem] = []
    if not isinstance(schema, dict):
        return [ArgumentProblem(path, "schema 必须是对象")]
    if schema.get("type") != "object":
        problems.append(ArgumentProblem(f"{path}.type", "顶层类型必须是 object"))

    unknown = set(schema) - _ALLOWED_KEYS
    if unknown:
        problems.append(
            ArgumentProblem(
                path,
                f"不认识的关键字 {sorted(unknown)}；允许的有 {sorted(_ALLOWED_KEYS)}",
            )
        )

    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        problems.append(ArgumentProblem(f"{path}.properties", "必须是非空对象"))
        return problems

    for name, spec in properties.items():
        location = f"{path}.properties.{name}"
        if not isinstance(spec, dict):
            problems.append(ArgumentProblem(location, "字段定义必须是对象"))
            continue
        bad = set(spec) - _ALLOWED_KEYS
        if bad:
            problems.append(ArgumentProblem(location, f"不认识的关键字 {sorted(bad)}"))
        kind = spec.get("type")
        if kind not in _TYPES:
            problems.append(
                ArgumentProblem(f"{location}.type", f"不支持的类型 {kind!r}")
            )

    required = schema.get("required", [])
    if not isinstance(required, list):
        problems.append(ArgumentProblem(f"{path}.required", "必须是数组"))
    else:
        for name in required:
            if name not in properties:
                problems.append(
                    ArgumentProblem(
                        f"{path}.required",
                        f"{name!r} 是必填但没在 properties 里声明",
                    )
                )
    return problems


def check_arguments(
    spec: ToolSpec, arguments: Any
) -> tuple[dict[str, Any], list[ArgumentProblem]]:
    """校验模型给的参数。返回 `(清洗后的参数, 问题列表)`。

    ============================ 三条硬规则 ============================
    ① **未知字段一律拒绝**（不是忽略）。多余字段是最常见的一类提示注入载体
       （`{"request_no": "...", "force": true}`）—— 忽略它等于默许它存在；
    ② **类型必须严格**：`bool` 不算 `int`、`"1"` 不算 `int`。
       宽松转换（`int("1")`）会让 `{"limit": "999999"}` 通过，
       而"模型给了字符串却当数字用"这类不一致在排查时极难联想；
    ③ **不在 schema 里的字段不会出现在结果里**：返回值只含已声明的字段，
       免得未知键顺着**关键字参数展开**漏进下游用例。
    """
    problems: list[ArgumentProblem] = []
    if not isinstance(arguments, dict):
        return {}, [ArgumentProblem("", f"参数必须是对象，实际是 {type(arguments).__name__}")]

    schema = spec.schema
    properties: dict[str, Any] = schema["properties"]

    unknown = set(arguments) - set(properties)
    for name in sorted(unknown):
        problems.append(
            ArgumentProblem(name, f"未知参数；本工具接受 {sorted(properties)}")
        )

    cleaned: dict[str, Any] = {}
    for name, definition in properties.items():
        if name not in arguments:
            if name in spec.required:
                problems.append(ArgumentProblem(name, "缺少必填参数"))
            continue
        value = arguments[name]
        field_problems = _check_value(value, definition, path=name)
        if field_problems:
            problems.extend(field_problems)
            continue
        cleaned[name] = value
    return cleaned, problems


def _check_value(
    value: Any, definition: dict[str, Any], *, path: str
) -> list[ArgumentProblem]:
    """校验单个字段的值。"""
    problems: list[ArgumentProblem] = []
    kind = definition.get("type")
    allowed = _TYPES.get(kind or "", ())

    # ⚠️ `bool` 是 `int` 的子类：`{"limit": true}` 会被 `isinstance(True, int)` 放行。
    # 所以整数/数字类型必须**显式排除** bool。
    if kind in ("integer", "number") and isinstance(value, bool):
        problems.append(ArgumentProblem(path, f"期望 {kind}，实际是 boolean"))
        return problems
    if kind == "boolean" and not isinstance(value, bool):
        problems.append(ArgumentProblem(path, f"期望 boolean，实际是 {type(value).__name__}"))
        return problems
    if allowed and not isinstance(value, allowed):
        problems.append(
            ArgumentProblem(path, f"期望 {kind}，实际是 {type(value).__name__}")
        )
        return problems

    if "enum" in definition and value not in definition["enum"]:
        problems.append(
            ArgumentProblem(path, f"取值必须是 {definition['enum']} 之一，实际 {value!r}")
        )
    if isinstance(value, str):
        if "minLength" in definition and len(value) < definition["minLength"]:
            problems.append(ArgumentProblem(path, f"长度不足 {definition['minLength']}"))
        if "maxLength" in definition and len(value) > definition["maxLength"]:
            problems.append(ArgumentProblem(path, f"长度超过 {definition['maxLength']}"))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in definition and value < definition["minimum"]:
            problems.append(ArgumentProblem(path, f"小于最小值 {definition['minimum']}"))
        if "maximum" in definition and value > definition["maximum"]:
            problems.append(ArgumentProblem(path, f"大于最大值 {definition['maximum']}"))
    return problems


def to_openai_tools(specs: list[ToolSpec]) -> list[dict[str, Any]]:
    """把工具声明翻成 OpenAI 兼容的 `tools` 数组。

    与 `ToolSpec.schema` **同一份数据**：发给模型的契约与校验用的契约必须同源，
    否则"模型以为正确的调用"会被自己的校验拒掉，而原因看不出来。
    """
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.schema,
            },
        }
        for spec in specs
    ]


def find_tool(specs: list[ToolSpec], name: str) -> ToolSpec | None:
    """按名字找工具。**找不到返回 `None`，由调用方决定怎么报**。

    不抛异常的刻意原因：调用方需要把"可用工具有哪些"回灌给模型
    （`unknown tool: X; available: [...]`），那比一个 500 有用得多。
    """
    for spec in specs:
        if spec.name == name:
            return spec
    return None
