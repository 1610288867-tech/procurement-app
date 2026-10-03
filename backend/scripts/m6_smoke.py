"""对着**真实运行的后端**走一遍 M6 全链路（httpx，不是 TestClient）。

与 `tests/test_api_approvals.py` 的分工：
    测试文件        在 CI/本地用 ASGI transport 跑，逐条对验收标准，可重复；
    这个脚本        对着 http://127.0.0.1:8000 打真接口，验证"装配真的通了"
                    （依赖注入、路由挂载、中间件、Cookie 认证这些只有真跑才暴露）。

跑法：
    .venv\\Scripts\\python.exe scripts\\m6_smoke.py   （后端需已在 8000 运行）

它跑完会把造出来的数据删干净，并在退出时打印每一步的实际结果。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from typing import Any

import httpx

BASE = "http://127.0.0.1:8000"
ADMIN = ("admin", "Dev-Passw0rd!")
PASSWORD = "M6-Smoke-Passw0rd!"

#: 与《金额-审批链对照表》一致：500/5000/50000/200000 元 → 1/2/4/5 级
POLICY = {
    "steps": [
        {"order": 10, "role": "DEPT_MANAGER", "mode": "SINGLE", "when": {"gte": ["amount_fen", 0]}},
        {"order": 20, "role": "DEPT_HEAD", "mode": "SINGLE", "when": {"gte": ["amount_fen", 100_000]}},
        {"order": 30, "role": "PROCUREMENT", "mode": "SINGLE", "when": {"gte": ["amount_fen", 1_000_000]}},
        {"order": 40, "role": "FINANCE", "mode": "SINGLE", "when": {"gte": ["amount_fen", 1_000_000]}},
        {"order": 50, "role": "GM", "mode": "SINGLE", "when": {"gte": ["amount_fen", 10_000_000]}},
    ],
    "skip_when_framework_contract": False,
}

results: list[tuple[bool, str, str]] = []


def check(ok: bool, title: str, evidence: str = "") -> None:
    results.append((ok, title, evidence))
    print(f"{'✓' if ok else '✗'} {title}" + (f"  —— {evidence}" if evidence else ""))


def client() -> httpx.Client:
    return httpx.Client(base_url=BASE, timeout=20.0)


def _run_db(coro_factory: Any) -> Any:
    """跑一次数据库操作，**跑完把连接池丢掉**。

    ⚠️ 必须这么做：`app.database.engine` 是模块级全局，连接池里的连接绑定在
    **创建它的那个事件循环**上。脚本里多次 `asyncio.run()` 会建多个循环，
    第二次复用旧连接就会炸在 `'NoneType' object has no attribute 'send'`
    （asyncpg 的传输层已经随旧循环关闭了）。
    `dispose()` 把池清掉，下一次调用重新建连，各用各的循环。
    """
    sys.path.insert(0, ".")
    from app.database import engine  # noqa: PLC0415

    async def wrapped() -> Any:
        try:
            return await coro_factory()
        finally:
            await engine.dispose()

    return asyncio.run(wrapped())


def make_user(username: str, roles: list[str], department_id: str | None) -> str:
    """建一个自检账号，返回**实际写库的用户名**。

    ⚠️ 账号名一律转小写：登录用例会 `username.strip().lower()` 之后再查库
    （刻意的：`Admin` 与 `admin` 不该是两个账号），而库里是**精确匹配**。
    建号时写成大写，登录必然 401，而报错是"用户名或密码不正确" ——
    完全看不出是大小写的问题（实测踩过）。

    **为什么走库而不是走接口**：M3 只提供 login / refresh / logout / me ——
    建账号**没有 HTTP 端点**（刻意的：谁能建账号是运维决定的事，不该是一个业务接口，
    否则一个拿到管理员令牌的人就能凭空造出审批人）。种子脚本 `seed.py` 也是直连库建号。
    """
    username = username.lower()
    sys.path.insert(0, ".")
    from sqlalchemy import select  # noqa: PLC0415

    from app.database import SessionFactory  # noqa: PLC0415

    # ⚠️ 记录类要**整包导入**：只导入 `records.auth` 的话，`users.department_id`
    # 外键指向的 `departments` 表不在元数据里，第一次 flush 就会报
    # NoReferencedTableError。
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


def submit(session: httpx.Client, category_id: str, title: str, amount_fen: int) -> dict[str, Any]:
    draft = session.post(
        "/api/v1/requests", json={"category_id": category_id, "title": title}
    ).json()
    items = session.put(
        f"/api/v1/requests/{draft['id']}/items",
        json={
            "version": draft["version"],
            "items": [{"name": "办公用品", "quantity": 1, "unit_price_fen": amount_fen}],
        },
    ).json()
    response = session.post(
        f"/api/v1/requests/{draft['id']}/submit",
        json={"version": items["version"]},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    if response.status_code != 200:
        raise SystemExit(f"提交失败: {response.status_code} {response.text}")
    return response.json()


def main() -> int:
    stamp = uuid.uuid4().hex[:6].upper()

    with client() as anon:
        health = anon.get("/health/ready")
        check(health.status_code == 200, "后端就绪", f"HTTP {health.status_code} {health.text}")

    admin = client()
    admin.post("/api/v1/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]})

    department = admin.post(
        "/api/v1/admin/departments", json={"name": "M6自检部", "code": f"TDEP-{stamp}"}
    ).json()
    category = admin.post(
        "/api/v1/admin/categories",
        json={"name": "M6自检品类", "code": f"TCAT-{stamp}", "approval_policy": POLICY},
    ).json()
    dept_id = department["id"]
    cat_id = category["id"]

    requester_name = f"t-smoke-req-{stamp}"
    users: list[str] = []
    requester_name = make_user(requester_name, ["REQUESTER"], dept_id)
    users.append(requester_name)
    # 每一级都造一个人：500/5000/50000/200000 元四笔都要能一次提交成功
    manager_name = make_user(f"t-smoke-deptmanager-{stamp}", ["DEPT_MANAGER"], dept_id)
    users.append(manager_name)
    head_name = make_user(f"t-smoke-depthead-{stamp}", ["DEPT_HEAD"], dept_id)
    users.append(head_name)
    for role in ("PROCUREMENT", "FINANCE", "GM"):
        users.append(make_user(f"t-smoke-{role.lower()}-{stamp}", [role], None))

    requester = login(requester_name)
    manager = login(manager_name)
    head = login(head_name)

    created_requests: list[str] = []
    # 提前声明：`finally` 里要用，而它可能因为中途异常还没被赋值
    empty_dept: dict[str, Any] | None = None
    try:
        # ---------- 1. 金额 → 任务数（真接口） ----------
        for amount_fen, expected in ((50_000, 1), (500_000, 2), (5_000_000, 4), (20_000_000, 5)):
            body = submit(requester, cat_id, f"自检 {amount_fen} 分", amount_fen)
            created_requests.append(body["id"])
            flow = requester.get(f"/api/v1/approvals/requests/{body['id']}/flow").json()
            check(
                len(flow["steps"]) == expected,
                f"{amount_fen // 100} 元 → {expected} 个审批任务",
                f"实际 {len(flow['steps'])} 个，角色={[s['role'] for s in flow['steps']]}",
            )

        # ---------- 2/3. 只有当前这一级可见 ----------
        small = requester.post(
            "/api/v1/requests",
            json={"category_id": cat_id, "title": "自检-可见性"},
        ).json()
        items = requester.put(
            f"/api/v1/requests/{small['id']}/items",
            json={
                "version": small["version"],
                "items": [{"name": "办公用品", "quantity": 1, "unit_price_fen": 500_000}],
            },
        ).json()
        submitted = requester.post(
            f"/api/v1/requests/{small['id']}/submit",
            json={"version": items["version"]},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).json()
        created_requests.append(submitted["id"])

        manager_todo = manager.get("/api/v1/approvals/tasks").json()
        head_todo = head.get("/api/v1/approvals/tasks").json()
        check(
            any(item["request"]["id"] == submitted["id"] for item in manager_todo["items"]),
            "第 1 级（部门主管）看得到待办",
            f"共 {manager_todo['total']} 条",
        )
        check(
            not any(item["request"]["id"] == submitted["id"] for item in head_todo["items"]),
            "第 2 级（部门负责人）**看不到**还没轮到的待办",
            f"共 {head_todo['total']} 条",
        )

        # ---------- 4. 越级审批 → 403 ----------
        flow = requester.get(f"/api/v1/approvals/requests/{submitted['id']}/flow").json()
        level2 = next(step for step in flow["steps"] if step["step_order"] == 20)
        out_of_turn = head.post(
            f"/api/v1/approvals/tasks/{level2['id']}/approve", json={"version": level2["version"]}
        )
        check(
            out_of_turn.status_code == 403,
            "越级审批被挡住（第 2 级直接批 → 403）",
            f"HTTP {out_of_turn.status_code} {out_of_turn.json().get('error', {}).get('code')}",
        )

        # ---------- 通过第 1 级 → 第 2 级才出现 ----------
        level1_task = next(
            item["task"]
            for item in manager_todo["items"]
            if item["request"]["id"] == submitted["id"]
        )
        approved = manager.post(
            f"/api/v1/approvals/tasks/{level1_task['id']}/approve",
            json={"version": level1_task["version"], "comment": "同意，走流程"},
        )
        check(
            approved.status_code == 200 and approved.json()["current_step_order"] == 20,
            "第 1 级通过后指针推进到第 2 级",
            f"HTTP {approved.status_code} current_step_order={approved.json().get('current_step_order')}",
        )
        head_todo_after = head.get("/api/v1/approvals/tasks").json()
        check(
            any(item["request"]["id"] == submitted["id"] for item in head_todo_after["items"]),
            "第 2 级现在出现在负责人的待办里",
            f"共 {head_todo_after['total']} 条",
        )

        # ---------- 驳回 ----------
        head_task = next(
            item["task"]
            for item in head_todo_after["items"]
            if item["request"]["id"] == submitted["id"]
        )
        rejected = head.post(
            f"/api/v1/approvals/tasks/{head_task['id']}/reject",
            json={"version": head_task["version"], "comment": "预算不够，下季度再报"},
        )
        check(
            rejected.status_code == 200
            and rejected.json()["request_status"] == "REJECTED",
            "驳回 → 主单变 REJECTED",
            f"HTTP {rejected.status_code} request_status={rejected.json().get('request_status')}",
        )
        detail = requester.get(f"/api/v1/requests/{submitted['id']}").json()
        types = [event["event_type"] for event in detail["timeline"]]
        check(
            "APPROVAL_REJECTED" in types and detail["request"]["status"] == "REJECTED",
            "申请人时间线里能看到驳回事件",
            f"事件={types}",
        )

        # ---------- 全部通过 → SOURCING ----------
        full = requester.post(
            "/api/v1/requests", json={"category_id": cat_id, "title": "自检-单级通过"}
        ).json()
        full_items = requester.put(
            f"/api/v1/requests/{full['id']}/items",
            json={
                "version": full["version"],
                "items": [{"name": "办公用品", "quantity": 1, "unit_price_fen": 50_000}],
            },
        ).json()
        full_submitted = requester.post(
            f"/api/v1/requests/{full['id']}/submit",
            json={"version": full_items["version"]},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).json()
        created_requests.append(full_submitted["id"])
        task = next(
            item["task"]
            for item in manager.get("/api/v1/approvals/tasks").json()["items"]
            if item["request"]["id"] == full_submitted["id"]
        )
        done = manager.post(
            f"/api/v1/approvals/tasks/{task['id']}/approve",
            json={"version": task["version"], "comment": "同意"},
        )
        check(
            done.status_code == 200 and done.json()["request_status"] == "SOURCING",
            "全部通过 → 主单自动进入 SOURCING（询价中）",
            f"HTTP {done.status_code} request_status={done.json().get('request_status')}",
        )
        final = requester.get(f"/api/v1/requests/{full_submitted['id']}").json()
        check(
            "APPROVAL_COMPLETED" in [event["event_type"] for event in final["timeline"]],
            "时间线里有 APPROVAL_COMPLETED",
            "",
        )

        # ---------- 缺人 → 422 ----------
        empty_dept = admin.post(
            "/api/v1/admin/departments", json={"name": "M6空部门", "code": f"TDEP-E{stamp}"}
        ).json()
        lonely_name = f"t-smoke-lonely-{stamp}"
        users.append(make_user(lonely_name, ["REQUESTER"], empty_dept["id"]))
        lonely = login(lonely_name)
        draft = lonely.post(
            "/api/v1/requests", json={"category_id": cat_id, "title": "自检-缺人"}
        ).json()
        lonely_items = lonely.put(
            f"/api/v1/requests/{draft['id']}/items",
            json={
                "version": draft["version"],
                "items": [{"name": "办公用品", "quantity": 1, "unit_price_fen": 50_000}],
            },
        ).json()
        no_approver = lonely.post(
            f"/api/v1/requests/{draft['id']}/submit",
            json={"version": lonely_items["version"]},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        created_requests.append(draft["id"])
        message = no_approver.json().get("error", {}).get("message", "")
        check(
            no_approver.status_code == 422 and "DEPT_MANAGER" in message,
            "审批链某一级没人可审 → 提交时 422 并说明是哪一级",
            f"HTTP {no_approver.status_code} {message[:70]}",
        )
        rollback = lonely.get(f"/api/v1/requests/{draft['id']}").json()
        check(
            rollback["request"]["status"] == "DRAFT",
            "提交失败整体回滚：主单仍是草稿（没留下半截审批链）",
            f"status={rollback['request']['status']}",
        )

        # ---------- 策略校验（第四层） ----------
        reachability = admin.post(
            "/api/v1/admin/categories",
            json={
                "name": "M6死步骤",
                "code": f"TCAT-D{stamp}",
                "approval_policy": {
                    "steps": [
                        {"order": 10, "role": "DEPT_MANAGER", "when": {"gte": ["amount_fen", 0]}},
                        {"order": 60, "role": "GM", "when": {"gte": ["amount_fen", 20_000_000]}},
                    ]
                },
            },
        )
        check(
            reachability.status_code == 422 and "order=60" in reachability.text,
            "配了永远不会生效的步骤 → 保存时 422（可达性校验）",
            f"HTTP {reachability.status_code}",
        )
    finally:
        # ---------- 清理 ----------
        # 管理员接口**没有删除端点**（这是刻意的：管理操作只增不改不删，
        # 删数据要走运维路径）。所以这里直连库、**按本次实际用到的 id 与前缀**删：
        #   1. 只删本次造出来的那几个 request_id（不是"按状态清空"）；
        #   2. 账号只删 `t-smoke-*` 前缀；
        #   3. 删完**回查一次**，把"删掉了什么"打在屏幕上供复核。
        admin.close()
        requester.close()
        manager.close()
        head.close()
        removed = cleanup(
            created_requests,
            users,
            cat_id,
            dept_id,
            extra_departments=[empty_dept["id"]],
        )
        print(f"\n清理：{removed}")
        check(
            removed["残留账号"] == 0,
            "自检数据已清理（回查残留账号为 0）",
            f"删除 申请 {removed['申请']} 条 / 事件 {removed['事件']} 条 / "
            f"任务 {removed['审批任务']} 条 / 流 {removed['审批流']} 条 / "
            f"账号 {removed['账号']} 个 / 品类 {removed['品类']} 个 / 部门 {removed['部门']} 个",
        )

    passed = sum(1 for ok, _, _ in results if ok)
    print(f"\n=== {passed}/{len(results)} 项通过 ===")
    for ok, title, evidence in results:
        if not ok:
            print(f"  未通过：{title}  {evidence}")
    return 0 if passed == len(results) else 1


def cleanup(
    request_ids: list[str],
    usernames: list[str],
    category_id: str,
    department_id: str,
    extra_departments: list[str] | None = None,
) -> dict[str, int]:
    """删掉本次自检造出来的数据。删完回查残留。"""
    sys.path.insert(0, ".")
    from sqlalchemy import delete, func, select, text  # noqa: PLC0415

    from app.database import SessionFactory  # noqa: PLC0415
    from app.infrastructure.records.approval import (  # noqa: PLC0415
        ApprovalFlowRecord,
        ApprovalTaskRecord,
    )
    from app.infrastructure.records.auth import UserRecord  # noqa: PLC0415
    from app.infrastructure.records.catalog import (  # noqa: PLC0415
        DepartmentRecord,
        PurchaseCategoryRecord,
    )
    from app.infrastructure.records.purchase import (  # noqa: PLC0415
        IdempotencyRecord,
        PurchaseItemRecord,
        PurchaseRequestRecord,
        RequestEventRecord,
    )

    counts: dict[str, int] = {}
    ids = [uuid.UUID(value) for value in request_ids]
    # 账号名大小写不敏感地匹配：登录会 lower()，所以库里可能存着混大小写的旧残留
    # （脚本早期版本就是这么写进去的），用精确 IN 会漏掉它们。
    lowered = [name.lower() for name in usernames]

    async def run() -> dict[str, int]:
        async with SessionFactory() as session:
            result: dict[str, int] = {}
            user_ids = list(
                (
                    await session.scalars(
                        select(UserRecord.id).where(
                            func.lower(UserRecord.username).in_(lowered)
                        )
                    )
                ).all()
            )
            if ids:
                for name, model, column in (
                    ("审批任务", ApprovalTaskRecord, ApprovalTaskRecord.request_id),
                    ("审批流", ApprovalFlowRecord, ApprovalFlowRecord.request_id),
                    ("事件", RequestEventRecord, RequestEventRecord.request_id),
                    ("明细", PurchaseItemRecord, PurchaseItemRecord.request_id),
                ):
                    outcome = await session.execute(delete(model).where(column.in_(ids)))
                    result[name] = outcome.rowcount or 0
                outcome = await session.execute(
                    delete(PurchaseRequestRecord).where(PurchaseRequestRecord.id.in_(ids))
                )
                result["申请"] = outcome.rowcount or 0
            if user_ids:
                outcome = await session.execute(
                    delete(IdempotencyRecord).where(IdempotencyRecord.actor_id.in_(user_ids))
                )
                result["幂等记录"] = outcome.rowcount or 0
                # 审批任务里可能还引用着这些账号（决定人 / 被指派），外键是 RESTRICT
                await session.execute(
                    text(
                        "UPDATE approval_tasks SET assignee_id = NULL, decided_by = NULL "
                        "WHERE assignee_id = ANY(:ids) OR decided_by = ANY(:ids)"
                    ),
                    {"ids": user_ids},
                )
                outcome = await session.execute(
                    delete(UserRecord).where(UserRecord.id.in_(user_ids))
                )
                result["账号"] = outcome.rowcount or 0
            outcome = await session.execute(
                delete(PurchaseCategoryRecord).where(
                    PurchaseCategoryRecord.id == uuid.UUID(category_id)
                )
            )
            result["品类"] = outcome.rowcount or 0
            department_ids = [uuid.UUID(department_id)] + [
                uuid.UUID(value) for value in (extra_departments or [])
            ]
            outcome = await session.execute(
                delete(DepartmentRecord).where(DepartmentRecord.id.in_(department_ids))
            )
            result["部门"] = outcome.rowcount or 0
            await session.commit()

            result["残留账号"] = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(UserRecord)
                        .where(UserRecord.username.like("t-smoke-%"))
                    )
                ).scalar_one()
            )
            return result

    counts.update(_run_db(run))
    return counts


if __name__ == "__main__":
    sys.exit(main())
