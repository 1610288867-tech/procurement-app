"""用例层依赖的端口。

只用 `Protocol` 描述"我需要什么能力"，**不 import 任何框架**——
这样 `application/` 就不会依赖 SQLAlchemy，换 ORM 或换存储时这层不用动。

具体实现（`CatalogRepository`、`AsyncSession`）在**结构上**满足这些协议，
所以边界层可以直接把它们传进来，不需要额外的适配器。

为什么主数据只有一个端口：它们本来就是同一组聚合（部门 / 品类 / 供应商 / 预算
都由管理员维护、都从 `CatalogRepository` 存取），拆成四个端口只会让协调者的
构造函数变长，并没有换来实际的可替换性。
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol


class SupplierView(Protocol):
    """ORM 记录在结构上满足它。"""

    id: Any
    name: str
    code: str
    active: bool
    qualification_expires_at: datetime | None


class DepartmentView(Protocol):
    id: Any
    name: str
    code: str
    parent_id: Any
    active: bool


class CategoryView(Protocol):
    id: Any
    name: str
    code: str
    form_schema: dict
    approval_policy: dict
    version: int
    active: bool


class BudgetView(Protocol):
    id: Any
    department_id: Any
    category_id: Any
    year: int
    amount_total: int
    amount_reserved: int
    amount_spent: int


class CatalogRepositoryPort(Protocol):
    # ---- 供应商 ----
    async def list_suppliers(self, *, only_active: bool = True) -> Sequence[SupplierView]: ...

    async def create_supplier(
        self,
        *,
        name: str,
        code: str,
        qualification_expires_at: datetime | None = None,
    ) -> SupplierView: ...

    async def disable_supplier(self, *, supplier_id: Any, reason: str) -> bool: ...

    # ---- 部门 ----
    async def list_departments(
        self, *, only_active: bool = True
    ) -> Sequence[DepartmentView]: ...

    async def create_department(
        self, *, name: str, code: str, parent_id: Any | None = None
    ) -> DepartmentView: ...

    async def get_department(self, department_id: Any) -> DepartmentView | None: ...

    # ---- 采购品类 ----
    async def list_categories(
        self, *, only_active: bool = True
    ) -> Sequence[CategoryView]: ...

    async def get_category(self, category_id: Any) -> CategoryView | None: ...

    async def create_category(
        self,
        *,
        name: str,
        code: str,
        form_schema: Mapping[str, Any],
        approval_policy: Mapping[str, Any],
    ) -> CategoryView: ...

    async def update_approval_policy(
        self, *, category_id: Any, approval_policy: Mapping[str, Any]
    ) -> CategoryView | None: ...

    # ---- 预算 ----
    async def list_budgets(
        self, *, department_id: Any | None = None, year: int | None = None
    ) -> Sequence[BudgetView]: ...

    async def create_budget(
        self, *, department_id: Any, category_id: Any, year: int, amount_fen: int
    ) -> BudgetView: ...


class UnitOfWork(Protocol):
    """事务边界。`AsyncSession` 在结构上满足它。"""

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class UserView(Protocol):
    """账号记录在结构上满足它。**注意有 password_hash** ——
    这一层要拿它校验口令，但**绝不能把它传到响应模型里**。"""

    id: Any
    username: str
    password_hash: str
    full_name: str
    department_id: Any
    active: bool
    token_version: int


class UserRepositoryPort(Protocol):
    async def find_by_username(self, username: str) -> UserView | None: ...

    async def find_by_id(self, user_id: Any) -> UserView | None: ...

    async def roles_of(self, user_id: Any) -> Sequence[str]: ...

    async def create_user(
        self,
        *,
        username: str,
        password_hash: str,
        full_name: str,
        department_id: Any | None = None,
    ) -> UserView: ...

    async def add_role(self, *, user_id: Any, role: str) -> None: ...

    async def freeze(self, user_id: Any) -> bool: ...
