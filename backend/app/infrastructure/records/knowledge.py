"""知识库与问答的四张表（M11）。

    knowledge_docs     制度文档（含**版本**与**生效时间**）
    knowledge_chunks   切分后的片段（含 `embedding vector(256)`）
    chat_sessions      会话
    chat_messages      消息与**引用来源**（`sources` 存 JSONB —— 见下）

============================ 三处不肯让步的设计 ============================
**1. 版本与生效时间必须落库，检索只取"当前生效版本"。**
   制度修订后，如果新旧条文同时被检索到，模型会把**已作废的规定**和现行规定
   一起引给用户 —— 对"制度助手"来说，**这比查不到更糟**（查不到用户会去问人，
   引错了用户会照着办）。所以每份文档带 `version` + `effective_from`，
   检索时按"同一份制度"分组、每组只取 `effective_from <= now()` 里版本最大的那一版。

**2. `embedding_model` 必须与向量一起存。**
   换 embedding 模型几乎必然换维度，而混用两种模型的向量会让检索结果
   **静默变差**（距离不可比，排序变成噪声），且极难排查。
   把模型名（含维度，如 `local-hash-256`）写在文档上，将来重灌时一眼看得出
   哪些文档是旧模型算的。

**3. `sources` 存 JSONB 而不是再开一张 `chat_sources` 表。**
   计划里列了 `chat_sources`，这里没有单独建表，理由：
   引用来源**只有一种读法** —— "跟着某条回答一起显示"。没有任何查询是
   "按来源反查消息"，所以拆表只会多一次 JOIN、多一处外键级联要维护。
   JSONB 里存的是不可变的快照（文档标题 + 版本 + 片段号 + 打分），
   这正是审计想要的形态：**回答当时引用的是什么，事后不随文档改动而变**。

   ⚠️ 代价写清楚：**不能按来源检索**（"哪些回答引用了第 5 版第 12 条"查不了）。
   真需要时再加一张 GIN 索引或拆表 —— 那是"按需加"，不是现在预留。

============================ 关于 `chunk_no` 与下标 ============================
`start_offset` / `end_offset` 存的是**原文字符下标**：计划里专门点了这一条
（"出问题时你能一眼看出是『没检索到』还是『切碎了』"）。
它们是排障入口，不是装饰。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.embedding import VECTOR_DIM
from app.infrastructure.records.base import Base

#: 文档处理状态。`PENDING` 是"文件收下了、文本还没切"——
#: 切分与 embedding 可能失败（文件坏了 / 没有可用文本），
#: 那时文档停在 `FAILED` 并带上原因，**不是删掉**（否则用户不知道为什么没生效）。
DOC_PENDING = "PENDING"
DOC_READY = "READY"
DOC_FAILED = "FAILED"
DOC_STATUSES: tuple[str, ...] = (DOC_PENDING, DOC_READY, DOC_FAILED)

#: 消息角色。只有两种 —— 没有 system/tool 角色：这个助手**不能执行操作**，
#: 所以也不需要"工具调用"这类角色（那是另一套权限模型）。
ROLE_USER = "USER"
ROLE_ASSISTANT = "ASSISTANT"
MESSAGE_ROLES: tuple[str, ...] = (ROLE_USER, ROLE_ASSISTANT)


class KnowledgeDocRecord(Base):
    """一份制度文档的**某一版**。

    `source_key` 是"同一份制度"的稳定标识（例如 `procurement-policy`）。
    同一 `source_key` 下 `version` 不能重复（`uq_knowledge_doc_version`）——
    这是版本机制的地基：检索要按它分组取最大版本。
    """

    __tablename__ = "knowledge_docs"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_knowledge_doc_version_positive"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in DOC_STATUSES) + ")",
            name="ck_knowledge_doc_status_known",
        ),
        UniqueConstraint("source_key", "version", name="uq_knowledge_doc_version"),
        # 检索的主查询：按 source_key 分组取"已生效里版本最大"的那一版
        Index("ix_knowledge_docs_key_version", "source_key", "version"),
        Index("ix_knowledge_docs_effective", "effective_from"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    #: "同一份制度"的稳定标识。改标题不算换制度，改这份 key 才算。
    source_key: Mapped[str] = mapped_column(String(120), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    #: 同一份制度的第几版（从 1 开始）
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 这一版从什么时候生效。检索只取 `<= now()` 的版本
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: 原始文件在对象存储里的键（**UUID 命名**，沿用 M9 的纪律）。
    #: 文本是打字粘贴进来的时候为空 —— 那种情况没有原文文件。
    storage_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(300), nullable=True)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    #: 归一化后的全文。留着它有三个用处：重新切分、人工核对切分结果、按 offset 回查
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 失败原因（`status = FAILED` 时给人和运维看）
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: 这份文档的向量是哪个模型算的（**含维度**，如 `local-hash-256`）
    embedding_model: Mapped[str] = mapped_column(String(80), nullable=False)
    # RESTRICT：谁上传的要留得住（知识库是"制度依据"，来源必须可追）
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class KnowledgeChunkRecord(Base):
    """切分后的片段（含向量）。"""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        CheckConstraint("chunk_no >= 0", name="ck_knowledge_chunk_no_non_negative"),
        CheckConstraint("start_offset >= 0", name="ck_knowledge_chunk_start_non_negative"),
        CheckConstraint("end_offset > start_offset", name="ck_knowledge_chunk_range_valid"),
        UniqueConstraint("doc_id", "chunk_no", name="uq_knowledge_chunk_no"),
        # HNSW + 余弦距离。**这是 M11 检索性能的全部**：
        # 不建索引时每次查询都是全表扫描 + 逐行算距离，几万片之后肉眼可见地慢。
        # `vector_cosine_ops` 必须与查询里用的算子一致（`<=>` 余弦距离），
        # 用错算子（例如 l2）时索引不会被使用，查询照样能跑 —— 只是慢，不报错。
        Index(
            "ix_knowledge_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    doc_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_docs.id", ondelete="CASCADE"), nullable=False
    )
    chunk_no: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: 原文字符下标 `[start_offset, end_offset)` —— 排障入口（见模块说明）
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    #: ⚠️ 维度与 `domain/embedding.VECTOR_DIM` **必须一致**（两个数字一起改）。
    #: 不一致时插入会直接报错（这是好事：总比静默存进不可比的向量强）。
    embedding: Mapped[list[float]] = mapped_column(Vector(VECTOR_DIM), nullable=False)


class ChatSessionRecord(Base):
    """一次问答会话。"""

    __tablename__ = "chat_sessions"
    __table_args__ = (Index("ix_chat_sessions_user_created", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    #: 会话标题 = 第一个问题（截断）。不为它单开一个"起标题"的动作 ——
    #: 让用户先想标题再提问，是把系统的工作推给人。
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ChatMessageRecord(Base):
    """会话里的一条消息（用户问题或助手回答）。

    `sources` 只在助手消息上有值，且是**回答当时**的不可变快照
    （见模块说明第 3 条）。
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint(
            "role IN (" + ", ".join(f"'{value}'" for value in MESSAGE_ROLES) + ")",
            name="ck_chat_message_role_known",
        ),
        # 同一次回答的"来源校验是否通过"也要落库：它是排障与将来收紧策略的样本
        Index("ix_chat_messages_session_seq", "session_id", "seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: 消息次序。用 `MAX(seq) + 1` 在**同一事务**里算 —— 与 M10 的
    #: `payments.attempt_no` 同一个套路（并发下由唯一约束兜底）。
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 引用来源快照（不可变）。没有引用时是 `NULL` 而不是 `[]` ——
    #: "这条回答没有来源"与"有来源但列表为空"是同一件事，但 NULL 让
    #: "这是不是一条助手消息"这类判断更干净。
    sources: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB, nullable=True
    )
    #: 溯源校验发现的问题（空 = 通过）。**落库而不是只写日志**：
    #: 计划里定的第一版策略是"不阻断，但把问题记下来"，
    #: 记在库里才能积累样本、决定将来是重试还是拒答。
    citation_problems: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    #: 检索最高分与"是否调用了模型" —— 验收项"检索为空时日志里能看到未调用模型"
    #: 在界面与库里的落点。
    top_score: Mapped[float | None] = mapped_column(nullable=True)
    used_model: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
