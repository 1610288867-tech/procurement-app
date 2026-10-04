"""附件表（M9）。

与采购主单、审批表分开放一个文件：它是**从属数据**（跟着申请走），
而且它的关注点完全不同 —— 前几张表关心的是状态与金额，
这张表关心的是"这串字节是什么、存在哪儿、谁传的"。

============================ 一条不许破的纪律 ============================
`storage_key` 是**服务端生成的 UUID 键**（见 `infrastructure/object_store.py`），
`original_filename` 只是给人看的元数据。两者的区别就是"能不能拿它拼路径"：

    storage_key          `ab/abcdef….pdf`    可以拼路径
    original_filename    `../../main.py`      永远只存不用

谁把 `original_filename` 拿去拼路径，谁就制造了一个路径穿越漏洞。

============================ 删除是软删除 ============================
`deleted_at` 为空 = 有效。附件是审计证据（报价单、合同、发票），
物理删除会让"当时到底附了什么"永远查不出来。
计划里第 8 条"常见坑"写的就是这一条：**用软删除标记，保留审计痕迹**。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import ATTACHMENT_KINDS
from app.infrastructure.records.base import Base


class AttachmentRecord(Base):
    __tablename__ = "attachments"
    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind}'" for kind in ATTACHMENT_KINDS) + ")",
            name="ck_attachment_kind_known",
        ),
        # 空文件不该被存下来：它既没有信息量，又会让"附件数"这个指标说谎
        CheckConstraint("size_bytes > 0", name="ck_attachment_size_positive"),
        # 列表主查询：按申请取附件，新的在前
        Index("ix_attachments_request_created", "request_id", "created_at"),
        # 按摘要查：将来做"同一份文件重复上传"提示 / 秒传时用得上。
        # 现在不加唯一约束 —— 同一张发票在不同申请下各传一份是**合法**的。
        Index("ix_attachments_sha256", "sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # CASCADE：附件是申请的从属数据（同 request_events / budget_transactions 的处理）
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_requests.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 用户看到的原始文件名。**只存不用**（绝不参与拼路径）。
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    #: 入库时校验过的那一个。下载响应的 Content-Type 用它，**不用请求里的**。
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    #: SHA-256（64 位十六进制）。留作完整性核对与将来的秒传。
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 服务端生成的存储键（`ab/abcdef….pdf`）。**唯一**：撞了就说明随机数出了问题。
    storage_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    # RESTRICT：传过附件的人不许被删（审计要求"谁传的"必须留得住）
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: 软删除标记：非空 = 已删除（不再出现在列表与下载里）
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
