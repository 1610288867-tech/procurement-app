"""知识助手接口（M11）。

五条路由：

    POST /api/v1/chat                       问制度（非流式，给 curl / 测试用）
    POST /api/v1/chat/stream                问制度（SSE 流式，界面用）
    GET  /api/v1/chat/sessions              我的会话列表
    GET  /api/v1/chat/sessions/{id}/messages 某个会话的消息
    POST /api/v1/admin/knowledge-docs       上传制度文档（管理员）
    GET  /api/v1/admin/knowledge-docs       文档列表（管理员）
    GET  /api/v1/admin/knowledge-docs/{id}/chunks  切分结果（管理员）

============================ SSE 的四条纪律（协议是前端契约） ============================
    event: meta     {"session_id", "sources", "used_model", "reason", "model"}
    event: delta    {"text": "..."}
    event: done     {"message_id", "citation_problems"}
    event: error    {"message": "..."}

1. **`meta` 必须先发**（计划里专门点了）：用户能在正文还没出来时先看到引用来源；
2. **`delta` 可能很多条**，前端要按到达顺序追加（逐字出现的验收项靠它）；
3. **出错发 `error` 而不是直接断开** —— 静默截断会让用户以为回答就这么多；
4. **`text/event-stream` + `no-cache` + `X-Accel-Buffering: no`**：
   最后一条是给反向代理看的 —— 不写它，nginx 之类的会**攒够一批才往下发**，
   表现为"流式看起来不流"（本地 Vite 代理不缓冲，所以这个问题在开发期看不出来，
   部署后才出现，很难查）。

============================ 为什么问答不要求 `Idempotency-Key` ============================
它与"提交申请""付款"不是一类：问两次得到两条消息是**正确的**语义
（用户在追问），而不是重复提交。所以这里**刻意不要**幂等键 ——
给它加一个反而是错的（会把"再问一次"变成"返回上次的答案"）。

============================ 角色口径 ============================
问答：**登录即可**（制度是给所有人看的）；会话**只列自己的**。
知识库管理：**仅管理员**（改知识库等于改"制度依据"，而依据被改过之后
所有回答的可信度都会变）。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.application.coordinators.knowledge import (
    ALLOWED_CONTENT_TYPES,
    KnowledgeCoordinator,
    build_knowledge_coordinator,
)
from app.application.ports import EmbeddingPort
from app.application.tools import ConfirmationError, verify_confirmation
from app.boundaries.deps import CurrentUser, SessionDep, require_roles
from app.boundaries.http.approvals import approval_coordinator
from app.boundaries.http.requests import purchase_coordinator
from app.config import get_settings
from app.domain.permissions import ROLE_ADMIN
from app.errors import BusinessError, ChatQuotaExceededError, NotFoundError
from app.infrastructure.chat_models import LocalExtractiveChat, OpenAiCompatibleChat
from app.infrastructure.embeddings import LocalHashEmbedding, OllamaEmbedding
from app.infrastructure.object_store import LocalObjectStore
from app.infrastructure.token_quota import estimate_tokens, token_quota

router = APIRouter(tags=["制度助手"])


def _chat_model() -> Any:
    """选生成回答的实现：配了密钥就用远端，否则本地兜底。

    ============================ 为什么默认是本地兜底 ============================
    这个项目默认跑在本地、没有 API 密钥。只有远端一条路的话：
    没密钥时整个助手不可用（连"检索到了什么"都演示不了）；
    配了密钥时每次自检都可能花钱。所以"没有密钥"是**一等公民**，
    不是错误分支 —— 它换来的是一条永远可用的链路。
    """
    settings = get_settings()
    if (
        settings.knowledge_llm_base_url
        and settings.knowledge_llm_api_key
        and settings.knowledge_llm_model
    ):
        return OpenAiCompatibleChat(
            base_url=settings.knowledge_llm_base_url,
            api_key=settings.knowledge_llm_api_key,
            model=settings.knowledge_llm_model,
        )
    return LocalExtractiveChat()


def _embedder() -> EmbeddingPort:
    """按配置选 embedding 实现。

    ============================ 这里**没有**"失败了退回哈希"的分支（刻意的）============================
    "模型不可用就用哈希顶上"看起来很稳，实际是错的：

        ① 哈希与真模型的向量**不可比**，混进同一列 → 检索静默变差；
        ② 检索按 `knowledge_docs.embedding_model` **过滤**，
           退回哈希之后真模型建的文档会**一条都检索不到**。

    所以配置说用哪个就用哪个，模型坏了让错误**显式地冒出来**
    （`EmbeddingError` → 503 + 原因），而不是把质量偷偷降下去。

    `provider` 写错时也**抛错**，不猜：写错的配置静默退化成哈希，
    表现是"检索质量莫名其妙很差"，而没人会想到是配置拼错了。
    """
    settings = get_settings()
    provider = (settings.knowledge_embedding_provider or "").strip().lower()
    if provider == "local":
        return LocalHashEmbedding()
    if provider == "ollama":
        # 用**embedding 自己的地址**（默认就指向本机 Ollama），
        # 不借用对话模型的地址 —— 对话可以用云端而 embedding 用本地，反之亦然。
        # 这里**不再做"没配地址就报错"的校验**：默认值一定存在，
        # 而"地址写错了"这件事由调用时更具体的连接错误来报（带模型名与 URL）。
        return OllamaEmbedding(
            base_url=settings.knowledge_embedding_base_url,
            model=settings.knowledge_embedding_model,
        )
    raise BusinessError(
        f"未知的 embedding 提供方：{provider!r}（可用值：local / ollama）",
        status_code=500,
        code="KNOWLEDGE_EMBEDDING_CONFIG",
    )


def knowledge_coordinator(session: SessionDep) -> KnowledgeCoordinator:
    """组装知识库用例的依赖（装配本身在 `coordinators/knowledge`）。"""
    settings = get_settings()
    return build_knowledge_coordinator(
        session,
        # 与附件共用同一个对象存储实现与同一个根目录：制度原文与附件是同一类
        # "服务端生成文件名"的东西，没必要为它再写一份
        object_store=LocalObjectStore(Path(settings.upload_dir)),
        chat_model=_chat_model(),
        embedder=_embedder(),
        top_k=settings.knowledge_top_k,
        min_score=settings.knowledge_min_score,
        # 价格表：用于成本核算。**配了本地模型也要写一条 `0,0`** ——
        # "确实不花钱"与"不知道花多少"在数据里必须区分（见 `domain/cost.py`）。
        price_config=settings.knowledge_llm_prices,
        # Agent 工具要用的两个协调者：**复用 requests / approvals 两个边界层里
        # 那份装配函数**，而不是在这里重写一遍 —— 重写就会出现"工具走的事务边界
        # 与接口不一样"这种事，而它不会报错、只会让副作用不一致。
        purchase=purchase_coordinator(session),
        approval=approval_coordinator(session),
    )


# ------------------------------------------------------------------ 模型


class SourceOut(BaseModel):
    number: int
    doc_id: str
    doc_title: str
    doc_version: int
    chunk_no: int
    #: 片段主键。**给评测与排障用**：检索层指标要判"期望命中的那一片有没有被检索到"，
    #: 而那个判据必须落在与切分无关的标识上（用 chunk_no 判的话，改一次切分就失效）。
    chunk_id: str = ""
    content: str
    score: float


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    #: 不传就新建一个会话
    session_id: uuid.UUID | None = None


class SupportProblemOut(BaseModel):
    """一条"引用内容不支撑"的问题。

    与 `citation_problems`（字符串列表）**分开**，因为两者的**处置不同**：
    编号造假是"模型违规"（可以重试或换 prompt），
    内容不支撑更可能是"来源本身不够清楚"（要改切分或补文档）。
    混在一个数组里就再也统计不出各自的比例。
    """

    kind: str
    detail: str
    #: 这句话在回答里的序号（`None` = 不针对具体某句）
    sentence_index: int | None = None


class AskOut(BaseModel):
    message_id: str
    session_id: str
    answer: str
    sources: list[SourceOut]
    #: **第一层**引用校验发现的问题（编号完整性）。**不阻断，但要说出来**
    citation_problems: list[str]
    #: **第二层**引用校验发现的问题（内容支撑性）。见 `domain/citation_support.py`
    support_problems: list[SupportProblemOut] = []
    #: **Faithfulness 的规则法近似**。⚠️ `null` = 这次回答不适用该指标（没有引用句），
    #: 与 `0.0`（有引用的句子全都不可信）**不是一回事**，汇总均值时必须分开。
    support_rate: float | None = None
    used_model: bool
    #: 为什么没调用模型（验收项：日志/界面里能看到"检索为空，未调用模型"）
    reason: str
    top_score: float | None
    #: ============================ 延迟与成本（可观测）============================
    #: 两段延迟分开给，因为要回答不同的问题：`retrieval_ms` 大 → 检索的问题；
    #: `model_ms` 大 → 模型或网络的问题。合起来只给一个"总耗时"就分不清该改哪边。
    #: ⚠️ token 与成本**可能是 `null`**（流式接口默认不报用量）——
    #: `null` 是"不知道"，`0` 是"确实是 0"（本地模型），两者不能当同一个意思用。
    retrieval_ms: int | None = None
    model_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    estimated_cost_micros: int | None = None
    #: ============================ Agent 轨迹（M12 增强）============================
    #: 只在走 Agent 时有值。`agent_stop_reason` 是"这次为什么停了"的唯一线索 ——
    #: 取值 `answered` / `max_rounds` / `timeout` / `too_many_failures`。
    #: **必须给前端**：这四种情况的用户体感都是"没答好"，
    #: 不给原因就没法归因（是超时？还是工具一直失败？）。
    agent_rounds: int | None = None
    agent_stop_reason: str | None = None


class SessionOut(BaseModel):
    id: str
    title: str
    created_at: str


class MessageOut(BaseModel):
    id: str
    role: str
    content: str
    seq: int
    sources: list[dict[str, Any]] | None
    citation_problems: list[str] | None
    support_details: list[SupportProblemOut] | None = None
    support_rate: float | None = None
    used_model: bool
    top_score: float | None
    #: 延迟与成本随历史一起回 —— 排障时就不必去查库。
    #: 与 `AskOut` 同一条口径：`None` = 不知道，`0` = 确实是 0。
    retrieval_ms: int | None = None
    model_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    estimated_cost_micros: int | None = None
    created_at: str


class DocOut(BaseModel):
    id: str
    source_key: str
    title: str
    version: int
    effective_from: str
    filename: str | None
    status: str
    embedding_model: str
    chunk_count: int
    content_chars: int
    failure_reason: str | None
    created_at: str


class ChunkOut(BaseModel):
    chunk_no: int
    content: str
    start_offset: int
    end_offset: int
    length: int


# ------------------------------------------------------------------ 问答


def _quota_error(reason: str) -> ChatQuotaExceededError:
    """把配额拒绝翻成 429。

    用 429 而不是 403：403 是"你没权限"，而这里是"你问得太快 / 用超了配额" ——
    重试有意义，所以状态码必须让客户端知道"可以再试"。
    """
    return ChatQuotaExceededError(reason)


def _source_content(source: Any) -> str:
    """从一条来源里取正文。**两种形状都要认**。

    ============================ 这里踩过一个"不报错只算错"的坑 ============================
    第一版写的是 `getattr(source, "content", "")` —— 那假设来源是 `Source` 对象。
    但 HTTP 路径拿到的 `body["sources"]` 是**已经序列化过的字典**：

        `getattr({"content": "..."}, "content", "")` → `""`   ← 不报错，静默取空

    后果：**每条来源都算成 0 token**，配额只按"问题 + 回答"记账。
    而上下文的主体恰恰是来源（5 条 × 600 字 ≈ 3000 token）——
    于是配额上限形同虚设，一条正常提问只被记十几个 token。

    这就是 AGENTS 里那类"不会报错、只会算错"的地方，只能靠**实际取值对账**发现
    （我对着 `手工估算 vs 已记` 两个数才看出来）。
    """
    if isinstance(source, dict):
        return str(source.get("content") or "")
    return str(getattr(source, "content", ""))


def _estimate_tokens_for_quota(*, question: str, sources: list[Any], answer: str) -> int:
    """这次问答大概吃掉多少 token（**配额记账用，不是真实用量**）。

    ============================ 为什么要算上来源 ============================
    上下文的主体是**检索到的制度片段**（检索 5 条 × 600 字 ≈ 3000 token），
    而不是用户那十几个字的问题。只按问题估算的话配额形同虚设 ——
    一次提问实际吃掉几千 token，账上只记十几，上限 20 万会被刷成几万次。

    ⚠️ 这个数**只用于配额判断**：`chat_messages.prompt_tokens` 里存的必须是
    模型返回的真实用量（拿不到就留 `NULL`），两者绝不能混
    （见 `domain/cost.py` 的口径说明）。
    """
    return estimate_tokens(question + answer) + sum(
        estimate_tokens(_source_content(source)) for source in sources
    )


@router.post("/api/v1/chat", response_model=AskOut, summary="问制度（非流式）")
async def ask(
    payload: AskIn, user: CurrentUser, session: SessionDep
) -> AskOut:
    """问一句制度问题。

    **查不到就明说查不到**：检索为空、或全部低于相似度阈值时，
    这句话由后端直接给出（`used_model=false`），不会去问模型 ——
    决定权在 `domain/grounded_answer.decide_grounding`，只有那一处。

    **提问要过 token 配额**（见 `infrastructure/token_quota.py`）：
    AI 接口的滥用成本在 token 上而不在请求数上，所以按 token 限额。
    """
    now = time.time()
    reason = token_quota.check(str(user.id), now=now)
    if reason is not None:
        raise _quota_error(reason)

    coordinator = knowledge_coordinator(session)
    body = await coordinator.ask(
        user_id=user.id,
        question=payload.question,
        session_id=payload.session_id,
        # 把已登录用户交下去 —— 它是**工具调用的权限来源**（见 `application/tools.py`）
        actor=user,
    )
    _charge_quota(user, body, question=payload.question, now=now)
    return AskOut.model_validate(body)


def _charge_quota(
    user: CurrentUser, body: dict[str, Any], *, question: str, now: float
) -> None:
    """按这次问答记一笔 token 用量。

    ============================ 拿不到真实用量时**必须**按估算记账 ============================
    流式接口可能不返回 `usage`。此时若记 0，"上游不返回 usage"就成了一条
    **绕过配额的免费通道** —— 而那恰恰是最该被限制的路径（用量无法核对）。
    所以这里：**有真实用量就用真实的，没有就用估算上界**。
    """
    prompt_tokens = body.get("prompt_tokens")
    completion_tokens = body.get("completion_tokens")
    if prompt_tokens is not None and completion_tokens is not None:
        charged = int(prompt_tokens) + int(completion_tokens)
    else:
        charged = _estimate_tokens_for_quota(
            question=question,
            sources=body.get("sources") or [],
            answer=str(body.get("answer") or ""),
        )
    token_quota.record(str(user.id), tokens=charged, now=now)


@router.post("/api/v1/chat/stream", summary="问制度（SSE 流式）")
async def ask_stream(
    payload: AskIn, user: CurrentUser, session: SessionDep
) -> StreamingResponse:
    """流式问答。

    事件顺序固定为 `meta` → `delta`… → `done`（出错时以 `error` 结束）。
    前端按这个顺序就能做到"正文还没出来先渲染引用卡片"。

    **也要过 token 配额**（与非流式同一个入口）—— 只有非流式限流的话，
    前端走的就是流式那条路，等于没有限流。
    """
    now = time.time()
    reason = token_quota.check(str(user.id), now=now)
    if reason is not None:
        raise _quota_error(reason)

    coordinator = knowledge_coordinator(session)
    events = coordinator.ask_stream(
        user_id=user.id,
        question=payload.question,
        session_id=payload.session_id,
        # 同上：身份交下去，工具才有权限来源
        actor=user,
    )
    return StreamingResponse(
        _sse(_charge_quota_stream(events, user=user, question=payload.question, now=now)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # 给反向代理看：不写它，代理会攒批转发，"流式"就变成了"一次性"
            "X-Accel-Buffering": "no",
        },
    )


async def _charge_quota_stream(
    events: AsyncIterator[tuple[str, dict[str, Any]]],
    *,
    user: CurrentUser,
    question: str,
    now: float,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """在流式事件的 `done` 上顺带记一笔配额。

    ============================ 为什么在这里记账而不是在协调者里 ============================
    配额是**接入层**的关注点（它跟 HTTP 请求的生命周期绑定），
    而协调者不该知道自己被谁调用、更不该依赖"当前时间"这种外部状态。
    这与 `login_throttle` 放在边界层是同一个位置选择。

    ⚠️ **只在 `done` 上记**：出错（`error`）时中途放弃的回答也已经消耗了 token，
    但这里**仍然记账**（用估算）—— 不记的话"让模型出错"就成了一条免费通道。
    """
    charged_already = False
    answer_pieces: list[str] = []
    async for name, data in events:
        if name == "delta":
            answer_pieces.append(str(data.get("text") or ""))
        if name in ("done", "error") and not charged_already:
            charged_already = True
            _charge_quota(
                user,
                {
                    "prompt_tokens": data.get("prompt_tokens"),
                    "completion_tokens": data.get("completion_tokens"),
                    "sources": data.get("sources") or [],
                    "answer": "".join(answer_pieces),
                },
                question=question,
                now=now,
            )
        yield name, data


async def _sse(
    events: AsyncIterator[tuple[str, dict[str, Any]]],
) -> AsyncIterator[bytes]:
    """`(事件名, 数据)` → SSE 字节流。

    两处细节：
      - `ensure_ascii=False`：中文直接以 UTF-8 出去。用默认的 `\\uXXXX` 转义
        会让报文长三倍，而 SSE 本来就是 UTF-8 的；
      - 每个事件以**空行**结束（SSE 的帧分隔），少了空行浏览器不会派发这一帧 ——
        表现为"连接建立了但什么都不显示"。
    """
    try:
        async for name, data in events:
            payload = json.dumps(data, ensure_ascii=False)
            yield f"event: {name}\ndata: {payload}\n\n".encode()
    except Exception as exc:  # noqa: BLE001
        # 协调者内部的错误已经尽量转成 `error` 事件了；能走到这里的
        # 多半是更外层的问题（序列化、连接断开之前的最后一次写）。
        # **仍然发一条 error**：让前端知道"结束得不对"，而不是当成正常 done。
        payload = json.dumps({"message": f"服务端异常：{exc}"}, ensure_ascii=False)
        yield f"event: error\ndata: {payload}\n\n".encode()


class ConfirmDraftIn(BaseModel):
    """确认"由 Agent 准备的那张草稿"。

    ============================ 为什么要原样回传 `draft` ============================
    确认令牌是**可以推算的**（`uuid5(actor+标题+品类+备注)`），
    所以只验令牌等于"知道内容就能建单"。回传内容之后服务端会**逐字重算令牌比对** ——
    确认的因而是**这一份提案**，而不是某个能算出来的字符串。
    """

    confirmation_token: str = Field(min_length=8, max_length=64)
    #: 与 Agent 提案里 `draft` 一模一样的内容（标题 / 品类 / 备注）
    draft: dict[str, Any]


class ConfirmDraftOut(BaseModel):
    request_id: str
    code: str
    title: str
    status: str
    #: 这次是**真的建了**还是**重放了上次的结果**。
    #: 要发给前端：用户点两次确认时，第二次应当告诉他"已经建过了"，
    #: 而不是让他以为又建了一张（那会让他去找两张单）。
    replayed: bool


@router.post(
    "/api/v1/chat/confirm-draft",
    response_model=ConfirmDraftOut,
    summary="确认 Agent 准备的采购申请草稿",
    responses={
        404: {"description": "令牌与内容不匹配，或令牌不属于当前用户"},
        422: {"description": "内容缺字段 / 品类不存在 / 账号没有部门"},
    },
)
async def confirm_draft(
    payload: ConfirmDraftIn, user: CurrentUser, session: SessionDep
) -> ConfirmDraftOut:
    """人确认之后，**真正创建**那张草稿。

    ============================ 三条纪律 ============================
    ① **用确认者的身份**执行（`user`），不是"Agent 的身份" ——
       所以"模型准备、人来执行"这条链上，权限始终是人的；
    ② **走同一个用例** `purchase.create_draft` —— 不另写一份创建逻辑，
       否则"接口能建的"与"确认能建的"会漂移（而漂移方向通常是确认那条更松）；
    ③ **幂等键用令牌本身**：它由"谁 + 什么内容"算出来，天然唯一。
       重复点确认 → 命中幂等记录 → 返回上次那张单（`replayed=true`），
       而不是又建一张。

    ⚠️ **这不是"Agent 能写数据"**：模型只能**准备**，落库必须有人点这一下。
    见 `application/tools.py` 里 `mutating` 的说明。
    """
    try:
        cleaned = verify_confirmation(
            token=payload.confirmation_token, draft=payload.draft, actor=user
        )
    except ConfirmationError as exc:
        # 用 404 而不是 400：令牌与身份不匹配时**不告诉调用方"这份提案存在"**，
        # 与"可见范围外一律 404"是同一条纪律。
        raise NotFoundError(exc.reason) from exc

    coordinator = purchase_coordinator(session)
    request_id, code, replayed = await coordinator.create_draft_idempotent(
        actor_id=user.id,
        department_id=user.department_id,
        category_id=uuid.UUID(cleaned["category_id"]),
        title=cleaned["title"],
        reason=cleaned["reason"],
        # 令牌当幂等键：同一份提案重复确认只建一张
        idempotency_key=payload.confirmation_token,
    )
    return ConfirmDraftOut(
        request_id=request_id,
        code=code,
        # 重放时拿不到标题（幂等记录里只存了 id 与 code）——
        # 那就**用确认内容里的标题**：它刚刚被逐字校验过，与库里那张一致。
        title=cleaned["title"],
        # 状态恒为 DRAFT：这个端点只建草稿，**不提交**（提交是另一个动作、要人再点一次）。
        status="DRAFT",
        replayed=replayed,
    )


@router.get("/api/v1/chat/sessions", response_model=list[SessionOut], summary="我的会话")
async def list_sessions(user: CurrentUser, session: SessionDep) -> list[SessionOut]:
    rows = await knowledge_coordinator(session).list_sessions(user_id=user.id)
    return [SessionOut.model_validate(row) for row in rows]


@router.get(
    "/api/v1/chat/sessions/{session_id}/messages",
    response_model=list[MessageOut],
    summary="某个会话的消息",
    responses={404: {"description": "会话不存在，或不属于当前账号"}},
)
async def session_messages(
    session_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> list[MessageOut]:
    rows = await knowledge_coordinator(session).session_messages(
        user_id=user.id, session_id=session_id
    )
    return [MessageOut.model_validate(row) for row in rows]


# ------------------------------------------------------------------ 知识库管理（管理员）


@router.post(
    "/api/v1/admin/knowledge-docs",
    response_model=DocOut,
    status_code=201,
    summary="上传制度文档（切分 + 向量化）",
    dependencies=[Depends(require_roles(ROLE_ADMIN))],
    responses={
        409: {"description": "同一份制度的这一版已经存在"},
        422: {"description": "类型不支持 / 编码识别不了 / 文本为空或过大"},
    },
)
async def upload_doc(
    user: CurrentUser,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
    source_key: Annotated[str, Form()],
    title: Annotated[str, Form()],
    version: Annotated[int | None, Form()] = None,
    effective_from: Annotated[datetime | None, Form()] = None,
) -> DocOut:
    """上传一份制度文档。

    `source_key` 是"同一份制度"的稳定标识（例如 `procurement-policy`）——
    **改标题不算换制度，改它才算**。同一 `source_key` 下 `version` 不能重复，
    而检索只取"已生效里版本最大的那一版"（否则作废的条文会被一起引出来）。
    """
    raw = await file.read()
    content_type = file.content_type or "application/octet-stream"
    body = await knowledge_coordinator(session).upload_doc(
        actor_id=user.id,
        source_key=source_key,
        title=title,
        version=version,
        effective_from=effective_from,
        filename=file.filename,
        content_type=content_type,
        raw=raw,
    )
    return DocOut.model_validate(body)


@router.get(
    "/api/v1/admin/knowledge-docs",
    response_model=list[DocOut],
    summary="制度文档列表",
    dependencies=[Depends(require_roles(ROLE_ADMIN))],
)
async def list_docs(session: SessionDep) -> list[DocOut]:
    """全部版本（**不过滤生效时间**）：管理员要看到自己刚传、还没生效的那一版。"""
    rows = await knowledge_coordinator(session).list_docs()
    return [DocOut.model_validate(row) for row in rows]


@router.get(
    "/api/v1/admin/knowledge-docs/{doc_id}/chunks",
    response_model=list[ChunkOut],
    summary="切分结果（排障入口）",
    dependencies=[Depends(require_roles(ROLE_ADMIN))],
    responses={404: {"description": "文档不存在"}},
)
async def list_chunks(doc_id: uuid.UUID, session: SessionDep) -> list[ChunkOut]:
    """这份文档切成了什么样。

    计划里专门要求"切分结果建议存下 `chunk_no`、字符起止位置和原文片段，
    出问题时你能一眼看出是『没检索到』还是『切碎了』"——
    这个接口就是那个入口。
    """
    rows = await knowledge_coordinator(session).list_chunks(doc_id=doc_id)
    return [ChunkOut.model_validate(row) for row in rows]


__all__ = ["ALLOWED_CONTENT_TYPES", "router"]
