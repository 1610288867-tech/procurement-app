"""预算的数据访问（M7）：预留 / 释放 / 结转，以及账本流水。

纪律同其他仓库：**只 flush()，不 commit()** —— 事务边界在用例层。

============================ 这个仓库唯一不能写错的事 ============================
**绝不"先查余额再更新"。** 所有金额变动都必须是一次**条件更新**，
守卫条件（`amount_total − (amount_reserved + amount_spent) >= :amount`）整个写在 `WHERE` 里，
让数据库去做减法与判断：受影响行数为 0 就说明不够（或状态不对），直接当失败处理。

为什么不能在 Python 里做减法：
  1. 并发下两个人会同时读到"够"，然后双双扣款 → 超额占用，而且不报错；
  2. `Expression >= amount` 拿到的不是布尔值而是 SQL 子句，`where()` 会直接抛
     `TypeError: Boolean value of this clause is not defined`（M0 实测踩过）。

============================ 为什么流水写在仓库里 ============================
"每次金额变动都写一条流水"这条纪律，如果交给调用方去记得，**早晚会漏一处**
（漏了不报错，只是事后对不上账）。所以三个变动方法自带写流水：
想动钱，就必须经过这里，也就必然留下流水。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.domain.budget_policy import BudgetSettlement, settlement
from app.domain.enums import TX_RELEASE, TX_RESERVE, TX_SPEND
from app.errors import ConflictError
from app.infrastructure.records.catalog import (
    BudgetRecord,
    BudgetTransactionRecord,
    DepartmentRecord,
    PurchaseCategoryRecord,
)
from app.infrastructure.records.purchase import PurchaseRequestRecord


class BudgetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ 读

    async def find_budget(
        self, *, department_id: uuid.UUID, category_id: uuid.UUID, year: int
    ) -> BudgetRecord | None:
        """按"部门 × 品类 × 年度"找预算。唯一约束保证最多一条。"""
        statement = select(BudgetRecord).where(
            BudgetRecord.department_id == department_id,
            BudgetRecord.category_id == category_id,
            BudgetRecord.year == year,
        )
        return (await self.session.scalars(statement)).one_or_none()

    async def get_budget(self, budget_id: uuid.UUID) -> BudgetRecord | None:
        return await self.session.get(BudgetRecord, budget_id)

    async def snapshot(self, budget_id: uuid.UUID) -> tuple[int, int, int] | None:
        """**强制从库里读**当前的三个金额：(总额, 已预留, 已花)。

        用途只有一个：条件更新失败之后，把"还差多少"算成人话。

        ⚠️ 必须 `populate_existing=True`：`select(Model.col)` 在对象**已经加载过**时
        会拿回身份映射里缓存的那份旧值（M6 踩过同类坑：库里已是 APPROVED、
        接口却说"还没完成"）。并发失败的那一方如果读到旧值，
        就会得出"钱明明够啊"的结论，于是错误码从"预算不足"退化成一句含糊的冲突。

        这里**不做任何判断**：判定永远只由条件更新做（见模块开头）。
        """
        statement = (
            select(
                BudgetRecord.amount_total,
                BudgetRecord.amount_reserved,
                BudgetRecord.amount_spent,
            )
            .where(BudgetRecord.id == budget_id)
            .execution_options(populate_existing=True)
        )
        row = (await self.session.execute(statement)).one_or_none()
        if row is None:
            return None
        return int(row[0]), int(row[1]), int(row[2])

    async def list_budgets(
        self, *, year: int | None = None
    ) -> list[tuple[BudgetRecord, str, str]]:
        """全部预算 + 部门名 + 品类名（看板用，新年度在前）。

        为什么把名字一起带出来：看板的读者是**财务**，而财务没有"列出全部部门 / 品类"
        的权限（那两条是管理员接口）。少一次前端补查，也少开一个权限口子。
        """
        statement = (
            select(
                BudgetRecord,
                DepartmentRecord.name,
                PurchaseCategoryRecord.name,
            )
            .join(DepartmentRecord, DepartmentRecord.id == BudgetRecord.department_id)
            .join(
                PurchaseCategoryRecord,
                PurchaseCategoryRecord.id == BudgetRecord.category_id,
            )
            .order_by(
                BudgetRecord.year.desc(),
                DepartmentRecord.name,
                PurchaseCategoryRecord.name,
            )
        )
        if year is not None:
            statement = statement.where(BudgetRecord.year == year)
        return [(row[0], row[1], row[2]) for row in (await self.session.execute(statement)).all()]

    async def list_transactions(
        self, *, budget_id: uuid.UUID | None = None, limit: int = 50
    ) -> list[tuple[BudgetTransactionRecord, str]]:
        """流水（新→旧），带上申请单号方便人读。

        排序用 `seq` 倒序，不用 `created_at`：同一事务里写的两条流水
        （`settle()` 的 SPEND + RELEASE）时间戳可能相同，"按时间倒序"会退化成
        按随机的 `id` 排 —— 对账时看到的顺序每次都可能不一样。

        用 `tuple(流水, 单号)` 而不是给流水表加一列冗余单号：
        单号是申请的属性，抄一份进来就多一处会漂移的数据。
        """
        statement = (
            select(BudgetTransactionRecord, PurchaseRequestRecord.code)
            .join(
                PurchaseRequestRecord,
                PurchaseRequestRecord.id == BudgetTransactionRecord.request_id,
            )
            .order_by(
                BudgetTransactionRecord.seq.desc(),
            )
            .limit(max(1, min(limit, 200)))
        )
        if budget_id is not None:
            statement = statement.where(BudgetTransactionRecord.budget_id == budget_id)
        return [(row[0], row[1]) for row in (await self.session.execute(statement)).all()]

    async def reserved_fen(self, request_id: uuid.UUID) -> int:
        """这笔申请**当前还占着**多少预留（分）= RESERVE − RELEASE。

        `SPEND` 不参与：付款结转时 `amount_reserved` 已经减掉了，
        再算进来会把"还占着多少"算成负数。
        """
        statement = select(
            func.coalesce(
                func.sum(
                    case(
                        (BudgetTransactionRecord.kind == TX_RESERVE, BudgetTransactionRecord.amount),
                        else_=-BudgetTransactionRecord.amount,
                    )
                ),
                0,
            )
        ).where(BudgetTransactionRecord.request_id == request_id)
        return int((await self.session.scalars(statement)).one())

    async def has_transaction(self, *, request_id: uuid.UUID, kind: str) -> bool:
        statement = select(BudgetTransactionRecord.id).where(
            BudgetTransactionRecord.request_id == request_id,
            BudgetTransactionRecord.kind == kind,
        )
        return (await self.session.scalars(statement)).first() is not None

    # ------------------------------------------------------------------ 写

    async def reserve(
        self, *, budget_id: uuid.UUID, request_id: uuid.UUID, amount_fen: int
    ) -> bool:
        """预留：条件更新 + 一条 `RESERVE` 流水。返回是否成功。

        守卫条件整个写在 `where()` 里（见模块开头的说明）。
        金额 <= 0 由 `domain/budget_policy.py` 在调用前拦掉。
        """
        guard = (
            BudgetRecord.amount_total
            - (BudgetRecord.amount_reserved + BudgetRecord.amount_spent)
        ) >= amount_fen
        moved = await self._apply(
            budget_id=budget_id,
            guard=guard,
            values={
                "amount_reserved": BudgetRecord.amount_reserved + amount_fen,
            },
        )
        if not moved:
            return False
        await self._append(
            budget_id=budget_id, request_id=request_id, kind=TX_RESERVE, amount_fen=amount_fen
        )
        return True

    async def release(
        self, *, budget_id: uuid.UUID, request_id: uuid.UUID, amount_fen: int
    ) -> bool:
        """释放：条件更新 + 一条 `RELEASE` 流水。

        两个入口都会走到这里：驳回/撤回（全额释放）、付款结转（释放差额）。
        **两种都要写流水**，否则事后说不清额度是什么时候放掉的。

        守卫里那条 `amount_reserved >= :amount` 是兜底：释放不该让预留变成负数
        （数据库上还有 `ck_budget_not_overdrawn` 兜第二层）。
        """
        if amount_fen <= 0:
            # 差额为 0 时不写流水（金额为 0 的流水只会给对账添噪音）
            return True
        moved = await self._apply(
            budget_id=budget_id,
            guard=BudgetRecord.amount_reserved >= amount_fen,
            values={
                "amount_reserved": BudgetRecord.amount_reserved - amount_fen,
            },
        )
        if not moved:
            return False
        await self._append(
            budget_id=budget_id, request_id=request_id, kind=TX_RELEASE, amount_fen=amount_fen
        )
        return True

    async def spend(
        self, *, budget_id: uuid.UUID, request_id: uuid.UUID, amount_fen: int
    ) -> bool:
        """结转：预留 → 已花（按**实付**金额），并写一条 `SPEND` 流水。

        预留与实付的差额**不在这里处理** —— 那是 `settle()` 的职责。
        单独保留这个方法，是为了让"只结转、不释放"这条路径根本无法被误用成结算。
        """
        moved = await self._apply(
            budget_id=budget_id,
            guard=BudgetRecord.amount_reserved >= amount_fen,
            values={
                "amount_reserved": BudgetRecord.amount_reserved - amount_fen,
                "amount_spent": BudgetRecord.amount_spent + amount_fen,
            },
        )
        if not moved:
            return False
        await self._append(
            budget_id=budget_id, request_id=request_id, kind=TX_SPEND, amount_fen=amount_fen
        )
        return True

    async def settle(
        self, *, budget_id: uuid.UUID, request_id: uuid.UUID, actual_fen: int
    ) -> BudgetSettlement:
        """一笔申请结清：`SPEND(实付)` + `RELEASE(预留 − 实付)`。

        ⚠️ **这是"预留与实付差额必须显式释放"的唯一落点。**
        少了那条 RELEASE，差额会永久挂在 `amount_reserved` 上：账面正常、日志不报，
        可用额度却虚低，而且越积越多。

        金额换算在 `domain/budget_policy.settlement()`（纯函数，毫秒级可测），
        这里只负责把它算出来的两条流水分两次落库。
        """
        reserved = await self.reserved_fen(request_id)
        plan = settlement(reserved_fen=reserved, actual_fen=actual_fen)

        if not await self.spend(
            budget_id=budget_id, request_id=request_id, amount_fen=plan.spend_fen
        ):
            raise ConflictError("预算预留状态异常，结转失败（预留可能已被释放）")
        if plan.release_fen > 0 and not await self.release(
            budget_id=budget_id, request_id=request_id, amount_fen=plan.release_fen
        ):
            raise ConflictError("预算预留状态异常，差额释放失败")
        return plan

    # ------------------------------------------------------------------ 内部

    async def _apply(
        self,
        *,
        budget_id: uuid.UUID,
        guard: ColumnElement[bool],
        values: dict[str, Any],
    ) -> bool:
        """一次条件更新。受影响行数为 0 → False（不要在这里抛异常：
        "不够"和"状态不对"要用不同的错误码，那是用例层的事）。"""
        statement = (
            update(BudgetRecord)
            .where(BudgetRecord.id == budget_id, guard)
            .values(**values)
            .returning(BudgetRecord.id)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def _append(
        self, *, budget_id: uuid.UUID, request_id: uuid.UUID, kind: str, amount_fen: int
    ) -> BudgetTransactionRecord:
        """写一条流水。

        顺序由 `seq` 保证（数据库侧的单调序列，迁移 `b7d3f0c41a26`），
        `created_at` 只负责**显示**：同一事务里写的两条流水
        （`settle()` 的 SPEND + RELEASE）时间戳可能相同，按时间排会退化成按随机的 `id` 排。

        ⚠️ 但 `created_at` 仍然必须由**应用侧**给：这张表的该列没有数据库默认值
        （不同于 `request_events` 的 `now()`）。漏了它就是 `NOT NULL` 违约，
        而违约发生在 `flush()` 里，正好会被下面那个 `except` 接住 ——
        于是报错信息变成"net 上已经有一条 RESERVE 流水"，**完全指错方向**
        （2026-10-04 实测踩过：把 `created_at` 交给数据库，7 条预算用例全挂在这句话上）。

        撞 `UNIQUE(request_id, kind)` 说明同一笔申请被动了两次钱（重试 / 重复调用），
        这是**必须让人看见**的事故，所以转成一个明确的 409，而不是静默多占一份预算。
        """
        record = BudgetTransactionRecord(
            budget_id=budget_id,
            request_id=request_id,
            kind=kind,
            amount=amount_fen,
            created_at=datetime.now(UTC),
        )
        self.session.add(record)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise ConflictError(
                f"这笔申请已经有一条 {kind} 流水，不能重复记账（可能是重试）"
            ) from exc
        return record
