"""用例层依赖的端口。

只用 `Protocol` 描述"我需要什么能力"，**不 import 任何框架**——
这样 `application/` 就不会依赖 SQLAlchemy，换 ORM 或换存储时这层不用动。

具体实现（`CatalogRepository`、`AsyncSession`）在**结构上**满足这些协议，
所以边界层可以直接把它们传进来，不需要额外的适配器。

为什么主数据只有一个端口：它们本来就是同一组聚合（部门 / 品类 / 供应商 / 预算
都由管理员维护、都从 `CatalogRepository` 存取），拆成四个端口只会让协调者的
构造函数变长，并没有换来实际的可替换性。
"""

from collections.abc import AsyncIterator, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
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

    async def get_supplier(self, supplier_id: Any) -> SupplierView | None: ...

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


@dataclass(frozen=True)
class ModelUsage:
    """一次模型调用的用量（token 数与自报的模型名）。

    ============================ 三个字段都可能为 `None`，这是刻意的 ============================
    流式接口**默认不返回 token 用量** —— OpenAI 协议要显式传
    `stream_options: {"include_usage": true}`，而且不是所有网关 / 本地推理服务都支持。
    拿不到时**留 `None`，不要用字符数估算**：
    估算值一旦混进"真实用量"里，"这个月花了多少"就成了一笔算不清的账。
    **宁可空着，也不要一个看起来像真的假数。**

    为什么单独一个类型而不是三个散参数：将来要加"缓存命中 token"
    （`prompt_tokens_details.cached_tokens`，OpenAI/Anthropic 都有）时，
    只改这一处。
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    #: 模型自报的名字（可能与请求里的不同，例如带版本后缀）
    model: str | None = None


@dataclass(frozen=True)
class ToolCall:
    """模型要求调用一次工具。

    字段与 OpenAI 的 `tool_calls` 一一对应，因为那是**事实标准**：
    `id` 必须在把结果回灌时原样带回（上游用它配对请求与结果）；
    `arguments` 保持**原始 JSON 字符串**而不是解好的对象 ——
    解析失败也是一条必须被处理的分支（模型会给出坏 JSON），
    提前 parse 会把"JSON 坏了"变成调用方的异常，而不是一个可选结果。
    """

    id: str
    name: str
    #: 原始 JSON 字符串（**未解析**）。解析与校验在 `application/tools.py`。
    arguments: str


@dataclass(frozen=True)
class ChatMessage:
    """一轮对话里的消息。只有 `tool` 角色需要 `tool_call_id`。"""

    role: str
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    #: 工具结果消息要带的**工具名**。
    #:
    #: ⚠️ **Ollama 要求 `tool` 消息同时带 `tool_name` 与 `tool_call_id`** ——
    #: 只给 `tool_call_id` 是我的实现里真实存在过的一个 bug
    #: （实测 3B 模型容错没暴露出来，但换模型/换版本就可能开始报错，
    #: 而那种失败看起来像"模型不会用工具"）。
    #: 参考：Ollama 官方 tool calling 文档的 `{"role": "tool", "tool_name": ..., "content": ...}`，
    #: 以及 microsoft/agent-framework 的修复 PR「send the tool name on tool result messages」。
    tool_name: str | None = None


@dataclass(frozen=True)
class TurnResult:
    """一次模型回合的结果：说了一句话，和/或要调工具。"""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()


class ChatModelPort(Protocol):
    """生成回答的模型（M11）。`application` 只认这个协议，不认是哪一家。

    ============================ 实现者**不得**绕过溯源策略 ============================
    调用方（协调者）在调它**之前**已经判定"有依据、可以问模型"，
    在它**之后**还要做引用校验。所以实现只负责"把 Prompt 送出去、把文本流回来"，
    绝不负责决定"该不该答" —— 那条判断只有一处（`domain/grounded_answer`）。

    ============================ 两个方法，用途不同（不是两套实现）============================
        `stream`      纯文本流：制度问答那条快路径用它（要逐字显示）
        `stream_turn` 带工具的回合：Agent 循环用它（要拿到结构化 `tool_calls`）

    为什么不合成一个：文本流与工具调用在**协议层面的形状就不同** ——
    前者是"很多个小片段"，后者是"一个完整对象"。硬合成一个"既可能是文本片段
    也可能是工具调用"的事件流，会让**每个**消费方都要写一遍分派逻辑，
    而分派写错的表现是"工具调了但没人执行"（静默）。

    为什么不只留 `stream_turn`：现有问答路径（流式逐字显示）与它的
    54 条浏览器断言都建立在"纯文本流"上，为了加工具把它全部改写，
    风险远大于收益。两条路在**协调者**那一层汇合，而不是在模型适配器里。
    """

    #: 模型标识（进日志与 `chat_messages`，事后能分辨回答是谁生成的）
    name: str

    #: **这个实现会不会用工具。** 边界层据此决定走不走 Agent 循环。
    #:
    #: 为什么要显式声明而不是"试一下看会不会抛"：
    #: "不支持"与"支持但这次决定不调工具"在行为上完全不同，
    #: 而它们都会表现为"没有 tool_calls"。用异常去区分的话，
    #: 那个异常同时也会被网络错误触发 —— 两种情况要给的提示完全不一样。
    supports_tools: bool

    def stream(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        sources: Sequence[Any],
        on_usage: Callable[[ModelUsage], None] | None = None,
    ) -> AsyncIterator[str]: ...

    def stream_turn(
        self,
        *,
        messages: Sequence[ChatMessage],
        tools: Sequence[dict[str, Any]],
        on_usage: Callable[[ModelUsage], None] | None = None,
    ) -> AsyncIterator[TurnResult]:
        """跑一个回合，返回**一个** `TurnResult`（实现通常 yield 一次）。

        为什么是异步生成器而不是 `async def -> TurnResult`：
        与 `stream` 保持同一种调用形状（`async for`），
        这样协调者里两条路可以共用同一套超时与异常处理，
        不必为"普通协程"与"异步生成器"写两遍。
        """
        ...


class EmbeddingPort(Protocol):
    """把文本变成向量（M11 增强）。

    ============================ 为什么它是**异步**的 ============================
    真 embedding 模型（bge 系）是**网络调用**，所以协议必须是异步的。
    项目自带的哈希实现是纯函数、瞬间返回，但它也满足这个协议
    （异步函数里直接 return 即可）—— 这样"换回退路"不需要改调用方一行。

    ============================ `name` 的语义极其重要 ============================
    它写进 `knowledge_docs.embedding_model`，而检索会**按它过滤**：
    同一张表里混着两种模型算的向量，余弦距离**没有意义** ——
    检索结果会静默变差、且极难排查（两个向量空间不可比）。
    所以 `name` 必须**唯一标识一个向量空间**：
    换模型、换维度、换归一化方式 → 都要换一个新名字。
    名字里带上维度（如 `local-hash-512`）是最省事的做法。

    ⚠️ **不做静默降级**：模型不可用时应当抛错（由协调者翻成明确的失败），
    而不是偷偷用哈希顶上 —— 那会让"检索质量突然变差"变成一个查不出来的问题，
    而且两种模型的向量混在同一列里，等于**污染**了向量空间。
    """

    #: 向量空间的标识（进 `knowledge_docs.embedding_model`，检索时按它过滤）
    name: str

    #: 这个模型输出多少维。**必须与 `knowledge_chunks.embedding` 的列定义一致**
    #: （见 `domain/embedding.py` 的维度注册表）。
    dim: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量算向量（顺序与入参一一对应）。

        批量而不是逐条：入库时一份文档有几十个片段，
        逐条发 HTTP 会把"上传一份制度"变成几十次往返。
        """
        ...


