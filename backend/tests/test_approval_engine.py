"""审批链解析的纯函数测试（M6）—— 本项目最值钱的一组断言。

**这张表的四行就是需求文档、测试用例与验收标准三合一**
（见 `docs/金额-审批链对照表.md`）：

    | 金额 | 应走的审批级 | 任务数 |
    | 500 元 | 部门主管 | 1 |
    | 5,000 元 | 主管 → 负责人 | 2 |
    | 50,000 元 | 主管 → 负责人 → 采购 → 财务 | 4 |
    | 200,000 元 | 再加总经理 | 5 |

它证明的是**审批链是数据驱动的**：换一份阈值不同的策略，链路跟着变，而代码一行不改。

不碰数据库、不碰 HTTP。
"""

import pytest

from app.domain.approval_engine import (
    PROBE_AMOUNTS_FEN,
    ApprovalStep,
    check_reachability,
    exclude_requester,
    is_eligible_approver,
    resolve_approval_chain,
)
from app.domain.rules import RuleError, evaluate, read_path

pytestmark = pytest.mark.unit


#: 与 `docs/金额-审批链对照表.md` 第四节那段 JSON 完全一致的策略。
#: 阈值一律「分」：100_000 = 1,000 元，1_000_000 = 10,000 元，10_000_000 = 100,000 元。
FULL_POLICY: dict = {
    "steps": [
        {"order": 10, "role": "DEPT_MANAGER", "mode": "SINGLE", "when": {"gte": ["amount_fen", 0]}},
        {"order": 20, "role": "DEPT_HEAD", "mode": "SINGLE", "when": {"gte": ["amount_fen", 100_000]}},
        {"order": 30, "role": "PROCUREMENT", "mode": "SINGLE", "when": {"gte": ["amount_fen", 1_000_000]}},
        {"order": 40, "role": "FINANCE", "mode": "SINGLE", "when": {"gte": ["amount_fen", 1_000_000]}},
        {"order": 50, "role": "GM", "mode": "SINGLE", "when": {"gte": ["amount_fen", 10_000_000]}},
    ],
    "skip_when_framework_contract": False,
}


def _roles(chain: list[ApprovalStep]) -> list[str]:
    return [step.role for step in chain]


def _chain(amount_fen: int) -> list[ApprovalStep]:
    return resolve_approval_chain(FULL_POLICY, {"amount_fen": amount_fen})


# ---------------------------------------------------------------- 对照表（核心验收）


@pytest.mark.parametrize(
    ("amount_fen", "yuan", "expected_roles", "expected_count"),
    [
        (50_000, 500, ["DEPT_MANAGER"], 1),
        (500_000, 5_000, ["DEPT_MANAGER", "DEPT_HEAD"], 2),
        (
            5_000_000,
            50_000,
            ["DEPT_MANAGER", "DEPT_HEAD", "PROCUREMENT", "FINANCE"],
            4,
        ),
        (
            20_000_000,
            200_000,
            ["DEPT_MANAGER", "DEPT_HEAD", "PROCUREMENT", "FINANCE", "GM"],
            5,
        ),
    ],
)
def test_amount_to_chain_table(
    amount_fen: int, yuan: int, expected_roles: list[str], expected_count: int
) -> None:
    """M6 的头号验收：四笔基准金额 → 1 / 2 / 4 / 5 级，且角色顺序正确。"""
    chain = _chain(amount_fen)
    assert _roles(chain) == expected_roles, f"{yuan} 元的审批链不对"
    assert len(chain) == expected_count


def test_chain_is_sorted_by_order() -> None:
    chain = _chain(20_000_000)
    assert [step.order for step in chain] == [10, 20, 30, 40, 50]


def test_order_field_drives_the_sequence_not_the_array_position() -> None:
    """**顺序按 `order` 而不是数组下标**。

    策略是管理员配的 JSON，数组顺序不可靠。这条用例把 steps 倒着写一遍，
    结果必须一样 —— 否则"改策略不改代码"就成了"改顺序就改行为"。
    """
    reversed_policy = dict(FULL_POLICY, steps=list(reversed(FULL_POLICY["steps"])))
    assert _roles(resolve_approval_chain(reversed_policy, {"amount_fen": 20_000_000})) == [
        "DEPT_MANAGER",
        "DEPT_HEAD",
        "PROCUREMENT",
        "FINANCE",
        "GM",
    ]


