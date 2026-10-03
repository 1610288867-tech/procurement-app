"""账号与角色的数据访问（M3）。

纪律同其他仓库：**只 flush()，不 commit()** —— 事务边界在用例层。
`freeze` 用条件更新（`active` 一起进 WHERE），不是"先查再写"。
"""

import uuid
from collections.abc import Collection
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ConflictError
from app.infrastructure.records.auth import UserRecord, UserRoleRecord


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def find_by_username(self, username: str) -> UserRecord | None:
        return (
            await self.session.scalars(
                select(UserRecord).where(UserRecord.username == username)
            )
        ).one_or_none()

    async def find_by_id(self, user_id: Any) -> UserRecord | None:
        return await self.session.get(UserRecord, user_id)

    async def roles_of(self, user_id: Any) -> list[str]:
        roles = await self.session.scalars(
            select(UserRoleRecord.role).where(UserRoleRecord.user_id == user_id)
        )
        return list(roles.all())

    async def find_by_role(
        self,
        role: str,
        *,
        department_ids: Collection[uuid.UUID] | None = None,
    ) -> list[UserRecord]:
        """按角色找**可用**的审批候选人（M6）。

        `department_ids` 给了就限定在这些部门里；不给则是全局。

        ============================ 为什么要区分这两种 ============================
        `DEPT_MANAGER` / `DEPT_HEAD` 只能审**本部门（含子部门）**的单子 ——
        不限定的话，A 部门主管能审 B 部门的申请，而这是预算与职责的双重错位；
        `PROCUREMENT` / `FINANCE` / `GM` 的职责本来就是跨部门，必须全局找。

        条件里带 `active.is_(True)`：冻结的账号不该出现在候选人里 ——
        给他派任务等于让申请卡在一个不会有人处理的人手上。
        """
        statement = (
            select(UserRecord)
            .join(UserRoleRecord, UserRoleRecord.user_id == UserRecord.id)
            .where(UserRoleRecord.role == role, UserRecord.active.is_(True))
            # 第二排序键不能省：同一事务里建的账号 `created_at` 完全相同
            # （PostgreSQL 的 now() 是**事务开始时间**），只按时间排会得到不确定的顺序，
            # 于是"这一级派给谁"在测试里时对时错 —— M6 在事件排序上踩过同一个坑。
            .order_by(UserRecord.created_at, UserRecord.id)
        )
        if department_ids is not None:
            statement = statement.where(UserRecord.department_id.in_(department_ids))
        return list((await self.session.scalars(statement)).all())

    async def create_user(
        self,
        *,
        username: str,
        password_hash: str,
        full_name: str,
        department_id: uuid.UUID | None = None,
    ) -> UserRecord:
        """重名交给数据库唯一约束判定，不做"先查再插"。"""
        record = UserRecord(
            username=username,
            password_hash=password_hash,
            full_name=full_name,
            department_id=department_id,
        )
        self.session.add(record)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise ConflictError(f"用户名已存在：{username}") from exc
        return record

    async def add_role(self, *, user_id: uuid.UUID, role: str) -> None:
        self.session.add(UserRoleRecord(user_id=user_id, role=role))
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise ConflictError(f"该用户已经有角色 {role}") from exc

    async def freeze(self, user_id: uuid.UUID) -> bool:
        """冻结账号：`active=False` 并且 `token_version += 1`。

        两件事必须在同一条 UPDATE 里：少了后半句，冻结之前签发的令牌还能用
        到它自己过期为止（最长 7 天）。条件更新保证重复冻结不会把版本号
        越加越大，也保证它和并发解冻不会互相覆盖。
        """
        statement = (
            update(UserRecord)
            .where(UserRecord.id == user_id, UserRecord.active.is_(True))
            .values(active=False, token_version=UserRecord.token_version + 1)
            .returning(UserRecord.id)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none() is not None
