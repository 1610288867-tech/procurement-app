"""受限规则 DSL —— 词表、求值与**语义校验**（M2 校验 / M6 求值）。

这一层是纯 Python，**不 import 任何框架**。

============================ 为什么必须有语义校验 ============================
结构合法但语义错误的策略**不会报任何错**。因为规则求值对"缺字段"的语义是
返回 False（不满足），于是整条规则**静默失效**，申请会以**更低的审批层级**
被批准 —— 这是审批系统里最严重的失败模式。

所以保存策略时必须拦住三类问题：

    1. 路径不在白名单（典型：写成 amount 而不是 amount_fen）
    2. 阈值是浮点（金额一律整数「分」）
    3. 没有零阈值兜底步骤（某些金额区间会算出空审批链 = 无人审批）

第三层"每个步骤都可达"要用引擎反推，在 `approval_engine.check_reachability`（M6）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

LOGICAL_OPERATORS = frozenset({"all", "any", "not"})
COMPARISON_OPERATORS = frozenset({"eq", "ne", "in", "exists", "lt", "lte", "gt", "gte"})
OPERATORS = LOGICAL_OPERATORS | COMPARISON_OPERATORS
NUMERIC_OPERATORS = frozenset({"lt", "lte", "gt", "gte"})

APPROVAL_ROLES = ("DEPT_MANAGER", "DEPT_HEAD", "PROCUREMENT", "FINANCE", "GM")
APPROVAL_MODES = ("SINGLE", "ALL")


class RuleError(ValueError):
    """规则结构非法。"""


# 审批策略上下文里允许出现的字段。
# 金额字段叫 amount_fen（单位：分）—— 写成 amount 会被下面的白名单拦下。
ALLOWED_CONTEXT_KEYS = frozenset(
    {
        "amount_fen",
        "urgency",
        "framework_contract",
        "department_id",
        "category_id",
        "requester_id",
    }
)


def collect_paths(rule: Any) -> list[str]:
    """递归收集规则里引用的所有字段路径。"""
    if not isinstance(rule, Mapping) or len(rule) != 1:
        raise RuleError("每个规则对象必须且只能包含一个操作符")

    operator, operand = next(iter(rule.items()))
    if operator not in OPERATORS:
        raise RuleError(f"不支持的操作符：{operator}")

    if operator in {"all", "any"}:
        if not isinstance(operand, (list, tuple)) or not operand:
            raise RuleError(f"{operator} 需要一个非空数组")
        paths: list[str] = []
        for item in operand:
            paths.extend(collect_paths(item))
        return paths

    if operator == "not":
        return collect_paths(operand)

    if not isinstance(operand, (list, tuple)) or not operand:
        raise RuleError(f"{operator} 需要一个非空数组")
    path = operand[0]
    if not isinstance(path, str) or not path:
        raise RuleError(f"{operator} 的第一个元素必须是字段路径字符串")
    return [path]


def iter_numbers(node: Any) -> list[int | float]:
    """递归收集规则里出现过的所有数字（bool 不算数字）。"""
    if isinstance(node, Mapping):
        numbers: list[int | float] = []
        for value in node.values():
            numbers.extend(iter_numbers(value))
        return numbers
    if isinstance(node, (list, tuple)):
        numbers = []
        for item in node:
            numbers.extend(iter_numbers(item))
        return numbers
    if isinstance(node, bool):
        return []
    if isinstance(node, (int, float)):
        return [node]
    return []


def is_zero_amount_fallback(rule: Any) -> bool:
    """这条规则是不是"任何金额都命中"的兜底。

    只认标准写法 `{"gte": ["amount_fen", 0]}`（阈值 ≤ 0），以及 `any` 里含它。
    刻意不去猜别的形状 —— 认不出来就报"缺兜底"，让管理员改成标准写法，
    比放过一个真的没有兜底的策略安全得多。
    """
    if not isinstance(rule, Mapping) or len(rule) != 1:
        return False

    operator, operand = next(iter(rule.items()))
    if operator == "any":
        return isinstance(operand, (list, tuple)) and any(
            is_zero_amount_fallback(item) for item in operand
        )
    if operator not in {"gte", "gt"}:
        return False
    if not isinstance(operand, (list, tuple)) or len(operand) != 2:
        return False

    path, threshold = operand
    return (
        path == "amount_fen"
        and isinstance(threshold, int)
        and not isinstance(threshold, bool)
        and threshold <= 0
    )


def read_path(data: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    """按点路径取值，返回 `(是否存在, 值)`。**不抛异常** —— 缺路径就是不存在。

    为什么用返回值而不是抛 `KeyError`：业务上"字段没填"就是"这个条件不满足"，
    不是错误。抛异常会逼着每个调用点写 try/except，而漏写一处就是 500。
    """
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def evaluate(rule: Any, context: Mapping[str, Any]) -> bool:
    """求值一个受限规则。**不求值字符串，不执行任何代码。**

    ============================ 三条语义约定 ============================
    1. **每个对象只能有一个操作符** —— 多个操作符会让"谁先算"变成隐式规则；
    2. **缺字段返回 False（不满足）**，不抛异常、不做猜测 ——
       这正是 `{"gte": ["amount", …]}`（漏了 `_fen`）会**静默失效**的原因：
       申请会以更低的层级被批准，而且不报任何错。所以路径白名单必须在**保存策略时**
       就拦住（见 `validate_policy`），不能指望运行期发现；
    3. **永远不 eval** —— 这是"受限"二字的全部意义。
    """
    if not isinstance(rule, Mapping) or len(rule) != 1:
        raise RuleError("每个规则对象必须且只能包含一个操作符")

    operator, operand = next(iter(rule.items()))
    if operator not in OPERATORS:
        raise RuleError(f"不支持的操作符：{operator}")

    if operator == "all":
        if not isinstance(operand, (list, tuple)) or not operand:
            raise RuleError("all 需要一个非空数组")
        return all(evaluate(item, context) for item in operand)
    if operator == "any":
        if not isinstance(operand, (list, tuple)) or not operand:
            raise RuleError("any 需要一个非空数组")
        return any(evaluate(item, context) for item in operand)
    if operator == "not":
        return not evaluate(operand, context)

    if not isinstance(operand, (list, tuple)) or not operand:
        raise RuleError(f"{operator} 需要一个非空数组")
    path = operand[0]
    if not isinstance(path, str) or not path:
        raise RuleError(f"{operator} 的第一个元素必须是字段路径字符串")

    exists, value = read_path(context, path)

    if operator == "exists":
        return exists
    if not exists:
        return False  # 缺字段一律判为不满足，不做猜测

    expected = operand[1] if len(operand) > 1 else None
    if operator == "eq":
        return bool(value == expected)
    if operator == "ne":
        return bool(value != expected)
    if operator == "in":
        if not isinstance(expected, (list, tuple)):
            raise RuleError("in 的第二个元素必须是一个数组")
        return value in expected
    if operator in NUMERIC_OPERATORS:
        # 比较前先挡掉"拿字符串跟数字比"这类静默错误：Python 会抛 TypeError，
        # 那会变成 500；统一转成 RuleError，由调用方按 422 处理。
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise RuleError(f"字段 {path!r} 不是数字，不能做大小比较")
        if not isinstance(expected, (int, float)) or isinstance(expected, bool):
            raise RuleError(f"{operator} 的阈值必须是数字")
        if operator == "lt":
            return value < expected
        if operator == "lte":
            return value <= expected
        if operator == "gt":
            return value > expected
        return value >= expected
    raise RuleError(f"未实现的操作符：{operator}")  # pragma: no cover - 上面已穷举


def validate_policy(policy: Mapping[str, Any]) -> list[str]:
    """语义校验审批策略，返回问题列表（空 = 通过）。

    调用方拿到非空列表时应直接 422，并把这些文字**原样**给管理员 ——
    里面带具体字段名，这是他们唯一能自救的线索。
    """
    problems: list[str] = []

    steps = policy.get("steps")
    if not isinstance(steps, (list, tuple)) or not steps:
        return ["策略至少要有一个审批步骤（steps 不能为空）"]

    # 兜底步骤可能写在 urgency_extra_step 里，一起检查
    candidates: list[Any] = list(steps)
    urgency = policy.get("urgency_extra_step")
    if urgency is not None:
        candidates.append(urgency)

    seen_orders: set[int] = set()
    has_fallback = False

    for index, step in enumerate(candidates):
        if not isinstance(step, Mapping):
            problems.append(f"第 {index + 1} 个审批步骤必须是对象")
            continue

        order = step.get("order")
        if isinstance(order, int) and not isinstance(order, bool):
            if order in seen_orders:
                problems.append(
                    f"order={order} 重复：审批链会按角色去重，其中一条会静默消失"
                )
            seen_orders.add(order)

        when = step.get("when")
        if when is None:
            problems.append(f"order={order!r} 的审批步骤缺少 when 条件")
            continue

        try:
            paths = collect_paths(when)
        except RuleError as exc:
            problems.append(f"order={order!r} 的 when 结构非法：{exc}")
            continue

        for path in paths:
            root = path.split(".")[0]
            if root not in ALLOWED_CONTEXT_KEYS:
                problems.append(
                    f"规则引用了未知字段 {path!r}；允许的字段是 "
                    f"{sorted(ALLOWED_CONTEXT_KEYS)}。"
                    "注意金额字段叫 amount_fen（单位：分），不是 amount"
                )

        for number in iter_numbers(when):
            if isinstance(number, float):
                problems.append(f"阈值 {number} 是浮点数；金额一律整数「分」")

        if is_zero_amount_fallback(when):
            has_fallback = True

    if not has_fallback:
        problems.append(
            "策略缺少「任何金额都命中」的兜底步骤（标准写法 amount_fen >= 0）："
            "某些金额区间的申请会算出空审批链，等于无人审批直接通过"
        )

    return problems
