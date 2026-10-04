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
import json
import sys
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

import httpx

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
    from app.infrastructure.records import approval as _approval  # noqa: F401, PLC0415
    from app.infrastructure.records import auth  # noqa: PLC0415
    from app.infrastructure.records import catalog as _catalog  # noqa: F401, PLC0415
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
