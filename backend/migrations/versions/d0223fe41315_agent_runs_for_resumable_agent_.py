"""agent runs for resumable agent execution m12 enhancement

Revision ID: d0223fe41315
Revises: 0e9f1c4a77b2
Create Date: 2026-10-15 21:26:54.944197

============================ 手工改过这一行：删掉夹带的无关改动 ============================
`alembic revision --autogenerate` 还生成了这一段：

    op.alter_column('knowledge_chunks', 'embedding',
                    existing_type=pgvector.sqlalchemy.vector.VECTOR(dim=512),
                    nullable=False)

**它和本次改动无关**，是模型（`Mapped[list[float]]` 非 Optional）与库（列可空）
之间早已存在的漂移，autogenerate 每次都会把它带出来。

为什么必须删掉而不是顺手带上：
  ① 它会在 `upgrade` 里对**已有 37 行数据**的表加 NOT NULL —— 一旦哪一行是 NULL，
     迁移当场失败，而失败信息指向 `knowledge_chunks`，与"加 Agent 运行表"毫无关系；
  ② 一次提交只做一件事：夹带会让 `git log` 上的这个修订无法解释；
  ③ 它带着 `pgvector.sqlalchemy` 的 import —— 那是**另一条依赖路径**，
     不该为了一个无关改动被引进来。
  ④ 真要收紧这一列，应当单独一条迁移，并在里面**先清理 NULL 再改约束**。

（`knowledge_chunks` 上模型与库的这处漂移**仍然存在**，已如实记进 RISKS。）
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd0223fe41315'
down_revision: Union[str, Sequence[str], None] = '0e9f1c4a77b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('agent_runs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('session_id', sa.UUID(), nullable=False),
    sa.Column('actor_id', sa.UUID(), nullable=False),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('trace', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('stop_reason', sa.String(length=32), nullable=True),
    sa.Column('answer', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('FAILED', 'FINISHED', 'INTERRUPTED', 'RUNNING')", name='ck_agent_run_status_known'),
    sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['session_id'], ['chat_sessions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_agent_runs_actor_status', 'agent_runs', ['actor_id', 'status'], unique=False)
    op.create_index('ix_agent_runs_session_created', 'agent_runs', ['session_id', 'created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_agent_runs_session_created', table_name='agent_runs')
    op.drop_index('ix_agent_runs_actor_status', table_name='agent_runs')
    op.drop_table('agent_runs')