class QuotationView(Protocol):
    """供应商报价（M10 / 8.1）。"""

    id: Any
    request_id: Any
    supplier_id: Any
    amount: int
    currency: str
    status: str
    valid_until: datetime | None
    attachment_id: Any
    created_by: Any
    created_at: datetime


class QuotationRepositoryPort(Protocol):
    """比价与定标的数据访问。

    `mark_awarded` 与 `mark_not_selected` 是**条件更新**：
    定标这件事必须靠"受影响行数"判胜负，不能先查再写
    （两个人同时定标会各写一个 SELECTED，而主单只能有一个成交供应商）。
    """

    async def list_for_request(self, request_id: Any) -> Sequence[QuotationView]: ...

    async def get(self, quotation_id: Any, *, populate_existing: bool = False) -> QuotationView | None: ...

    async def selected_of(self, request_id: Any) -> QuotationView | None: ...

    async def create(
        self,
        *,
        request_id: Any,
        supplier_id: Any,
        amount_fen: int,
        valid_until: datetime | None,
        attachment_id: Any,
        created_by: Any,
    ) -> QuotationView: ...

    async def expire_overdue(self, request_id: Any) -> int: ...

    async def mark_awarded(self, quotation_id: Any) -> bool: ...

    async def mark_not_selected(self, request_id: Any, *, keep_id: Any) -> int: ...


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

    async def get_request_by_code(
        self, code: str, *, scope: RequestScope, actor_id: Any
    ) -> PurchaseRequestView | None:
        """按**单据编号**取（Agent 工具用；人说的是单号而不是 UUID）。

        协议里必须有它：`mypy` 就是靠这一条发现"用例层调了一个端口没声明的方法"
        （实测报 `"PurchaseRepositoryPort" has no attribute "get_request_by_code"`）。
        **没有端口声明的实现方法等于偷偷加宽了接口** —— 换一个实现时才发现。
        """
        ...

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
        scope: RequestScope,
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
