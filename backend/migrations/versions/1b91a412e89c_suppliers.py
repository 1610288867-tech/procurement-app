"""suppliers - M1 skeleton carrier table

Revision ID: 1b91a412e89c
Revises:
Create Date: 2026-10-02 16:35:39.823520

第一行刻意留成 ASCII：Alembic 会把 docstring 首行打进日志，
中文在 GBK 控制台上会乱码。下面这些说明不会被打印，可以放心写中文。

autogenerate 之后已人工检查过：
  - id 是 UUID 主键（客户端生成，库里无默认值）
  - code 有唯一约束（重码由数据库判定，代码里不做"先查再插"）
  - created_at 是 timestamptz + server_default now()
  - ix_suppliers_active_name 覆盖列表页主查询 (WHERE active ORDER BY name)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1b91a412e89c'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('suppliers',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('code', sa.String(length=64), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code')
    )
    op.create_index('ix_suppliers_active_name', 'suppliers', ['active', 'name'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_suppliers_active_name', table_name='suppliers')
    op.drop_table('suppliers')
