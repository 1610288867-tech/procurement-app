"""采购目录相关的表。

命名纪律：ORM 映射类一律 `*Record`，与 `domain/` 里的业务实体严格分开。
数据库对象和业务对象混用，早晚会出事。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.records.base import Base


class SupplierRecord(Base):
    """供应商 —— M1 的贯通载体。

    字段少、没有状态机、不依赖任何其他表，是最干净的模板。
    """

    __tablename__ = "suppliers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # M2：资质有效期。过期即不可选（用查询时过滤实现，不用定时任务）
    qualification_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # M2：停用原因。业务规则里要求停用必须留痕（审计需要）
    disabled_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # code 的唯一索引由 unique=True 建立；这个复合索引正好覆盖列表页的主查询
        #   SELECT ... WHERE active ORDER BY name
        Index("ix_suppliers_active_name", "active", "name"),
    )


class DepartmentRecord(Base):
    """部门。`parent_id` 指向自身，构成部门树。"""

    __tablename__ = "departments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=True
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_departments_active_name", "active", "name"),
        Index("ix_departments_parent", "parent_id"),
    )


class PurchaseCategoryRecord(Base):
    """采购品类。

    这是整个系统最"重"的一张主数据表：它同时承载**动态表单**和**审批策略**，
    M5 的审批链就是从 `approval_policy` 里算出来的。

    M2 只做存取；`approval_policy` 的语义解释留给 M5 的 `domain/rules.py`。
    """

    __tablename__ = "purchase_categories"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # 动态表单（JSON Schema 子集）与审批策略，都用 JSONB —— 将来才能用 PG 的 JSON 查询
    form_schema: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    approval_policy: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_categories_active_name", "active", "name"),)


class BudgetRecord(Base):
    """预算：部门 × 年度 × 品类。

    三个金额字段**一开始就全部建出来**（哪怕 M6 才用），
    分三次迁移改同一张表没必要。金额一律整数「分」。
    """

    __tablename__ = "budgets"
    __table_args__ = (
        # 少了这条唯一约束，M6 的可用余额就会算错
        UniqueConstraint("department_id", "year", "category_id", name="uq_budget_scope"),
        # 数据库兜底：应用层（M6 的条件更新）总有一条路径没想到。
        # 有它，超预留最多是「这行更新失败」，而不是「账面数字悄悄变成负数」。
        CheckConstraint(
            "amount_reserved >= 0 AND amount_spent >= 0 "
            "AND amount_reserved + amount_spent <= amount_total",
            name="ck_budget_not_overdrawn",
        ),
        Index("ix_budgets_department_year", "department_id", "year"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    department_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_categories.id", ondelete="RESTRICT"), nullable=False
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    # 单位：分
    amount_total: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    amount_reserved: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    amount_spent: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
