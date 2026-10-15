"""Agent 工具的声明与执行（M12 增强）。

============================ 这一层最要紧的一句话 ============================
**工具不新开权限口径，它以调用者的身份走同一批用例。**

具体做法：本模块只从 `Principal`（HTTP 请求里那个已登录用户）出发，
调 `PurchaseCoordinator` / `ApprovalCoordinator` **与 HTTP 接口完全相同的入口**。
于是"能看什么、能改什么"的判定**只有一处**（`resolve_request_scope`），
工具与接口不可能不一致。

这条设计带来的性质：**即使模型被提示注入完全控制，它也拿不到调用者本来没有的数据。**
不是靠提示词里写"不要越权"，而是靠**它调用时用的身份本来就没有那些权限**。
这是本项目在 Agent 安全上最有价值的一点，也是绝大多数 demo 缺的那一层。

============================ 写工具为什么必须"两段式" ============================
`create_purchase_draft` 有副作用，所以它**不能**在一次模型回合里直接落地：

    ① 模型说"我要建这张草稿" → 系统**只校验、不执行**，返回一个待确认令牌；
    ② 人确认之后，用那个令牌调**同一个用例**（`create_draft`）才会真的落库。

为什么不让模型直接建草稿：草稿也不是无害的 —— 它会占一个单号、出现在列表里、
可能被同事当成真需求去审批。而"模型自作主张建了十几张草稿"这件事，
一旦发生就只能靠人去一张张删。

============================ 返回给模型的数据要"够用且不泄密" ============================
工具的返回值会**整段进模型的上下文**，所以要克制：

    · 只给模型回答问题需要的字段（单号、标题、金额、状态、时间）；
    · **不给**内部 UUID、数据库 id、无关的 form_data —— 它们既没用又扩大泄露面；
    · 金额同时给"分"与"元"的展示串：模型不擅长做单位换算，
      只给分它会把 `120000` 说成"12 万元"（错 10 倍，实测过这类错误极常见）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.application.coordinators.approval import ApprovalCoordinator
from app.application.coordinators.auth import Principal
from app.application.coordinators.purchase import PurchaseCoordinator
from app.domain.tooling import ArgumentProblem, ToolSpec, check_arguments

#: 一次工具调用最多回多少条 —— **硬上界，不给模型调大的机会**。
#: 20 条已经能回答"我最近提了什么"，而它同时限制了"把整张表拖进上下文"这条路。
MAX_TOOL_ROWS = 20


@dataclass(frozen=True)
class ToolOutcome:
    """工具执行结果。**给模型看的是 `payload`，给审计看的是 `ok` 与 `error`。**

    `ok=False` 时 `payload` 仍要给模型一段**能读懂的说明**（例如"没有找到这张单子"
    或"参数不对，本工具接受这些字段"）—— 让模型有机会改一次重试，
    而不是直接把它变成一次对用户报错。这是 ReAct 循环能自纠的前提。
    """

    ok: bool
    payload: dict[str, Any]
    #: 给审计/日志的一句话（不进模型上下文）
    summary: str = ""
    #: 如果这个工具是写操作且**还没有真正执行**（等人工确认），这里带上令牌
    confirmation_token: str | None = None


def tool_specs() -> list[ToolSpec]:
    """本系统暴露给模型的工具。**每次新建列表**，避免被就地修改。

    刻意只有这几个：工具越多，模型选错的概率越高，而每个工具都要
    有人维护它的权限与上界。**先少而后对，比先多而后乱好。**
    """
    return [
        ToolSpec(
            name="list_my_purchase_requests",
            description=(
                "列出**当前用户可见的**采购申请（最新在前）。"
                "用于回答『我提过哪些申请』『最近有什么单子』『有没有待审批的』。"
                "只能看到你自己权限范围内的单子。"
            ),
            schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "按状态过滤，不传表示全部",
                        "enum": [
                            "DRAFT",
                            "IN_APPROVAL",
                            "APPROVED",
                            "REJECTED",
                            "WITHDRAWN",
                            "CLOSED",
                        ],
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"返回条数，1–{MAX_TOOL_ROWS}",
                        "minimum": 1,
                        "maximum": MAX_TOOL_ROWS,
                    },
                },
            },
        ),
        ToolSpec(
            name="get_purchase_request",
            description=(
                "按**单号**（如 REQ-2026-0001）查一张采购申请的详情、"
                "明细与审批时间线。单号可以从『列出采购申请』的结果里拿到。"
                "只能查你有权限查看的单子。"
            ),
            schema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "采购申请单号，形如 REQ-2026-0001",
                        "minLength": 4,
                        "maxLength": 32,
                    }
                },
                "required": ["code"],
            },
        ),
        ToolSpec(
            name="list_my_approval_todos",
            description=(
                "列出**现在轮到我审批**的任务。用于回答『我有什么要审的』"
                "『有没有待办』。只列当前这一级、只列你部门范围内的。"
            ),
            schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": f"返回条数，1–{MAX_TOOL_ROWS}",
                        "minimum": 1,
                        "maximum": MAX_TOOL_ROWS,
                    }
                },
            },
        ),
        ToolSpec(
            name="create_purchase_draft",
            description=(
                "为当前用户**创建一张采购申请草稿**（只建草稿，不会提交审批）。"
                "⚠️ 这是写操作：调用后**不会立即生效**，"
                "需要用户在前端确认之后才真正创建。"
                "商品明细需要品类 ID，通常先用『列出采购申请』确认现有数据，"
                "或让用户在前端补全明细。"
            ),
            schema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "申请标题（事由），例如『采购办公用笔记本电脑 5 台』",
                        "minLength": 2,
                        "maxLength": 200,
                    },
                    "category_id": {
                        "type": "string",
                        "description": "采购品类的 UUID",
                        "minLength": 8,
                        "maxLength": 64,
                    },
                    "reason": {
                        "type": "string",
                        "description": "补充说明（可选）",
                        "maxLength": 500,
                    },
                },
                "required": ["title", "category_id"],
            },
            mutating=True,
        ),
    ]


async def run_tool(
    *,
    name: str,
    arguments: Any,
    specs: list[ToolSpec],
    actor: Principal,
    purchase: PurchaseCoordinator,
    approval: ApprovalCoordinator,
) -> ToolOutcome:
    """执行一次工具调用。

    ============================ 四道关口，顺序不能换 ============================
        ① **工具名必须已登记**（未知即拒绝，并回报可用工具名）；
        ② **参数必须过 schema 校验**（未知字段一律拒绝，见 `domain/tooling.py`）；
        ③ **身份从 `actor` 来**，不从参数来 —— 参数里**没有**任何"以谁的身份"的字段；
        ④ **执行走用例层**（权限与事务边界都在那里），本函数不碰数据库。

    第 ③ 条是硬要求：一旦参数里能指定 actor，模型就能"以别人的身份"调工具。
    所以这里连"可选的身份参数"都不留。
    """
    spec = next((item for item in specs if item.name == name), None)
    if spec is None:
        available = [item.name for item in specs]
        return ToolOutcome(
            ok=False,
            payload={
                "error": f"没有名为 {name!r} 的工具。",
                "available_tools": available,
            },
            summary=f"未知工具 {name}",
        )

    cleaned, problems = check_arguments(spec, arguments)
    if problems:
        return ToolOutcome(
            ok=False,
            payload={
                "error": "参数不合法，请修正后重试。",
                "problems": [f"{p.path}: {p.detail}" for p in problems],
            },
            summary=f"参数校验失败：{_problem_brief(problems)}",
        )

    if spec.mutating:
        # 写操作**在这里就停住**：只做"可执行性预检"，不落库。
        # 真正的执行由边界层在人工确认之后调 `execute_confirmed_write`。
        return await _prepare_write(
            spec=spec, cleaned=cleaned, actor=actor, purchase=purchase
        )

    return await _run_read(spec=spec, cleaned=cleaned, actor=actor, purchase=purchase, approval=approval)


def _problem_brief(problems: list[ArgumentProblem]) -> str:
    return "；".join(f"{p.path}({p.detail})" for p in problems)


async def _run_read(
    *,
    spec: ToolSpec,
    cleaned: dict[str, Any],
    actor: Principal,
    purchase: PurchaseCoordinator,
    approval: ApprovalCoordinator,
) -> ToolOutcome:
    """只读工具：直接执行，返回给模型看的数据。"""
    if spec.name == "list_my_purchase_requests":
        limit = int(cleaned.get("limit", 10))
        # 变量名与下面 `list_todo` 的分开：同名会让 mypy 复用一个错误的推断类型
        # （实测报 `Sequence[PurchaseRequestView]` 与任务元组不兼容）
        request_rows, total = await purchase.list_requests(
            actor_id=actor.id,
            roles=actor.roles,
            department_id=actor.department_id,
            status=cleaned.get("status"),
            limit=limit,
        )
        return ToolOutcome(
            ok=True,
            payload={
                "total_visible": total,
                "returned": len(request_rows),
                "requests": [_request_brief(row) for row in request_rows],
            },
            summary=f"列出申请 {len(request_rows)}/{total} 条",
        )

    if spec.name == "get_purchase_request":
        code = str(cleaned["code"]).strip()
        record, item_rows, timeline = await purchase.get_request_detail_by_code(
            code=code,
            actor_id=actor.id,
            roles=actor.roles,
            department_id=actor.department_id,
        )
        return ToolOutcome(
            ok=True,
            payload={
                **_request_brief(record),
                "items": [
                    {
                        "name": item.name,
                        "quantity": item.quantity,
                        "unit_price": _yuan(item.unit_price),
                        "amount": _yuan(item.amount),
                    }
                    for item in item_rows
                ],
                "timeline": [
                    {
                        "event": event.event_type,
                        "to_status": event.to_status,
                        "at": _stamp(event.created_at),
                    }
                    for event in timeline
                ],
            },
            summary=f"查到 {code}，明细 {len(item_rows)} 条、事件 {len(timeline)} 条",
        )

    if spec.name == "list_my_approval_todos":
        limit = int(cleaned.get("limit", 10))
        rows, total = await approval.list_todo(
            actor_id=actor.id,
            roles=actor.roles,
            department_id=actor.department_id,
            limit=limit,
        )
        # 显式标注解包类型：`ApprovalTaskView` / 申请视图的协议里有两个 `Any`
        # 占位（历史原因），mypy 于是在解包处报 `has-type`。在这里标一次，
        # 比去改那些协议更小、也更能说明"这一行期望的是什么形状"。
        typed_rows: list[tuple[Any, Any, Any]] = list(rows)
        return ToolOutcome(
            ok=True,
            payload={
                "total_visible": total,
                "returned": len(typed_rows),
                "todos": [
                    {
                        "request_code": request.code,
                        "title": request.title,
                        "amount": _yuan(request.amount_total),
                        "step": task.step_order,
                        "status": task.status,
                    }
                    for task, request, _flow in typed_rows
                ],
            },
            summary=f"列出待办 {len(typed_rows)}/{total} 条",
        )

    # 走到这里说明 `tool_specs()` 加了工具但 `_run_read` 没实现分支。
    # **不能静默返回空**：那会表现成"工具调了但没结果"，极难定位。
    raise NotImplementedError(f"只读工具 {spec.name} 没有实现分支")


async def _prepare_write(
    *,
    spec: ToolSpec,
    cleaned: dict[str, Any],
    actor: Principal,
    purchase: PurchaseCoordinator,
) -> ToolOutcome:
    """写工具的**第一段**：只校验、不执行，返回待确认描述。

    这里做的预检与真正执行时**用的是同一个用例**能给出的判断（部门、品类存在性），
    只是不写库。这样"模型说要建"与"人确认后真建"之间的失败面很小。
    """
    if spec.name != "create_purchase_draft":
        raise NotImplementedError(f"写工具 {spec.name} 没有实现分支")

    if actor.department_id is None:
        # 与 `create_draft` 里同一句判断。在这里**先**拦住的意义：
        # 让模型当场就知道"这个用户建不了"，而不是等他确认之后才失败。
        return ToolOutcome(
            ok=False,
            payload={
                "error": "当前账号没有所属部门，无法创建采购申请。",
                "hint": "请让用户在系统里补充部门，或改用其他账号。",
            },
            summary="无部门，建草稿预检失败",
        )

    category_id = _parse_uuid(str(cleaned["category_id"]))
    if category_id is None:
        return ToolOutcome(
            ok=False,
            payload={
                "error": "category_id 不是合法的 UUID。",
                "hint": "品类 ID 需要是 UUID 形式；请让用户在前端选择品类。",
            },
            summary="品类 id 不是 UUID",
        )

    draft = {
        "title": str(cleaned["title"]).strip(),
        "category_id": str(category_id),
        "reason": (str(cleaned.get("reason")) if cleaned.get("reason") else None),
    }
    token = _confirmation_token(actor=actor, draft=draft)
    return ToolOutcome(
        ok=True,
        payload={
            "status": "PENDING_CONFIRMATION",
            "action": "create_purchase_draft",
            "draft": draft,
            "confirmation_token": token,
            "message": (
                "已准备好创建草稿，但**还没有创建**。"
                "请让用户在前端确认；确认后才会真正建单。"
            ),
        },
        summary=f"待确认：建草稿「{draft['title']}」",
        confirmation_token=token,
    )


def agent_used_tools(steps: list[dict[str, Any]] | None) -> bool:
    """这批 Agent 轨迹里**有没有真的调过工具**。

    ============================ 为什么不能只看"有没有轨迹" ============================
    调用方（`_finish`）用它决定"**要不要豁免「有断言必须带引用」这条引用规则**"：
    查业务数据的回答本来就没有制度引用，套那条规则会得到假问题 + 误导性免责声明。

    但"**走了 Agent 路径**"与"**真的用了工具**"是两件事，实测踩到过：

        `agent_steps` 在走 Agent 路径时**必然非空**（至少有一条 `answer` 步）→
        用 `bool(agent_steps)` 当判据会**恒为真** → 一个"没调任何工具、
        直接答了制度问题"的回答也被豁免 → 它"通篇断言却零引用"**没被标出来**。

    所以判据只认 `tool_call` / `tool_result`：那才代表"这次回答的依据来自工具调用"。

    抽成纯函数是为了**能离线单测**：上面那个恒真条件，
    留在协调者里就只能靠"检索命中 + 模型不调工具"这种很难凑齐的场景才测得到
    （实测就是这样漏掉的 —— 集成测试里凑不出那个组合）。
    """
    return any(
        isinstance(step, dict) and step.get("kind") in {"tool_call", "tool_result"}
        for step in (steps or [])
    )


def _confirmation_token(*, actor: Principal, draft: dict[str, Any]) -> str:
    """给人确认用的令牌。**它绑定"谁 + 要做什么"**。

    ⚠️ **它不是安全边界，而且它本身是可以被推算的**（见 `_CONFIRM_NAMESPACE` 的说明）：
    令牌是 `uuid5(actor + 标题 + 品类 + 备注)`，所以任何知道这四个值的人都能算出它。

    因此确认接口**不能只验令牌** —— 它必须同时要求客户端把
    **待确认的内容原样回传**，然后逐字比对（见 `verify_confirmation`）。
    这样"确认"确认的是**这一份提案**，而不是"某个能算出来的字符串"。
    真正的权限边界仍然是"确认之后用**确认者的身份**再走一遍 `create_draft`"。

    用 `uuid5`（确定性）而不是随机串的好处：同一人同一草稿得到同一个令牌，
    于是它天然适合当**幂等键** —— 重复点确认不会造出两张单。
    """
    seed = f"{actor.id}:{draft['title']}:{draft['category_id']}:{draft['reason'] or ''}"
    return str(uuid.uuid5(_CONFIRM_NAMESPACE, seed))


#: `uuid5` 的命名空间（固定值，别改 —— 改了历史令牌就对不上了）
_CONFIRM_NAMESPACE = uuid.UUID("6f1d4b2a-9c3e-4a7f-8d51-2b6e0c9a7f13")


class ConfirmationError(ValueError):
    """确认被拒绝。`reason` 是给用户看的一句话。"""

    def __init__(self, reason: str, *, code: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


def verify_confirmation(
    *,
    token: str,
    draft: dict[str, Any],
    actor: Principal,
) -> dict[str, Any]:
    """校验"确认"这一步。通过就返回**清洗后的草稿**，否则抛 `ConfirmationError`。

    ============================ 四道校验，缺一不可 ============================
    ① **令牌必须与"这份内容 + 这个身份"匹配**：不匹配说明客户端在确认
       另一份东西（或令牌是伪造/过期的）。这是"确认的是提案本身"那句话的落点；
    ② **身份必须一致**：令牌里含 actor，所以"别人拿到的令牌"在这里直接被拒 ——
       Agent 的提案只能由它服务的那个用户确认；
    ③ **字段白名单**：只接受 `title` / `category_id` / `reason`。
       多出来的字段一律拒绝（与工具参数校验同一条纪律：未知即拒绝，
       否则 `**kwargs` 会把它们带进下游用例）；
    ④ **标题与品类必须非空**：这里是"人确认后"的最后一道关口，
       它应该比模型给的输入更严格，而不是更松。

    ⚠️ **它不做业务校验**（品类是否存在、用户有没有部门）——
    那些由 `create_draft` 用例本身负责。重复实现一遍会让两处规则漂移，
    而漂移的方向是"确认时放行了一个用例会拒绝的东西"。
    """
    if not isinstance(draft, dict):
        raise ConfirmationError("确认内容必须是一个对象", code="CONFIRM_BAD_PAYLOAD")

    allowed = {"title", "category_id", "reason"}
    unknown = set(draft) - allowed
    if unknown:
        raise ConfirmationError(
            f"确认内容里有未知字段 {sorted(unknown)}；只接受 {sorted(allowed)}",
            code="CONFIRM_UNKNOWN_FIELD",
        )

    title = str(draft.get("title") or "").strip()
    category_raw = str(draft.get("category_id") or "").strip()
    reason_raw = draft.get("reason")
    reason = str(reason_raw).strip() if reason_raw else None

    if not title:
        raise ConfirmationError("标题不能为空", code="CONFIRM_TITLE_EMPTY")
    category_id = _parse_uuid(category_raw)
    if category_id is None:
        raise ConfirmationError("category_id 不是合法的 UUID", code="CONFIRM_BAD_CATEGORY")

    cleaned = {"title": title, "category_id": str(category_id), "reason": reason}

    # ① + ②：用**同一份内容**重算令牌，与客户端给的那个比对。
    # 逐字比对（含 reason 的 None/空串归一）是关键 —— 内容改一个字令牌就变。
    expected = _confirmation_token(actor=actor, draft=cleaned)
    if expected != token:
        raise ConfirmationError(
            "确认令牌与当前内容不匹配（内容被改过，或令牌不属于当前用户）。"
            "请重新发起创建。",
            code="CONFIRM_TOKEN_MISMATCH",
        )
    return cleaned


def _parse_uuid(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        return None


def _yuan(fen: int) -> str:
    """分 → 元的展示串。

    **必须同时给模型一个人类可读的金额**：整数分（`120000`）直接进上下文时，
    模型很容易说成"12 万元"（错 10 倍）。这类算术错误在问答里非常常见，
    而它比"少说一个字段"严重得多 —— 用户会照着办。
    """
    sign = "-" if fen < 0 else ""
    cents = abs(fen)
    return f"{sign}{cents // 100}.{cents % 100:02d} 元"


def _stamp(value: Any) -> str:
    return value.isoformat() if value is not None else ""


def _request_brief(record: Any) -> dict[str, Any]:
    """一条申请给模型看的精简形状（**不含内部 UUID**）。

    刻意不给 `id` / `department_id` / `category_id`（除了写工具需要的那个入口）：
    模型用不到它们，而它们进了上下文就等于扩大了泄露面。
    模型要引用这张单子时用 `code` —— 那也是人用的标识。
    """
    return {
        "code": record.code,
        "title": record.title,
        "status": record.status,
        "amount": _yuan(record.amount_total),
        "amount_fen": record.amount_total,
        "urgency": record.urgency,
        "created_at": _stamp(record.created_at),
        "submitted_at": _stamp(record.submitted_at),
    }
