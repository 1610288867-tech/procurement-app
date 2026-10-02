"""主数据（部门 / 品类 / 供应商 / 预算）的数据访问。

纪律：**只 flush()，不 commit()** —— 事务边界在 `application/coordinators/`。
数据库层的冲突（重码、重复预算、外键不存在）在这里翻译成业务错误，
让调用方拿到的是「哪里错了」，而不是一个 SQL 报错。
"""

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import BusinessError, ConflictError
from app.infrastructure.records.catalog import (
    BudgetRecord,
    DepartmentRecord,
    PurchaseCategoryRecord,
    SupplierRecord,
)


class CatalogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ 供应商

    async def list_suppliers(self, *, only_active: bool = True) -> list[SupplierRecord]:
        statement = select(SupplierRecord).order_by(SupplierRecord.name)
        if only_active:
            statement = statement.where(SupplierRecord.active.is_(True))
        return list((await self.session.scalars(statement)).all())

    async def get_supplier(self, supplier_id: uuid.UUID) -> SupplierRecord | None:
        return await self.session.get(SupplierRecord, supplier_id)

    async def create_supplier(
        self,
        *,
        name: str,
        code: str,
        qualification_expires_at: datetime | None = None,
    ) -> SupplierRecord:
        """冲突交给数据库唯一约束判定，不做"先查再插"。"""
        record = SupplierRecord(
            name=name, code=code, qualification_expires_at=qualification_expires_at
        )
        self.session.add(record)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise ConflictError(f"供应商编码已存在：{code}") from exc
        return record

    async def disable_supplier(self, *, supplier_id: uuid.UUID, reason: str) -> bool:
        """条件更新：已经停用的再停一次返回 False，不重复写。

        用 RETURNING 而不是 rowcount —— 异步 Result 的类型标注里没有 rowcount，
        而 RETURNING 既能判断命中、又是强类型的（M6 的预算预留会沿用这个写法）。
        """
        statement = (
            update(SupplierRecord)
            .where(SupplierRecord.id == supplier_id, SupplierRecord.active.is_(True))
            .values(active=False, disabled_reason=reason)
            .returning(SupplierRecord.id)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none() is not None

    # -------------------------------------------------------------------- 部门

    async def list_departments(self, *, only_active: bool = True) -> list[DepartmentRecord]:
        statement = select(DepartmentRecord).order_by(DepartmentRecord.name)
        if only_active:
            statement = statement.where(DepartmentRecord.active.is_(True))
        return list((await self.session.scalars(statement)).all())

    async def get_department(self, department_id: uuid.UUID) -> DepartmentRecord | None:
        return await self.session.get(DepartmentRecord, department_id)

    async def create_department(
        self, *, name: str, code: str, parent_id: uuid.UUID | None = None
    ) -> DepartmentRecord:
        if parent_id is not None and await self.get_department(parent_id) is None:
            raise BusinessError(
                f"上级部门不存在：{parent_id}",
                status_code=422,
                code="DEPARTMENT_NOT_FOUND",
            )
        record = DepartmentRecord(name=name, code=code, parent_id=parent_id)
        self.session.add(record)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise ConflictError(f"部门编码已存在：{code}") from exc
        return record

    # ---------------------------------------------------------------- 采购品类

    async def list_categories(self, *, only_active: bool = True) -> list[PurchaseCategoryRecord]:
        statement = select(PurchaseCategoryRecord).order_by(PurchaseCategoryRecord.name)
        if only_active:
            statement = statement.where(PurchaseCategoryRecord.active.is_(True))
        return list((await self.session.scalars(statement)).all())

    async def get_category(self, category_id: uuid.UUID) -> PurchaseCategoryRecord | None:
        return await self.session.get(PurchaseCategoryRecord, category_id)

    async def create_category(
        self,
        *,
        name: str,
        code: str,
        form_schema: Mapping[str, Any],
        approval_policy: Mapping[str, Any],
    ) -> PurchaseCategoryRecord:
        record = PurchaseCategoryRecord(
            name=name,
            code=code,
            form_schema=dict(form_schema),
            approval_policy=dict(approval_policy),
        )
        self.session.add(record)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise ConflictError(f"品类编码已存在：{code}") from exc
        return record

    async def update_approval_policy(
        self, *, category_id: uuid.UUID, approval_policy: Mapping[str, Any]
    ) -> PurchaseCategoryRecord | None:
        """改审批策略 —— 版本号 +1，方便日后追溯策略是什么时候变的。"""
        record = await self.get_category(category_id)
        if record is None:
            return None
        record.approval_policy = dict(approval_policy)
        record.version += 1
        await self.session.flush()
        return record

    # -------------------------------------------------------------------- 预算

    async def list_budgets(
        self, *, department_id: uuid.UUID | None = None, year: int | None = None
    ) -> list[BudgetRecord]:
        statement = select(BudgetRecord).order_by(BudgetRecord.year, BudgetRecord.id)
        if department_id is not None:
            statement = statement.where(BudgetRecord.department_id == department_id)
        if year is not None:
            statement = statement.where(BudgetRecord.year == year)
        return list((await self.session.scalars(statement)).all())

    async def create_budget(
        self, *, department_id: uuid.UUID, category_id: uuid.UUID, year: int, amount_fen: int
    ) -> BudgetRecord:
        # 先给出「哪个引用不存在」这种可自救的提示；剩下的冲突交给唯一约束
        if await self.get_department(department_id) is None:
            raise BusinessError(
                f"部门不存在：{department_id}", status_code=422, code="DEPARTMENT_NOT_FOUND"
            )
        if await self.get_category(category_id) is None:
            raise BusinessError(
                f"品类不存在：{category_id}", status_code=422, code="CATEGORY_NOT_FOUND"
            )

        record = BudgetRecord(
            department_id=department_id,
            category_id=category_id,
            year=year,
            amount_total=amount_fen,
        )
        self.session.add(record)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise ConflictError("该部门 + 年度 + 品类的预算已存在") from exc
        return record
