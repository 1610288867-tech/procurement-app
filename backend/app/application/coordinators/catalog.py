"""采购目录与主数据用例（M1 供应商 → M2 部门 / 品类 / 预算）。

它是后面所有用例的模板：协调者拿端口 → 调数据 → **在这里 commit** → 返回。

唯一的例外是"业务判断"：审批策略的语义校验交给 `domain/rules.py`，
这一层只负责**调用**它并把结果转成错误，不自己写规则。
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from app.application.ports import (
    BudgetView,
    CatalogRepositoryPort,
    CategoryView,
    DepartmentView,
    SupplierView,
    UnitOfWork,
)
from app.domain.rules import validate_policy
from app.errors import BusinessError, NotFoundError


class CatalogCoordinator:
    def __init__(self, *, catalog: CatalogRepositoryPort, uow: UnitOfWork) -> None:
        self._catalog = catalog
        self._uow = uow

    # ------------------------------------------------------------------ 供应商

    async def list_suppliers(self, *, only_active: bool = True) -> Sequence[SupplierView]:
        return await self._catalog.list_suppliers(only_active=only_active)

    async def create_supplier(
        self,
        *,
        name: str,
        code: str,
        qualification_expires_at: datetime | None = None,
    ) -> SupplierView:
        record = await self._catalog.create_supplier(
            name=name, code=code, qualification_expires_at=qualification_expires_at
        )
        await self._uow.commit()
        return record

    async def disable_supplier(self, *, supplier_id: uuid.UUID, reason: str) -> None:
        disabled = await self._catalog.disable_supplier(
            supplier_id=supplier_id, reason=reason
        )
        if not disabled:
            # 条件更新没命中 = 要么不存在，要么已经停用
            raise NotFoundError("供应商不存在，或已经停用")
        await self._uow.commit()

    # -------------------------------------------------------------------- 部门

    async def list_departments(self, *, only_active: bool = True) -> Sequence[DepartmentView]:
        return await self._catalog.list_departments(only_active=only_active)

    async def create_department(
        self, *, name: str, code: str, parent_id: uuid.UUID | None = None
    ) -> DepartmentView:
        record = await self._catalog.create_department(
            name=name, code=code, parent_id=parent_id
        )
        await self._uow.commit()
        return record

    # ---------------------------------------------------------------- 采购品类

    async def list_categories(self, *, only_active: bool = True) -> Sequence[CategoryView]:
        return await self._catalog.list_categories(only_active=only_active)

    async def get_category(self, category_id: uuid.UUID) -> CategoryView:
        record = await self._catalog.get_category(category_id)
        if record is None:
            raise NotFoundError(f"品类不存在：{category_id}")
        return record

    async def create_category(
        self,
        *,
        name: str,
        code: str,
        form_schema: Mapping[str, Any],
        approval_policy: Mapping[str, Any],
    ) -> CategoryView:
        _assert_policy_valid(approval_policy)
        record = await self._catalog.create_category(
            name=name,
            code=code,
            form_schema=form_schema,
            approval_policy=approval_policy,
        )
        await self._uow.commit()
        return record

    async def update_approval_policy(
        self, *, category_id: uuid.UUID, approval_policy: Mapping[str, Any]
    ) -> CategoryView:
        _assert_policy_valid(approval_policy)
        record = await self._catalog.update_approval_policy(
            category_id=category_id, approval_policy=approval_policy
        )
        if record is None:
            raise NotFoundError(f"品类不存在：{category_id}")
        await self._uow.commit()
        return record

    # -------------------------------------------------------------------- 预算

    async def list_budgets(
        self, *, department_id: uuid.UUID | None = None, year: int | None = None
    ) -> Sequence[BudgetView]:
        return await self._catalog.list_budgets(department_id=department_id, year=year)

    async def create_budget(
        self, *, department_id: uuid.UUID, category_id: uuid.UUID, year: int, amount_fen: int
    ) -> BudgetView:
        record = await self._catalog.create_budget(
            department_id=department_id,
            category_id=category_id,
            year=year,
            amount_fen=amount_fen,
        )
        await self._uow.commit()
        return record


def _assert_policy_valid(policy: Mapping[str, Any]) -> None:
    """三层校验的第二层：语义。

    结构（Pydantic）第一层在边界层；可达性第三层在 M5。
    这一层不过 = 直接 422，且把具体问题原样写给管理员。
    """
    problems = validate_policy(policy)
    if problems:
        raise BusinessError(
            "审批策略不合法：" + "；".join(problems),
            status_code=422,
            code="INVALID_APPROVAL_POLICY",
        )
