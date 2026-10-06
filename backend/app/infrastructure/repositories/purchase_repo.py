"""采购申请的数据访问（M5）。

纪律同其他仓库：**只 flush()，不 commit()** —— 事务边界在用例层。

============================ 这个仓库唯一不能忘的事 ============================
**每个查询方法都必须收下 `RequestScope`**，并把可见范围写进 `WHERE`。

"能不能看"和"能不能用这个接口"是两套权限；前者漏了不会报错，
只会安静地把别人的申请查出来。所以这里不留任何"这次先不过滤"的口子 ——
想查申请，就得先说明自己在按什么范围查。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import RequestStatus
from app.domain.permissions import RequestScope
from app.errors import BusinessError, ConflictError
from app.infrastructure.records.purchase import (
    PurchaseItemRecord,
    PurchaseRequestRecord,
)

# 单号前缀。人可读，且一眼能看出是哪一年的单子。
_CODE_PREFIX = "PR"


def _deny_all():
    """恒假条件。

    用于"算不出可见范围"的兜底：宁可查不到，也不放行。
    **空条件在 SQLAlchemy 里等于不过滤**（全放出去），那才是最危险的写法。
    """
    return PurchaseRequestRecord.id.is_(None)


class PurchaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ 主单

    async def create_request(
        self,
        *,
        requester_id: uuid.UUID,
        department_id: uuid.UUID,
        category_id: uuid.UUID,
        title: str,
        reason: str | None,
        urgency: str,
        form_data: Mapping[str, Any],
    ) -> PurchaseRequestRecord:
        """建草稿。`amount_total` 先落 0 —— 明细存进来之后再重算。

        重号交给数据库唯一约束判定（不做"先查再插"）：撞了就换一个再试。
        单号是给人看的，撞号概率极低，但**低概率不等于不会发生**，
        所以这里真的重试，而不是"大概不会撞"。
        """
        last_error: IntegrityError | None = None
        for _ in range(5):
            record = PurchaseRequestRecord(
                code=self._new_code(),
                requester_id=requester_id,
                department_id=department_id,
                category_id=category_id,
                title=title,
                reason=reason,
                urgency=urgency,
                amount_total=0,
                status=RequestStatus.DRAFT.value,
                form_data=dict(form_data),
            )
            self.session.add(record)
            try:
                await self.session.flush()
            except IntegrityError as exc:
                # 整个事务已经被标记失败，必须先回滚到可用状态才能重试
                await self.session.rollback()
                last_error = exc
                continue
            return record
        raise ConflictError("单号生成冲突，请重试") from last_error

    @staticmethod
    def _new_code() -> str:
        """`PR-2026-XXXXXXXX`。随机后缀而不是自增：不需要额外的序列表，
        也不会因为并发插入而互相等待。"""
        return f"{_CODE_PREFIX}-{datetime.now(UTC).year}-{uuid.uuid4().hex[:8].upper()}"

    async def get_request(
        self,
        request_id: uuid.UUID,
        *,
        scope: RequestScope,
        actor_id: uuid.UUID,
        populate_existing: bool = False,
    ) -> PurchaseRequestRecord | None:
        """按 id 取，**同时限定可见范围**。

        范围外返回 `None`，由用例层翻译成 404 —— 不告诉调用方"这东西存在"。

        `actor_id` 是**必填**的：`only_self` 那一档必须拿它过滤。
        它曾经是可选的，结果漏传时条件退化成"恒假"，接口一律 404 ——
        这正是"权限兜底往窄里兜"的效果：不会越权，但会静默地什么都查不到。
        把它改成必填，漏传就在类型检查阶段暴露，而不是等到页面上什么都没有。

        `populate_existing=True` 用于**刚做完 bulk UPDATE 之后立刻回读**的场景
        （例如定标把主单推到 AWARDED）：条件更新用的是 `synchronize_session=False`，
        会话里那份对象还是旧值，而它已在身份映射里 —— 普通查询会把它原样还回来，
        于是响应显示"定标没生效"。这个坑在 M5 / M6 各踩过一次。
        """
        statement = select(PurchaseRequestRecord).where(
            PurchaseRequestRecord.id == request_id,
            *_scope_conditions(scope, actor_id=actor_id),
        )
        if populate_existing:
            statement = statement.execution_options(populate_existing=True)
        return (await self.session.scalars(statement)).one_or_none()

    async def list_requests(
        self,
        *,
        scope: RequestScope,
        actor_id: uuid.UUID,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[PurchaseRequestRecord], int]:
        """列表 + 总数。

        `scope` 决定看得到哪些行；`only_self` 时按**当前用户**过滤
        （REQUESTER 的"我的申请"就是这条）。
        """
        conditions = list(_scope_conditions(scope, actor_id=actor_id))
        if status is not None:
            conditions.append(PurchaseRequestRecord.status == status)

        total = (
            await self.session.execute(
                select(func.count()).select_from(PurchaseRequestRecord).where(*conditions)
            )
        ).scalar_one()

        statement = (
            select(PurchaseRequestRecord)
            .where(*conditions)
            .order_by(PurchaseRequestRecord.created_at.desc(), PurchaseRequestRecord.id)
            .limit(limit)
            .offset(offset)
        )
        rows = list((await self.session.scalars(statement)).all())
        return rows, int(total)

    async def apply_draft_patch(
        self,
        *,
        request_id: uuid.UUID,
        expected_version: int,
        changes: Mapping[str, Any],
    ) -> bool:
        """改草稿：**条件更新**，`version` 与 `status='DRAFT'` 一起进 WHERE。

        这就是乐观锁：受影响行数为 0 说明"版本过期"或"已经不是草稿了"，
        两者都该 409。写成"先查出来、比一下版本、再写回去"在并发下依然会冲突 ——
        两次查询之间别人可能已经改过了。
        """
        values: dict[str, Any] = dict(changes)
        values["version"] = PurchaseRequestRecord.version + 1
        values["updated_at"] = func.now()

        statement = (
            update(PurchaseRequestRecord)
            .where(
                PurchaseRequestRecord.id == request_id,
                PurchaseRequestRecord.version == expected_version,
                PurchaseRequestRecord.status == RequestStatus.DRAFT.value,
            )
            .values(**values)
            .returning(PurchaseRequestRecord.id)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def transition_status(
        self,
        *,
        request_id: uuid.UUID,
        expected_version: int,
        from_status: str,
        to_status: str,
        extra: Mapping[str, Any] | None = None,
    ) -> bool:
        """推进状态：`version` + `status` **一起**放进 `WHERE`。

        这样一次条件更新同时挡住两种冲突：
          - 版本过期（别人先改了）
          - 状态不对（比如已经提交过了，还想再提交）
        """
        values: dict[str, Any] = {
            "status": to_status,
            "version": PurchaseRequestRecord.version + 1,
            "updated_at": func.now(),
        }
        values.update(extra or {})

        statement = (
            update(PurchaseRequestRecord)
            .where(
                PurchaseRequestRecord.id == request_id,
                PurchaseRequestRecord.version == expected_version,
                PurchaseRequestRecord.status == from_status,
            )
            .values(**values)
            .returning(PurchaseRequestRecord.id)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none() is not None

    # ------------------------------------------------------------------ 明细

    async def list_items(self, request_id: uuid.UUID) -> list[PurchaseItemRecord]:
        statement = (
            select(PurchaseItemRecord)
            .where(PurchaseItemRecord.request_id == request_id)
            .order_by(PurchaseItemRecord.line_no)
        )
        return list((await self.session.scalars(statement)).all())

    async def replace_items(
        self,
        *,
        request_id: uuid.UUID,
        expected_version: int,
        lines: Sequence[Any],
    ) -> bool:
        """**覆盖式**保存明细：先删后插，整批替换。

        为什么是覆盖式而不是逐行增删改：明细是一个整体（行号、顺序、合计都相关），
        逐行接口会让"改到一半"成为可能的状态。覆盖式只有两个结果：要么全对，要么全不动。

        `lines` 收的是 `domain/purchase.py` 的 `LineDraft`（这里只用它的字段，
        所以标注成 `Any` 的序列即可 —— 仓库不需要知道那个类型）。
        """
        # 只有草稿能改明细。这一条必须先判断，否则会出现"改了明细但主单没动"的半截状态
        owner = (
            await self.session.scalars(
                select(PurchaseRequestRecord.status).where(
                    PurchaseRequestRecord.id == request_id,
                    PurchaseRequestRecord.version == expected_version,
                )
            )
        ).one_or_none()
        if owner is None or owner != RequestStatus.DRAFT.value:
            return False

        await self.session.execute(
            delete(PurchaseItemRecord).where(PurchaseItemRecord.request_id == request_id)
        )
        for line in lines:
            self.session.add(
                PurchaseItemRecord(
                    request_id=request_id,
                    line_no=line.line_no,
                    name=line.name,
                    spec=line.spec,
                    quantity=line.quantity,
                    unit_price=line.unit_price_fen,
                    amount=line.amount_fen,
                )
            )

        total = sum(line.amount_fen for line in lines)
        # 金额与明细在**同一个事务**里更新：题面金额和明细永远不会对不上
        patched = await self.apply_draft_patch(
            request_id=request_id,
            expected_version=expected_version,
            changes={"amount_total": total},
        )
        if not patched:  # pragma: no cover - 上面刚确认过版本与状态
            raise BusinessError("明细保存时状态已变化，请刷新后重试", status_code=409)
        return True


def _scope_conditions(scope: RequestScope, *, actor_id: uuid.UUID | None = None):
    """把可见范围翻译成 `WHERE` 条件。

    这是"权限必须写进 WHERE"的**唯一**落点：所有查询都从这里取条件，
    改规则只需要改这一个函数。

    三条兜底都往**窄**里兜：算不出范围时返回一个恒假条件（查不到），
    而不是返回空条件（等于不过滤）。空条件在 SQLAlchemy 里就是"全放出去"，
    而那正是最危险的那种写法 —— 它不会报错。
    """
    if scope.sees_all:
        return []
    if scope.only_self:
        if actor_id is None:
            return [_deny_all()]
        return [PurchaseRequestRecord.requester_id == actor_id]
    if not scope.department_ids:
        return [_deny_all()]
    return [PurchaseRequestRecord.department_id.in_(scope.department_ids)]