# ---------------------------------------------------------------- 同角色去重


def test_same_role_twice_keeps_the_earliest_one() -> None:
    """同一角色被策略写了两次 → 只保留 `order` 靠前的那个。

    不去重的话，部门主管会在待办里看到**两条一模一样的审批**。
    """
    policy = {
        "steps": [
            {"order": 10, "role": "DEPT_MANAGER", "when": {"gte": ["amount_fen", 0]}},
            {"order": 30, "role": "DEPT_MANAGER", "when": {"gte": ["amount_fen", 100_000]}},
            {"order": 40, "role": "FINANCE", "when": {"gte": ["amount_fen", 0]}},
        ]
    }
    chain = resolve_approval_chain(policy, {"amount_fen": 500_000})
    assert _roles(chain) == ["DEPT_MANAGER", "FINANCE"]
    assert chain[0].order == 10


# ---------------------------------------------------------------- 加急追加级


def test_urgency_extra_step_adds_one_level() -> None:
    policy = dict(
        FULL_POLICY,
        urgency_extra_step={
            "order": 5,
            "role": "DEPT_HEAD",
            "mode": "SINGLE",
            "when": {"eq": ["urgency", "URGENT"]},
        },
    )
    chain = resolve_approval_chain(policy, {"amount_fen": 50_000, "urgency": "URGENT"})
    # 500 元本来是 1 级（主管），加急追加负责人 → 2 级
    assert _roles(chain) == ["DEPT_HEAD", "DEPT_MANAGER"]
    assert chain[0].source == "URGENCY"
    assert chain[1].source == "POLICY"


def test_urgency_extra_step_ignored_when_not_urgent() -> None:
    policy = dict(
        FULL_POLICY,
        urgency_extra_step={
            "order": 5,
            "role": "DEPT_HEAD",
            "when": {"eq": ["urgency", "URGENT"]},
        },
    )
    chain = resolve_approval_chain(policy, {"amount_fen": 50_000, "urgency": "NORMAL"})
    assert _roles(chain) == ["DEPT_MANAGER"]


# ---------------------------------------------------------------- 框架协议


def test_framework_contract_skips_procurement() -> None:
    """走框架协议的品类跳过询价环节，因此不需要采购专员审批。"""
    policy = dict(FULL_POLICY, skip_when_framework_contract=True)
    chain = resolve_approval_chain(
        policy, {"amount_fen": 5_000_000, "framework_contract": True}
    )
    assert "PROCUREMENT" not in _roles(chain)
    assert _roles(chain) == ["DEPT_MANAGER", "DEPT_HEAD", "FINANCE"]


def test_framework_flag_off_keeps_procurement() -> None:
    policy = dict(FULL_POLICY, skip_when_framework_contract=True)
    chain = resolve_approval_chain(
        policy, {"amount_fen": 5_000_000, "framework_contract": False}
    )
    assert "PROCUREMENT" in _roles(chain)


# ---------------------------------------------------------------- 求值语义


def test_missing_field_is_false_not_an_error() -> None:
    """**缺字段返回 False，不抛异常** —— 这正是"写错字段名会静默失效"的根源。

    所以 `validate_policy` 的路径白名单不是可选项：它必须**在保存策略时**拦住，
    运行期是发现不了的。
    """
    assert evaluate({"gte": ["amount_fen", 0]}, {}) is False


def test_wrong_field_name_silently_matches_nothing() -> None:
    """漏了 `_fen` 的规则求值为假 —— 链路因此少一级，且不报任何错。"""
    assert evaluate({"gte": ["amount", 100_000]}, {"amount_fen": 500_000}) is False


