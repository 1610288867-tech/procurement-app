"""用例层依赖的端口。

只用 `Protocol` 描述"我需要什么能力"，**不 import 任何框架**——
这样 `application/` 就不会依赖 SQLAlchemy，换 ORM 或换存储时这层不用动。

具体实现（`CatalogRepository`、`AsyncSession`）在**结构上**满足这些协议，
所以边界层可以直接把它们传进来，不需要额外的适配器。

为什么主数据只有一个端口：它们本来就是同一组聚合（部门 / 品类 / 供应商 / 预算
都由管理员维护、都从 `CatalogRepository` 存取），拆成四个端口只会让协调者的
构造函数变长，并没有换来实际的可替换性。
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from app.domain.permissions import RequestScope


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
    async def list_suppliers(
        self, *, only_active: bool = True, only_qualified: bool = True
    ) -> Sequence[SupplierView]: ...

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


# ---------------------------------------------------------------- 采购申请（M5）


class PurchaseItemView(Protocol):
    id: Any
    request_id: Any
    line_no: int
    name: str
    spec: str | None
    quantity: int
    unit_price: int
    amount: int


class PurchaseRequestView(Protocol):
    id: Any
    code: str
    requester_id: Any
    department_id: Any
    category_id: Any
    #: 提交时认领的预算（M7）。M7 之前的历史单据是 None —— 那种单子没有预留可释放。
    budget_id: Any
    title: str
    reason: str | None
    amount_total: int
    urgency: str
    status: str
    version: int
    form_data: dict
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None
    closed_at: datetime | None


class RequestEventView(Protocol):
    id: Any
    request_id: Any
    event_type: str
    from_status: str | None
    to_status: str | None
    actor_id: Any
    payload: dict
    created_at: datetime


class AttachmentView(Protocol):
    """附件（M9）。

    `storage_key` 与 `original_filename` **必须分开**：
    前者是服务端生成的落地键（可以拼路径），后者是用户给的名字（只能显示）。
    把它们合成一个字段，早晚有人拿后者去拼路径。
    """

    id: Any
    request_id: Any
    kind: str
    original_filename: str
    content_type: str
    size_bytes: int
    sha256: str
    storage_key: str
    uploaded_by: Any
    created_at: datetime
    deleted_at: datetime | None


class AttachmentRepositoryPort(Protocol):
    async def create(
        self,
        *,
        request_id: Any,
        kind: str,
        original_filename: str,
        content_type: str,
        size_bytes: int,
        sha256: str,
        storage_key: str,
        uploaded_by: Any,
    ) -> AttachmentView: ...

    async def get(self, attachment_id: Any) -> AttachmentView | None: ...

    async def list_for_request(self, request_id: Any) -> Sequence[AttachmentView]: ...

    async def soft_delete(self, attachment_id: Any) -> bool: ...


class ObjectStorePort(Protocol):
    """对象存储。只有三个动作：发键、存、取。

    `new_key` 单独存在，是为了让"落盘成功但入库失败"能把文件删掉
    （见 `infrastructure/object_store.py` 的说明）。
    """

    def new_key(self, suffix: str) -> str: ...

    def save(self, content: bytes, suffix: str, key: str | None = None) -> str: ...

    def load(self, key: str) -> bytes: ...

    def delete(self, key: str) -> bool: ...


class PurchaseRepositoryPort(Protocol):
    # ---- 主单 ----
    async def create_request(
        self,
        *,
        requester_id: Any,
        department_id: Any,
        category_id: Any,
        title: str,
        reason: str | None,
        urgency: str,
        form_data: Mapping[str, Any],
    ) -> PurchaseRequestView: ...

    async def get_request(
        self, request_id: Any, *, scope: RequestScope, actor_id: Any
    ) -> PurchaseRequestView | None: ...

    async def list_requests(
        self,
        *,
        scope: RequestScope,
        actor_id: Any,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[Sequence[PurchaseRequestView], int]: ...

    async def apply_draft_patch(
        self,
        *,
        request_id: Any,
        expected_version: int,
        changes: Mapping[str, Any],
    ) -> bool: ...

    async def transition_status(
        self,
        *,
        request_id: Any,
        expected_version: int,
        from_status: str,
        to_status: str,
        extra: Mapping[str, Any] | None = None,
    ) -> bool: ...

    # ---- 明细 ----
    async def list_items(self, request_id: Any) -> Sequence[PurchaseItemView]: ...

    async def replace_items(
        self, *, request_id: Any, expected_version: int, lines: Sequence[Any]
    ) -> bool: ...


class RequestEventRepositoryPort(Protocol):
    """事件流。**只有 append 与读取** —— 接口上没有改和删，
    因为"事件只追加"这件事不该靠自觉。"""

    async def append(
        self,
        *,
        request_id: Any,
        event_type: str,
        actor_id: Any,
        from_status: str | None = None,
        to_status: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> RequestEventView: ...

    async def list_for_request(self, request_id: Any) -> Sequence[RequestEventView]: ...


# ---------------------------------------------------------------- 审批（M6）


class ApprovalFlowView(Protocol):
    id: Any
    request_id: Any
    status: str
    current_step_order: int
    policy_snapshot: dict
    policy_version: int
    started_at: datetime
    completed_at: datetime | None


class ApprovalTaskView(Protocol):
    id: Any
    flow_id: Any
    request_id: Any
    step_order: int
    role: str
    mode: str
    status: str
    assignee_id: Any
    decided_by: Any
    decided_at: datetime | None
    comment: str | None
    version: int
    created_at: datetime


class ApprovalRepositoryPort(Protocol):
    async def create_flow(
        self,
        *,
        request_id: Any,
        current_step_order: int,
        policy_snapshot: dict,
        policy_version: int,
    ) -> ApprovalFlowView: ...

    async def add_tasks(
        self,
        *,
        flow_id: Any,
        request_id: Any,
        tasks: Iterable[Any],
    ) -> Sequence[ApprovalTaskView]: ...

    async def get_flow(self, request_id: Any) -> ApprovalFlowView | None: ...

    async def get_task(self, task_id: Any) -> ApprovalTaskView | None: ...

    async def list_tasks(
        self, *, flow_id: Any | None = None, request_id: Any | None = None
    ) -> Sequence[ApprovalTaskView]: ...

    async def step_tasks(
        self, *, flow_id: Any, step_order: int
    ) -> Sequence[ApprovalTaskView]: ...

    async def step_statuses(
        self, *, flow_id: Any, step_order: int | None = None
    ) -> Sequence[tuple[int, str]]: ...

    async def list_todo(
        self,
        *,
        actor_id: Any,
        my_roles: Sequence[str],
        limit: int,
        offset: int,
    ) -> tuple[
        Sequence[tuple[ApprovalTaskView, PurchaseRequestView, CategoryView]], int
    ]: ...

    async def decide(
        self,
        *,
        task_id: Any,
        expected_version: int,
        current_step_order: int,
        status: str,
        actor_id: Any,
        comment: str | None,
        request_id: Any | None = None,
    ) -> bool: ...

    async def claim(
        self,
        *,
        task_id: Any,
        actor_id: Any,
        expected_version: int,
        current_step_order: int,
        request_id: Any | None = None,
    ) -> bool: ...

    async def advance_step(
        self, *, flow_id: Any, expected_step_order: int, next_step_order: int
    ) -> bool: ...

    async def flow_step_order(self, flow_id: Any) -> int | None: ...

    async def lock_flow_for_update(self, flow_id: Any) -> int | None: ...

    async def complete_flow(
        self, *, flow_id: Any, status: str, expected_step_order: int
    ) -> bool: ...

    async def cancel_pending_tasks(
        self, *, flow_id: Any, except_task_id: Any | None = None
    ) -> int: ...


class UserDirectoryPort(Protocol):
    """按角色找审批候选人（M6）。

    单独一个端口而不是塞进 `UserRepositoryPort`：它的用途是"给审批链找人"，
    与认证那套（找账号、验口令、冻结）不是同一个关注点。
    """

    async def find_by_role(
        self, role: str, *, department_ids: Collection[Any] | None = None
    ) -> Sequence[UserView]: ...


# ---------------------------------------------------------------- 预算（M7）


class BudgetTransactionView(Protocol):
    """一条预算流水（只读展示用）。"""

    id: Any
    budget_id: Any
    request_id: Any
    kind: str
    amount: int
    created_at: datetime


class BudgetLedgerPort(Protocol):
    """预算的**金额变动**入口（M7）。

    单独一个端口而不是塞进 `CatalogRepositoryPort`：那个端口管的是"管理员维护的主数据"
    （增删改查），这里管的是"钱怎么动"—— 两者的纪律完全不同：
    变动只允许走条件更新，而且**每次变动都必须留下一条流水**。
    """

    async def find_budget(
        self, *, department_id: Any, category_id: Any, year: int
    ) -> BudgetView | None: ...

    async def list_budgets(
        self, *, year: int | None = None
    ) -> Sequence[tuple[BudgetView, str, str]]: ...

    async def snapshot(self, budget_id: Any) -> tuple[int, int, int] | None: ...

    async def list_transactions(
        self, *, budget_id: Any | None = None, limit: int = 50
    ) -> Sequence[tuple[BudgetTransactionView, str]]: ...

    async def reserved_fen(self, request_id: Any) -> int: ...

    async def reserve(
        self, *, budget_id: Any, request_id: Any, amount_fen: int
    ) -> bool: ...

    async def release(
        self, *, budget_id: Any, request_id: Any, amount_fen: int
    ) -> bool: ...
