"""invoice void - M10 8.3

Revision ID: a445e844f29a
Revises: 6e360e408435
Create Date: 2026-10-05 20:17:11.039524

第一行刻意留成 ASCII：Alembic 会把 docstring 首行打进日志，中文在 GBK 控制台上会乱码。

============================ 这次加三样东西，其中一样是"改约束" ============================
1. `invoices.status`（`ACTIVE` / `VOIDED`）+ CHECK：发票作废是**状态变更**，不是删行。
   删行会丢掉审计痕迹 —— 而"这张票曾经登记过、后来作废了"本身就是必须留存的
   事实（与 M9 的附件删除同一条口径：`ATTACHMENT_REMOVED` 也进时间线）。

2. **把 `UNIQUE(request_id, invoice_no)` 换成部分唯一索引**
   `uq_invoice_active_no ... WHERE status = 'ACTIVE'`。

   为什么要换：作废之后**发票号应当可以重新登记**。原来是普通唯一约束的话，
   "录错了发票号 → 作废 → 重新按正确的号登记"这条最正常的纠错路径会被挡住
   （作废的那行还占着号）。而部分索引仍然保证"**有效**发票里号不重复"——
   那才是真正要守的不变量。

3. `ix_invoices_request_active`：匹配要按"这张申请的有效发票"过滤 + 求和，
   索引带 `status` 让这条查询不必回表扫已作废的行。
   索引列顺序是 `(request_id, status)`：`request_id` 先（等值过滤选择性最高），
   `status` 后（第二个等值条件）。

============================ 为什么默认值是 ACTIVE ============================
`server_default='ACTIVE'` 让**已有的行**在迁移时自动成为有效发票 ——
迁移前登记的票显然都还没作废，这是唯一说得通的历史口径。
应用侧仍然显式传状态（不依赖默认值），默认值只服务于"存量数据"。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a445e844f29a'
down_revision: Union[str, Sequence[str], None] = '6e360e408435'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1) 状态列。已有行自动成为 ACTIVE（迁移前登记的票都还没作废）
    op.add_column(
        "invoices",
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="ACTIVE",
        ),
    )
    op.create_check_constraint(
        "ck_invoice_status_known",
        "invoices",
        "status IN ('ACTIVE', 'VOIDED')",
    )

    # 2) 换成部分唯一索引：**有效**发票里号不重复（作废的不占号）
    op.drop_constraint("uq_invoice_request_no", "invoices", type_="unique")
    op.create_index(
        "uq_invoice_active_no",
        "invoices",
        ["request_id", "invoice_no"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    # 3) 匹配用的过滤索引
    op.create_index(
        "ix_invoices_request_active",
        "invoices",
        ["request_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema.

    ⚠️ 回退时**作废的行会撞回普通唯一约束**（同一个号可能既有作废行又有有效行）。
    与 8.1–8.3 同一条口径：**迁移只负责结构，数据怎么办由人决定**。回退前先处理：

        -- 先删掉作废的行（它们的存在是"号可以被复用"的前提）：
        DELETE FROM invoices WHERE status = 'VOIDED';

    不自动删是因为删的是业务凭证 —— 那该由人确认，不该由迁移脚本顺手做掉。
    """
    op.drop_index("ix_invoices_request_active", table_name="invoices")
    op.drop_index("uq_invoice_active_no", table_name="invoices")
    op.create_unique_constraint(
        "uq_invoice_request_no", "invoices", ["request_id", "invoice_no"]
    )
    op.drop_constraint("ck_invoice_status_known", "invoices", type_="check")
    op.drop_column("invoices", "status")
