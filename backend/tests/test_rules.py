"""审批策略语义校验的单元测试（纯函数，不连数据库）。

这一层的价值全在"拦得住"：结构合法但语义错误的策略如果放进去，
**不会报错、只会静默失效**，申请会以更低的审批层级被批准。
"""

import pytest

from app.domain.rules import (
    RuleError,
    collect_paths,
    is_zero_amount_fallback,
    iter_numbers,
    validate_policy,
)

pytestmark = pytest.mark.unit


def _policy(*steps: dict) -> dict:
    return {"steps": list(steps)}


FALLBACK = {"order": 10, "role": "DEPT_MANAGER", "when": {"gte": ["amount_fen", 0]}}


# ---------- collect_paths ----------


def test_collect_paths_walks_nested_rules() -> None:
    rule = {
        "all": [
            {"gte": ["amount_fen", 100_000]},
            {"any": [{"eq": ["urgency", "URGENT"]}, {"not": {"exists": ["framework_contract"]}}]},
        ]
    }
    assert sorted(collect_paths(rule)) == [
        "amount_fen",
        "framework_contract",
        "urgency",
    ]


def test_collect_paths_rejects_multi_operator_object() -> None:
    with pytest.raises(RuleError):
        collect_paths({"gte": ["amount_fen", 0], "lte": ["amount_fen", 1]})


def test_collect_paths_rejects_unknown_operator() -> None:
    with pytest.raises(RuleError):
        collect_paths({"between": ["amount_fen", 0, 1]})


# ---------- iter_numbers ----------


def test_iter_numbers_skips_booleans() -> None:
    assert iter_numbers({"eq": ["framework_contract", True]}) == []


def test_iter_numbers_finds_float_threshold() -> None:
    assert iter_numbers({"gte": ["amount_fen", 10000.5]}) == [10000.5]


# ---------- is_zero_amount_fallback ----------


def test_fallback_recognises_canonical_shape() -> None:
    assert is_zero_amount_fallback({"gte": ["amount_fen", 0]}) is True


def test_fallback_rejects_positive_threshold() -> None:
    assert is_zero_amount_fallback({"gte": ["amount_fen", 1]}) is False


def test_fallback_ignores_other_fields() -> None:
    assert is_zero_amount_fallback({"gte": ["urgency", 0]}) is False


# ---------- validate_policy ----------


def test_valid_policy_passes() -> None:
    policy = _policy(
        FALLBACK,
        {"order": 20, "role": "DEPT_HEAD", "when": {"gte": ["amount_fen", 100_000]}},
    )
    assert validate_policy(policy) == []


def test_policy_without_fallback_is_rejected() -> None:
    """缺兜底 → 某些金额区间会算出空审批链 = 无人审批直接通过。"""
    policy = _policy(
        {"order": 10, "role": "DEPT_HEAD", "when": {"gte": ["amount_fen", 100_000]}}
    )
    problems = validate_policy(policy)
    assert len(problems) == 1
    assert "兜底" in problems[0]


def test_field_typo_amount_is_rejected_and_names_amount_fen() -> None:
    """最危险的错误：写成 amount。必须拦下，并且提示里点名字段名。"""
    policy = _policy(
        FALLBACK,
        {"order": 20, "role": "DEPT_HEAD", "when": {"gte": ["amount", 100_000]}},
    )
    problems = validate_policy(policy)
    assert any("amount_fen" in problem and "'amount'" in problem for problem in problems)


def test_float_threshold_is_rejected() -> None:
    policy = _policy(
        FALLBACK,
        {"order": 20, "role": "DEPT_HEAD", "when": {"gte": ["amount_fen", 10000.5]}},
    )
    assert any("浮点" in problem for problem in validate_policy(policy))


def test_duplicate_order_is_rejected() -> None:
    policy = _policy(
        FALLBACK,
        {"order": 10, "role": "DEPT_HEAD", "when": {"gte": ["amount_fen", 100_000]}},
    )
    assert any("order=10 重复" in problem for problem in validate_policy(policy))


def test_empty_steps_is_rejected() -> None:
    assert validate_policy({"steps": []}) == ["策略至少要有一个审批步骤（steps 不能为空）"]


def test_step_without_when_is_rejected() -> None:
    problems = validate_policy(_policy({"order": 10, "role": "DEPT_MANAGER"}))
    assert any("缺少 when" in problem for problem in problems)


def test_urgency_step_also_counts_towards_fallback() -> None:
    """兜底写在 urgency_extra_step 里也算 —— 但主 steps 里仍要有能命中的步骤。"""
    policy = {
        "steps": [{"order": 10, "role": "DEPT_HEAD", "when": {"gte": ["amount_fen", 1]}}],
        "urgency_extra_step": {"order": 5, "role": "GM", "when": {"gte": ["amount_fen", 0]}},
    }
    assert validate_policy(policy) == []
