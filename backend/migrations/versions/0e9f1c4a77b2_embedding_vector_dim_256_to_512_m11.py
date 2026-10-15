"""embedding vector dim 256 -> 512 (M11 enhancement)

Revision ID: 0e9f1c4a77b2
Revises: 0d682af81117
Create Date: 2026-10-14

把 `knowledge_chunks.embedding` 从 `vector(256)` 改成 `vector(512)`，
并把 `knowledge_docs.embedding_model` 从 `local-hash-256` 迁到新的向量空间。

============================ 这里有一个必须正视的问题 ============================
**256 维的向量没法"变成"512 维。** 维度是向量空间的形状，
不是可以补齐的东西（补零会得到一个与任何真实 512 维向量都近似正交的向量，
检索结果变成噪声）。所以这次迁移的做法是：

    ① 丢掉旧向量（置 `NULL`）—— 它们属于**旧的向量空间**，本来就不该再用；
    ② 列维度改成 512，`NOT NULL` 放开（允许"有正文但暂时没有向量"）；
    ③ HNSW 索引重建。

============================ 为什么放开 NOT NULL（而不是拒绝迁移）============================
`embedding` 原本是 `NOT NULL`，因为"有片段就必须有向量"。但迁移之后会出现一个
**过渡态**：片段正文还在（`content` 列），向量是 `NULL`。此时：

    · 检索按 `embedding IS NOT NULL` 过滤 → 这些片段**不会被检索到**（安全）；
    · 想恢复就是把文档**重新上传同一版本**（切分与向量都会重算）。

保留 `NOT NULL` 的话这次迁移只能"删掉所有片段"，那会连带删掉正文、
用户必须重新找原文。**留着正文、丢向量**是损失最小的做法。

============================ 为什么不用一次 UPDATE 重算向量 ============================
重算需要调用 embedding 模型，而**迁移脚本里不该发网络请求**：
它要能在没有模型的机器上跑（CI、别人的机器），
也应该在几毫秒内完成、而不是卡在一次 HTTP 超时上。
所以重算交给"重新上传文档"这个显式动作，而不是藏在迁移里。
"""  # noqa: D400

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0e9f1c4a77b2"
down_revision: Union[str, Sequence[str], None] = "0d682af81117"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 迁移前的向量空间标识与维度（见 `domain/embedding.py` 的历史值）
OLD_MODEL = "local-hash-256"
OLD_DIM = 256
#: 迁移后的
NEW_DIM = 512


def upgrade() -> None:
    """Upgrade schema."""
    # ① HNSW 索引必须**先删**：它建在 `embedding` 上，
    #    改列类型时索引会让 ALTER 失败（索引的运算符类绑定了具体维度）。
    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_embedding_hnsw")

    # ② 丢掉旧向量。**这一步是"丢数据"，但它丢的是已经不可用的东西**：
    #    256 维向量在任何 512 维向量空间里都没有意义。
    #    正文（`content`）保留 —— 重新上传即可重算向量。
    op.execute("UPDATE knowledge_chunks SET embedding = NULL")

    # ③ 改维度，并放开 NOT NULL（见文件头的说明：允许"有正文、暂时无向量"的过渡态）
    op.execute(
        f"ALTER TABLE knowledge_chunks "
        f"ALTER COLUMN embedding TYPE vector({NEW_DIM}) USING NULL, "
        f"ALTER COLUMN embedding DROP NOT NULL"
    )

    # ④ 重建 HNSW 索引。参数与迁移前**保持一致**（m=16, ef_construction=64）——
    #    这次迁移的目的是换维度，不是顺手调索引参数；
    #    混在一起会让"效果变了"归因不清（索引参数与 embedding 同时变）。
    op.execute(
        "CREATE INDEX ix_knowledge_chunks_embedding_hnsw "
        "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )

    # ⑤ 把文档的向量空间标识改到新的。
    #    ⚠️ 这些文档此刻**没有向量**（刚被置空），所以把它们标成新模型名
    #    会有点"名不副实" —— 但这是**正确的**：检索按 `embedding_model` 过滤，
    #    而它们的向量确实是空的、也检索不到（`IS NOT NULL` 那条兜住了）。
    #    标成新名字之后，重新上传同一版本会走"新版本覆盖"的正常路径。
    op.execute(
        f"UPDATE knowledge_docs SET embedding_model = 'local-hash-{NEW_DIM}' "
        f"WHERE embedding_model = '{OLD_MODEL}'"
    )


def downgrade() -> None:
    """Downgrade schema.

    ⚠️ **这个 downgrade 不能恢复数据**（旧向量在 upgrade 里已经丢了）。
    它只把结构退回 256 维，让"降级代码版本"这一步不至于因为列类型不符而崩。
    退回去之后那些片段**仍然是空的向量**，需要重新上传文档。
    """
    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_embedding_hnsw")
    op.execute("UPDATE knowledge_chunks SET embedding = NULL")
    op.execute(
        f"ALTER TABLE knowledge_chunks "
        f"ALTER COLUMN embedding TYPE vector({OLD_DIM}) USING NULL"
    )
    op.execute(
        "CREATE INDEX ix_knowledge_chunks_embedding_hnsw "
        "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )
    op.execute(
        f"UPDATE knowledge_docs SET embedding_model = '{OLD_MODEL}' "
        f"WHERE embedding_model = 'local-hash-{NEW_DIM}'"
    )
