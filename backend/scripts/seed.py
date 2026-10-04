"""演示数据：部门 / 品类 / 供应商 / 预算 / 账号。

用法（**在 backend 目录下**）：

    python -m scripts.seed

为什么必须按模块跑：`python scripts/seed.py` 会把 sys.path[0] 设成 scripts/，
于是 import app 失败。用 `-m` 才会把 backend 放进搜索路径。

两条设计：

1. **幂等**：按 code / 唯一键查重，已存在就跳过 —— 可以反复跑，不会造出重复数据，
   也不会覆盖你手工改过的审批策略。
2. **用同一套 ORM 模型写数据，不手写 SQL**：迁移一改，种子数据跟着改，不会悄悄失效。

M3 已加账号：**8 个角色、7 个人**（`GM` 由 `ADMIN` 兼任）。
M4 再加草稿申请。
"""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.database import SessionFactory
from app.infrastructure.records.auth import UserRecord, UserRoleRecord
from app.infrastructure.records.catalog import (
    BudgetRecord,
    DepartmentRecord,
    PurchaseCategoryRecord,
    SupplierRecord,
)
from app.infrastructure.security import hash_password

# 审批阈值一律「分」。这份策略与 docs/金额-审批链对照表.md 一致：
# 500 / 5000 / 50000 / 200000 元 → 1 / 2 / 4 / 5 级
OFFICE_POLICY = {
    "steps": [
        {"order": 10, "role": "DEPT_MANAGER", "mode": "SINGLE", "when": {"gte": ["amount_fen", 0]}},
        {"order": 20, "role": "DEPT_HEAD", "mode": "SINGLE", "when": {"gte": ["amount_fen", 100_000]}},
        {"order": 30, "role": "PROCUREMENT", "mode": "SINGLE", "when": {"gte": ["amount_fen", 1_000_000]}},
        {"order": 40, "role": "FINANCE", "mode": "SINGLE", "when": {"gte": ["amount_fen", 1_000_000]}},
        {"order": 50, "role": "GM", "mode": "SINGLE", "when": {"gte": ["amount_fen", 10_000_000]}},
    ],
    "skip_when_framework_contract": False,
}

# 小额度品类：只走到部门负责人
IT_POLICY = {
    "steps": [
        {"order": 10, "role": "DEPT_MANAGER", "mode": "SINGLE", "when": {"gte": ["amount_fen", 0]}},
        {"order": 20, "role": "DEPT_HEAD", "mode": "SINGLE", "when": {"gte": ["amount_fen", 500_000]}},
    ],
    "skip_when_framework_contract": False,
}

FORM_SCHEMA = {
    "type": "object",
    "properties": {
        "purpose": {"type": "string", "title": "用途"},
        "expected_date": {"type": "string", "format": "date", "title": "期望到货日期"},
    },
    "required": ["purpose"],
}

DEPARTMENTS = [
    ("ADM", "行政部", None),
    ("ADM-LOG", "后勤组", "ADM"),
    ("PUR", "采购部", None),
    ("FIN", "财务部", None),
]

CATEGORIES = [
    ("CAT-OFFICE", "办公用品", OFFICE_POLICY),
    ("CAT-IT", "电脑设备", IT_POLICY),
]

SUPPLIERS = [
    ("SUP-CG-001", "晨光文具（演示）", 365),
    ("SUP-LENOVO-001", "联想华东（演示）", None),
]

# 演示口令。**只用于本地合成数据**，任何真实环境都必须换掉。
DEMO_PASSWORD = "Dev-Passw0rd!"

# (用户名, 姓名, 主角色, 所属部门代码)
# 8 个角色、7 个人：GM 由 admin 兼任（user_roles 本来就是多对多，不用额外建表）。
USERS = [
    ("requester", "张申请", "REQUESTER", "ADM"),
    ("dept-manager", "李主管", "DEPT_MANAGER", "ADM"),
    ("dept-head", "王负责人", "DEPT_HEAD", "ADM"),
    ("procurement", "赵采购", "PROCUREMENT", "PUR"),
    ("finance", "钱财务", "FINANCE", "FIN"),
    ("receiver", "孙收货", "RECEIVER", "ADM"),
    ("admin", "系统管理员", "ADMIN", None),
]

# 一个用户可持多个角色；这里只有 admin 需要第二个
EXTRA_ROLES: dict[str, list[str]] = {"admin": ["GM"]}


