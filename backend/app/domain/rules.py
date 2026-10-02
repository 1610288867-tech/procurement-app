"""受限规则 DSL —— 词表、遍历与**语义校验**（M2）。求值在 M5。

这一层是纯 Python，**不 import 任何框架**。

============================ 为什么必须有语义校验 ============================
结构合法但语义错误的策略**不会报任何错**。因为规则求值对"缺字段"的语义是
返回 False（不满足），于是整条规则**静默失效**，申请会以**更低的审批层级**
被批准 —— 这是审批系统里最严重的失败模式。

所以保存策略时必须拦住三类问题：

    1. 路径不在白名单（典型：写成 amount 而不是 amount_fen）
    2. 阈值是浮点（金额一律整数「分」）
    3. 没有零阈值兜底步骤（某些金额区间会算出空审批链 = 无人审批）

第三层"每个步骤都可达"要用引擎反推，属于 M5。
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
