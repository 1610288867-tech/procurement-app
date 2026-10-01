"""domain —— 纯 Python 业务逻辑层。

============================ 这一层的铁律 ============================
本目录下的任何文件**不允许出现**：

    import sqlalchemy
    from fastapi import ...
    import httpx / redis / minio / alembic

一旦出现，说明分层错了。可以用这条命令自查（应无任何输出）：

    Select-String -Path backend\\app\\domain\\*.py -Pattern "import (fastapi|sqlalchemy|redis|httpx|minio)"

============================ 为什么值得坚持 ============================
这一层里的东西是纯函数：没有数据库、没有 HTTP、没有全局状态。
所以它能脱离环境直接单测，跑一次是毫秒级。

M5（审批引擎）和 M6（预算策略）必须先在这里写完并测绿，再去动数据库——
"改一行、重启服务、点半小时页面"的痛苦，全部来自顺序反了。

============================ 这一层应当占一半以上 ============================
判断标准：如果 `domain/` 里出现了技术细节（SQL、HTTP、JSON 序列化），
那部分逻辑就不该待在这里。

M0 阶段本目录为空，从 M2 起开始填充：
    enums.py              状态、角色、品类枚举
    entities.py           dataclass 业务实体
    rules.py              受限规则 DSL 求值器（M-06）
    state_machine.py      主单状态迁移表（M-05）
    approval_engine.py    resolve_approval_chain 纯函数（M-07，全项目核心）
    budget_policy.py      预算可用额计算（M-08）
    sourcing_policy.py    三家比价规则（M-12）
    settlement_policy.py  三单匹配规则（M-14）
    grounded_answer.py    溯源与引用校验（M-15）
    permissions.py        数据级权限的可见范围条件（M-03）
"""
