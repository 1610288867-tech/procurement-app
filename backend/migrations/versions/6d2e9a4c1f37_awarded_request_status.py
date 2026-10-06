"""awarded request status - M10 8.1

Revision ID: 6d2e9a4c1f37
Revises: 415a3b8cd7d6
Create Date: 2026-10-05

第一行刻意留成 ASCII：Alembic 会把 docstring 首行打进日志，中文在 GBK 控制台上会乱码。

============================ 这条迁移是"实测撞出来"的 ============================
`purchase_requests.status` 上有一条 CHECK `ck_request_status_known`，
它是在 M5 建的（当时只有四个状态），M6 加 `SOURCING` / `REJECTED` 时换过一次。
M10 的定标要把主单推到 `AWARDED` —— **如果忘了换这条 CHECK**，会撞：

    CheckViolationError: new row for relation "purchase_requests"
    violates check constraint "ck_request_status_known"

而它经过应用层的 `SQLAlchemyError` 处理器之后，返回给用户的是
**503「数据库暂不可用，请稍后重试」** —— 完全指错方向（数据库好得很，
是状态白名单少了一个值）。所以这条约束更新必须与"新增状态"同一次改动：
模型里的 `KNOWN_REQUEST_STATUSES` 与这里一起改。

============================ 为什么不用 autogenerate ============================
Alembic **检测不到 CHECK 约束的内容变化**（它只比对约束的名字与存在性）。
所以这里手写：先 drop 旧的、再建新的。`downgrade` 换回不含 `AWARDED` 的那一版 ——
但**先要把已经处于 AWARDED 的行处理掉**，否则加回旧约束会失败。
处理方式写在 `downgrade` 的注释里。
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '6d2e9a4c1f37'
down_revision: Union[str, Sequence[str], None] = '415a3b8cd7d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONSTRAINT = "ck_request_status_known"

#: 旧（M6 之后）与新的白名单。字面量与 `records/purchase.KNOWN_REQUEST_STATUSES`
#: 保持一致 —— 迁移里**不 import 应用代码**（迁移要能在任意历史版本下跑）。
_OLD = ("DRAFT", "IN_APPROVAL", "DISCARDED", "WITHDRAWN", "SOURCING", "REJECTED")
_NEW = (*_OLD, "AWARDED")


def _check(statuses: tuple[str, ...]) -> str:
    return "status IN (" + ", ".join(f"'{value}'" for value in statuses) + ")"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(CONSTRAINT, "purchase_requests", type_="check")
    op.create_check_constraint(CONSTRAINT, "purchase_requests", _check(_NEW))


def downgrade() -> None:
    """Downgrade schema.

    ⚠️ 回退前**必须先把 AWARDED 的行挪走**，否则新约束加不上（已有行违反它）。
    这里不擅自改业务数据 —— 迁移只负责结构，数据怎么办由人决定：

        -- 回退前先执行（按你的口径选一条）：
        UPDATE purchase_requests SET status = 'SOURCING' WHERE status = 'AWARDED';

    这样写是刻意的：**自动把 AWARDED 改回 SOURCING 会静默抹掉"已定标"这个事实**，
    而它已经写进了事件流与报价表 —— 结构与历史数据不该由迁移脚本擅自对齐。
    """
    op.drop_constraint(CONSTRAINT, "purchase_requests", type_="check")
    op.create_check_constraint(CONSTRAINT, "purchase_requests", _check(_OLD))
