"""master data - departments / categories / budgets + supplier columns

Revision ID: 6a1b7479a762
Revises: 1b91a412e89c
Create Date: 2026-10-02 19:00:24.426018

autogenerate 之后已人工检查过：
  - budgets 有 uq_budget_scope（部门 × 年度 × 品类），少了它 M6 的可用余额会算错
  - budgets 的两个外键都是 ON DELETE RESTRICT（部门/品类删不掉，避免预算悬空）
  - form_schema / approval_policy 用 JSONB，将来才能用 PostgreSQL 的 JSON 查询
  - suppliers 新增 qualification_expires_at / disabled_reason，都是可空列，不影响既有数据
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '6a1b7479a762'
down_revision: Union[str, Sequence[str], None] = '1b91a412e89c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('departments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('code', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('parent_id', sa.UUID(), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['parent_id'], ['departments.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code')
    )
    op.create_index('ix_departments_active_name', 'departments', ['active', 'name'], unique=False)
    op.create_index('ix_departments_parent', 'departments', ['parent_id'], unique=False)
    op.create_table('purchase_categories',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('code', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('form_schema', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('approval_policy', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code')
    )
    op.create_index('ix_categories_active_name', 'purchase_categories', ['active', 'name'], unique=False)
    op.create_table('budgets',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('department_id', sa.UUID(), nullable=False),
    sa.Column('category_id', sa.UUID(), nullable=False),
    sa.Column('year', sa.Integer(), nullable=False),
    sa.Column('amount_total', sa.BigInteger(), nullable=False),
    sa.Column('amount_reserved', sa.BigInteger(), nullable=False),
    sa.Column('amount_spent', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['category_id'], ['purchase_categories.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['department_id'], ['departments.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('department_id', 'year', 'category_id', name='uq_budget_scope')
    )
    op.create_index('ix_budgets_department_year', 'budgets', ['department_id', 'year'], unique=False)
    op.add_column('suppliers', sa.Column('qualification_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('suppliers', sa.Column('disabled_reason', sa.String(length=200), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('suppliers', 'disabled_reason')
    op.drop_column('suppliers', 'qualification_expires_at')
    op.drop_index('ix_budgets_department_year', table_name='budgets')
    op.drop_table('budgets')
    op.drop_index('ix_categories_active_name', table_name='purchase_categories')
    op.drop_table('purchase_categories')
    op.drop_index('ix_departments_parent', table_name='departments')
    op.drop_index('ix_departments_active_name', table_name='departments')
    op.drop_table('departments')