async def seed() -> None:
    now = datetime.now(UTC)
    created: list[str] = []
    skipped: list[str] = []

    async with SessionFactory() as session:
        # ---- 部门（先建父，再建子，才能引用 parent_id）----
        departments: dict[str, DepartmentRecord] = {}
        for code, name, parent_code in DEPARTMENTS:
            existing = (
                await session.scalars(
                    select(DepartmentRecord).where(DepartmentRecord.code == code)
                )
            ).one_or_none()
            if existing is not None:
                departments[code] = existing
                skipped.append(f"部门 {code}")
                continue
            record = DepartmentRecord(
                code=code,
                name=name,
                parent_id=departments[parent_code].id if parent_code else None,
            )
            session.add(record)
            await session.flush()
            departments[code] = record
            created.append(f"部门 {code} {name}")

        # ---- 品类 ----
        categories: dict[str, PurchaseCategoryRecord] = {}
        for code, name, policy in CATEGORIES:
            existing = (
                await session.scalars(
                    select(PurchaseCategoryRecord).where(PurchaseCategoryRecord.code == code)
                )
            ).one_or_none()
            if existing is not None:
                categories[code] = existing
                skipped.append(f"品类 {code}")
                continue
            record = PurchaseCategoryRecord(
                code=code,
                name=name,
                form_schema=FORM_SCHEMA,
                approval_policy=policy,
            )
            session.add(record)
            await session.flush()
            categories[code] = record
            created.append(f"品类 {code} {name}（{len(policy['steps'])} 级审批）")

        # ---- 供应商 ----
        for code, name, valid_days in SUPPLIERS:
            existing = (
                await session.scalars(
                    select(SupplierRecord).where(SupplierRecord.code == code)
                )
            ).one_or_none()
            if existing is not None:
                skipped.append(f"供应商 {code}")
                continue
            session.add(
                SupplierRecord(
                    code=code,
                    name=name,
                    qualification_expires_at=(
                        now + timedelta(days=valid_days) if valid_days else None
                    ),
                )
            )
            created.append(f"供应商 {code} {name}")

        # ---- 预算 ----
        # M7 起**提交必须有预算**：该部门 × 品类 × 年度没有预算行 = 计划外 → 422。
        # 所以演示数据要给"会被用来提单"的品类都配一条，否则页面上一提交就报错。
        year = now.year
        for department_code, category_code, amount_fen, label in (
            ("ADM", "CAT-OFFICE", 10_000_000, "100000.00 元"),
            ("ADM", "CAT-IT", 5_000_000, "50000.00 元"),
        ):
            department = departments[department_code]
            category = categories[category_code]
            existing_budget = (
                await session.scalars(
                    select(BudgetRecord).where(
                        BudgetRecord.department_id == department.id,
                        BudgetRecord.category_id == category.id,
                        BudgetRecord.year == year,
                    )
                )
            ).one_or_none()
            if existing_budget is not None:
                skipped.append(f"预算 {year} {department_code} × {category_code}")
                continue
            session.add(
                BudgetRecord(
                    department_id=department.id,
                    category_id=category.id,
                    year=year,
                    amount_total=amount_fen,
                )
            )
            created.append(f"预算 {year} {department_code} × {category_code} = {label}")

        # ---- 账号（口令一律 Argon2id 哈希，库里看不到原文）----
        for username, full_name, role, department_code in USERS:
            existing_user = (
                await session.scalars(
                    select(UserRecord).where(UserRecord.username == username)
                )
            ).one_or_none()
            if existing_user is not None:
                skipped.append(f"账号 {username}")
                continue
            user = UserRecord(
                username=username,
                password_hash=hash_password(DEMO_PASSWORD),
                full_name=full_name,
                department_id=(
                    departments[department_code].id if department_code else None
                ),
            )
            session.add(user)
            await session.flush()
            assigned = [role, *EXTRA_ROLES.get(username, [])]
            for role_name in assigned:
                session.add(UserRoleRecord(user_id=user.id, role=role_name))
            created.append(f"账号 {username} {full_name}（{' + '.join(assigned)}）")

        await session.commit()

    print(f"新建 {len(created)} 项：")
    for line in created:
        print(f"  + {line}")
    if skipped:
        print(f"已存在，跳过 {len(skipped)} 项：")
        for line in skipped:
            print(f"  = {line}")
    print(f"\n账号口令统一为：{DEMO_PASSWORD}（仅本地演示数据）")


if __name__ == "__main__":
    asyncio.run(seed())
