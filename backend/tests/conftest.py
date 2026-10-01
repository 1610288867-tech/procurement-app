"""测试共用的 fixture。

分档纪律（见 pyproject.toml）：

    pytest -m unit          纯函数，不连数据库，毫秒级（**默认档**）
    pytest -m integration   需要 PostgreSQL 在跑
    pytest -m ""            全量

M0 阶段只有一个健康检查冒烟测试，用来证明"pytest 能跑"。
从 M2 起开始放真正的东西：test_rules.py / test_approval_engine.py /
test_state_machine.py / test_budget_concurrency.py / test_settlement_policy.py。
"""

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
