"""账号与角色的表（M3）。

`UserRecord` 是认证的唯一事实来源，`UserRoleRecord` 是多对多关系
（一个用户可持多个角色，比如 ADMIN 兼 GM）。

`token_version` 是这一层最值得注意的字段：**冻结账号时把它 +1**，
签发和校验时都比对它，旧令牌立刻就失效了 —— 不需要黑名单，
也不需要再做一遍 refresh rotation。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.records.base import Base


class UserRecord(Base):
    """账号。

    **只存哈希，不存口令原文**（Argon2id，见 infrastructure/security.py）。
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # argon2id 的哈希串约 97 字符，留到 200 够用且不浪费
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # 部门决定"本部门可见"的范围；系统级账号（如管理员）可以为空
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=True
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 冻结账号 = active 置 false + 这里 +1，旧令牌立刻失效
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # M5 解析审批人时要按部门找人（"这个部门的 DEPT_MANAGER 是谁"）
        Index("ix_users_department", "department_id"),
    )


class UserRoleRecord(Base):
    """用户 ↔ 角色。复合主键天然防重复挂同一个角色。"""

    __tablename__ = "user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        # 用户没了，他的角色挂载关系跟着走（从属数据，不是独立聚合）
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(32), primary_key=True)
