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
from app.boundaries.deps import CurrentUser, SessionDep, require_roles
from app.config import get_settings
from app.domain.permissions import ROLE_ADMIN
from app.infrastructure.chat_models import LocalExtractiveChat, OpenAiCompatibleChat
from app.infrastructure.object_store import LocalObjectStore

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


def knowledge_coordinator(session: SessionDep) -> KnowledgeCoordinator:
    """组装知识库用例的依赖（装配本身在 `coordinators/knowledge`）。"""
    settings = get_settings()
    return build_knowledge_coordinator(
        session,
        # 与附件共用同一个对象存储实现与同一个根目录：制度原文与附件是同一类
        # "服务端生成文件名"的东西，没必要为它再写一份
        object_store=LocalObjectStore(Path(settings.upload_dir)),
        chat_model=_chat_model(),
        top_k=settings.knowledge_top_k,
        min_score=settings.knowledge_min_score,
    )


# ------------------------------------------------------------------ 模型


class SourceOut(BaseModel):
    number: int
    doc_id: str
    doc_title: str
    doc_version: int
    chunk_no: int
    content: str
    score: float


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    #: 不传就新建一个会话
    session_id: uuid.UUID | None = None


class AskOut(BaseModel):
    message_id: str
    session_id: str
    answer: str
    sources: list[SourceOut]
    #: 引用校验发现的问题（空 = 通过）。**不阻断，但要说出来** —— 见协调者说明
    citation_problems: list[str]
    used_model: bool
    #: 为什么没调用模型（验收项：日志/界面里能看到"检索为空，未调用模型"）
    reason: str
    top_score: float | None


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
    used_model: bool
    top_score: float | None
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


@router.post("/api/v1/chat", response_model=AskOut, summary="问制度（非流式）")
async def ask(
    payload: AskIn, user: CurrentUser, session: SessionDep
) -> AskOut:
    """问一句制度问题。

    **查不到就明说查不到**：检索为空、或全部低于相似度阈值时，
    这句话由后端直接给出（`used_model=false`），不会去问模型 ——
    决定权在 `domain/grounded_answer.decide_grounding`，只有那一处。
    """
    body = await knowledge_coordinator(session).ask(
        user_id=user.id, question=payload.question, session_id=payload.session_id
    )
    return AskOut.model_validate(body)


@router.post("/api/v1/chat/stream", summary="问制度（SSE 流式）")
async def ask_stream(
    payload: AskIn, user: CurrentUser, session: SessionDep
) -> StreamingResponse:
    """流式问答。

    事件顺序固定为 `meta` → `delta`… → `done`（出错时以 `error` 结束）。
    前端按这个顺序就能做到"正文还没出来先渲染引用卡片"。
    """
    coordinator = knowledge_coordinator(session)
    events = coordinator.ask_stream(
        user_id=user.id, question=payload.question, session_id=payload.session_id
    )
    return StreamingResponse(
        _sse(events),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # 给反向代理看：不写它，代理会攒批转发，"流式"就变成了"一次性"
            "X-Accel-Buffering": "no",
        },
    )


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
