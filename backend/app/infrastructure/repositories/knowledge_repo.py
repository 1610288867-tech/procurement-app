"""知识库的数据访问（M11）。

纪律同其他仓库：**只 flush()，不 commit()** —— 事务边界在用例层。

============================ 两处要害 SQL，各守一条不变量 ============================

**① 版本过滤（`_current_doc_ids`）：检索只取"当前生效版本"。**
   一份制度改了 3 版，库里就有 3 份文档、3 套片段。检索必须只命中
   **已生效里版本最大**的那一版 —— 否则模型会把已作废的规定和现行规定
   一起引给用户，**这比查不到更糟**（查不到用户会去问人，引错了用户会照着办）。

   实现用 PostgreSQL 的 `DISTINCT ON (source_key) ... ORDER BY source_key, version DESC`：
   一句话就能表达"按制度分组、每组取版本最大"，比"先查全部再在 Python 里筛"
   少一次全表搬运。

**② 向量检索（`search_chunks`）：只在这批文档的片段里按余弦距离排序。**
   两件事必须同时做对：
     - 查询里的算子与索引的算子**一致**（索引是 `vector_cosine_ops`，
       排序就必须用 `cosine_distance`）。用错时索引不被使用，**查询照样能跑，只是慢** ——
       不报错，所以最容易漏；
     - `WHERE doc_id = ANY(...)` 把范围先缩小到当前生效版本。
       少了它，作废版本的片段照样会被检索到（第 ① 条就白做了）。

============================ 为什么相似度在 Python 里算而不是写进 SQL ============================
`score = 1 - cosine_distance`。两者的排序是**反的**，所以 SQL 里按距离升序、
应用层再取 `1 - 距离` 当分数。把这个换算写在 SQL 的 `ORDER BY` 里也行，
但那样"排序键"和"对外分数"就成了两个地方各写一遍 ——
换算只有一处（`_to_source`），排序只有一处（`ORDER BY`），改哪个都不会漏掉另一个。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.embedding import VECTOR_DIM
from app.infrastructure.records.knowledge import (
    DOC_READY,
    ROLE_ASSISTANT,
    ROLE_USER,
    ChatMessageRecord,
    ChatSessionRecord,
    KnowledgeChunkRecord,
    KnowledgeDocRecord,
)


class KnowledgeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ 文档

    async def create_doc(
        self,
        *,
        source_key: str,
        title: str,
        version: int,
        effective_from: datetime,
        storage_key: str | None,
        filename: str | None,
        content_type: str,
        content: str,
        embedding_model: str,
        uploaded_by: uuid.UUID,
    ) -> KnowledgeDocRecord:
        """建文档，状态先落 `PENDING`。

        **先 PENDING 再切分**：切分与 embedding 可能失败（文件坏了、抽不出文本），
        那时文档停在 `FAILED` 并带上原因，用户看得到"为什么不生效"。
        如果一开始就写 `READY`，失败之后库里留下一份"看起来正常但没有任何片段"的文档，
        检索时它永远命中不了，而没人知道为什么。

        `UNIQUE(source_key, version)`：用例层已经查过一次并给了 409，
        这里是**兜底**（两个请求同时传同一版时唯一约束才是真正的守门人）。
        """
        record = KnowledgeDocRecord(
            source_key=source_key,
            title=title,
            version=version,
            effective_from=effective_from,
            storage_key=storage_key,
            filename=filename,
            content_type=content_type,
            content=content,
            status="PENDING",
            embedding_model=embedding_model,
            uploaded_by=uploaded_by,
        )
        self.session.add(record)
        # ⚠️ **必须显式 flush**：交给后面某个查询去 flush 的话，唯一约束会在
        # "那条无关的 SQL" 上炸出来，调用方的 `except IntegrityError` 接不住 ——
        # 用户看到的是 503 而不是 409（实测踩到）。让异常在**最接近它原因的地方**抛出。
        await self.session.flush()
        return record

    async def get_doc(self, doc_id: uuid.UUID) -> KnowledgeDocRecord | None:
        return await self.session.get(KnowledgeDocRecord, doc_id)

    async def latest_version_of(self, source_key: str) -> int:
        """这份制度现有的最大版本号（没有就是 0）。用例层据此算下一版。"""
        statement = select(func.coalesce(func.max(KnowledgeDocRecord.version), 0)).where(
            KnowledgeDocRecord.source_key == source_key
        )
        return int((await self.session.scalars(statement)).one())

    async def version_exists(self, *, source_key: str, version: int) -> bool:
        """这一版是不是已经传过了。

        ============================ 为什么要**显式查**，不能只靠唯一约束 ============================
        项目里"先查再写"通常是被禁止的（并发下会漏），但这里查的**不是并发条件**，
        而是一条业务规则的提前告知：`UNIQUE(source_key, version)` 当然会拦住重复，
        可它拦住的时机在**切分与向量化之后**的 flush 上 ——
        那时异常从"插入片段"那条 SQL 冒出来，`except IntegrityError` 接不住，
        用户看到的是 **503「数据库暂不可用」**（实测踩到，指错方向）。

        真正防并发的仍然是那条唯一约束（两个请求同时传同一版时由它兜底），
        这里的查重只负责把**绝大多数情况下**的错误变成一句人话。
        """
        statement = select(KnowledgeDocRecord.id).where(
            KnowledgeDocRecord.source_key == source_key,
            KnowledgeDocRecord.version == version,
        )
        return (await self.session.scalars(statement)).first() is not None

    async def list_docs(self) -> list[KnowledgeDocRecord]:
        """全部文档，新的在前（管理页用）。

        **不过滤生效时间**：管理员要看到自己刚上传、还没生效的那一版。
        """
        statement = select(KnowledgeDocRecord).order_by(
            KnowledgeDocRecord.source_key, KnowledgeDocRecord.version.desc()
        )
        return list((await self.session.scalars(statement)).all())

    async def chunk_count_of(self, doc_id: uuid.UUID) -> int:
        statement = select(func.count(KnowledgeChunkRecord.id)).where(
            KnowledgeChunkRecord.doc_id == doc_id
        )
        return int((await self.session.scalars(statement)).one())

    async def mark_ready(self, doc_id: uuid.UUID) -> None:
        record = await self.session.get(KnowledgeDocRecord, doc_id)
        if record is not None:
            record.status = DOC_READY
            record.failure_reason = None

    async def mark_failed(self, doc_id: uuid.UUID, *, reason: str) -> None:
        record = await self.session.get(KnowledgeDocRecord, doc_id)
        if record is not None:
            record.status = "FAILED"
            record.failure_reason = reason[:500]

    # ------------------------------------------------------------------ 片段

    async def replace_chunks(
        self,
        doc_id: uuid.UUID,
        chunks: Sequence[tuple[int, str, int, int, list[float]]],
    ) -> int:
        """整批替换这份文档的片段：`(chunk_no, content, start, end, embedding)`。

        **先删后插**而不是"逐条 upsert"：切分参数改了之后片数会变，
        upsert 会把多出来的旧片留在库里（它们的内容已经不属于任何新切分），
        而检索照样能命中它们 —— 又是一类"不报错但结果错"。
        重灌一份文档本身是幂等语义："让它的片段等于这次切分的结果"。
        """
        await self.session.execute(
            delete(KnowledgeChunkRecord).where(KnowledgeChunkRecord.doc_id == doc_id)
        )
        for chunk_no, content, start, end, embedding in chunks:
            if len(embedding) != VECTOR_DIM:
                # 早失败、说清原因：维度不对时数据库报的是
                # "expected 256 dimensions, not N"，那句话不告诉你维度是哪来的
                raise ValueError(
                    f"向量维度 {len(embedding)} 与列定义 {VECTOR_DIM} 不一致"
                    "（domain/embedding.VECTOR_DIM 与迁移里的 vector(N) 必须一起改）"
                )
            self.session.add(
                KnowledgeChunkRecord(
                    doc_id=doc_id,
                    chunk_no=chunk_no,
                    content=content,
                    start_offset=start,
                    end_offset=end,
                    embedding=embedding,
                )
            )
        await self.session.flush()
        return len(chunks)

    async def list_chunks(self, doc_id: uuid.UUID) -> list[KnowledgeChunkRecord]:
        """这份文档的切分结果，按片段号（管理页的"看切分结果"）。"""
        statement = (
            select(KnowledgeChunkRecord)
            .where(KnowledgeChunkRecord.doc_id == doc_id)
            .order_by(KnowledgeChunkRecord.chunk_no)
        )
        return list((await self.session.scalars(statement)).all())

    # ------------------------------------------------------------------ 检索（要害 SQL）

    async def current_doc_ids(self, *, now: datetime | None = None) -> list[uuid.UUID]:
        """**当前生效版本**的文档 id 列表（每个 `source_key` 取版本最大的那一版）。

        只算 `status = READY` 的：还在切分中的文档没有片段，把它算进来
        会让"检索范围"包含一个空文档（无影响但没意义）；失败的更不该算。
        """
        moment = now or datetime.now(UTC)
        statement = (
            select(KnowledgeDocRecord.id)
            .where(
                KnowledgeDocRecord.status == DOC_READY,
                KnowledgeDocRecord.effective_from <= moment,
            )
            # DISTINCT ON 要求 ORDER BY 以它开头，第二列才是"每组取哪一条"
            .distinct(KnowledgeDocRecord.source_key)
            .order_by(KnowledgeDocRecord.source_key, KnowledgeDocRecord.version.desc())
        )
        return list((await self.session.scalars(statement)).all())

    async def search_chunks(
        self,
        *,
        query_embedding: list[float],
        top_k: int,
        embedding_model: str | None = None,
        now: datetime | None = None,
    ) -> list[tuple[KnowledgeChunkRecord, KnowledgeDocRecord, float]]:
        """按余弦距离取最近的 `top_k` 片。返回 `(片段, 文档, 距离)`。

        `embedding_model` 给了就只在这批模型的文档里检索 —— 换模型时旧向量必须重灌，
        而混着检索的结果**不可比**（距离没有意义，排序变成噪声），且不报错。
        所以这里宁可能力弱一点（只检索当前模型），也不要给出静默变差的结果。
        """
        if len(query_embedding) != VECTOR_DIM:
            raise ValueError(f"查询向量维度 {len(query_embedding)} 应为 {VECTOR_DIM}")

        doc_ids = await self.current_doc_ids(now=now)
        if not doc_ids:
            # 一条文档都没有时**不要**走到 SQL：`= ANY('{}')` 虽然能跑，
            # 但提前返回能让上层日志里出现"没有可用文档"而不是"检索到 0 条"
            return []

        statement = (
            select(
                KnowledgeChunkRecord,
                KnowledgeDocRecord,
                KnowledgeChunkRecord.embedding.cosine_distance(query_embedding).label(
                    "distance"
                ),
            )
            .join(KnowledgeDocRecord, KnowledgeChunkRecord.doc_id == KnowledgeDocRecord.id)
            .where(KnowledgeChunkRecord.doc_id.in_(doc_ids))
            # 算子必须与 HNSW 索引的 `vector_cosine_ops` 一致（见模块说明）
            .order_by(KnowledgeChunkRecord.embedding.cosine_distance(query_embedding))
            .limit(max(1, top_k))
        )
        if embedding_model is not None:
            statement = statement.where(KnowledgeDocRecord.embedding_model == embedding_model)

        rows = (await self.session.execute(statement)).all()
        return [
            (chunk, doc, float(distance))
            for chunk, doc, distance in rows
        ]

    async def delete_docs_of_source(self, source_key: str) -> int:
        """删掉这份制度的**全部版本**（片段随之 CASCADE）。

        只在测试清理与运维路径用：正常流程里制度是**追加版本**，不是删除。
        留着这个方法是因为"传错了、要撤掉"确实会发生，
        而删文档必须有明确的入口（不能让运维去手写 SQL）。
        """
        result = await self.session.execute(
            delete(KnowledgeDocRecord)
            .where(KnowledgeDocRecord.source_key == source_key)
            .returning(KnowledgeDocRecord.id)
        )
        return len(list(result.scalars().all()))

    # ------------------------------------------------------------------ 会话与消息

    async def create_session(
        self, *, user_id: uuid.UUID, title: str
    ) -> ChatSessionRecord:
        record = ChatSessionRecord(user_id=user_id, title=title[:200])
        self.session.add(record)
        await self.session.flush()
        return record

    async def get_session(self, session_id: uuid.UUID) -> ChatSessionRecord | None:
        return await self.session.get(ChatSessionRecord, session_id)

    async def list_sessions(self, *, user_id: uuid.UUID) -> list[ChatSessionRecord]:
        """**只列自己的会话**。不做"管理员能看所有人的问答"——
        问答内容里可能有业务信息，而"谁能看谁问过什么"是要单独设计的口径，
        不该顺手放开。"""
        statement = (
            select(ChatSessionRecord)
            .where(ChatSessionRecord.user_id == user_id)
            .order_by(ChatSessionRecord.created_at.desc())
        )
        return list((await self.session.scalars(statement)).all())

    async def next_seq(self, session_id: uuid.UUID) -> int:
        statement = select(func.coalesce(func.max(ChatMessageRecord.seq), 0)).where(
            ChatMessageRecord.session_id == session_id
        )
        return int((await self.session.scalars(statement)).one()) + 1

    async def append_message(
        self,
        *,
        session_id: uuid.UUID,
        role: str,
        content: str,
        seq: int,
        sources: list[dict[str, Any]] | None = None,
        citation_problems: list[str] | None = None,
        top_score: float | None = None,
        used_model: bool = False,
    ) -> ChatMessageRecord:
        if role not in (ROLE_USER, ROLE_ASSISTANT):
            raise ValueError(f"未知的消息角色：{role}")
        record = ChatMessageRecord(
            session_id=session_id,
            role=role,
            content=content,
            seq=seq,
            sources=sources,
            citation_problems=citation_problems,
            top_score=top_score,
            used_model=used_model,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def list_messages(self, session_id: uuid.UUID) -> list[ChatMessageRecord]:
        statement = (
            select(ChatMessageRecord)
            .where(ChatMessageRecord.session_id == session_id)
            .order_by(ChatMessageRecord.seq)
        )
        return list((await self.session.scalars(statement)).all())

    # ------------------------------------------------------------------ 运维

    async def source_keys(self) -> list[str]:
        """库里现有的 `source_key`（测试清理与"这份制度传过没有"的判断）。"""
        statement = select(KnowledgeDocRecord.source_key).distinct()
        return list((await self.session.scalars(statement)).all())
