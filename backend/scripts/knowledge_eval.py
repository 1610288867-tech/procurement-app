"""知识库检索与生成评测运行器（M11 增强）。

它回答一个问题：**这套 RAG 到底好不好，好在哪、差在哪。**

============================ 为什么必须分层测 ============================
RAG 出问题时最难回答的是"**到底是检索没找到，还是模型没答对**"：

    该找的资料没进上下文        → 检索问题（改切分 / 换 embedding / 调阈值）
    资料进了上下文但答案还是错的  → 生成问题（改 prompt / 换模型）

只看"最终回答对不对"永远分不开这两者。所以本脚本出**两层指标**：

    检索层：Recall@K / Precision@K / MRR / 命中率   （`domain/retrieval_metrics.py`）
    生成层：引用完整性 + 引用支撑率（Faithfulness 近似）（`domain/citation_support.py`）

============================ 定位标记法（本评测集的关键设计）============================
用例文件里给每一节制度埋了 `[[cN]]` 标记，运行时把标记**映射成真实的 chunk id**。
好处：**改了切分参数（size / overlap）也不用改评测集** ——
而写死 chunk 编号的评测集，在改一次切分之后就整份失效了。

============================ 用法 ============================
    cd backend
    .venv\\Scripts\\python.exe scripts\\knowledge_eval.py                    # 跑全量
    .venv\\Scripts\\python.exe scripts\\knowledge_eval.py --category unanswerable
    .venv\\Scripts\\python.exe scripts\\knowledge_eval.py --save baseline.json
    .venv\\Scripts\\python.exe scripts\\knowledge_eval.py --compare baseline.json

⚠️ **本脚本会往数据库写测试数据，跑完自清理**（用 `EVAL-` 前缀的 source_key
与临时账号，退出前删干净）。它**不依赖外部服务**（用 ASGI 直连应用）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, ".")

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import SessionFactory  # noqa: E402
from app.domain.citation_support import (  # noqa: E402
    KIND_DISTANT,
    KIND_NEGATION,
    KIND_NUMBER,
)
from app.domain.grounded_answer import NO_GROUNDING_ANSWER  # noqa: E402
from app.domain.permissions import ROLE_ADMIN  # noqa: E402
from app.domain.retrieval_metrics import (  # noqa: E402
    ndcg_at_k,
    recall_at_k,
    summarize_retrieval,
)
from app.infrastructure.records.auth import UserRecord, UserRoleRecord  # noqa: E402
from app.infrastructure.records.knowledge import (  # noqa: E402
    ChatMessageRecord,
    ChatSessionRecord,
    KnowledgeChunkRecord,
    KnowledgeDocRecord,
)
from app.infrastructure.security import hash_password  # noqa: E402
from app.main import app  # noqa: E402

CASES_FILE = Path(__file__).parent / "eval" / "knowledge_eval_cases.json"
PASSWORD = "Dev-Passw0rd!"
#: 本脚本造的数据一律带这个前缀，清理时按它认（与集成测试的 M11TEST- 区分开）。
SOURCE_PREFIX = "EVAL-"
USERNAME_PREFIX = "eval-"
#: 相关性的收益等级（NDCG 用）：命中期望片段记"完全相关"。
#: 只给 3 与 0 两档是因为评测集里没有标"部分相关" —— **不猜没标的东西**。
GAIN_FULL = 3


# ---------------------------------------------------------------- 数据准备


async def purge_previous_evals() -> int:
    """跑之前先清掉**上一次评测留下的** `EVAL-` 文档。

    ============================ 为什么要先清（这是一个实测踩到的坑）============================
    第一次跑评测时，库里残留着更早一次手工运行留下的 `EVAL-` 制度文档。
    结果是：定位标记 `[[c2]]` 在**两份文档**里都能找到，
    `resolve_markers` 把标记映射到了其中一份的片段，而检索命中的是另一份 ——
    指标看起来很正常（Recall 1.000 / 段内排名 0.977），**但那是污染后的数字**。
    等回归入口里的清理脚本把它们删掉之后，真实的 MRR 只有 0.879。

    **教训**：评测脚本必须**自己保证起点干净**，而不是依赖别处恰好清过。
    否则"同一份代码两次跑出不同数字"，而且差得不多、很难发现。
    """
    async with SessionFactory() as session:
        doc_ids = select(KnowledgeDocRecord.id).where(
            KnowledgeDocRecord.source_key.like(f"{SOURCE_PREFIX}%")
        )
        count = len(list((await session.scalars(doc_ids)).all()))
        if count:
            await session.execute(
                delete(KnowledgeChunkRecord).where(
                    KnowledgeChunkRecord.doc_id.in_(doc_ids)
                )
            )
            await session.execute(
                delete(KnowledgeDocRecord).where(
                    KnowledgeDocRecord.source_key.like(f"{SOURCE_PREFIX}%")
                )
            )
            await session.commit()
        return count


async def create_admin() -> tuple[uuid.UUID, str]:
    """造一个临时管理员账号（上传制度、提问都用它）。"""
    username = f"{USERNAME_PREFIX}{uuid.uuid4().hex[:8]}"
    async with SessionFactory() as session:
        user = UserRecord(
            username=username,
            password_hash=hash_password(PASSWORD),
            full_name="Eval 评测账号",
            department_id=None,
            active=True,
        )
        session.add(user)
        await session.flush()
        session.add(UserRoleRecord(user_id=user.id, role=ROLE_ADMIN))
        await session.commit()
        return user.id, username


async def purge(user_id: uuid.UUID) -> None:
    """清掉本脚本造的一切（**顺序由外键决定，不能调**）。"""
    async with SessionFactory() as session:
        doc_ids = select(KnowledgeDocRecord.id).where(
            KnowledgeDocRecord.source_key.like(f"{SOURCE_PREFIX}%")
        )
        session_ids = select(ChatSessionRecord.id).where(
            ChatSessionRecord.user_id == user_id
        )
        await session.execute(
            delete(ChatMessageRecord).where(ChatMessageRecord.session_id.in_(session_ids))
        )
        await session.execute(
            delete(ChatSessionRecord).where(ChatSessionRecord.user_id == user_id)
        )
        await session.execute(
            delete(KnowledgeChunkRecord).where(KnowledgeChunkRecord.doc_id.in_(doc_ids))
        )
        await session.execute(
            delete(KnowledgeDocRecord).where(
                KnowledgeDocRecord.source_key.like(f"{SOURCE_PREFIX}%")
            )
        )
        await session.execute(
            delete(UserRoleRecord).where(UserRoleRecord.user_id == user_id)
        )
        await session.execute(delete(UserRecord).where(UserRecord.id == user_id))
        await session.commit()


async def upload_documents(client: AsyncClient, documents: list[dict[str, Any]]) -> None:
    """把评测用的制度文档传进知识库。"""
    for doc in documents:
        data: dict[str, str] = {
            "source_key": doc["source_key"],
            "title": doc["title"],
            "version": str(doc["version"]),
        }
        if doc.get("effective_from"):
            data["effective_from"] = doc["effective_from"]
        response = await client.post(
            "/api/v1/admin/knowledge-docs",
            data=data,
            files={"file": (f"{doc['source_key']}-v{doc['version']}.txt", doc["text"].encode(), "text/plain")},
        )
        if response.status_code != 201:
            raise SystemExit(
                f"上传失败（{doc['source_key']} v{doc['version']}）："
                f"{response.status_code} {response.text[:200]}"
            )


async def resolve_markers() -> dict[str, str]:
    """把制度文本里的 `[[cN]]` 标记映射成真实的 chunk id。

    做法：查所有评测文档的片段，按标记出现的位置认领。
    **标记会留在片段内容里**（切分不会把它切掉，因为它是纯文本），
    所以这一步是确定性的 —— 不依赖 chunk 编号，只依赖标记本身。
    """
    mapping: dict[str, str] = {}
    async with SessionFactory() as session:
        doc_ids = list(
            (
                await session.scalars(
                    select(KnowledgeDocRecord.id).where(
                        KnowledgeDocRecord.source_key.like(f"{SOURCE_PREFIX}%")
                    )
                )
            ).all()
        )
        chunks = list(
            (
                await session.scalars(
                    select(KnowledgeChunkRecord)
                    .where(KnowledgeChunkRecord.doc_id.in_(doc_ids))
                    .order_by(KnowledgeChunkRecord.chunk_no)
                )
            ).all()
        )
    for chunk in chunks:
        content = chunk.content
        cursor = 0
        while True:
            start = content.find("[[", cursor)
            if start < 0:
                break
            end = content.find("]]", start)
            if end < 0:
                break
            marker = content[start + 2 : end]
            # 一个标记只认一次（同一片里出现两次的情况不存在，但防御一下）
            mapping.setdefault(marker, str(chunk.id))
            cursor = end + 2
    return mapping


# ---------------------------------------------------------------- 单例评测

#: 被限流时最多重试几次、每次等多久。
#:
#: 问答接口有**最小提问间隔**限流（见 `infrastructure/token_quota.py`），
#: 而评测脚本是连发的 —— 实测：`F-01` 过、`F-02` 起全被 429 拦，
#: 27 条里只有 1 条跑通，门禁于是报"指标不适用"。
#:
#: 处理方式是"**像真客户端那样**"：拿到 429 就等一拍再试。
#: **不是无条件 sleep**（那等于把限流当成没发生），
#: 而是承认它存在并遵守它；被限流的次数会记下来打进报告，
#: 免得"评测偶尔变慢/变少"没人知道原因。
#: 生产限流默认 0.35 秒一拍，所以等 0.4 秒足够跨过去。
_RATE_LIMIT_RETRIES = 3
_RATE_LIMIT_WAIT_SECONDS = 0.45
#: 这一次评测里被限流（429）拦下的次数，按用例 id 记
_rate_limited_attempts: list[str] = []


async def evaluate_case(
    client: AsyncClient, case: dict[str, Any], markers: dict[str, str]
) -> dict[str, Any]:
    """跑一条用例，返回它的原始结果（指标在汇总阶段算）。"""
    question = case["question"]
    response = None
    for attempt in range(_RATE_LIMIT_RETRIES + 1):
        response = await client.post("/api/v1/chat", json={"question": question})
        if response.status_code != 429:
            break
        if attempt < _RATE_LIMIT_RETRIES:
            _rate_limited_attempts.append(case["id"])
            await asyncio.sleep(_RATE_LIMIT_WAIT_SECONDS)
    if response is None or response.status_code != 200:
        status = "无响应" if response is None else response.status_code
        text = "" if response is None else response.text[:200]
        return {"id": case["id"], "ok": False, "error": f"{status} {text}"}
    body = response.json()

    # 检索层要的是**片段主键**：出参里的 `chunk_id` 就是它（见 `Source.chunk_id`）。
    # 用主键而不是 `chunk_no` 判命中 —— 后者会随切分参数变化，评测集会因此失效。
    retrieved_ids = [source.get("chunk_id") or "" for source in body["sources"]]
    return {
        "id": case["id"],
        "category": case["category"],
        "difficulty": case["difficulty"],
        "ok": True,
        "question": question,
        "answer": body["answer"],
        "used_model": body["used_model"],
        "reason": body["reason"],
        "top_score": body["top_score"],
        "retrieved": retrieved_ids,
        "citation_problems": body["citation_problems"],
        "support_problems": body["support_problems"],
        "support_rate": body["support_rate"],
        "is_refusal": body["answer"].lstrip().startswith(NO_GROUNDING_ANSWER),
    }


# ---------------------------------------------------------------- 汇总与报告


def summarize(results: list[dict[str, Any]], k: int) -> dict[str, Any]:
    """把逐条结果汇总成两层指标。

    ============================ 两个容易算错的地方 ============================
    ① **无答案问题的 Recall 是"不适用"而不是 0** ——
       `summarize_retrieval` 会剔除它们并报出剔除条数；
    ② **生成层指标只对"确实调了模型"的用例算** ——
       拒答（未调用模型）时没有引用句，`support_rate` 本来就是 `None`。
       把拒答算进支撑率会**系统性拉低**它，而拒答恰恰是正确行为。
    """
    ok = [row for row in results if row.get("ok")]
    scored = [row for row in ok if row["category"] != "unanswerable"]

    retrieval_pairs = [(row["retrieved"], row["relevant"]) for row in scored]
    retrieval = summarize_retrieval(retrieval_pairs, k=k)

    ndcgs: list[float] = []
    for row in scored:
        gains = {chunk: GAIN_FULL for chunk in row["relevant"]}
        if gains:
            value = ndcg_at_k(row["retrieved"], gains, k)
            if value is not None:
                ndcgs.append(value)

    # 生成层：只在"有引用句"的回答上算支撑率（拒答与整段无引用都不适用）
    support_rates = [
        row["support_rate"] for row in ok if row.get("support_rate") is not None
    ]
    support_counts = {kind: 0 for kind in (KIND_NUMBER, KIND_NEGATION, KIND_DISTANT)}
    for row in ok:
        for problem in row.get("support_problems") or []:
            if problem["kind"] in support_counts:
                support_counts[problem["kind"]] += 1

    integrity_failures = sum(1 for row in ok if row["citation_problems"])

    # 拒答相关的三条
    unanswerable = [row for row in ok if row["category"] == "unanswerable"]
    refusal_correct = sum(1 for row in unanswerable if row["is_refusal"])
    over_refusal = sum(1 for row in scored if row["is_refusal"])

    # ---- 「检索成功但没能回答」的比例（**最容易漏掉的一个指标**）----
    # 只测检索的话会看到"Recall@5 = 1.000"（要的那一片确实被检索到了），
    # 但用户实际拿到的是"制度中没有找到相关规定" —— 因为**阈值判定**把它拦了。
    # 这两个数字同时摆在报告里，才能看清"问题出在检索还是出在阈值"。
    retrieval_won = [
        row for row in scored if set(row["retrieved"][:k]) & set(row["relevant"])
    ]
    grounded = [row for row in retrieval_won if not row["is_refusal"]]

    return {
        "cases_total": len(results),
        "cases_ok": len(ok),
        "cases_failed": len(results) - len(ok),
        "retrieval": {**retrieval, "ndcg": (sum(ndcgs) / len(ndcgs)) if ndcgs else None},
        "generation": {
            "support_rate_mean": (
                sum(support_rates) / len(support_rates) if support_rates else None
            ),
            "support_rate_scored": len(support_rates),
            "support_problem_counts": support_counts,
            "citation_integrity_failures": integrity_failures,
            "refusal_on_unanswerable": (
                f"{refusal_correct}/{len(unanswerable)}" if unanswerable else "n/a"
            ),
            "over_refusal_on_answerable": over_refusal,
            # 「该找到的找到了，且真的回答了」——**用户体感上的成功率**
            "grounded_rate": (
                len(grounded) / len(retrieval_won) if retrieval_won else None
            ),
            "grounded_scored": len(retrieval_won),
        },
    }


def print_report(summary: dict[str, Any], results: list[dict[str, Any]], k: int) -> None:
    """打印一张人看得懂的报告（含**分桶**，因为平均值会掩盖"某一类特别差"）。"""
    print()
    print("=" * 74)
    print(f"知识库评测报告（Recall@{k}）")
    # **必须写明是哪个向量空间算的**：哈希与真模型的数字不可比，
    # 一份不注明 embedding 的报告拿去做对比就是错的。
    print(f"embedding：{summary.get('embedding_model', '(未记录)')}")
    print("=" * 74)
    retrieval = summary["retrieval"]
    generation = summary["generation"]
    print(f"用例：{summary['cases_ok']}/{summary['cases_total']} 跑通"
          + (f"，{summary['cases_failed']} 条失败" if summary["cases_failed"] else ""))
    print()
    print("【检索层】")
    print(f"  Recall@{k}      : {fmt(retrieval['recall'])}"
          f"   （{retrieval['cases_scored']} 条计入，{retrieval['recall_skipped']} 条不适用）")
    print(f"  Precision@{k}   : {fmt(retrieval['precision'])}")
    print(f"  命中率          : {fmt(retrieval['hit_rate'])}")
    print(f"  MRR             : {fmt(retrieval['mrr'])}")
    print(f"  NDCG@{k}        : {fmt(retrieval['ndcg'])}")
    print()
    print("【生成层】")
    print(f"  引用支撑率均值  : {fmt(generation['support_rate_mean'])}"
          f"   （{generation['support_rate_scored']} 条计入；拒答与整段无引用不计入）")
    print(f"  引用完整性问题  : {generation['citation_integrity_failures']} 条")
    counts = generation["support_problem_counts"]
    print(f"  支撑性问题      : 数字不支撑 {counts[KIND_NUMBER]} / "
          f"否定翻转 {counts[KIND_NEGATION]} / 引用错位 {counts[KIND_DISTANT]}")
    print(f"  无答案问题的拒答: {generation['refusal_on_unanswerable']}"
          f"   （有答案问题误拒答 {generation['over_refusal_on_answerable']} 条）")
    print(f"  **实际回答成功率**: {fmt(generation['grounded_rate'])}"
          f"   （{generation['grounded_scored']} 条检索成功的问题里，真的给出了回答的比例）")
    if generation["over_refusal_on_answerable"]:
        print("     ⚠️ 检索到了却拒答 —— 多半是**阈值与 embedding 不匹配**：")
        print("        本地哈希 embedding 对口语化问法只能给出很低的相似度，")
        print("        于是阈值把它拦下了。换真 embedding（bge-m3）后这一项应当明显改善。")
    print()
    print("【分桶：按问题类型】")
    print(f"  {'类型':<14}{'条数':>5}{'Recall':>9}{'命中率':>9}{'支撑率':>9}")
    for category in ("fact", "condition", "cross_version", "colloquial", "unanswerable"):
        rows = [row for row in results if row.get("ok") and row["category"] == category]
        if not rows:
            continue
        answered = [row for row in rows if row["category"] != "unanswerable"]
        recall = mean_or_none(
            [recall_at_k(row["retrieved"], row["relevant"], k) for row in answered]
        )
        hit = mean_or_none(
            [
                (1.0 if set(row["retrieved"][:k]) & set(row["relevant"]) else 0.0)
                for row in answered
                if row["relevant"]
            ]
        )
        support = mean_or_none(
            [row["support_rate"] for row in rows if row.get("support_rate") is not None]
        )
        print(f"  {category:<14}{len(rows):>5}{fmt(recall):>9}{fmt(hit):>9}{fmt(support):>9}")
    print()
    misses = [
        row for row in results
        if row.get("ok") and row["relevant"] and not (set(row["retrieved"][:k]) & set(row["relevant"]))
    ]
    if misses:
        print(f"【未命中 {len(misses)} 条】（这些是改检索的第一批目标）")
        for row in misses:
            print(f"  {row['id']:<6} [{row['category']:<13}] {row['question']}")
            print(f"         期望 {row['relevant']}  实得 {row['retrieved'][:k]}"
                  f"  最高分 {fmt(row['top_score'])}")
    print("=" * 74)


def fmt(value: Any) -> str:
    """把指标格式化成固定宽度（`None` 打 `n/a`，与 0 区分开）。"""
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def mean_or_none(values: list[Any]) -> float | None:
    real = [value for value in values if value is not None]
    return (sum(real) / len(real)) if real else None


# ---------------------------------------------------------------- 主流程


async def run(args: argparse.Namespace) -> int:
    if args.quiet:
        # 关掉应用与 httpx 的 INFO 日志。**必须在 import app 之后、发第一个请求之前**做，
        # 而且只降这两个 logger 的级别（不动 root）——
        # 这样"脚本自己的 print 报告"照旧出来，而每请求一行的访问日志被摘掉。
        import logging

        logging.getLogger("procurement").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)

    # ---- 选 embedding 实现（命令行优先于 .env）----
    # 为什么用命令行而不是只读 .env：**对比实验要在同一次运行里改这一项**，
    # 改 .env 文件再跑容易忘、也说不清"这份报告是哪套配置出的"。
    # 而"这份数字是哪个向量空间算的"必须写在报告里（见 `embedding_model`）。
    settings = get_settings()
    if args.embedding:
        provider = args.embedding
        settings.knowledge_embedding_provider = provider
    else:
        provider = settings.knowledge_embedding_provider
    if args.embedding_model:
        settings.knowledge_embedding_model = args.embedding_model
    if args.min_score is not None:
        settings.knowledge_min_score = args.min_score
    # 注意：**不需要**在这里补 embedding 的服务地址 —— 它有自己的配置项
    # `KNOWLEDGE_EMBEDDING_BASE_URL`，且默认就指向本机 Ollama。
    # （早先这里补过 `knowledge_llm_base_url`，那是 embedding 与对话模型
    #   共用地址时的遗留；现在两者解耦了，那行是死代码。）
    #
    # ============================ 对话模型：默认**与 .env 隔离** ============================
    # **实测踩到（2026-10-14）**：`backend/.env` 配上对话模型之后，这份评测的
    # 拒答率从 3/5 掉到 2/5、引用支撑率从 1.000 掉到 0.895、MRR 从 0.932 变成 0.955 ——
    # 门禁直接红。原因不是检索变差了，而是**评测的行为被本机配置改变了**：
    # 对话模型一配上，Agent 路径就开了（`supports_tools=True`），
    # 于是那些"制度里没有答案"的问题被拿去**查业务数据**，不再回
    # `NO_GROUNDING_ANSWER`，`refusal_rate` 这个指标量的东西就变了。
    #
    # 这是**不可接受的**：评测数字必须只反映"检索 + 提示 + 校验"这条链路，
    # 否则换个机器、换个已装模型，同一份 Golden QA 就得出另一套数字，
    # 既不可复现、也不能横向比较 —— 而门槛是按它定的。
    #
    # 所以默认把三项**清空**（`_chat_model()` 判据是"三项都非空才用远端"），
    # 稳定走 `LocalExtractiveChat`：离线、确定性、只依赖检索。
    # 想看"接上真模型之后是什么样"，显式加 `--use-chat-model`。
    if args.use_chat_model:
        chat_model_note = settings.knowledge_llm_model or "(已配置)"
    else:
        chat_model_note = "local-extractive（离线兜底，已忽略 .env 的对话模型配置）"
        settings.knowledge_llm_base_url = None
        settings.knowledge_llm_api_key = None
        settings.knowledge_llm_model = None
    print(f"对话模型：{chat_model_note}")

    # `get_settings` 带缓存，上面改的是那个缓存对象本身；
    # 但边界层每次请求都会再调一次 `get_settings()`，拿到的就是同一个对象 ——
    # 所以改动生效，不需要清缓存。
    print(f"embedding：provider={provider}", end="")
    if provider == "ollama":
        print(f"  model={settings.knowledge_embedding_model}")
    else:
        print()

    data = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    cases = data["cases"]
    if args.category:
        cases = [case for case in cases if case["category"] == args.category]
    if args.id:
        cases = [case for case in cases if case["id"] in args.id]
    if not cases:
        print("没有匹配的用例（检查 --category / --id）")
        return 1

    user_id, username = await create_admin()
    transport = ASGITransport(app=app)
    try:
        # **先清上一次的残留**：否则标记会映射到旧文档的片段，指标被污染
        # （实测踩到：污染的 MRR 0.977 vs 真实的 0.879）。见 `purge_previous_evals`。
        stale = await purge_previous_evals()
        if stale:
            print(f"清掉上一次评测残留的 {stale} 份制度文档")

        async with AsyncClient(transport=transport, base_url="http://eval") as client:
            login = await client.post(
                "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
            )
            if login.status_code != 200:
                print(f"登录失败：{login.status_code} {login.text[:200]}")
                return 1

            await upload_documents(client, data["documents"])
            marker_to_chunk = await resolve_markers()
            print(f"制度已入库；解析出 {len(marker_to_chunk)} 个定位标记")

            results: list[dict[str, Any]] = []
            for case in cases:
                row = await evaluate_case(client, case, marker_to_chunk)
                if row.get("ok"):
                    row["relevant"] = [
                        marker_to_chunk[marker]
                        for marker in case.get("markers", [])
                        if marker in marker_to_chunk
                    ]
                    missing = [m for m in case.get("markers", []) if m not in marker_to_chunk]
                    if missing:
                        print(f"⚠️ 用例 {case['id']} 的标记没解析到：{missing}")
                results.append(row)
                mark = "✓" if row.get("ok") else "✗"
                print(f"  {mark} {row['id']:<6} {case['question'][:34]}")

            summary = summarize(results, k=args.k)
            summary["embedding_model"] = embedding_model_of(results)
            print_report(summary, results, args.k)

            if args.save:
                Path(args.save).write_text(
                    json.dumps(
                        {"summary": summary, "results": results},
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                print(f"结果已存：{args.save}")

            if args.compare:
                compare_with_baseline(Path(args.compare), summary)

            if args.gate:
                failures = check_gates(summary)
                print()
                if failures:
                    print("【质量门禁】未通过：")
                    for failure in failures:
                        print(f"  ✗ {failure}")
                    print("  门槛定义见 knowledge_eval.py 的 GATES（含『为什么定这么松』的说明）")
                    return 1
                print("【质量门禁】通过：" + "，".join(
                    f"{name} ≥ {threshold:.2f}" for name, threshold in GATES.items()
                ))
                if _rate_limited_attempts:
                    # **把它打出来**：不说的话，"评测偶尔变慢 / 条数变少"
                    # 就没人知道是接口限流造成的。
                    print(
                        f"  （本次有 {len(_rate_limited_attempts)} 次被接口限流 429 拦下、"
                        f"已等一拍重试：{_rate_limited_attempts[:5]}）"
                    )

            return 0 if summary["cases_failed"] == 0 else 1
    finally:
        await purge(user_id)


def embedding_model_of(results: list[dict[str, Any]]) -> str:
    """报告里要写明"这份数字是哪个向量空间算的" —— 否则换模型后无法对比。

    取的是**已配置的实现**的名字（不是硬编码的哈希名）：
    这份报告是"哈希 vs 真模型"对比的证据，写错名字就白跑了。
    """
    # 局部 import：`_embedder` 只在报告阶段用一次，放模块顶部会让
    # "脚本依赖边界层私有函数"这件事看起来像常态（它其实是个例外）。
    from app.boundaries.http.knowledge import _embedder

    return _embedder().name


def compare_with_baseline(path: Path, summary: dict[str, Any]) -> None:
    """与基线对比，**下降就报出来**（这是评测进 CI 的意义）。

    只比"越大越好"的几个指标；`None`（不适用）不参与比较 ——
    把不适用当成 0 会造出假的"下降"。
    """
    if not path.exists():
        print(f"⚠️ 找不到基线：{path}（跳过对比）")
        return
    baseline = json.loads(path.read_text(encoding="utf-8"))["summary"]
    pairs = [
        ("Recall", baseline["retrieval"]["recall"], summary["retrieval"]["recall"]),
        ("MRR", baseline["retrieval"]["mrr"], summary["retrieval"]["mrr"]),
        ("NDCG", baseline["retrieval"]["ndcg"], summary["retrieval"]["ndcg"]),
        (
            "支撑率",
            baseline["generation"]["support_rate_mean"],
            summary["generation"]["support_rate_mean"],
        ),
    ]
    print()
    print("【与基线对比】")
    for name, old, new in pairs:
        if old is None or new is None:
            print(f"  {name:<8} 基线 {fmt(old)} → 现在 {fmt(new)}   （一方不适用，跳过比较）")
            continue
        delta = new - old
        flag = "↓ 下降" if delta < -0.005 else ("↑ 提升" if delta > 0.005 else "持平")
        print(f"  {name:<8} 基线 {fmt(old)} → 现在 {fmt(new)}   {delta:+.3f}  {flag}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="知识库检索与生成评测")
    parser.add_argument("--k", type=int, default=5, help="@K 的 K（默认 5）")
    parser.add_argument("--category", help="只跑某一类（fact/condition/cross_version/colloquial/unanswerable）")
    parser.add_argument("--id", nargs="*", help="只跑指定用例 id")
    parser.add_argument("--save", help="把结果存成 JSON（可作为下次的基线）")
    parser.add_argument("--compare", help="与基线 JSON 对比，下降就报出来")
    parser.add_argument(
        "--gate",
        action="store_true",
        help="质量门禁模式：任一硬门槛不达标就返回非 0（供回归入口调用）",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="关掉应用 INFO 日志（只留报告）。**回归入口会用这个** —— "
        "否则每请求一行日志会把报告淹掉，失败时看不到真实指标",
    )
    parser.add_argument(
        "--embedding",
        choices=["local", "ollama"],
        help="用哪套 embedding 跑这次评测（默认读 .env 里的 KNOWLEDGE_EMBEDDING_PROVIDER）。"
        "**对比实验要用它**：同一次运行里改这一项，报告里会写明是哪套算的数字",
    )
    parser.add_argument(
        "--embedding-model",
        help="`--embedding ollama` 时用哪个模型（默认读 .env）",
    )
    parser.add_argument(
        "--use-chat-model",
        action="store_true",
        help="**接上 .env 里配的对话模型**再跑（默认不接，固定走离线的抽取式兜底）。"
        "默认不接的理由：配了对话模型就会打开 Agent 路径，"
        '那些"制度里没有答案"的问题会被拿去查业务数据、不再回拒答，'
        "于是 refusal_rate 这个指标量的东西就变了 —— 数字随机器配置变，不可复现也不可比较。"
        "想看真模型下的表现时显式加它，报告里会写明用的是哪个模型",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        help="覆盖『要不要回答』的相似度阈值（默认读 .env）。**标定阈值要用它**："
        "换 embedding 之后分数分布整体变了，阈值必须重新量、不能照旧",
    )
    return parser.parse_args()


# ================================================================ 质量门禁

#: **硬门槛**。低于它就返回非 0，让回归入口红。
#:
#: ============================ 阈值怎么定的（不是拍的）============================
#: 依据是**实测值**，默认配置（真 embedding `quentinz/bge-small-zh-v1.5` + `min_score=0.50`）：
#:
#:     Recall@5 = 1.000   MRR = 0.932 ~ 0.955   NDCG@5 = 0.950 ~ 0.966
#:     实际回答成功率 = 1.000            拒答率 = 3/5
#:
#: ⚠️ **MRR/NDCG 给的是区间，不是单点** —— 这个区间是被实测纠正出来的：
#: 我先量到 0.932 并连跑三次都一致，就把"0.932 是稳定值"写进了注释；
#: 几天后再量变成 0.955（也连跑三次一致）。**两次都是稳定的，但不同**，
#: 说明单点观测会跟着**语料状态**变（评测会清掉自己的残留，但库里还有别的文档）。
#: 所以记录成区间才诚实，也才解释得清为什么门槛要留余量。
#:
#: 门槛取"实测下沿再往下留一段"，而不是等于实测值 ——
#: 等于实测值的话**任何无关改动都会让门禁红**，人就会开始忽略它，
#: 而门禁的价值在于"抓真问题"、不在于"永远绿"。
#:
#: ⚠️ **同一个坑我踩了两次，都记在这里**：
#:  ① 第一版把 MRR 门槛写成 0.90，依据是**被污染的那次 baseline（0.977）**——
#:     当时（哈希）真实值是 0.879，于是门禁一上线就红；
#:  ② 接真 embedding 之后我把门槛提到 0.95，依据是我当时看到的 **MRR 1.000** ——
#:     后来连跑三次才发现不是 1.000，门禁又红了。
#:
#: **两次都是"拿一个没核实的数字去定阈值"**。所以现在的规矩是：
#: 定/改门槛之前，**先连跑三次确认它稳定**，再用它定；**并记区间而不是单点**。
#:
#: ⚠️ `grounded_rate` 与 `refusal_rate` 是在真 embedding 下量出来的。
#: 换回 `--embedding local` 时它们会掉到 0.864 / 0.60（**已知的**哈希局限，不是回归），
#: 那时这两个门槛要一起放宽 —— 见 `docs/AI-评测baseline.md` 的两套数字对比。
GATES: dict[str, float] = {
    "recall": 0.95,        # 该找到的片段有没有进 top-k（实测 1.000）
    "mrr": 0.90,           # 第一个正确结果排得够不够前（实测 0.932~0.955，留 ≥0.032 余量）
    "grounded_rate": 0.95,  # 检索成功的问题里，真的给出回答的比例（实测 1.000）
    "refusal_rate": 0.55,  # 无答案问题被正确拒答的比例（实测 0.600）
}


def check_gates(summary: dict[str, Any]) -> list[str]:
    """逐项比硬门槛，返回未达标项（空 = 通过）。"""
    retrieval = summary["retrieval"]
    generation = summary["generation"]
    actual: dict[str, float | None] = {
        "recall": retrieval["recall"],
        "mrr": retrieval["mrr"],
        "grounded_rate": generation["grounded_rate"],
        "refusal_rate": parse_fraction(generation["refusal_on_unanswerable"]),
    }
    failures: list[str] = []
    for name, threshold in GATES.items():
        value = actual[name]
        if value is None:
            # ⚠️ `None` 是"不适用"，**不能当成 0**（那会造出假的失败）。
            # 但它也确实是"这份报告看不出来" —— 所以要单独报出来，不能静默放过。
            failures.append(f"{name} 无法判定（指标不适用）—— 门禁失效，请检查评测集")
            continue
        if value < threshold:
            failures.append(f"{name} = {value:.3f} < 门槛 {threshold:.2f}")
    return failures


def parse_fraction(text: str) -> float | None:
    """把 `"3/5"` 解析成 `0.6`；解析不了返回 `None`（不适用）。"""
    if "/" not in text:
        return None
    left, _, right = text.partition("/")
    try:
        total = int(right)
        return (int(left) / total) if total else None
    except ValueError:
        return None


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
