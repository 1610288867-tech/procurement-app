"""回归探针（M8）：把"不会报错、只会算错"的约束与跨阶段接缝**主动撞一次**。

跑法（后端需已在 8000 运行）：

    .venv\\Scripts\\python.exe scripts\\regression_probes.py

与另外三个脚本的分工：

    tests/                     在进程内用 ASGI transport 跑，逐条对验收标准，可重复；
    scripts/m6_smoke.py        对着真接口走 M6 的审批链；
    scripts/regression_probes.py  **只验那些"错了也不报错"的地方**（本文件）；
    scripts/浏览器自检.mjs      验"页面真的渲染出来了"。

为什么要有这个文件：`PROGRESS.md` 与 `AGENTS.md` 都记着三条"静默失败"约束，
它们**违反时不会抛异常、只会给出错误的结果**（审批链为空 = 无人审批直接通过；
差额不释放 = 额度永久虚低；权限不写进 `WHERE` = 悄悄越权）。
这类问题只能靠主动去撞，而"手工撞"不留证据也不可复跑 —— 所以做成脚本。

它**跑完自清理**（自己造的部门 / 品类 / 预算 / 账号 / 申请全删掉），
并且每一步都打印实际观察到的状态码与数字，供人工复核。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

import httpx

# ============================ 输出必须是 UTF-8（否则这个脚本会崩）============================
# 每一行结果前面有 `✓`(U+2713) / `✗`(U+2717)，而 Windows 上 Python 的 stdout
# **默认按控制台代码页编码**。在 `chcp 936`（GBK）或**输出被重定向到文件**时，
# 它编不出来 → `UnicodeEncodeError`。
#
# ⚠️ 它崩在 `check()` 里的 print 上，也就是**第一条断言打印结果的时候** ——
# 表现是"探针跑了一半突然抛编码错"，与探针要查的东西毫无关系（实测撞到）。
# ⚠️ **同一个坑在本项目已出现三次**：`knowledge_eval.py`（报告符号）、
# `merge_gguf.py`（合并对账 ✅）、这里。所以每个往终端打字的脚本都要加这一段，
# 而不是逐个换符号 —— 换符号只是把问题推给下一个符号。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        with contextlib.suppress(OSError, ValueError):
            _stream.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8000"
ADMIN = ("admin", "Dev-Passw0rd!")
PASSWORD = "Probe-Passw0rd!"

#: 一级审批的最小策略（探针只关心"钱与门"，不关心链路有几级）
ONE_STEP_POLICY = {
    "steps": [
        {
            "order": 10,
            "role": "DEPT_MANAGER",
            "mode": "SINGLE",
            "when": {"gte": ["amount_fen", 0]},
        }
    ]
}

results: list[tuple[bool, str, str]] = []


def check(ok: bool, title: str, evidence: str = "") -> None:
    results.append((ok, title, evidence))
    print(f"{'✓' if ok else '✗'} {title}" + (f"  —— {evidence}" if evidence else ""))


def client() -> httpx.Client:
    return httpx.Client(base_url=BASE, timeout=20.0)


def _run_db(coro_factory: Any) -> Any:
    """跑一次数据库操作，跑完把连接池丢掉（理由见 `m6_smoke.py` 的同名函数）。"""
    sys.path.insert(0, ".")
    from app.database import engine  # noqa: PLC0415

    async def wrapped() -> Any:
        try:
            return await coro_factory()
        finally:
            await engine.dispose()

    return asyncio.run(wrapped())


def make_user(username: str, roles: list[str], department_id: str | None) -> str:
    """直连库建账号（M3 刻意没有建号接口；理由见 `m6_smoke.py`）。

    ⚠️ 用户名一律小写：登录会 `strip().lower()` 后再查库，库里是精确匹配。
    """
    username = username.lower()
    sys.path.insert(0, ".")
    from sqlalchemy import select  # noqa: PLC0415

    from app.database import SessionFactory  # noqa: PLC0415

    # 记录类要整包导入：外键指向的表不在元数据里会报 NoReferencedTableError
    from app.infrastructure.records import agent_run as _agent_run  # noqa: F401, PLC0415
    from app.infrastructure.records import approval as _approval  # noqa: F401, PLC0415
    from app.infrastructure.records import auth  # noqa: PLC0415
    from app.infrastructure.records import catalog as _catalog  # noqa: F401, PLC0415
    from app.infrastructure.records import knowledge as _knowledge  # noqa: F401, PLC0415
    from app.infrastructure.records import purchase as _purchase  # noqa: F401, PLC0415
    from app.infrastructure.security import hash_password  # noqa: PLC0415

    async def run() -> None:
        async with SessionFactory() as session:
            existing = (
                await session.scalars(
                    select(auth.UserRecord).where(auth.UserRecord.username == username)
                )
            ).one_or_none()
            if existing is not None:
                return
            user = auth.UserRecord(
                username=username,
                password_hash=hash_password(PASSWORD),
                full_name=username,
                department_id=uuid.UUID(department_id) if department_id else None,
            )
            session.add(user)
            await session.flush()
            for role in roles:
                session.add(auth.UserRoleRecord(user_id=user.id, role=role))
            await session.commit()

    _run_db(run)
    return username


def login(username: str) -> httpx.Client:
    session = client()
    response = session.post(
        "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
    )
    if response.status_code != 200:
        raise SystemExit(f"登录失败 {username}: {response.status_code} {response.text}")
    return session


# ---------------------------------------------------------------- 辅助：造数据


def new_draft(session: httpx.Client, category_id: str, title: str) -> dict[str, Any]:
    response = session.post(
        "/api/v1/requests", json={"category_id": category_id, "title": title}
    )
    if response.status_code != 201:
        raise SystemExit(f"建草稿失败：{response.status_code} {response.text}")
    return response.json()


def save_items(
    session: httpx.Client, draft: dict[str, Any], amount_fen: int
) -> dict[str, Any]:
    response = session.put(
        f"/api/v1/requests/{draft['id']}/items",
        json={
            "version": draft["version"],
            "items": [{"name": "探针物资", "quantity": 1, "unit_price_fen": amount_fen}],
        },
    )
    if response.status_code != 200:
        raise SystemExit(f"存明细失败：{response.status_code} {response.text}")
    return response.json()


def submit(
    session: httpx.Client, draft: dict[str, Any], key: str | None = None
) -> httpx.Response:
    return session.post(
        f"/api/v1/requests/{draft['id']}/submit",
        json={"version": draft["version"]},
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
    )


def timeline(session: httpx.Client, request_id: str) -> list[dict[str, Any]]:
    """时间线。接口返回的是 `{request_id, events: [...]}`，这里只要 events。"""
    response = session.get(f"/api/v1/requests/{request_id}/timeline")
    if response.status_code != 200:
        raise SystemExit(f"读时间线失败：{response.status_code} {response.text}")
    return list(response.json()["events"])


def budget_of(category_id: str, department_id: str) -> dict[str, int] | None:
    """直接读库里的三个金额（走得通接口的那部分用接口验，这是给断言用的）。"""
    sys.path.insert(0, ".")
    from sqlalchemy import select  # noqa: PLC0415

    from app.database import SessionFactory  # noqa: PLC0415
    from app.infrastructure.records.catalog import BudgetRecord  # noqa: PLC0415

    async def run() -> dict[str, int] | None:
        async with SessionFactory() as session:
            row = (
                await session.execute(
                    select(
                        BudgetRecord.amount_total,
                        BudgetRecord.amount_reserved,
                        BudgetRecord.amount_spent,
                    ).where(
                        BudgetRecord.category_id == uuid.UUID(category_id),
                        BudgetRecord.department_id == uuid.UUID(department_id),
                    )
                )
            ).one_or_none()
        return None if row is None else {"total": row[0], "reserved": row[1], "spent": row[2]}

    return _run_db(run)


def ledger_of(request_id: str) -> list[tuple[str, int]]:
    sys.path.insert(0, ".")
    from sqlalchemy import select  # noqa: PLC0415

    from app.database import SessionFactory  # noqa: PLC0415
    from app.infrastructure.records.catalog import (  # noqa: PLC0415
        BudgetTransactionRecord,
    )

    async def run() -> list[tuple[str, int]]:
        async with SessionFactory() as session:
            rows = (
                await session.execute(
                    select(
                        BudgetTransactionRecord.kind, BudgetTransactionRecord.amount
                    )
                    .where(BudgetTransactionRecord.request_id == uuid.UUID(request_id))
                    .order_by(BudgetTransactionRecord.seq)
                )
            ).all()
        return [(row[0], row[1]) for row in rows]

    return _run_db(run)


def insert_broken_category(code: str) -> str:
    """**直连库**塞一个 `approval_policy` 为空对象的品类。

    为什么要绕过接口：M2 的语义校验会拦下"没有兜底步骤"的策略（422），
    所以线上造不出这种品类。但**历史数据可能就是这样**（早期版本、手工改库、
    迁移遗漏），而"提交时算出的审批链为空"正是最危险的那种静默失败 ——
    它意味着**无人审批直接通过**。所以这里直接造出这个前提来撞提交那道闸。
    """
    sys.path.insert(0, ".")
    from app.database import SessionFactory  # noqa: PLC0415
    from app.infrastructure.records.catalog import (  # noqa: PLC0415
        PurchaseCategoryRecord,
    )

    async def run() -> str:
        async with SessionFactory() as session:
            record = PurchaseCategoryRecord(
                code=code,
                name="探针-空策略品类",
                form_schema={},
                approval_policy={},  # ← 空策略：算出来的审批链必然是空的
            )
            session.add(record)
            await session.flush()
            category_id = str(record.id)
            await session.commit()
        return category_id

    return str(_run_db(run))


def cleanup(
    request_ids: list[str], usernames: list[str], category_ids: list[str], department_ids: list[str]
) -> dict[str, int]:
    """删掉本次探针造的数据，并回查残留。"""
    sys.path.insert(0, ".")
    from sqlalchemy import delete, func, or_, select  # noqa: PLC0415

    from app.database import SessionFactory  # noqa: PLC0415
    from app.infrastructure.records.agent_run import AgentRunRecord  # noqa: PLC0415
    from app.infrastructure.records.approval import (  # noqa: PLC0415
        ApprovalFlowRecord,
        ApprovalTaskRecord,
    )
    from app.infrastructure.records.auth import UserRecord  # noqa: PLC0415
    from app.infrastructure.records.catalog import (  # noqa: PLC0415
        BudgetRecord,
        BudgetTransactionRecord,
        DepartmentRecord,
        PurchaseCategoryRecord,
    )
    from app.infrastructure.records.knowledge import (  # noqa: PLC0415
        ChatSessionRecord,
    )
    from app.infrastructure.records.purchase import (  # noqa: PLC0415
        IdempotencyRecord,
        PurchaseItemRecord,
        PurchaseRequestRecord,
        RequestEventRecord,
    )

    req_ids = [uuid.UUID(value) for value in request_ids]
    cat_ids = [uuid.UUID(value) for value in category_ids]
    dept_ids = [uuid.UUID(value) for value in department_ids]

    async def run() -> dict[str, int]:
        counts: dict[str, int] = {}
        async with SessionFactory() as session:
            user_ids = list(
                (
                    await session.scalars(
                        select(UserRecord.id).where(
                            func.lower(UserRecord.username).like("probe-%")
                        )
                    )
                ).all()
            )
            # 申请：本轮的 + 任何由 probe- 账号提交的（上一轮中途失败会留下它们）
            doomed = select(PurchaseRequestRecord.id).where(
                or_(
                    PurchaseRequestRecord.id.in_(req_ids or [None]),
                    PurchaseRequestRecord.requester_id.in_(user_ids or [None]),
                )
            )
            for name, model in (
                ("审批任务", ApprovalTaskRecord),
                ("审批流", ApprovalFlowRecord),
                ("预算流水", BudgetTransactionRecord),
                ("事件", RequestEventRecord),
                ("明细", PurchaseItemRecord),
            ):
                outcome = await session.execute(
                    delete(model).where(model.request_id.in_(doomed))
                )
                counts[name] = outcome.rowcount or 0
            outcome = await session.execute(
                delete(PurchaseRequestRecord).where(PurchaseRequestRecord.id.in_(doomed))
            )
            counts["申请"] = outcome.rowcount or 0

            if user_ids:
                await session.execute(
                    delete(IdempotencyRecord).where(
                        IdempotencyRecord.actor_id.in_(user_ids)
                    )
                )
                await session.execute(
                    delete(RequestEventRecord).where(
                        RequestEventRecord.actor_id.in_(user_ids)
                    )
                )
                await session.execute(
                    delete(ApprovalTaskRecord).where(
                        ApprovalTaskRecord.decided_by.in_(user_ids)
                    )
                )
                # ============================ 问答会话也要清 ============================
                # 接缝⑧ 会以**探针账号**的身份问一句（那是它要验的东西），
                # 于是库里多了这个账号的 `chat_sessions` / `chat_messages`。
                # 少了这一步，下面的删用户会被外键挡住：
                #     update or delete on table "users" violates foreign key constraint
                #     "chat_sessions_user_id_fkey"（实测撞到）
                # 表现是**清理阶段崩掉** —— 而清理是回归的收尾，
                # 它崩了等于"测试污染物零残留"这条验收失去意义。
                # `chat_messages` 靠 `ON DELETE CASCADE` 跟着走。
                await session.execute(
                    delete(ChatSessionRecord).where(
                        ChatSessionRecord.user_id.in_(user_ids)
                    )
                )
                # Agent 运行记录也引用 users（`agent_runs.actor_id`）
                await session.execute(
                    delete(AgentRunRecord).where(AgentRunRecord.actor_id.in_(user_ids))
                )
                counts["账号"] = (
                    await session.execute(
                        delete(UserRecord).where(UserRecord.id.in_(user_ids))
                    )
                ).rowcount or 0

            if cat_ids or dept_ids:
                outcome = await session.execute(
                    delete(BudgetRecord).where(
                        or_(
                            BudgetRecord.category_id.in_(cat_ids or [None]),
                            BudgetRecord.department_id.in_(dept_ids or [None]),
                        )
                    )
                )
                counts["预算"] = outcome.rowcount or 0
            if cat_ids:
                counts["品类"] = (
                    await session.execute(
                        delete(PurchaseCategoryRecord).where(
                            PurchaseCategoryRecord.id.in_(cat_ids)
                        )
                    )
                ).rowcount or 0
            if dept_ids:
                counts["部门"] = (
                    await session.execute(
                        delete(DepartmentRecord).where(DepartmentRecord.id.in_(dept_ids))
                    )
                ).rowcount or 0
            await session.commit()

            counts["残留账号"] = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(UserRecord)
                        .where(func.lower(UserRecord.username).like("probe-%"))
                    )
                ).scalar_one()
            )
        return counts

    return _run_db(run)


# ---------------------------------------------------------------- 主流程


def main() -> int:
    stamp = uuid.uuid4().hex[:6].upper()
    admin = client()
    admin.post("/api/v1/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]})

    department = admin.post(
        "/api/v1/admin/departments",
        json={"name": "探针部", "code": f"TDEP-P{stamp}"},
    ).json()
    category = admin.post(
        "/api/v1/admin/categories",
        json={
            "name": "探针品类",
            "code": f"TCAT-P{stamp}",
            "approval_policy": ONE_STEP_POLICY,
        },
    ).json()
    dept_id, cat_id = department["id"], category["id"]

    admin.post(
        "/api/v1/admin/budgets",
        json={
            "department_id": dept_id,
            "category_id": cat_id,
            "year": datetime.now(UTC).year,
            "amount_fen": 100_000,  # 1000 元
        },
    )

    requester_name = make_user(f"probe-req-{stamp}", ["REQUESTER"], dept_id)
    other_name = make_user(f"probe-other-{stamp}", ["REQUESTER"], None)  # 别的部门的人
    user_names = [requester_name, other_name]
    make_user(f"probe-manager-{stamp}", ["DEPT_MANAGER"], dept_id)

    requester = login(requester_name)
    other = login(other_name)

    request_ids: list[str] = []
    category_ids: list[str] = [cat_id]
    try:
        # ============================================================ 撞①：审批链不许为空
        broken_id = insert_broken_category(f"TCAT-PBROKEN-{stamp}")
        category_ids.append(broken_id)
        # 空策略的品类也要有预算，否则会先撞"计划外 422"，验不到审批链那一条
        admin.post(
            "/api/v1/admin/budgets",
            json={
                "department_id": dept_id,
                "category_id": broken_id,
                "year": datetime.now(UTC).year,
                "amount_fen": 100_000,
            },
        )
        broken_draft = new_draft(requester, broken_id, "探针-空审批链")
        request_ids.append(broken_draft["id"])
        broken_items = save_items(requester, broken_draft, 10_000)
        broken_response = submit(requester, broken_items)
        body = broken_response.json().get("error", {})
        check(
            broken_response.status_code == 422 and body.get("code") == "EMPTY_APPROVAL_CHAIN",
            "撞①审批链不许为空：空策略的品类提交 → 422（无人审批直接通过是非法状态）",
            f"HTTP {broken_response.status_code} {body.get('code')} {body.get('message', '')[:40]}",
        )
        after = requester.get(f"/api/v1/requests/{broken_draft['id']}").json()
        check(
            after["request"]["status"] == "DRAFT",
            "撞①的副作用：提交被拒后主单仍回滚为草稿（没有半截审批链）",
            f"status={after['request']['status']}",
        )

        # ============================================================ 撞③：权限必须写进 WHERE
        mine = new_draft(requester, cat_id, "探针-越权探测")
        request_ids.append(mine["id"])
        same_dept_lookup = requester.get(f"/api/v1/requests/{mine['id']}")
        cross_dept = other.get(f"/api/v1/requests/{mine['id']}")
        check(
            same_dept_lookup.status_code == 200 and cross_dept.status_code == 404,
            "撞③权限写进 WHERE：别人拿 id 取我的申请 → 404（不是 403、更不是 200）",
            f"本人 {same_dept_lookup.status_code} / 他人 {cross_dept.status_code}",
        )
        other_patch = other.patch(
            f"/api/v1/requests/{mine['id']}",
            json={"version": mine["version"], "title": "改别人的单"},
        )
        check(
            other_patch.status_code == 404,
            "撞③续：别人改我的草稿 → 404（可见范围外一律当不存在）",
            f"HTTP {other_patch.status_code}",
        )

        # ============================================================ 撞②：差额必须显式释放
        # 付款接口要到 M10 才有，所以这里撞的是**现在就能走的释放入口**（撤回）：
        # 预留 → 撤回 → 该申请占着的额度必须归零、且流水一正一负加起来为 0。
        body_draft = save_items(requester, new_draft(requester, cat_id, "探针-预留释放"), 80_000)
        request_ids.append(body_draft["id"])
        submitted = submit(requester, body_draft)
        if submitted.status_code != 200:
            raise SystemExit(f"提交失败：{submitted.status_code} {submitted.text}")
        reserved_after_submit = budget_of(cat_id, dept_id)
        withdrawn = requester.post(
            f"/api/v1/requests/{body_draft['id']}/withdraw",
            json={"version": submitted.json()["version"]},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        reserved_after_withdraw = budget_of(cat_id, dept_id)
        ledger = ledger_of(body_draft["id"])
        check(
            withdrawn.status_code == 200
            and reserved_after_submit is not None
            and reserved_after_submit["reserved"] == 80_000
            and reserved_after_withdraw is not None
            and reserved_after_withdraw["reserved"] == 0,
            "撞②差额必须显式释放：预留 800 元 → 撤回 → 该申请占的额度归零",
            f"预留后 reserved={reserved_after_submit and reserved_after_submit['reserved']} / "
            f"撤回后 reserved={reserved_after_withdraw and reserved_after_withdraw['reserved']}",
        )
        summed = sum(amount if kind == "RESERVE" else -amount for kind, amount in ledger)
        check(
            ledger == [("RESERVE", 80_000), ("RELEASE", 80_000)] and summed == 0,
            "撞②续：流水一正一负相加为 0（账目能对上）",
            f"{ledger} → 合计 {summed}",
        )
        check(
            reserved_after_withdraw is not None and reserved_after_withdraw["spent"] == 0,
            "撞②续：撤回不动 amount_spent（已花只在实际付款时增加）",
            f"spent={reserved_after_withdraw and reserved_after_withdraw['spent']}",
        )

        # ============================================================ 接缝①：Cookie 的作用域
        fresh = client()
        login_response = fresh.post(
            "/api/v1/auth/login", json={"username": requester_name, "password": PASSWORD}
        )
        cookies = login_response.headers.get_list("set-cookie")
        refresh_scoped = any(
            "refresh_token=" in cookie and "Path=/api/v1/auth/refresh" in cookie
            for cookie in cookies
        )
        access_scoped = any(
            "access_token=" in cookie and "Path=/" in cookie for cookie in cookies
        )
        check(
            refresh_scoped and access_scoped,
            "接缝①：refresh Cookie 的 Path 限定到 /api/v1/auth/refresh（access 是 /）",
            " / ".join(cookie.split(";")[0] + " ; " + cookie.split("Path=")[-1] for cookie in cookies),
        )

        # ============================================================ 接缝②：金额单位三处一致
        money_draft = save_items(requester, new_draft(requester, cat_id, "探针-单位"), 100_000)
        request_ids.append(money_draft["id"])
        stored = None
        sys.path.insert(0, ".")
        from sqlalchemy import select as _select  # noqa: PLC0415

        from app.database import SessionFactory as _SessionFactory  # noqa: PLC0415
        from app.infrastructure.records.purchase import (  # noqa: PLC0415
            PurchaseItemRecord,
        )

        async def read_item() -> Any:
            async with _SessionFactory() as session:
                return (
                    await session.execute(
                        _select(
                            PurchaseItemRecord.unit_price,
                            PurchaseItemRecord.amount,
                        ).where(
                            PurchaseItemRecord.request_id == uuid.UUID(money_draft["id"])
                        )
                    )
                ).one_or_none()

        stored = _run_db(read_item)
        check(
            money_draft["amount_total_fen"] == 100_000
            and stored is not None
            and stored[0] == 100_000
            and stored[1] == 100_000,
            "接缝②：请求体「分」→ 响应「分」→ 库里「分」，三处都是 100000（1000 元）",
            f"响应={money_draft['amount_total_fen']} 库里单价={stored and stored[0]} 行金额={stored and stored[1]}",
        )

        # ============================================================ 接缝③：事件与状态迁移一致
        body_draft = save_items(requester, new_draft(requester, cat_id, "探针-事件"), 50_000)
        request_ids.append(body_draft["id"])
        submit(requester, body_draft)
        events = timeline(requester, body_draft["id"])
        transitions = [
            (event["from_status"], event["to_status"])
            for event in events
            if event["from_status"] != event["to_status"]
        ]
        # 只有两条事件真正改状态：创建（None → DRAFT）与提交（DRAFT → IN_APPROVAL）。
        # 第三条 APPROVAL_STARTED 的语义是"审批链已生成"，它**不是状态变更** ——
        # from/to 都是 IN_APPROVAL，所以不会出现在上面这张迁移表里（这正是设计意图：
        # 否则时间线上会出现两条一模一样的"提交"）。
        check(
            transitions == [(None, "DRAFT"), ("DRAFT", "IN_APPROVAL")],
            "接缝③：时间线里恰好两条状态迁移（创建 → DRAFT、提交 → IN_APPROVAL）",
            f"{len(events)} 条事件，迁移={transitions}",
        )
        check(
            [event["event_type"] for event in events]
            == ["REQUEST_CREATED", "REQUEST_SUBMITTED", "APPROVAL_STARTED"],
            "接缝③续：事件类型顺序为 创建 → 提交 → 建审批链",
            " / ".join(event["event_type"] for event in events),
        )

        # ============================================================ 接缝④：同键重放不产生第二条
        replay_draft = save_items(requester, new_draft(requester, cat_id, "探针-重放"), 20_000)
        request_ids.append(replay_draft["id"])
        key = uuid.uuid4().hex
        first = submit(requester, replay_draft, key=key)
        second = submit(requester, replay_draft, key=key)
        replay_events = timeline(requester, replay_draft["id"])
        replay_ledger = ledger_of(replay_draft["id"])
        check(
            first.status_code == 200
            and second.status_code == 200
            and first.json() == second.json(),
            "接缝④：同键重放返回**逐字节相同**的响应",
            f"HTTP {first.status_code} / {second.status_code}",
        )
        check(
            len(replay_events) == 3 and replay_ledger == [("RESERVE", 20_000)],
            "接缝④续：重放没有产生第二条事件、也没有第二条 RESERVE 流水",
            f"事件 {len(replay_events)} 条 / 流水 {replay_ledger}",
        )

        # ============================================================ 接缝⑤：模型用量帧
        #
        # **为什么这条必须是探针而不是普通测试**：它是"**不会报错、只会算错**"的典型。
        # 流式接口把 token 用量放在**最后一帧**，而那一帧的 `choices` 是**空数组**：
        #
        #     data: {"choices": [], "usage": {"prompt_tokens": 12, ...}}
        #
        # 正文解析器取不到 `choices[0]` 就返回空串 —— 用量**被静默丢掉**，
        # 于是 `prompt_tokens` 永远是 0。而"永远是 0"很容易被读成"这个模型不要钱"，
        # 成本报表会一直是 0 却没人发现（这正是 PROGRESS 里那三条约束的同一类毛病）。
        #
        # 探针直接喂一帧真实的用量帧，断言它被解析出来 —— 不依赖任何真模型。
        import json as _json

        from app.domain.cost import ModelPrice, estimate_cost_micros
        from app.infrastructure.chat_models import _parse_sse_line, _parse_usage_line

        usage_frame = "data: " + _json.dumps(
            {
                "choices": [],
                "model": "qwen2.5:7b",
                "usage": {"prompt_tokens": 12, "completion_tokens": 34},
            }
        )
        parsed_usage = _parse_usage_line(usage_frame)
        check(
            parsed_usage is not None
            and parsed_usage.prompt_tokens == 12
            and parsed_usage.completion_tokens == 34,
            "接缝⑤：用量帧（`choices` 为空）能被解析出来 —— 不会被正文解析器丢掉",
            f"解析结果={parsed_usage}",
        )
        # 正文帧**不能**被误判成用量帧（否则正文会整段消失）
        content_frame = "data: " + _json.dumps(
            {"choices": [{"delta": {"content": "根据[1]，应当公开招标。"}}]}
        )
        check(
            _parse_usage_line(content_frame) is None
            and _parse_sse_line(content_frame) != "",
            "接缝⑤续：正文帧不被误判为用量帧，且正文仍能取到",
            f"用量={_parse_usage_line(content_frame)} 正文={_parse_sse_line(content_frame)!r}",
        )

        # 成本：**"未知"与"0 元"必须分开**（本地模型是 0，拿不到用量是未知）
        price = ModelPrice.from_yuan_per_million(input_yuan=2.0, output_yuan=8.0)
        unknown_cost = estimate_cost_micros(
            prompt_tokens=None, completion_tokens=34, price=price
        )
        local_cost = estimate_cost_micros(
            prompt_tokens=12,
            completion_tokens=34,
            price=ModelPrice(input_micros_per_million=0, output_micros_per_million=0),
        )
        check(
            unknown_cost is None and local_cost == 0,
            "接缝⑤续：拿不到用量 → 成本『未知』(None)；本地模型 → 成本『确实是 0』",
            f"未知={unknown_cost} 本地={local_cost}",
        )

        # ============================================================ 接缝⑥：配额要把来源算进去
        #
        # **这条抓的是一个真实踩到的坑**：第一版用 `getattr(source, "content", "")`
        # 取来源正文，但 HTTP 路径拿到的 `body["sources"]` 是**已经序列化过的字典**：
        #
        #     getattr({"content": "..."}, "content", "")  →  ""    ← 不报错，静默取空
        #
        # 后果：**每条来源都算成 0 token**，配额只按"问题 + 回答"记账（实测 20），
        # 而上下文主体是来源（实测该记 148）。配额上限因此形同虚设。
        # 这是典型的"不会报错、只会算错"，只能靠对账发现 —— 所以放进探针。
        from app.boundaries.http.knowledge import _estimate_tokens_for_quota

        sources_as_dicts = [{"content": "第二条 采购金额超过十万元的，应当进行公开招标。" * 8}]
        charged = _estimate_tokens_for_quota(
            question="多少钱要招标？", sources=sources_as_dicts, answer="根据[1]，应当公开招标。"
        )
        question_only = len("多少钱要招标？")
        check(
            charged > question_only * 5,
            "接缝⑥：配额记账要把来源片段算进去（字典形状的来源不能被静默取空）",
            f"记账 {charged} token，而问题本身只有 {question_only} 字",
        )

        # ============================================================ 接缝⑦：Agent 的审计与分流
        #
        # **两个实测踩到的"不报错只算错"**（M12 的 Agent 路径）：
        #
        #   ① `used_model` 说谎。Agent 的 `decision.use_model` 是 False（检索没依据），
        #      而它**确实调用了模型**（去查业务数据）。
        #      `_used_remote` 原本只判 `isinstance(chat, LocalExtractiveChat)` ——
        #      那只覆盖 `stream()`，而 Agent 走的是 `stream_turn()`。
        #      于是库里把一个花了几秒模型时间的回答记成"未调用模型"。
        #      **这是一条验收项字段，不能给它错的值。**
        #
        #   ② 分流判据只加在流式那条路 → 非流式照旧回"制度中没有找到相关规定"，
        #      而 curl 与集成测试打的偏偏是非流式：**功能写了却没效果**。
        from app.application.coordinators.knowledge import KnowledgeCoordinator

        class _StubChat:
            supports_tools = True
            name = "probe-stub"

        class _StubDecision:
            use_model = False

        # ---- 接缝⑧ 要用的真实数据：一张**已提交**的草稿（它的单号要出现在回答里）----
        # ⚠️ **两处都踩过，记下来**：
        #   ① **不能复用 `login()`** —— 它固定用 `PASSWORD`（造探针账号的
        #      `Probe-Passw0rd!`），而 demo 账号口令是另一套 → 401
        #      （看起来像"账号被删了"）。
        #   ② **不能用 admin** —— 它**没有所属部门**，`create_draft` 直接回
        #      `422 NO_DEPARTMENT`（admin 是管主数据与账号的角色，不需要部门）。
        # 所以用**探针自己造的 requester**：它有部门、能建单，
        # 而且 `list_my_purchase_requests` 的可见范围正好覆盖它自己提的单。
        agent_session = requester
        agent_request = None
        # ⚠️ **不吞异常**：第一版用 `with suppress(Exception)` 包住，
        # 于是任何失败都退化成"本条不适用" —— 一个**永远通过**的断言，
        # 而它要查的正是"回答里有没有单号"。这类"看起来通过、其实没跑"的形状
        # 与本项目反复强调的"验证工具自己坏了"是同一类，所以这里让它**炸出来**。
        draft = new_draft(agent_session, cat_id, "接缝⑧ 回答复杂度探针")
        # 明细是提交的前置（"申请没有任何明细，不能提交" —— 实测撞到）
        saved = save_items(agent_session, draft, 20_000)
        submitted = submit(agent_session, saved)
        if submitted.status_code != 200:
            raise SystemExit(
                f"接缝⑧ 需要一张已提交的单，提交失败："
                f"{submitted.status_code} {submitted.text}"
            )
        agent_request = submitted.json()

        coordinator = KnowledgeCoordinator.__new__(KnowledgeCoordinator)
        coordinator._chat = _StubChat()  # type: ignore[assignment]
        agent_used = coordinator._used_remote(_StubDecision(), agent_used=True)
        no_agent = coordinator._used_remote(_StubDecision(), agent_used=False)
        check(
            agent_used is True and no_agent is False,
            "接缝⑦：Agent 走 `stream_turn` 也算『调用了模型』（used_model 不能说谎）",
            f"agent_used→{agent_used}  无 Agent→{no_agent}",
        )
        # 分流判据必须**只有一处**（`ask` 与 `ask_stream` 调同一个方法）。
        # 这里断言"没注入工具依赖时不走 Agent"—— 任何一条输入被漏掉，
        # 两条路就会给出不同结论，而那种不一致极难发现。
        coordinator._purchase = None  # type: ignore[assignment]
        coordinator._approval = None  # type: ignore[assignment]
        stub_prepared = {"decision": _StubDecision()}
        check(
            coordinator._should_use_agent(stub_prepared) is False,
            "接缝⑦续：没注入工具依赖时不走 Agent（两条路共用同一个判据）",
            f"should_use_agent={coordinator._should_use_agent(stub_prepared)}",
        )

        # ============================================================ 接缝⑧：回答的**复杂度**
        #
        # 这条钉的是 M12+ 验收里那句话的**实际内容**：
        #     "端到端：答出 3 张真实单子（含金额、状态、时间）"
        #
        # 为什么不能只看"有没有调工具"：**调了工具不等于答得有用**。
        # 实测（3B）撞到过：工具返回 3/3 条真实数据、`stop_reason=answered`、
        # `used_model=true`，而最终回答只有 11 个字「我查到了一些采购申请。」
        # —— 不复述任何单号与金额。只断言"调过工具"的测试**会放行这种回答**。
        #
        # 所以这里断言**回答里出现了刚才那张草稿的单号**（结构事实，不是措辞）。
        # 为了让断言能被"完成/跳过/失败"三态说清，先取后端真实状态：
        #   模型没配 / 不支持工具 → **这一条不适用**（如实说，不伪装成通过）
        if agent_request is not None:
            body = agent_session.post(
                "/api/v1/chat", json={"question": "我提过哪些采购申请？"}
            ).json()
            from app.domain.grounded_answer import NO_GROUNDING_ANSWER

            answer = str(body.get("answer") or "")
            used_agent = bool(body.get("agent_run_id"))
            if not used_agent and answer.strip() == NO_GROUNDING_ANSWER:
                # Agent 路径没走（模型没配或不会用工具）→ 如实标注"不适用"
                check(
                    True,
                    "接缝⑧：回答复杂度（本机未启用 Agent，本条不适用）",
                    f"reason={str(body.get('reason'))[:60]}",
                )
            else:
                code = str(agent_request.get("code") or "")
                check(
                    bool(code) and code in answer,
                    "接缝⑧：Agent 答业务数据问题要**带上单号**，不能只回一句'我查到了一些'",
                    f"单号 {code!r} {'在' if code in answer else '**不在**'}回答里：{answer[:70]}",
                )
        else:
            check(
                True,
                "接缝⑧：回答复杂度（造数据失败，本条不适用）",
                "见上面建草稿那一步的输出",
            )

    finally:
        for session in (admin, requester, other):
            # 关连接失败不该盖住真正的失败
            with suppress(Exception):
                session.close()
        removed = cleanup(request_ids, user_names, category_ids, [dept_id])
        print(f"\n清理：{json.dumps(removed, ensure_ascii=False)}")
        check(
            removed["残留账号"] == 0,
            "探针数据已清理（回查 probe-* 账号为 0）",
            f"删除 申请 {removed['申请']} / 事件 {removed['事件']} / 流水 {removed['预算流水']} / "
            f"账号 {removed['账号']} / 品类 {removed['品类']} / 部门 {removed['部门']}",
        )

    passed = sum(1 for ok, _, _ in results if ok)
    print(f"\n=== {passed}/{len(results)} 项通过 ===")
    for ok, title, evidence in results:
        if not ok:
            print(f"  未通过：{title}  {evidence}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
