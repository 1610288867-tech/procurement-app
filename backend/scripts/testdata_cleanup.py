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
| 预算流水 | 不按前缀认：按"上面这些**要删的预算**"反查（M7 起预算行不能带着流水被删） |

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
from pathlib import Path

sys.path.insert(0, ".")

from sqlalchemy import delete, func, or_, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import SessionFactory, engine  # noqa: E402
from app.infrastructure.records.approval import (  # noqa: E402
    ApprovalFlowRecord,
    ApprovalTaskRecord,
)
from app.infrastructure.records.attachment import AttachmentRecord  # noqa: E402
from app.infrastructure.records.auth import UserRecord  # noqa: E402
from app.infrastructure.records.catalog import (  # noqa: E402
    BudgetRecord,
    BudgetTransactionRecord,
    DepartmentRecord,
    PurchaseCategoryRecord,
)
from app.infrastructure.records.knowledge import (  # noqa: E402
    ChatMessageRecord,
    ChatSessionRecord,
    KnowledgeChunkRecord,
    KnowledgeDocRecord,
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

#: M11 页面实测（m11-知识库实测.mjs）与集成测试造的制度文档。
#: **按 `source_key` 前缀认**，不按标题：标题是人写的（可能重名），
#: 而 `source_key` 是脚本自己拼的唯一标识。
KNOWLEDGE_SOURCE_PREFIXES = ("M11PAGE-%", "M11TEST-%", "PROBE%")

#: 集成测试的兜底前缀（正常情况下测试自己会清，这里防漏）
TEST_CATEGORY_PREFIX = "TCAT-%"
TEST_DEPARTMENT_PREFIX = "TDEP-%"
TEST_USERNAME_PREFIX = "t-%"

#: **演示用的系统管理员账号名**。
#:
#: 为什么要单列它：脚本与集成测试大量用 `admin` 提问（M11 的问答要一个登录账号），
#: 于是会话与消息挂在 admin 名下积起来（实测跑几轮攒了 14 个）。
#: 它既不是 `t-%` 测试账号、也没法按 `source_key` 前缀认 ——
#: 所以这里**按账号名清**：演示库里 admin 的聊天记录一定是测试/演示留下的，
#: 而真要有"管理员在用"的场合，这个清理脚本本来就不该在那台机器上跑。
#: 只清**聊天**（会话 / 消息），不动 admin 账号与它建的其它数据。
DEMO_ADMIN_USERNAME = "admin"


async def _registered_keys(session: AsyncSession) -> set[str]:
    """库里所有**在册**的 `storage_key`：附件 + 知识库文档。

    两组都要算，因为它们**共用同一个 `uploads/` 根目录**：

      · 附件（M9）：软删除的也要算"在册"—— 附件是审计证据，删除只是打标记，
        磁盘上那份必须留着，否则"当时到底附了什么"就真查不出来了；
      · 制度文档原文（M11）：`knowledge_docs.storage_key`。漏了它
        就会把制度原文当成孤儿文件删掉（库里有行、文件没了）。
    """
    attachment_rows = await session.scalars(select(AttachmentRecord.storage_key))
    doc_rows = await session.scalars(select(KnowledgeDocRecord.storage_key))
    return {key for key in (*attachment_rows.all(), *doc_rows.all()) if key}


def _list_orphan_files(registered: set[str]) -> list[Path]:
    """磁盘上**没有数据库记录指向**的附件文件（M9）。

    判据是"库里有没有这条键"，不是文件名前缀 —— 磁盘上的名字是 UUID，
    本来就认不出测试数据（见 `infrastructure/object_store.py` 的说明）。

    ⚠️ `registered` 必须**同时包含附件与知识库文档的 `storage_key`**：
    两者落在同一个 `uploads/` 根目录下（M11 沿用 M9 的对象存储）。
    只登记附件的话，**制度文档的原文会被当成孤儿文件删掉** ——
    而 `knowledge_docs` 那一行还在，表现为"库里说有这份制度、文件却没了"，
    等到要重新切分或人工核对时才发现（子代理实测撞到过这个组合）。
    """
    root = Path(get_settings().upload_dir)
    if not root.exists():
        return []
    orphans: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # 真实路径是 `<父目录>/<32 位键><后缀>`，storage_key 是 `<前两位>/<全长键>`
        key = f"{path.parent.name}{path.name}"
        if path.parent.name and key not in registered:
            orphans.append(path)
    return orphans


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
        # 知识库文档（M11）：**在这里查**（而不是在后面删的那一段里），
        # 因为它要出现在下面的汇总行里，而空跑分支也会用到这个数 ——
        # 差值一多就会变成"删了但没报"，那种清理是没法核对的。
        knowledge_doc_ids = list(
            (
                await session.scalars(
                    select(KnowledgeDocRecord.id).where(
                        or_(
                            *[
                                KnowledgeDocRecord.source_key.like(pattern)
                                for pattern in KNOWLEDGE_SOURCE_PREFIXES
                            ]
                        )
                    )
                )
            ).all()
        )

        print(f"范围={scope}{'（空跑）' if dry_run else ''}")
        print(
            f"  匹配到：品类 {len(category_ids)} / 部门 {len(department_ids)} / "
            f"账号 {len(user_ids)} / 申请 {len(request_ids)} / 制度文档 {len(knowledge_doc_ids)}"
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
            dry_orphans = _list_orphan_files(await _registered_keys(session))
            print(f"    会删孤儿附件文件：{len(dry_orphans)} 个")
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

        # ---- ②a 知识库与问答（M11）----
        # 顺序由外键决定，**不能调**：
        #   消息 → 会话（`chat_sessions.user_id` 是 RESTRICT，会话不清掉就删不了账号）；
        #   片段 → 文档（片段是 CASCADE，但显式删一次让"清了多少"可核对，也免得
        #   依赖"建表时写了 CASCADE"这件事）。
        # 会话的清理范围 = 测试账号 ∪ 演示 admin。
        # 后者见 `DEMO_ADMIN_USERNAME` 的说明：脚本与集成测试都用 admin 提问，
        # 不按账号名清的话它们会一直攒着（实测几轮攒了 14 个）。
        chat_user_ids = list(
            (
                await session.scalars(
                    select(UserRecord.id).where(
                        or_(
                            UserRecord.id.in_(user_ids or [None]),
                            UserRecord.username == DEMO_ADMIN_USERNAME,
                        )
                    )
                )
            ).all()
        )
        doomed_session_ids = select(ChatSessionRecord.id).where(
            ChatSessionRecord.user_id.in_(chat_user_ids or [None])
        )
        counts["聊天消息"] = (
            await session.execute(
                delete(ChatMessageRecord).where(
                    ChatMessageRecord.session_id.in_(doomed_session_ids)
                )
            )
        ).rowcount or 0
        counts["会话"] = (
            await session.execute(
                delete(ChatSessionRecord).where(
                    ChatSessionRecord.user_id.in_(chat_user_ids or [None])
                )
            )
        ).rowcount or 0
        counts["制度片段"] = (
            await session.execute(
                delete(KnowledgeChunkRecord).where(
                    KnowledgeChunkRecord.doc_id.in_(knowledge_doc_ids or [None])
                )
            )
        ).rowcount or 0
        counts["制度文档"] = (
            await session.execute(
                delete(KnowledgeDocRecord).where(
                    KnowledgeDocRecord.id.in_(knowledge_doc_ids or [None])
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

        # ---- ③ 预算流水：`budget_transactions.budget_id` 是 RESTRICT，
        #      预算行不能带着流水被删（流水本身不按前缀认，只能按"要删的预算"反查）----
        doomed_budget_ids = select(BudgetRecord.id).where(
            or_(
                BudgetRecord.category_id.in_(category_ids or [None]),
                BudgetRecord.department_id.in_(department_ids or [None]),
            )
        )
        counts["预算流水"] = (
            await session.execute(
                delete(BudgetTransactionRecord).where(
                    BudgetTransactionRecord.budget_id.in_(doomed_budget_ids)
                )
            )
        ).rowcount or 0

        # ---- ④ 品类：预算引用它（RESTRICT），必须先删预算 ----
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

        # ---- ⑤ 部门：**四处** RESTRICT 引用都要先清 ----
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

        # ---- ⑥ 附件文件（M9）：删掉**没有数据库记录指向**的文件 ----
        #
        # 为什么按"孤儿"而不是按前缀删：磁盘上的文件名是服务端生成的 UUID
        # （`infrastructure/object_store.py`），**看不出是不是测试数据** ——
        # 这正是它的优点（用户文件名不参与拼路径），代价是清理脚本没法按名字判据。
        #
        # 所以判据换成"库里还有没有这条 storage_key"：
        #   有记录（含已软删除的）  → 留着，它是业务数据
        #   没有任何记录指向        → 孤儿文件，删掉
        #
        # 这个判据**在空跑时也能算出条数**，而且不会误伤任何在册附件。
        orphan_files = _list_orphan_files(await _registered_keys(session))
        if orphan_files:
            freed = sum(path.stat().st_size for path in orphan_files)
            if dry_run:
                print(f"  会删孤儿附件文件：{len(orphan_files)} 个（{freed} 字节）")
            else:
                for path in orphan_files:
                    path.unlink()
                print(f"  已删除孤儿附件文件：{len(orphan_files)} 个（{freed} 字节）")
        else:
            print("  孤儿附件文件：0 个")

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
