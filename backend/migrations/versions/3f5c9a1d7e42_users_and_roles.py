"""users and user_roles - M3 accounts and roles

Revision ID: 3f5c9a1d7e42
Revises: 6a1b7479a762
Create Date: 2026-10-02 20:05:00.000000

第一行刻意留成 ASCII：Alembic 会把 docstring 首行打进日志，中文在 GBK 控制台上会乱码。

人工检查过：
  - users.username 唯一（登录靠它，重名必须由数据库来拦）
  - users.password_hash 只存 Argon2id 哈希，绝不存原文
  - users.token_version 用于冻结账号后让旧令牌立即失效
  - user_roles 复合主键 (user_id, role)，天然防重复；外键 ON DELETE CASCADE
    （角色挂载是从属数据，用户删了就该跟着走）
  - users.department_id 是 ON DELETE RESTRICT（不许删掉还有人在的部门）
  - ix_users_department 给 M5 解析审批人用
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3f5c9a1d7e42'
down_revision: Union[str, Sequence[str], None] = '6a1b7479a762'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('users',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('password_hash', sa.String(length=200), nullable=False),
    sa.Column('full_name', sa.String(length=200), nullable=False),
    sa.Column('department_id', sa.UUID(), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('token_version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['department_id'], ['departments.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('username')
    )
    op.create_index('ix_users_department', 'users', ['department_id'], unique=False)
    op.create_table('user_roles',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('role', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'role')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('user_roles')
    op.drop_index('ix_users_department', table_name='users')
    op.drop_table('users')
