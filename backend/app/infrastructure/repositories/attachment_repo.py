"""附件的数据访问（M9）。

纪律同其他仓库：**只 flush()，不 commit()** —— 事务边界在用例层。
区别在于这个仓库背后还有**磁盘上的一份文件**：落盘发生在用例层
（`PurchaseCoordinator.upload_attachment`），所以"库回滚了、文件还在"
这件事必须在那边处理掉，见 `object_store.LocalObjectStore.new_key` 的说明。

============================ 软删除 ============================
`delete` 方法**刻意不提供**。附件是审计证据，物理删除等于抹掉"当时附了什么"。
唯一改状态的方式是 `soft_delete`（写 `deleted_at`），
而列表与读取都带 `deleted_at IS NULL` —— 删掉的东西不会从别的口子漏出来。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.records.attachment import AttachmentRecord


class AttachmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        request_id: uuid.UUID,
        kind: str,
        original_filename: str,
        content_type: str,
        size_bytes: int,
        sha256: str,
        storage_key: str,
        uploaded_by: uuid.UUID,
    ) -> AttachmentRecord:
        record = AttachmentRecord(
            request_id=request_id,
            kind=kind,
            original_filename=original_filename,
            content_type=content_type,
            size_bytes=size_bytes,
            sha256=sha256,
            storage_key=storage_key,
            uploaded_by=uploaded_by,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def get(self, attachment_id: uuid.UUID) -> AttachmentRecord | None:
        """按 id 取**有效**附件。已软删除的一律当不存在（返回 None）。

        为什么把"已删除"混进"不存在"：调用方据此统一回 404，
        不会因为"这条被删过"而泄露它存在过。
        """
        statement = select(AttachmentRecord).where(
            AttachmentRecord.id == attachment_id,
            AttachmentRecord.deleted_at.is_(None),
        )
        return (await self.session.scalars(statement)).one_or_none()

    async def list_for_request(self, request_id: uuid.UUID) -> list[AttachmentRecord]:
        """某张申请下的有效附件，**新的在前**（列表页最常见的诉求）。"""
        statement = (
            select(AttachmentRecord)
            .where(
                AttachmentRecord.request_id == request_id,
                AttachmentRecord.deleted_at.is_(None),
            )
            .order_by(AttachmentRecord.created_at.desc(), AttachmentRecord.id)
        )
        return list((await self.session.scalars(statement)).all())

    async def soft_delete(self, attachment_id: uuid.UUID) -> bool:
        """标记删除。返回是否**真的**改到了行（已经是删除态 → False）。

        条件更新：`deleted_at IS NULL` 写进 `WHERE`，受影响行数 != 1 就当失败。
        两个人同时点删除时只有一个会拿到 True —— 与全项目的并发纪律一致
        （"先查再写"在这里的后果是两条 UPDATE 都报告成功）。
        """
        statement = (
            update(AttachmentRecord)
            .where(
                AttachmentRecord.id == attachment_id,
                AttachmentRecord.deleted_at.is_(None),
            )
            .values(deleted_at=datetime.now(UTC))
            .returning(AttachmentRecord.id)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none() is not None
