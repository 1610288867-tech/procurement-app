"""知识库与问答的用例（M11）。

============================ 这个协调者负责的六件事 ============================
1. **上传制度文档**（管理员）：落原文 → 归一化 → 切分 → 算向量 → 存片段；
2. **看切分结果**（管理员）：出问题时一眼看出是"没检索到"还是"切碎了"；
3. **提问**（登录用户）：向量检索 → 阈值判定 → （可选）调模型 → 引用校验 → 存消息；
4. **会话历史**（只列自己的）；
5. **SSE 事件流**：`meta` → `delta` → `done` / `error`。

============================ 顺序不能反：先检索，再决定要不要问模型 ============================
计划里说得很直白："很多人一上手就『接个大模型』，结果做出一个会编造制度条文的玩具。
**正确顺序是：先有知识库，再有检索，最后才是模型。**"

落到代码里就是这一行判断的位置：

    decision = decide_grounding(sources, min_score=...)
    if not decision.use_model:
        # 检索为空或全部低于阈值：**不调用模型**，直接返回"未找到相关规定"
        ...

**它必须在构造 Prompt 之前、在拿到模型客户端之前**。写成"先问模型、
拿到回答再检查有没有依据"就等于没有这道闸门 —— 模型已经答完了。

============================ 引用校验：发现问题不阻断，但要说出来 ============================
计划定的第一版策略：**不阻断**，把问题记进 `chat_messages.citation_problems`
并在回答末尾追加一行提示。理由：直接拒答会让用户觉得"明明答得挺好却用不了"，
而这类问题需要样本量才能决定是"重试一次"还是"换成拒答"。
**关键是这个校验必须存在** —— 它今天的价值是让问题可见。

============================ 为什么问答不写 `request_events` ============================
`request_events` 是**单据的时间线**（只追加、永不修改，是审计证据）。
问答不是单据事件，它有自己的一张表（`chat_messages`）。
把两者混在一起会让"这张单子经历过什么"这条查询里出现一堆聊天记录 ——
而且会破坏"事件流只记单据状态变化"这个语义。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.application.ports import ChatModelPort, ObjectStorePort, UnitOfWork
from app.domain.chunking import chunk_text, normalize_text
from app.domain.embedding import VECTOR_DIM, embed_text, embedding_model_name
from app.domain.grounded_answer import (
    CITATION_WARNING,
    SYSTEM_PROMPT,
    Source,
    build_user_prompt,
    check_citation_integrity,
    decide_grounding,
)
from app.errors import BusinessError, ConflictError, NotFoundError
from app.infrastructure.chat_models import LocalExtractiveChat
from app.infrastructure.repositories.knowledge_repo import KnowledgeRepository

#: 允许的制度文档类型。**刻意只有两种**：
#: PDF 抽文本要额外的库（`pypdf` 之类），而那属于"要不要为 M11 再加一个依赖"的决定 ——
#: 第一版先用最稳的两类（纯文本 / Markdown），把链路跑通。
#: 需要 PDF 时再加解析器，而不是现在装一个用不上的库。
ALLOWED_CONTENT_TYPES = {
    "text/plain": ".txt",
    "text/markdown": ".md",
}

#: 类型缺失时**按扩展名补全**（与 M9 附件那条规则同一个思路，但这里更窄：
#: 只认这两个扩展名，不做通用 MIME 猜测）。
#:
#: 为什么必须有这一步：`httpx` 与一些客户端在发送 `text/plain` 的 multipart 部分时
#: **不带 `Content-Type`**，于是 Starlette 给的是 `None`。实测就是这样 ——
#: 结果所有 `.txt` 制度都被 422 挡掉（"只收纯文本与 Markdown（收到 application/octet-stream）"），
#: 而 curl 与浏览器的行为还不完全一样，属于"换个人试就复现不了"的那类问题。
#:
#: ⚠️ 这里**只补声明类型，不代替内容校验** —— 与 M9 的纪律一致：
#: 扩展名永远只是"补全声明"，不能用来判断文件内容安不安全。
#: 制度文档不执行、不渲染，所以第一版不做魔数校验；真要收 PDF 时必须补上。
EXTENSION_CONTENT_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
}
#: 单份制度的文本上限（字符）。比附件上限（10 MB）小得多 ——
#: 因为这里要**全量切分并逐片算向量**，一份几十万字的文档会让上传请求卡很久。
MAX_DOC_CHARS = 200_000
#: 会话标题取问题的前 N 个字符。
TITLE_LENGTH = 60


def _infer_content_type(filename: str | None) -> str | None:
    """按扩展名补全声明类型。认不出返回 `None`。

    ⚠️ **不要兜底成 `application/octet-stream`**：那样调用处就分不清
    "补全成功"与"压根没认出来"，而这两种情况要给用户完全不同的提示
    （一个是"能收"，一个是"只收 txt / md"）。
    """
    if not filename:
        return None
    lowered = filename.lower()
    for suffix, content_type in EXTENSION_CONTENT_TYPES.items():
        if lowered.endswith(suffix):
            return content_type
    return None


class KnowledgeCoordinator:
    def __init__(
        self,
        *,
        knowledge: KnowledgeRepository,
        object_store: ObjectStorePort,
        chat_model: ChatModelPort,
        uow: UnitOfWork,
        top_k: int,
        min_score: float,
    ) -> None:
        self._knowledge = knowledge
        self._store = object_store
        self._chat = chat_model
        self._uow = uow
        self._top_k = top_k
        self._min_score = min_score

    # ------------------------------------------------------------------ 文档入库

    async def upload_doc(
        self,
        *,
        actor_id: uuid.UUID,
        source_key: str,
        title: str,
        version: int | None,
        effective_from: datetime | None,
        filename: str | None,
        content_type: str,
        raw: bytes,
    ) -> dict[str, Any]:
        """上传一份制度文档：落原文 → 切分 → 算向量 → 存片段。

        `version` 不传就**自动取下一版**（`MAX + 1`）—— 这是最常见的用法：
        "这是这份制度的新版本"。传了就按传的算（允许补录历史版本）。

        ============================ 失败也要留下文档 ============================
        切分 / 向量化失败时，文档**留在 `FAILED` 并带上原因**，而不是根本不建。
        否则用户看到的是"上传失败了"，但不知道是文件的问题还是系统的问题；
        留下一条 `FAILED` 记录，管理页就能显示"为什么这一版没生效"。
        """
        source_key = source_key.strip()
        title = title.strip()
        if not source_key or not title:
            raise BusinessError(
                "source_key 与标题都不能为空（source_key 是「同一份制度」的稳定标识）",
                status_code=422,
                code="KNOWLEDGE_DOC_FIELDS_REQUIRED",
            )
        # 类型：**先看声明，只在声明缺失时**按扩展名补全一次。
        #
        # ⚠️ 顺序不能反。反过来（扩展名优先）会让"声明 application/pdf、文件名 .txt"
        # 这种组合被当成纯文本收下 —— 实测踩到：一条"应当被拒"的用例变成了 201。
        # 声明是本意，扩展名只是它在 multipart 里丢了之后的**兜底**。
        resolved_type: str | None = content_type
        if resolved_type in (None, "", "application/octet-stream"):
            resolved_type = _infer_content_type(filename)
        if resolved_type not in ALLOWED_CONTENT_TYPES:
            raise BusinessError(
                f"制度文档目前只收纯文本与 Markdown（收到 {resolved_type or '未知类型'}）",
                status_code=422,
                code="KNOWLEDGE_DOC_TYPE_UNSUPPORTED",
            )
        # 补全之后的类型要带下去：后面按它选落盘后缀
        content_type = resolved_type

        text = normalize_text(self._decode(raw))
        if not text:
            raise BusinessError(
                "文件里没有可用的文本内容", status_code=422, code="KNOWLEDGE_DOC_EMPTY"
            )
        if len(text) > MAX_DOC_CHARS:
            raise BusinessError(
                f"文本长度 {len(text)} 超过上限 {MAX_DOC_CHARS} 字符"
                "（这一版会全量切分并逐片算向量，太大请拆分后分次上传）",
                status_code=422,
                code="KNOWLEDGE_DOC_TOO_LARGE",
            )

        if version is None:
            version = await self._knowledge.latest_version_of(source_key) + 1
        elif version <= 0:
            raise BusinessError("版本号必须为正整数", status_code=422, code="KNOWLEDGE_VERSION_INVALID")
        # **显式查重**，把"这一版传过了"变成一句人话的 409。
        # 不查的话，唯一的拦截点是切分/向量化之后的 flush —— 那时异常从
        # "插入片段"那条 SQL 冒出来，`except IntegrityError` 接不住，
        # 用户看到的是 503「数据库暂不可用」（实测踩到，指错方向）。
        # 并发下仍然由 `uq_knowledge_doc_version` 兜底（见下面那个 except）。
        if await self._knowledge.version_exists(source_key=source_key, version=version):
            raise ConflictError(
                f"《{title}》的第 {version} 版已经存在了（同一份制度同一版本只能传一次）"
            )

        # 原文落盘（**UUID 命名**，沿用 M9 纪律：永不用用户提供的文件名）
        suffix = ALLOWED_CONTENT_TYPES[content_type]
        storage_key = self._store.save(raw, suffix)

        doc = await self._knowledge.create_doc(
            source_key=source_key,
            title=title,
            version=version,
            effective_from=effective_from or datetime.now(UTC),
            storage_key=storage_key,
            filename=filename,
            content_type=content_type,
            content=text,
            embedding_model=embedding_model_name(),
            uploaded_by=actor_id,
        )
        try:
            await self._index(doc.id, text)
        except IntegrityError as exc:
            # `UNIQUE(source_key, version)`：这一版已经传过了。
            # 落盘的原文要删掉 —— 否则文件永远留在磁盘上没人引用
            # （对象存储没有"垃圾回收"，只有人工清理脚本会看它）。
            await self._uow.rollback()
            self._store.delete(storage_key)
            raise ConflictError(
                f"《{title}》的第 {version} 版已经存在了（同一份制度同一版本只能传一次）"
            ) from exc

        await self._uow.commit()
        # 与 `list_docs` **同一个形状**（直接复用它，不手拼第二份）：
        # 上传的响应体与列表里那一行长得一样，前端才能只写一套渲染。
        # 手拼一份的代价是实测过的 —— 少三个字段（`filename` / `failure_reason` /
        # `created_at`）而 Pydantic 对**可选**字段缺失照样报错，成了"上传成功但响应 500"。
        return await self._doc_payload(doc)

    async def _doc_payload(self, doc: Any) -> dict[str, Any]:
        """一条文档的对外形状（上传与列表共用）。"""
        return {
            "id": str(doc.id),
            "source_key": doc.source_key,
            "title": doc.title,
            "version": doc.version,
            "effective_from": doc.effective_from.isoformat(),
            "filename": doc.filename,
            "status": doc.status,
            "embedding_model": doc.embedding_model,
            "chunk_count": await self._knowledge.chunk_count_of(doc.id),
            "content_chars": len(doc.content),
            "failure_reason": doc.failure_reason,
            "created_at": doc.created_at.isoformat(),
        }

    async def _index(self, doc_id: uuid.UUID, text: str) -> int:
        """切分 + 算向量 + 入库。返回片段数。

        向量在**这里**算（而不是数据库里）：数据库只负责"存向量、按距离排序"，
        "怎么把文本变成向量"是模型的事 —— 把它塞进 SQL 会绑定死某一种实现。
        """
        chunks = chunk_text(text)
        if not chunks:
            await self._knowledge.mark_failed(doc_id, reason="切分后没有任何片段")
            raise BusinessError(
                "切分后没有任何片段（文本可能全是空白）",
                status_code=422,
                code="KNOWLEDGE_CHUNK_EMPTY",
            )
        prepared = [
            (
                chunk.chunk_no,
                chunk.content,
                chunk.start_offset,
                chunk.end_offset,
                embed_text(chunk.content),
            )
            for chunk in chunks
        ]
        count = await self._knowledge.replace_chunks(doc_id, prepared)
        await self._knowledge.mark_ready(doc_id)
        return count

    @staticmethod
    def _decode(raw: bytes) -> str:
        """字节 → 文本。**解码失败要报出来**，不能悄悄用替换字符。

        `errors="replace"` 会把乱码变成 `\\ufffd`，于是"文件编码不对"这件事
        就变成了"文本里有一堆问号" —— 切分照样成功、检索照样能跑，
        只是检索出来的是垃圾。GBK 文件（国内制度文档很常见）就会踩这一条。
        """
        for encoding in ("utf-8", "utf-8-sig", "gb18030"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise BusinessError(
            "文件编码无法识别（试过 UTF-8 / GB18030）—— 请另存为 UTF-8 后重传",
            status_code=422,
            code="KNOWLEDGE_DOC_ENCODING",
        )

    # ------------------------------------------------------------------ 管理页读接口

    async def list_docs(self) -> list[dict[str, Any]]:
        docs = await self._knowledge.list_docs()
        return [await self._doc_payload(doc) for doc in docs]

    async def list_chunks(self, *, doc_id: uuid.UUID) -> list[dict[str, Any]]:
        """这份文档的切分结果（**排障入口**：一眼看出是没检索到还是切碎了）。"""
        doc = await self._knowledge.get_doc(doc_id)
        if doc is None:
            raise NotFoundError(f"制度文档不存在：{doc_id}")
        chunks = await self._knowledge.list_chunks(doc_id)
        return [
            {
                "chunk_no": chunk.chunk_no,
                "content": chunk.content,
                "start_offset": chunk.start_offset,
                "end_offset": chunk.end_offset,
                "length": chunk.end_offset - chunk.start_offset,
            }
            for chunk in chunks
        ]

    # ------------------------------------------------------------------ 会话

    async def list_sessions(self, *, user_id: uuid.UUID) -> list[dict[str, Any]]:
        sessions = await self._knowledge.list_sessions(user_id=user_id)
        return [
            {
                "id": str(session.id),
                "title": session.title,
                "created_at": session.created_at.isoformat(),
            }
            for session in sessions
        ]

    async def session_messages(
        self, *, user_id: uuid.UUID, session_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        session = await self._knowledge.get_session(session_id)
        if session is None or session.user_id != user_id:
            # 别人的会话一律 404（不告诉它存在）—— 与全项目的可见范围口径一致
            raise NotFoundError(f"会话不存在：{session_id}")
        messages = await self._knowledge.list_messages(session_id)
        return [
            {
                "id": str(message.id),
                "role": message.role,
                "content": message.content,
                "seq": message.seq,
                "sources": message.sources,
                "citation_problems": message.citation_problems,
                "used_model": message.used_model,
                "top_score": message.top_score,
                "created_at": message.created_at.isoformat(),
            }
            for message in messages
        ]

    # ------------------------------------------------------------------ 提问（核心）

    async def ask(
        self,
        *,
        user_id: uuid.UUID,
        question: str,
        session_id: uuid.UUID | None,
    ) -> dict[str, Any]:
        """非流式提问：把整段回答拼好再返回（给 curl / 测试用）。

        与 `ask_stream` **共用同一段逻辑**（`_prepare`），
        所以两条路不会出现"流式说没依据、非流式却调了模型"这种不一致。
        """
        prepared = await self._prepare(user_id=user_id, question=question, session_id=session_id)
        if not prepared["decision"].use_model:
            answer = prepared["decision"].fallback_answer
        else:
            chunks: list[str] = []
            async for piece in self._chat.stream(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=prepared["user_prompt"],
                sources=prepared["sources"],
            ):
                chunks.append(piece)
            answer = "".join(chunks)
        return await self._finish(prepared, answer=answer)

    def _used_remote(self, decision: Any) -> bool:
        """这次回答有没有真的调用**远端模型**。

        ⚠️ 语义是"远端模型有没有被调用"，**不是**"要不要生成回答"。
        两者在本地兜底时分开：那时 `decision.use_model` 是 True（有依据、可以生成），
        但一次网络调用都没发生 —— 而 `used_model` 这个字段存在的意义，
        正是让"未调用模型"这件事在界面与日志里看得见（一条验收项）。
        实测踩到：把它写成 `decision.use_model`，那条验收项在本地兜底下永远显示错误。
        `ask` 与 `ask_stream` **两条路都要用它** —— 只改一条的后果是
        "流式说没调模型、非流式说调了"，而测试打的往往是非流式那条。
        """
        return decision.use_model and not isinstance(self._chat, LocalExtractiveChat)

    async def ask_stream(
        self,
        *,
        user_id: uuid.UUID,
        question: str,
        session_id: uuid.UUID | None,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """流式提问：产出 `(事件名, 数据)`，由边界层翻成 SSE。

        ============================ 事件顺序（协议的一部分） ============================
            `meta`  → 会话 id、来源列表、是否调用模型、为什么
            `delta` → 正文片段（可能很多条；未调用模型时只有一条）
            `done`  → 结束（带落库后的消息 id）
            `error` → 出错（**不静默截断**）

        **`meta` 必须先发**（计划里专门点了这一条）：用户能在正文还没出来时就
        先看到引用来源，信任感强很多；反过来（先出正文再补来源）用户读完一段
        断言才发现没有出处，那时的体感是"它在编"。

        `sources` 在 `meta` 里给全，而不是等正文里的 `[n]` 出现再补 ——
        前端因此可以先把引用卡片渲染出来。
        """
        prepared = await self._prepare(
            user_id=user_id, question=question, session_id=session_id
        )
        sources: list[Source] = prepared["sources"]
        decision = prepared["decision"]
        # `used_model` 的语义是"**远端模型**有没有被调用"，不是"要不要生成回答"。
        # 两者的区别在本地兜底时体现出来：那时 `decision.use_model` 是 True
        # （有依据、可以生成），但**一次网络调用都没发生**。
        # 实测踩到：把它写成 `decision.use_model`，于是"未调用模型"这条
        # 验收项在本地兜底下永远显示成"已调用" —— 而它恰恰是要证明这件事的字段。
        used_remote = self._used_remote(decision)
        yield (
            "meta",
            {
                "session_id": str(prepared["session"].id),
                "sources": [self._source_payload(source) for source in sources],
                "used_model": used_remote,
                "reason": self._reason(decision, used_remote=used_remote),
                "model": self._chat.name if used_remote else None,
            },
        )

        pieces: list[str] = []
        if not decision.use_model:
            # **不调用模型**：直接给那句固定回答。文字不进模型、不进网络
            yield "delta", {"text": decision.fallback_answer}
            pieces.append(decision.fallback_answer)
        else:
            try:
                async for piece in self._chat.stream(
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=prepared["user_prompt"],
                    sources=sources,
                ):
                    pieces.append(piece)
                    yield "delta", {"text": piece}
            except Exception as exc:  # noqa: BLE001
                # 明确告知，**不静默截断**：已经吐出去的部分保留（用户看到了什么就是什么），
                # 但必须有一条 error 让它知道后面还有内容没出来。
                # 这条例外**不吞**：它要被记进日志，否则线上"偶尔答一半"永远查不出来。
                await self._record_failure(prepared, reason=f"模型调用失败：{exc}")
                yield "error", {"message": f"模型调用失败：{exc}"}
                return

        result = await self._finish(prepared, answer="".join(pieces))
        yield "done", {"message_id": result["message_id"], "citation_problems": result["citation_problems"]}

    # ------------------------------------------------------------------ 内部

    async def _prepare(
        self,
        *,
        user_id: uuid.UUID,
        question: str,
        session_id: uuid.UUID | None,
    ) -> dict[str, Any]:
        """提问的**公共前半段**：存问题 → 检索 → 阈值判定 → 拼 Prompt。

        流式与非流式共用它，所以"要不要问模型"这个决定只有一处。
        """
        question = question.strip()
        if not question:
            raise BusinessError("问题不能为空", status_code=422, code="CHAT_QUESTION_EMPTY")
        if len(question) > 500:
            raise BusinessError(
                "问题过长（上限 500 字）", status_code=422, code="CHAT_QUESTION_TOO_LONG"
            )

        session = await self._ensure_session(
            user_id=user_id, session_id=session_id, question=question
        )
        seq = await self._knowledge.next_seq(session.id)
        await self._knowledge.append_message(
            session_id=session.id, role="USER", content=question, seq=seq
        )

        query_embedding = embed_text(question)
        if not any(query_embedding):
            # 问题里一个可检索的词都没有（例如全是标点）：**不检索**。
            # 全零向量进 SQL 之后余弦距离没有意义（NULL 或 1），排序结果不可解释。
            sources: list[Source] = []
        else:
            rows = await self._knowledge.search_chunks(
                query_embedding=query_embedding,
                top_k=self._top_k,
                # 只检索**当前模型**算的向量：混着检索的结果不可比（见仓库说明）
                embedding_model=embedding_model_name(),
            )
            sources = [self._to_source(index, row) for index, row in enumerate(rows, start=1)]

        decision = decide_grounding(sources, min_score=self._min_score)
        return {
            "session": session,
            "sources": sources,
            "decision": decision,
            "user_prompt": build_user_prompt(question, sources) if sources else "",
        }

    async def _ensure_session(
        self, *, user_id: uuid.UUID, session_id: uuid.UUID | None, question: str
    ) -> Any:
        """拿到会话；没给就新建一个，标题取问题的前若干字。

        会话**归属校验**在这里：给了别人的 session_id 一律 404，
        不能把问题追加到别人的会话里（那既是越权、也会把两个人的对话混在一起）。
        """
        if session_id is None:
            return await self._knowledge.create_session(
                user_id=user_id, title=question[:TITLE_LENGTH]
            )
        session = await self._knowledge.get_session(session_id)
        if session is None or session.user_id != user_id:
            raise NotFoundError(f"会话不存在：{session_id}")
        return session

    @staticmethod
    def _reason(decision: Any, *, used_remote: bool) -> str:
        """给人和日志看的一句话：为什么是这个回答方式。

        三种情况要说得出区别，否则排障时分不清"没检索到"、"阈值太高"、
        "用的是兜底而不是模型"—— 这三件事的修法完全不同。
        """
        if not decision.use_model:
            return decision.reason
        if used_remote:
            return decision.reason
        return f"{decision.reason}；本次用**本地兜底**整理原文，未调用远端模型"

    def _to_source(self, number: int, row: tuple[Any, Any, float]) -> Source:
        """检索结果 → 领域层的 `Source`。

        **相似度 = 1 − 余弦距离**（唯一的换算处）：SQL 按**距离升序**排，
        而对外给的是**分数越高越相关**。把换算只留在这里，
        排序与分数就不会各写一遍、各错一次。

        `max(0.0, ...)`：浮点误差可能让相等的向量算出 `1e-16` 的距离，
        于是分数变成 `0.9999999999`；反过来也可能出现略大于 1 的值。
        夹到 `[0, 1]` 让"分数"这个数在界面上始终可解释。
        """
        chunk, doc, distance = row
        return Source(
            number=number,
            doc_id=str(doc.id),
            doc_title=doc.title,
            doc_version=doc.version,
            chunk_no=chunk.chunk_no,
            content=chunk.content,
            score=max(0.0, min(1.0, 1.0 - distance)),
        )

    @staticmethod
    def _source_payload(source: Source) -> dict[str, Any]:
        return {
            "number": source.number,
            "doc_id": source.doc_id,
            "doc_title": source.doc_title,
            "doc_version": source.doc_version,
            "chunk_no": source.chunk_no,
            "content": source.content,
            "score": round(source.score, 4),
        }

    async def _finish(self, prepared: dict[str, Any], *, answer: str) -> dict[str, Any]:
        """回答的**公共后半段**：引用校验 → 追加提示 → 落库。

        校验结果**落库**（`chat_messages.citation_problems`）而不是只写日志：
        计划里定的第一版策略是"不阻断，但把问题记下来"，
        记在库里才能积累样本、决定将来是重试还是拒答。
        """
        sources: list[Source] = prepared["sources"]
        problems = check_citation_integrity(answer, sources)
        final = answer + (CITATION_WARNING if problems else "")
        top_score = max((source.score for source in sources), default=None)

        session = prepared["session"]
        seq = await self._knowledge.next_seq(session.id)
        message = await self._knowledge.append_message(
            session_id=session.id,
            role="ASSISTANT",
            content=final,
            seq=seq,
            sources=[self._source_payload(source) for source in sources],
            citation_problems=problems or None,
            top_score=top_score,
            used_model=self._used_remote(prepared["decision"]),
        )
        await self._uow.commit()
        return {
            "message_id": str(message.id),
            "session_id": str(session.id),
            "answer": final,
            "sources": [self._source_payload(source) for source in sources],
            "citation_problems": problems,
            "used_model": self._used_remote(prepared["decision"]),
            "reason": self._reason(
                prepared["decision"],
                used_remote=self._used_remote(prepared["decision"]),
            ),
            "top_score": top_score,
        }

    async def _record_failure(self, prepared: dict[str, Any], *, reason: str) -> None:
        """模型失败时也把这件事落库（用户看到的是 error 事件）。

        不写这条的话，库里只有"用户问了"没有"回答失败了" ——
        事后统计"多少问题没答出来"会漏掉最需要看的那一类。
        """
        session = prepared["session"]
        seq = await self._knowledge.next_seq(session.id)
        await self._knowledge.append_message(
            session_id=session.id,
            role="ASSISTANT",
            content=f"（回答失败：{reason}）",
            seq=seq,
            sources=[self._source_payload(source) for source in prepared["sources"]],
            citation_problems=[reason],
            used_model=self._used_remote(prepared["decision"]),
        )
        await self._uow.commit()


def build_knowledge_coordinator(
    session: Any,
    *,
    object_store: ObjectStorePort,
    chat_model: ChatModelPort,
    top_k: int,
    min_score: float,
) -> KnowledgeCoordinator:
    """按一次请求的会话装配知识库用例的依赖。

    与其它 `build_*_coordinator` 同一个理由：装配只写一份，边界层与测试共用。

    `chat_model` 是**参数**而不是在这里 new 出来的：测试要注入一个
    "故意引用不存在编号"的假模型来验证引用校验（那是一条验收项），
    而把选模型的逻辑写死在装配里，那条验收就只能靠真模型碰运气。
    选哪一个实现由**边界层**决定（见 `boundaries/http/knowledge.py`）。
    """
    return KnowledgeCoordinator(
        knowledge=KnowledgeRepository(session),
        object_store=object_store,
        chat_model=chat_model,
        uow=session,
        top_k=top_k,
        min_score=min_score,
    )


__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "MAX_DOC_CHARS",
    "KnowledgeCoordinator",
    "VECTOR_DIM",
    "build_knowledge_coordinator",
]
