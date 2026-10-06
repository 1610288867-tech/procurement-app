"""采购申请相关的四张表（M5）。

命名纪律同其他 records：ORM 映射类一律 `*Record`，与 `domain/` 的业务对象分开。

============================ 四张表各自的"不许" ============================
    purchase_requests     主单。**金额是算出来的**（后端按明细重算），状态只能经状态机改
    purchase_items        明细行。行金额同样算出来；`line_no` 由后端按顺序生成
    request_events        **只追加**。任何 UPDATE / DELETE 都是 bug
    idempotency_records   幂等占位。`(actor_id, scope, key)` 唯一

============================ 两个容易写错的地方 ============================
1. **`key` 在 PostgreSQL 里是保留字**。用 `quoted_name` 标出来，否则生成的 SQL
   会变成 `insert ... (key, ...)` 而报语法错 —— 手写迁移时也必须写 `"key"`。
2. **金额列一律 BigInteger（单位：分）**。`amount_total` 上加 CHECK（>= 0）：
   金额算错时宁可让写入失败，也不要让一条负数金额的单子进审批流。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import quoted_name

from app.domain.enums import RequestStatus
from app.infrastructure.records.base import Base

# 主单状态的白名单。**每加一个状态，这里与迁移里的 CHECK 必须一起改**
# （同一次改动，否则写入新状态会撞 CHECK —— M10 加 AWARDED 时实测撞到过，
#  报出来的是 503 "数据库暂不可用"，指错方向）。
# 所以它是常量，不是散在 SQL 里的字面量。
KNOWN_REQUEST_STATUSES = (
    RequestStatus.DRAFT.value,
    RequestStatus.IN_APPROVAL.value,
    RequestStatus.DISCARDED.value,
    RequestStatus.WITHDRAWN.value,
    RequestStatus.SOURCING.value,  # M6
    RequestStatus.REJECTED.value,  # M6
    RequestStatus.AWARDED.value,  # M10 / 8.1
    RequestStatus.ORDERED.value,  # M10 / 8.2
    RequestStatus.RECEIVED.value,  # M10 / 8.2
    RequestStatus.INVOICED.value,  # M10 / 8.3
    RequestStatus.PAID.value,  # M10 / 8.4
    RequestStatus.CLOSED.value,  # M10 / 8.4
)


class PurchaseRequestRecord(Base):
    """采购申请主单。"""

    __tablename__ = "purchase_requests"
    __table_args__ = (
        CheckConstraint("amount_total >= 0", name="ck_request_amount_non_negative"),
        CheckConstraint(
            "status IN ("
            + ", ".join(f"'{status}'" for status in KNOWN_REQUEST_STATUSES)
            + ")",
            name="ck_request_status_known",
        ),
        # 列表页主查询：我的申请（按状态筛选、按时间倒序）
        Index("ix_requests_requester_status", "requester_id", "status"),
        # 部门/品类的可见范围过滤 + 排序
        Index(
            "ix_requests_department_category_status",
            "department_id",
            "category_id",
            "status",
        ),
        Index("ix_requests_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 人可读的单号，如 PR-2026-A1B2C3。唯一约束由数据库判定，代码里不做"先查再插"
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    requester_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    department_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_categories.id", ondelete="RESTRICT"), nullable=False
    )
    # 预算在 M7（预留）时才认领，所以现在可空。
    # 刻意**不加外键约束**：这一列是"将来指向哪条预算"的软引用，
    # 加 RESTRICT 会让预算行永远删不掉，而删除预算在管理端是合法操作。
    budget_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # 单位：分。**后端按明细重算**，不接受客户端传值
    amount_total: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    urgency: Mapped[str] = mapped_column(String(16), nullable=False, default="NORMAL")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RequestStatus.DRAFT.value
    )
    # 乐观锁：每次状态变更 +1；条件更新时和 status 一起进 WHERE
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 动态表单的填写结果（按品类的 form_schema 渲染出来的那些字段）
    form_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PurchaseItemRecord(Base):
    """申请明细行。"""

    __tablename__ = "purchase_items"
    __table_args__ = (
        # 同一张单子里行号不许重复 —— 顺序是明细的一部分
        UniqueConstraint("request_id", "line_no", name="uq_item_request_line"),
        CheckConstraint("quantity > 0", name="ck_item_quantity_positive"),
        CheckConstraint("unit_price > 0", name="ck_item_price_positive"),
        CheckConstraint("amount >= 0", name="ck_item_amount_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 申请删了，明细跟着走（从属数据，不是独立聚合）
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_requests.id", ondelete="CASCADE"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    spec: Mapped[str | None] = mapped_column(String(500), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    # 单位：分
    unit_price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)


class RequestEventRecord(Base):
    """事件流。**只追加，永不更新或删除。**

    这张表的价值在于"能还原完整历史"：时间线页面直接读它，
    排查问题时看它，将来做审计不用另建表。一旦允许改，这些价值同时归零。

    `payload` 放事件当时的补充信息（比如明细行数、金额），
    刻意**不设外键**到明细：明细会被覆盖式替换，事件必须留下当时的快照。

    ============================ 排序只认 `seq` ============================
    时间线的顺序是产品语义（"先发生什么"），所以它必须有一个**不受时钟影响**的排序键。
    `created_at` 做不到：同一事务里写的两条事件可能落在**同一微秒**上，
    这时按 `created_at` 排就退化成按 `id` 排 —— 而 `id` 是 uuid4，**随机**，
    于是同一份历史每次读出来的顺序都可能不同（实测撞到过 `REQUEST_SUBMITTED`
    与 `APPROVAL_STARTED` 颠倒）。

    `seq` 由数据库侧的单调序列发号：谁先落盘谁的值小，与时钟精度、并发、UUID 都无关。
    `created_at` 保留，但只负责**显示**，不参与排序。
    """

    __tablename__ = "request_events"
    __table_args__ = (
        # 时间线主查询：按申请取事件，按时间正序
        Index("ix_events_request_created", "request_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 只追加日志的单调序号（迁移 b7d3f0c41a26）。写入时**不要**给它赋值，
    # 值由数据库发；手工指定会打乱"先落盘者值小"这条保证。
    seq: Mapped[int] = mapped_column(BigInteger, Identity(start=1), nullable=False)
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_requests.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IdempotencyRecord(Base):
    """幂等记录。`(actor_id, scope, key)` 唯一。

    ============================ 三个字段各管什么 ============================
        scope         **端点标识**（如 `POST:/api/v1/requests/{id}/submit`）。
                      不留空、不随手写：scope 混了，同一把 key 会在"提交"和"撤回"
                      之间串味 —— 第二次请求拿到第一次的响应，而且看起来一切正常。
        payload_hash  同键**不同载荷**必须 409，靠它区分"重放"与"改主意"。
        status        IN_PROGRESS → COMPLETED / FAILED。
                      `IN_PROGRESS` 是并发占位的核心：两个请求同时到，
                      只有一个能插进去，另一个看见的就是"正在处理中"。
    """

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("actor_id", "scope", "key", name="uq_idempotency_scope"),
        Index("ix_idempotency_actor_created", "actor_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    scope: Mapped[str] = mapped_column(String(200), nullable=False)
    # `key` 是 PostgreSQL 保留字：不标 quoted_name 的话，生成出来的 SQL 会语法错
    key: Mapped[str] = mapped_column(
        quoted_name("key", quote=True), String(128), nullable=False
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # 存"上次执行成功后的响应体"，重放时原样返回
    response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
