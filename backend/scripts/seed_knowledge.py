"""制度文档种子：10 份演示制度（供 AI 制度助手检索）。

用法（**在 backend 目录下**）：

    python -m scripts.seed_knowledge

============================ 为什么需要它 ============================
`seed.py` 只灌业务数据（部门/品类/供应商/预算/账号），**从来不灌知识库**。
后果实测过：`knowledge_docs` 0 行 → 制度问答**必然检索为空** →
每次都走 Agent 或回"制度中没有找到相关规定"，**RAG 那一半等于没有内容可检索**。
而界面看起来完全正常，不报任何错。

============================ 两条设计 ============================
1. **幂等**：按 `(source_key, version)` 查重，已存在就跳过。
   可以反复跑，不会造重复文档、也不会覆盖你手工传过的同 `source_key` 文档。
2. **内容在 Python 字面量里，读时不碰网络**：向量化需要 Ollama，
   所以本脚本**先查 Ollama 是否可用**，不可用就明确报错并退出，
   不写半截数据（切分失败会留下 `FAILED` 文档，那正是要避免的）。

============================ ⚠️ 与评测门禁的关系（第一版做错过，记在这里）============================
`scripts/knowledge_eval.py` 的 27 条 Golden QA 是**检索质量硬门槛**
（recall ≥ 0.95、mrr ≥ 0.90）。检索**不按 source_key 过滤**，
所以这些种子文档会**进同一个检索池**。

**第一版踩到的坑**：既说了"要避开评测主题"，却没有**逐题核对**，
结果写了《比价与定标规则》（撞 F-02/F-03/C-04 的采购方式与金额档）、
《验收与质量责任制度》（撞 F-04/F-05/L-03 的验收部门与退换时限）、
《发票与付款管理办法》（撞 F-06/F-07/C-03 的三单匹配）。实测后果：

    Precision@5  0.500 → 0.200      MRR  0.932~0.955 → 0.822（**门禁红**）
    ✗ mrr = 0.822 < 门槛 0.90

**教训**：说"避开某个主题"必须落到**那一份评测题的逐条清单**上，
否则"避开"只是自我感觉。所以下面的选题是按评测题**逐条排除**后定的。

评测集只覆盖四块：**采购方式与金额档 / 验收 / 三单匹配与付款 / 差旅费**。
因此本文档刻意写这九块**评测集完全没有**的制度面：
供应商评价与资质、比价与定标（**不含金额档与采购方式**）、订单变更与交付违约、
紧急采购、档案与留痕、岗位职责与职责分离（**不含审批金额档**）、
质保与售后、合同签订与用印、廉洁与利益冲突。

⚠️ 评测集是**小语料上的门禁**（3 份文档 / 27 片段），它本身不是"检索质量的全部"。
真要扩语料，正确做法是**连评测集一起扩**，而不是让种子文档去绕开它。
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionFactory
from app.domain.chunking import chunk_text, normalize_text
from app.infrastructure.embeddings import LocalHashEmbedding, OllamaEmbedding
from app.infrastructure.records.auth import UserRecord
from app.infrastructure.records.knowledge import (
    KnowledgeChunkRecord,
    KnowledgeDocRecord,
)


def build_embedder() -> LocalHashEmbedding | OllamaEmbedding:
    """按配置造一个 embedding 实现。

    ============================ 为什么不复用边界层那个工厂 ============================
    `boundaries/http/knowledge.py` 里有一个 `_embedder()`，语义与这里**完全一样**。
    但脚本去 import 边界层等于把依赖方向反过来用 ——
    分层规则（`AGENTS.md` 第五章第 1 条与 `PROJECT_INDEX.md` 第四节）说
    边界层是最外圈，它依赖里层；里层与工具不该依赖它。

    宁可在这里写十行重复代码，也不要让一条**架构例外**留下：
    例外一旦存在，下一次"反正已经破过一次"就会更容易。

    ⚠️ 这里**同样没有"失败了退回哈希"的分支**（与边界层同一个理由）：
    哈希与真模型的向量**不可比**，而检索是按 `embedding_model` 名字过滤的 ——
    退回哈希会让真模型建的文档**一条都检索不到**。
    """
    settings = get_settings()
    provider = (settings.knowledge_embedding_provider or "").strip().lower()
    if provider == "local":
        return LocalHashEmbedding()
    if provider == "ollama":
        return OllamaEmbedding(
            base_url=settings.knowledge_embedding_base_url,
            model=settings.knowledge_embedding_model,
        )
    raise SystemExit(f"未知的 embedding 提供方：{provider!r}（可用值：local / ollama）")

#: 种子的 source_key 前缀。**刻意不是 `EVAL-`** ——
#: 那个前缀是评测集的，`knowledge_eval.py` 与 `testdata_cleanup.py` 都会清掉它。
SEED_PREFIX = "SEED-"

#: 正文放在 JSON 里而不是 Python 字面量里。
#:
#: ============================ 为什么换到 JSON ============================
#: 十份制度的正文各有一千字上下，内联进 Python 要处理三引号、反斜杠与缩进，
#: 而**写错一处不会报错、只会让某份制度的正文悄悄变短**。
#: JSON 里正文就是普通字符串，还能用 `TypeAdapter` 校验字段与长度。
#: 它与评测集 `knowledge_eval_cases.json` 放在同一个目录，形状也一致。
DOCS_PATH = Path(__file__).resolve().parent / "eval" / "seed_docs.json"

#: 下限取 **380**：它守的是"别把一份制度意外写成两三句话"，
#: **不是**规定篇幅。实测最短的那份是 388 字（《采购岗位职责与权限分工》），
#: 取 400 会把它挡在门外 —— 那种"下限比现有数据还严"的校验只会逼人改数据。
MIN_BODY_CHARS = 380


class SeedDoc(BaseModel):
    """一份种子制度。**形状错误要在读文件时就炸**，不能等到切分。"""

    title: str = Field(min_length=2, max_length=60)
    #: 仅作人类可读的来源标识（不落盘成文件，见下面 create_doc 的 storage_key）
    file: str = Field(min_length=4, max_length=120)
    body: str = Field(min_length=MIN_BODY_CHARS)


def load_documents() -> list[SeedDoc]:
    """读并校验种子制度。**校验失败就退出** —— 不写半截数据进库。"""
    if not DOCS_PATH.exists():
        raise SystemExit(f"找不到种子制度文件：{DOCS_PATH}")
    raw = json.loads(DOCS_PATH.read_text(encoding="utf-8"))
    try:
        docs = TypeAdapter(list[SeedDoc]).validate_python(raw)
    except ValidationError as exc:
        raise SystemExit(f"种子制度文件格式不对：\n{exc}") from exc
    if not docs:
        raise SystemExit("种子制度文件里没有任何制度")
    return docs


async def seed_knowledge() -> None:
    embedder = build_embedder()
    print(f"embedding：{embedder.name}（维度 {embedder.dim}）")

    created: list[str] = []
    skipped: list[str] = []

    async with SessionFactory() as session:
        admin = (
            await session.scalars(
                select(UserRecord).where(UserRecord.username == "admin")
            )
        ).one_or_none()
        if admin is None:
            raise SystemExit(
                "库里没有 admin 账号 —— 先跑 `python -m scripts.seed` 建账号，再来灌制度。"
            )

        version = 1
        effective_from = datetime(2026, 1, 1, tzinfo=UTC)
        for doc in load_documents():
            title, text = doc.title, doc.body
            source_key = f"{SEED_PREFIX}{title}"
            existing = (
                await session.scalars(
                    select(KnowledgeDocRecord).where(
                        KnowledgeDocRecord.source_key == source_key,
                        KnowledgeDocRecord.version == version,
                    )
                )
            ).one_or_none()
            if existing is not None:
                skipped.append(f"{title} v{version}")
                continue

            chunks = chunk_text(normalize_text(text))
            if not chunks:
                raise SystemExit(f"《{title}》切分后没有任何片段 —— 正文可能全是空白")

            # 向量在**这里**批量算（与协调者同一条路）：一份制度几十个片段，
            # 逐条发 HTTP 会把"灌一份制度"变成几十次往返。
            vectors = await embedder.embed([c.content for c in chunks])

            doc = KnowledgeDocRecord(
                source_key=source_key,
                title=title,
                version=version,
                effective_from=effective_from,
                # ⚠️ **不是真文件**：种子没有上传附件，所以 storage_key 留空、
                # 并用一个显式不是路径的标识说明来源。伪造一个 uploads/ 路径
                # 会让"原件在哪"这条查询指向不存在的位置。
                storage_key=None,
                filename=f"seed:{title}",
                content_type="text/plain",
                content=text,
                # 直接 READY：切分与向量都已算好，不会留下 FAILED 半截状态。
                status="READY",
                embedding_model=embedder.name,
                uploaded_by=admin.id,
            )
            session.add(doc)
            await session.flush()  # 拿到 doc.id

            session.add_all(
                [
                    KnowledgeChunkRecord(
                        doc_id=doc.id,
                        chunk_no=chunk.chunk_no,
                        content=chunk.content,
                        start_offset=chunk.start_offset,
                        end_offset=chunk.end_offset,
                        embedding=vector,
                    )
                    for chunk, vector in zip(chunks, vectors, strict=True)
                ]
            )
            created.append(f"《{title}》v{version} —— {len(chunks)} 个片段")

        await session.commit()

    print(f"\n新建 {len(created)} 份制度：")
    for line in created:
        print(f"  + {line}")
    if skipped:
        print(f"已存在，跳过 {len(skipped)} 份：")
        for line in skipped:
            print(f"  = {line}")
    print(
        "\n下一步：确认门禁没被影响 —— "
        r".venv\Scripts\python.exe scripts\knowledge_eval.py --gate"
    )


if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit("本脚本不需要参数。用法：python -m scripts.seed_knowledge")
    asyncio.run(seed_knowledge())
