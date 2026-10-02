"""budgets: CHECK constraint not overdrawn

Revision ID: 9c4e2b7f1a05
Revises: 3f5c9a1d7e42
Create Date: 2026-10-02 21:10:00.000000

第一行刻意留成 ASCII：Alembic 会把 docstring 首行打进日志，中文在 GBK 控制台上会乱码。

为什么补这条约束（`docs/ER-图.md` 早就写了，代码里一直缺）：

  `amount_reserved >= 0 AND amount_spent >= 0
   AND amount_reserved + amount_spent <= amount_total`

  M6 的预算预留靠应用层的条件更新（`WHERE amount_total - (reserved + spent) >= :amount`）
  保证不超支。但应用层**总有一条路径没想到**：漏写这个守卫、或者将来多一条
  手动调整预算的入口，超支就会**安静地发生**——账面允许花掉比总额更多的钱。

  这条 CHECK 是最后一层兜底：真超了，那一行 UPDATE 直接失败，而不是数字悄悄变负。

加之前已核对：库中现有 budgets 行满足该条件（0 行违反），所以 `upgrade` 不会失败。
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '9c4e2b7f1a05'
down_revision: Union[str, Sequence[str], None] = '3f5c9a1d7e42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONSTRAINT_NAME = "ck_budget_not_overdrawn"
CONDITION = (
    "amount_reserved >= 0 AND amount_spent >= 0 "
    "AND amount_reserved + amount_spent <= amount_total"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_check_constraint(CONSTRAINT_NAME, "budgets", CONDITION)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(CONSTRAINT_NAME, "budgets", type_="check")
