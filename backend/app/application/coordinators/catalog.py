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
from app.domain.approval_engine import check_reachability
from app.domain.enums import URGENCY_URGENT
from app.domain.rules import validate_policy
from app.errors import BusinessError, NotFoundError


class CatalogCoordinator:
    def __init__(self, *, catalog: CatalogRepositoryPort, uow: UnitOfWork) -> None:
        self._catalog = catalog
        self._uow = uow

    # ------------------------------------------------------------------ 供应商

    async def list_suppliers(
        self, *, only_active: bool = True, only_qualified: bool = True
    ) -> Sequence[SupplierView]:
        """`only_qualified=False` 只有管理员列表会用（要能看到已过期/已停用的）。

        业务上的选人只能拿到 `only_qualified=True` 的结果 —— 资质过期即不可选。
        """
        return await self._catalog.list_suppliers(
            only_active=only_active, only_qualified=only_qualified
        )

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
    """三层校验的第二层（语义）+ 第三层（可达性）。

    结构（Pydantic）第一层在边界层。这一层不过 = 直接 422，且把具体问题原样写给管理员。

    - 第二层 `validate_policy`：路径白名单 / 整数分 / 零阈值兜底 / order 不重复 ——
      拦的是"规则本身写错"；
    - 第三层 `check_reachability`（M6 补上）：拿几个代表性金额反推 ——
      拦的是"规则写得没错、但永远不会生效"，例如某一级的阈值高到任何金额都够不着。
      这类问题**不会报错、只会静默降低审批层级**，所以必须在这里拦住。
    """
    problems = validate_policy(policy)
    # 加急件那一级只在 urgency=URGENT 时生效，探测时要带上它，否则会被误判成死步骤
    problems += check_reachability(policy, urgency=URGENCY_URGENT)
    if problems:
        raise BusinessError(
            "审批策略不合法：" + "；".join(problems),
            status_code=422,
            code="INVALID_APPROVAL_POLICY",
        )
