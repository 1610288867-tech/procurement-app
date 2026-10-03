"""统一的**测试数据清理**入口 —— 浏览器自检与页面实测共用这一个脚本。

    cd backend

    # 只清页面实测（scripts/m6-页面实测.mjs）造的数据
    .venv\\Scripts\\python.exe scripts\\testdata_cleanup.py

    # 连浏览器自检（scripts/浏览器自检.mjs）造的一起清（推荐：两个都跑过之后用这个）
    .venv\\Scripts\\python.exe scripts\\testdata_cleanup.py --scope all

    # 只看会删什么，不真删
    .venv\\Scripts\\python.exe scripts\\testdata_cleanup.py --scope all --dry-run

============================ 为什么需要它 ============================
采购申请、品类、部门、账号都**没有删除端点** —— 这是刻意的：

  - `discard` 是把状态置为终态，**不是删除**；
  - 删数据属于运维动作，不该是一个业务接口（否则一个拿到令牌的人就能抹掉台账）。

而两个浏览器脚本**必须造数据**（不造就验不了"界面真的建出了东西""审批链真的画出来了"）。
所以清理单独做成脚本，**判据写死、可以复核、可以空跑**。

============================ 判据（明确前缀，不做模糊匹配）============================
| 对象 | 判据 |
| --- | --- |
| 条目 | `title` 以 `M6 页面实测` 开头（页面实测） |
| 品类 | `code` 以 `TCAT-PAGE-`（页面实测）/ `SELFCHECK-`（浏览器自检）/ `TCAT-`（集成测试兜底）开头 |
| 部门 | `code` 以 `TDEP-` 开头（集成测试兜底） |
| 账号 | `username` 以 `t-` 开头（测试工厂用的前缀） |

**保留**：种子数据（`CAT-OFFICE` / `CAT-IT` / `ADM` / `PUR` / `FIN` / …）、
手工数据、以及任何不符合上述前缀的记录。

============================ 它清不掉什么（已知边界）============================
**用种子品类建的申请清不掉**，而且这是刻意的：

  - 申请是按"品类前缀"或"标题前缀 `M6 页面实测`"选的；
  - 手工/演示建的申请通常用**种子品类**（`CAT-OFFICE` 等），标题也不是测试前缀，
    所以它**不在删除范围内** —— 这是**保护**，不是遗漏。

想清掉这类数据只有两条路：① 在界面上用「丢弃」（那是状态终态，不是删除）；
② 明确指定 id 删（属于运维动作，别写进自动化脚本）。

删完会**回查并打印残留条数**，非 0 时退出码为 1（便于接进流水线）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

sys.path.insert(0, ".")

from sqlalchemy import delete, func, or_, select  # noqa: E402

from app.database import SessionFactory, engine  # noqa: E402
from app.infrastructure.records.approval import (  # noqa: E402
    ApprovalFlowRecord,
    ApprovalTaskRecord,
)
from app.infrastructure.records.auth import UserRecord  # noqa: E402
from app.infrastructure.records.catalog import (  # noqa: E402
    BudgetRecord,
    DepartmentRecord,
    PurchaseCategoryRecord,
)
from app.infrastructure.records.purchase import (  # noqa: E402
    IdempotencyRecord,
    PurchaseItemRecord,
    PurchaseRequestRecord,
    RequestEventRecord,
)

#: 页面实测（m6-页面实测.mjs）造的数据
PAGE_TITLE_PREFIX = "M6 页面实测%"
PAGE_CATEGORY_PREFIX = "TCAT-PAGE-%"

#: 浏览器自检（浏览器自检.mjs）造的数据
SELFCHECK_CATEGORY_PREFIX = "SELFCHECK-%"

#: 集成测试的兜底前缀（正常情况下测试自己会清，这里防漏）
TEST_CATEGORY_PREFIX = "TCAT-%"
TEST_DEPARTMENT_PREFIX = "TDEP-%"
TEST_USERNAME_PREFIX = "t-%"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="清理浏览器自检 / 页面实测留下的测试数据",
    )
    parser.add_argument(
        "--scope",
        choices=["page", "all"],
        default="page",
        help="page = 只清页面实测（默认）；all = 连浏览器自检与集成测试残留一起清",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只列出会删什么，不真删",
    )
    return parser.parse_args()


async def _run(scope: str, dry_run: bool) -> int:
    category_prefixes = [PAGE_CATEGORY_PREFIX]
    if scope == "all":
        category_prefixes += [SELFCHECK_CATEGORY_PREFIX, TEST_CATEGORY_PREFIX]

    async with SessionFactory() as session:
        # ---- 选定要删的对象 ----
        category_ids = list(
            (
                await session.scalars(
                    select(PurchaseCategoryRecord.id).where(
                        or_(
                            *[
                                PurchaseCategoryRecord.code.like(prefix)
                                for prefix in category_prefixes
                            ]
                        )
                    )
                )
            ).all()
        )
        department_ids = (
            list(
                (
                    await session.scalars(
                        select(DepartmentRecord.id).where(
                            DepartmentRecord.code.like(TEST_DEPARTMENT_PREFIX)
                        )
                    )
                ).all()
            )
            if scope == "all"
            else []
        )
        username_pattern = TEST_USERNAME_PREFIX if scope == "all" else "t-never-match-%"

        request_ids = list(
            (
                await session.scalars(
                    select(PurchaseRequestRecord.id).where(
                        or_(
                            PurchaseRequestRecord.title.like(PAGE_TITLE_PREFIX),
                            PurchaseRequestRecord.category_id.in_(category_ids or [None]),
                        )
                    )
                )
            ).all()
        )
        user_ids = list(
            (
                await session.scalars(
                    select(UserRecord.id).where(UserRecord.username.like(username_pattern))
                )
            ).all()
        )

        print(f"范围={scope}{'（空跑）' if dry_run else ''}")
        print(
            f"  匹配到：品类 {len(category_ids)} / 部门 {len(department_ids)} / "
            f"账号 {len(user_ids)} / 申请 {len(request_ids)}"
        )
        if dry_run:
            names = list(
                (
                    await session.scalars(
                        select(PurchaseCategoryRecord.code).where(
                            PurchaseCategoryRecord.id.in_(category_ids or [None])
                        )
                    )
                ).all()
            )
            for name in names:
                print(f"    会删品类：{name}")
            return 0

        counts: dict[str, int] = {}

        # ---- ① 申请及其从属数据（先删引用方，避免外键挡住）----
        if request_ids:
            for label, model in (
                ("审批任务", ApprovalTaskRecord),
                ("审批流", ApprovalFlowRecord),
                ("事件", RequestEventRecord),
                ("明细", PurchaseItemRecord),
            ):
                counts[label] = (
                    await session.execute(
                        delete(model).where(model.request_id.in_(request_ids))
                    )
                ).rowcount or 0
            counts["申请"] = (
                await session.execute(
                    delete(PurchaseRequestRecord).where(
                        PurchaseRequestRecord.id.in_(request_ids)
                    )
                )
            ).rowcount or 0

        # ---- ② 账号：先清引用它的三张表，再删账号本身 ----
        if user_ids:
            counts["幂等记录"] = (
                await session.execute(
                    delete(IdempotencyRecord).where(
                        IdempotencyRecord.actor_id.in_(user_ids)
                    )
                )
            ).rowcount or 0
            counts["事件(actor)"] = (
                await session.execute(
                    delete(RequestEventRecord).where(
                        RequestEventRecord.actor_id.in_(user_ids)
                    )
                )
            ).rowcount or 0
            counts["审批任务(决定人)"] = (
                await session.execute(
                    delete(ApprovalTaskRecord).where(
                        or_(
                            ApprovalTaskRecord.decided_by.in_(user_ids),
                            ApprovalTaskRecord.assignee_id.in_(user_ids),
                        )
                    )
                )
            ).rowcount or 0
            counts["账号"] = (
                await session.execute(
                    delete(UserRecord).where(UserRecord.id.in_(user_ids))
                )
            ).rowcount or 0

        # ---- ③ 品类：预算引用它（RESTRICT），必须先删预算 ----
        if category_ids:
            counts["预算(按品类)"] = (
                await session.execute(
                    delete(BudgetRecord).where(BudgetRecord.category_id.in_(category_ids))
                )
            ).rowcount or 0
            counts["品类"] = (
                await session.execute(
                    delete(PurchaseCategoryRecord).where(
                        PurchaseCategoryRecord.id.in_(category_ids)
                    )
                )
            ).rowcount or 0

        # ---- ④ 部门：**四处** RESTRICT 引用都要先清 ----
        # users.department_id / budgets.department_id /
        # purchase_requests.department_id / departments.parent_id
        # 漏一处，删部门就静默 0 行、留下孤儿部门（实测踩过）。
        if department_ids:
            counts["预算(按部门)"] = (
                await session.execute(
                    delete(BudgetRecord).where(
                        BudgetRecord.department_id.in_(department_ids)
                    )
                )
            ).rowcount or 0
            counts["账号(按部门)"] = (
                await session.execute(
                    delete(UserRecord).where(
                        UserRecord.username.like(TEST_USERNAME_PREFIX),
                        UserRecord.department_id.in_(department_ids),
                    )
                )
            ).rowcount or 0
            counts["申请(按部门)"] = (
                await session.execute(
                    delete(PurchaseRequestRecord).where(
                        PurchaseRequestRecord.department_id.in_(department_ids)
                    )
                )
            ).rowcount or 0
            counts["部门"] = (
                await session.execute(
                    delete(DepartmentRecord).where(
                        DepartmentRecord.id.in_(department_ids)
                    )
                )
            ).rowcount or 0

        await session.commit()

        deleted = {key: value for key, value in counts.items() if value}
        print(f"  已删除：{deleted if deleted else '（没有符合条件的记录）'}")

        # ---- 回查：残留必须为 0 ----
        leftover = {
            "品类": (
                await session.execute(
                    select(func.count())
                    .select_from(PurchaseCategoryRecord)
                    .where(
                        or_(
                            *[
                                PurchaseCategoryRecord.code.like(prefix)
                                for prefix in category_prefixes
                            ]
                        )
                    )
                )
            ).scalar_one(),
            "部门": (
                await session.execute(
                    select(func.count())
                    .select_from(DepartmentRecord)
                    .where(DepartmentRecord.code.like(TEST_DEPARTMENT_PREFIX))
                )
            ).scalar_one()
            if scope == "all"
            else 0,
            "账号": (
                await session.execute(
                    select(func.count())
                    .select_from(UserRecord)
                    .where(UserRecord.username.like(username_pattern))
                )
            ).scalar_one(),
            "申请": (
                await session.execute(
                    select(func.count())
                    .select_from(PurchaseRequestRecord)
                    .where(PurchaseRequestRecord.title.like(PAGE_TITLE_PREFIX))
                )
            ).scalar_one(),
        }
        print(f"  清理后残留：{leftover}")
        ok = all(value == 0 for value in leftover.values())
        print("  ✅ 干净" if ok else "  ❌ 仍有残留，请检查上面的判据")
        return 0 if ok else 1


def main() -> int:
    args = _parse_args()

    async def wrapped() -> int:
        try:
            return await _run(args.scope, args.dry_run)
        finally:
            # 连接池绑定在创建它的事件循环上，脚本结束前丢掉，避免复用时报
            # "'NoneType' object has no attribute 'send'"
            await engine.dispose()

    return asyncio.run(wrapped())


if __name__ == "__main__":
    sys.exit(main())