def test_logical_operators() -> None:
    context = {"amount_fen": 500_000, "urgency": "URGENT"}
    assert evaluate({"all": [{"gte": ["amount_fen", 0]}, {"eq": ["urgency", "URGENT"]}]}, context)
    assert evaluate({"any": [{"gte": ["amount_fen", 10_000_000]}, {"eq": ["urgency", "URGENT"]}]}, context)
    assert evaluate({"not": {"exists": ["framework_contract"]}}, context)
    assert evaluate({"exists": ["amount_fen"]}, context)
    assert evaluate({"in": ["urgency", ["NORMAL", "URGENT"]]}, context)


def test_comparing_a_non_number_raises_rule_error_not_type_error() -> None:
    """拿字符串跟数字比要报 RuleError（→422），而不是 TypeError（→500）。"""
    with pytest.raises(RuleError):
        evaluate({"gte": ["urgency", 10]}, {"urgency": "URGENT"})


def test_read_path_returns_existence_and_value() -> None:
    assert read_path({"a": 1}, "a") == (True, 1)
    assert read_path({"a": 1}, "b") == (False, None)


# ---------------------------------------------------------------- 可达性校验


def test_reachability_ok_for_the_reference_policy() -> None:
    assert check_reachability(FULL_POLICY) == []


def test_reachability_reports_a_step_that_never_triggers() -> None:
    """配了但永远不会生效的一级 = 审批层级**静默降低**。"""
    policy = {
        "steps": [
            {"order": 10, "role": "DEPT_MANAGER", "when": {"gte": ["amount_fen", 0]}},
            # 阈值高到探测金额都够不着（10_000_000 是探测上限，这里要 20_000_000）
            {"order": 20, "role": "GM", "when": {"gte": ["amount_fen", 20_000_000]}},
        ]
    }
    problems = check_reachability(policy)
    assert any("order=20" in problem for problem in problems)


def test_reachability_reports_empty_chain_for_some_amount() -> None:
    """最低阈值 100 元、没有零阈值兜底 → 小额申请会算出**空审批链**。"""
    policy = {
        "steps": [
            {"order": 10, "role": "DEPT_MANAGER", "when": {"gte": ["amount_fen", 10_000]}},
        ]
    }
    problems = check_reachability(policy)
    assert any("审批链为空" in problem for problem in problems)


def test_probe_amounts_cover_the_first_fen() -> None:
    """探测金额必须包含 0 分：兜底步骤通常就是"金额 ≥ 0"，少了它就误报。"""
    assert PROBE_AMOUNTS_FEN[0] == 0


# ---------------------------------------------------------------- 职责分离（SoD）

#: 一个最小的"候选人"：只要有 `id`，`exclude_requester` 就能用。
class _Person:
    def __init__(self, person_id: str) -> None:
        self.id = person_id


REQUESTER = "u-requester"
OTHER_A = "u-other-a"
OTHER_B = "u-other-b"


def test_requester_is_not_an_eligible_approver() -> None:
    """**提交人不能当自己申请这一级的审批人。**（2026-10-03 补）"""
    assert is_eligible_approver(OTHER_A, REQUESTER) is True
    assert is_eligible_approver(REQUESTER, REQUESTER) is False


def test_none_is_never_eligible() -> None:
    """fail-closed：身份缺失一律判不合格，不能"认不出就放行"。"""
    assert is_eligible_approver(None, REQUESTER) is False


def test_exclude_requester_removes_only_the_requester() -> None:
    candidates = [_Person(REQUESTER), _Person(OTHER_A), _Person(OTHER_B)]
    kept = exclude_requester(candidates, REQUESTER)
    assert [item.id for item in kept] == [OTHER_A, OTHER_B]


def test_exclude_requester_can_empty_the_list() -> None:
    """部门只有 1 个主管时，把他剔掉就没人了 —— 调用方必须据此报错。

    这正是漏掉 SoD 时最危险的情形：不剔除的话会生成一条"自己审自己"的任务，
    而且看起来完全正常。
    """
    assert exclude_requester([_Person(REQUESTER)], REQUESTER) == []


def test_exclude_requester_keeps_order_for_all_mode() -> None:
    """会签（ALL）预分配时顺序要保持，剔掉提交人后其余人照旧。"""
    people = [_Person(f"u-{index}") for index in range(5)]
    kept = exclude_requester(people, "u-2")
    assert [item.id for item in kept] == ["u-0", "u-1", "u-3", "u-4"]
