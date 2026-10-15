"""知识库与问答的用例（M11）。

============================ 这个协调者负责的六件事 ============================
1. **上传制度文档**（管理员）：落原文 → 归一化 → 切分 → 算向量 → 存片段；
2. **看切分结果**（管理员）：出问题时一眼看出是"没检索到"还是"切碎了"；
3. **提问**（登录用户）：向量检索 → 阈值判定 → （可选）调模型 → 引用校验 → 存消息；
4. **会话历史**（只列自己的）；
5. **SSE 事件流**：`meta` → `delta` → `done` / `error`。

============================ 顺序不能反：先检索，再决定要不要问模型 ============================
计划里说得很直白："很多人一上手就『接个大模型』，结果做出一个会编造制度条文的玩具。
**正确顺序是：先有知识库，再有检索，最后才是模型。**"

落到代码里就是这一行判断的位置：

    decision = decide_grounding(sources, min_score=...)
    if not decision.use_model:
        # 检索为空或全部低于阈值：**不调用模型**，直接返回"未找到相关规定"
        ...

**它必须在构造 Prompt 之前、在拿到模型客户端之前**。写成"先问模型、
拿到回答再检查有没有依据"就等于没有这道闸门 —— 模型已经答完了。

============================ 引用校验：发现问题不阻断，但要说出来 ============================
计划定的第一版策略：**不阻断**，把问题记进 `chat_messages.citation_problems`
并在回答末尾追加一行提示。理由：直接拒答会让用户觉得"明明答得挺好却用不了"，
而这类问题需要样本量才能决定是"重试一次"还是"换成拒答"。
**关键是这个校验必须存在** —— 它今天的价值是让问题可见。

============================ 为什么问答不写 `request_events` ============================
`request_events` 是**单据的时间线**（只追加、永不修改，是审计证据）。
问答不是单据事件，它有自己的一张表（`chat_messages`）。
把两者混在一起会让"这张单子经历过什么"这条查询里出现一堆聊天记录 ——
而且会破坏"事件流只记单据状态变化"这个语义。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:  # 只在类型检查时导入：运行时不希望 application 依赖 SQLAlchemy 的具体类型
    from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agent import AgentStep, run_agent
from app.application.coordinators.approval import ApprovalCoordinator
from app.application.coordinators.auth import Principal
from app.application.coordinators.purchase import PurchaseCoordinator
from app.application.ports import (
    ChatMessage,
    ChatModelPort,
    EmbeddingPort,
    ModelUsage,
    ObjectStorePort,
    UnitOfWork,
)
from app.application.tools import agent_used_tools
from app.database import SessionFactory
from app.domain.agent_run import RESUMABLE_STATUSES, RUN_FINISHED
from app.domain.chunking import chunk_text, normalize_text
from app.domain.citation_support import (
    SupportProblem,
    check_citation_support,
    support_rate,
)
from app.domain.cost import ModelPrice, estimate_cost_micros
from app.domain.embedding import VECTOR_DIM
from app.domain.grounded_answer import (
    CITATION_WARNING,
    SYSTEM_PROMPT,
    Source,
    build_user_prompt,
    check_citation_integrity,
    decide_grounding,
)
from app.domain.model_pricing import parse_model_prices
from app.errors import BusinessError, ConflictError, NotFoundError
from app.infrastructure.chat_models import LocalExtractiveChat
from app.infrastructure.repositories.knowledge_repo import KnowledgeRepository

#: 与 `app/main.py` 用同一个 logger 名（`procurement`）——
#: 这样日志的格式、级别与输出目标由**一处**决定，不在这里另起一套。
logger = logging.getLogger("procurement")


def _merge_details(
    support_details: list[dict[str, Any]],
    *,
    agent_steps: list[dict[str, Any]] | None,
    agent_stop_reason: str | None,
    agent_run_id: str | None = None,
) -> list[dict[str, Any]] | None:
    """把"引用支撑性问题"与"Agent 执行轨迹"合并进同一个 JSONB 列。

    ============================ 为什么合进 `support_details` 而不加新列 ============================
    Agent 轨迹是**诊断信息**，它不是"回答质量"的一部分，查询方式也还不确定。
    为一个用途未定的东西加一列（还要一条迁移）不划算 ——
    而 `support_details` 本来就是"这次回答的一些结构化问题与说明"，语义上放得下。

    代价说清楚：`support_details` 因此**不再只装 `SupportProblem`**，
    读它的人要知道里面可能有一个 `kind == "agent"` 的条目。
    所以这里**显式用 `kind` 区分**，而不是靠位置或额外字段。

    `agent_steps` 为空时不写任何东西（**不加一个空壳条目**：
    那会让"这次是 Agent 回答"与"这次不是"看起来一样）。
    """
    merged = list(support_details)
    if agent_steps:
        merged.append(
            {
                "kind": "agent",
                "detail": f"Agent 执行 {len(agent_steps)} 步，停止原因 {agent_stop_reason}",
                "sentence_index": None,
                "steps": agent_steps,
                "stop_reason": agent_stop_reason,
                #: 这次运行的 id（M12++）。有了它，这条回答就能追溯到
                #: `agent_runs` 里那次运行 —— 中断过、续跑过都查得到。
                "run_id": agent_run_id,
            }
        )
    return merged or None

#: 允许的制度文档类型。**刻意只有两种**：
#: PDF 抽文本要额外的库（`pypdf` 之类），而那属于"要不要为 M11 再加一个依赖"的决定 ——
#: 第一版先用最稳的两类（纯文本 / Markdown），把链路跑通。
#: 需要 PDF 时再加解析器，而不是现在装一个用不上的库。
ALLOWED_CONTENT_TYPES = {
    "text/plain": ".txt",
    "text/markdown": ".md",
}

#: 类型缺失时**按扩展名补全**（与 M9 附件那条规则同一个思路，但这里更窄：
#: 只认这两个扩展名，不做通用 MIME 猜测）。
#:
#: 为什么必须有这一步：`httpx` 与一些客户端在发送 `text/plain` 的 multipart 部分时
#: **不带 `Content-Type`**，于是 Starlette 给的是 `None`。实测就是这样 ——
#: 结果所有 `.txt` 制度都被 422 挡掉（"只收纯文本与 Markdown（收到 application/octet-stream）"），
#: 而 curl 与浏览器的行为还不完全一样，属于"换个人试就复现不了"的那类问题。
#:
#: ⚠️ 这里**只补声明类型，不代替内容校验** —— 与 M9 的纪律一致：
#: 扩展名永远只是"补全声明"，不能用来判断文件内容安不安全。
#: 制度文档不执行、不渲染，所以第一版不做魔数校验；真要收 PDF 时必须补上。
EXTENSION_CONTENT_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
}
#: 单份制度的文本上限（字符）。比附件上限（10 MB）小得多 ——
#: 因为这里要**全量切分并逐片算向量**，一份几十万字的文档会让上传请求卡很久。
MAX_DOC_CHARS = 200_000
#: 会话标题取问题的前 N 个字符。
TITLE_LENGTH = 60


def _infer_content_type(filename: str | None) -> str | None:
    """按扩展名补全声明类型。认不出返回 `None`。

    ⚠️ **不要兜底成 `application/octet-stream`**：那样调用处就分不清
    "补全成功"与"压根没认出来"，而这两种情况要给用户完全不同的提示
    （一个是"能收"，一个是"只收 txt / md"）。
    """
    if not filename:
        return None
    lowered = filename.lower()
    for suffix, content_type in EXTENSION_CONTENT_TYPES.items():
        if lowered.endswith(suffix):
            return content_type
    return None


class KnowledgeCoordinator:
    @asynccontextmanager
    async def _agent_run(
        self,
        *,
        session_id: uuid.UUID,
        actor_id: uuid.UUID,
        question: str,
        run_id: uuid.UUID | None,
    ) -> AsyncIterator[tuple[uuid.UUID, list[dict[str, Any]], Any]]:
        """一次 Agent 运行的**生命周期**：开 → （跑）→ 结束或标中断。

        产出 `(run_id, 已记录的轨迹, checkpoint 回调)`。

        ============================ 为什么用**请求那个会话**，而不是自己开一个 ============================
        第一版用的是独立会话（`SessionFactory()`），理由听起来很正当：
        "断点要真落盘，就得 commit，而请求会话里可能压着别的东西"。

        实测撞到了它的**致命前提**：独立会话**看不到请求会话里尚未提交的行**。
        `agent_runs.session_id` 外键指向 `chat_sessions`，而那个会话行是
        `_prepare` 在**请求会话里刚建、还没提交**的 →
        独立会话插入时外键直接违约（`ForeignKeyViolation`）。
        在集成测试的夹具（外层事务 + savepoint）里 100% 复现。

        更根本的是：**一次请求里的东西本来就该在同一个事务边界内**。
        "断点"要的是"每一步之后让状态对后来者可见"，而那不需要另一个会话 ——
        只需要**提交**。用请求会话提交，语义反而更正确：
        已经跑过的步骤与它引用的会话、消息一起可见，不会出现
        "运行记录说存在、它引用的会话却还没提交"这种半截状态。

        代价要说清：这里的 `commit()` 会把请求会话里**已积压的东西一起提交**
        （例如 `_prepare` 追加的那条用户消息）。这可以接受，因为那些都是
        **只追加**的写入（消息、运行记录），没有"回滚它们才有意义"的语义。

        ============================ 三种退出，三种状态 ============================
            正常跑完                    → `FINISHED`
            调用方自己出错               → `FAILED`
            生成器被提前关闭（SSE 断开） → `INTERRUPTED`（**可续跑**）

        ⚠️ **"进程被杀"这一支这里接不住**（没有任何代码会执行）。
        它的兜底是**下一次请求**触发的 `sweep_stale_running`：
        把卡住的 `RUNNING` 按时间判成 `INTERRUPTED`。两者合起来才闭环。
        """
        # 顺手把卡住的 RUNNING 判成 INTERRUPTED —— 被杀的进程不会自己写库，
        # 只能由后来者按时间判定。必须**发生在开始新的运行之前**。
        swept = await self._run_repo.sweep_stale_running()
        if swept:
            await self._uow.commit()
            logger.info("把 %d 次卡住的 Agent 运行标成中断", swept)

        # ============================ `run_id` 是"续跑键"，不是"指定新建 id"的键 ============================
        # 第一版把 `run_id` 直接拿去 `start()`（= INSERT 一个指定 id 的行）——
        # 而续跑时那一行**本来就在库里**，于是主键冲突 →
        # 被全局异常处理器翻成 503「数据库暂不可用」（实测撞到，指错方向）。
        #
        # 正确语义：传了 `run_id` 就是**接着那一次跑**，所以先加载它、校验它。
        # 也让"传一个不存在的 id"变成明确的 404 而不是 500。
        if run_id is not None:
            existing = await self._run_repo.get(run_id)
            if existing is None:
                raise NotFoundError(f"这次 Agent 运行不存在：{run_id}")
            if existing.actor_id != actor_id:
                # 别人的运行不能被你接着跑（与"别人的会话一律 404"同一个口径：
                # 不告诉它存在）。**这一条是权限边界**，不是体验优化。
                raise NotFoundError(f"这次 Agent 运行不存在：{run_id}")
            if existing.status not in RESUMABLE_STATUSES:
                raise ConflictError(
                    f"这次运行的状态是 {existing.status}，不能续跑"
                    "（只有中断的运行可以续跑）"
                )
            run = existing
            recorded: list[dict[str, Any]] = list(run.trace or [])
            # 让"正在续跑"这件事立刻可见：别的请求不会再把它当成可续跑的。
            await self._run_repo.mark_running(run_id=run.id)
            await self._uow.commit()
        else:
            run = await self._run_repo.start(
                session_id=session_id,
                actor_id=actor_id,
                question=question,
            )
            # **一开始就提交**：后面若在第一步之前就被杀掉，这次运行至少是可见的
            # （否则它连"存在过"都没留下，续跑无从谈起）。
            await self._uow.commit()
            recorded = []

        async def checkpoint(trace: list[dict[str, Any]]) -> None:
            """每步之后落盘。**这就是"断点"的全部实现。**"""
            await self._run_repo.save_trace(run_id=run.id, trace=trace)
            await self._uow.commit()

        try:
            yield run.id, recorded, checkpoint
        except GeneratorExit:
            # 客户端断开（SSE 生成器被关闭）：这是**可续跑**的中断。
            # ⚠️ 这里**不能 yield**（生成器正在关闭），只能用同步的收尾路径；
            # 而 `commit` 是异步的 —— 所以退回"不写状态"，
            # 让 `sweep_stale_running` 在下次请求时把它判成 INTERRUPTED。
            # 少一次精确标记，换来的是不会在关闭过程中抛二次异常。
            logger.info("Agent 运行 %s 的客户端断开，留给 sweep 判定中断", run.id)
            raise
        except BaseException as exc:  # noqa: BLE001 - 要如实归类后重新抛出
            await self._run_repo.mark_failed(run_id=run.id, reason=type(exc).__name__)
            await self._uow.commit()
            raise

    async def _finish_run(
        self,
        *,
        run_id: uuid.UUID,
        answer: str,
        stop_reason: str,
    ) -> None:
        """把这次运行标成 `FINISHED` 并留下最终回答。

        **不传轨迹**：完整轨迹由 `checkpoint`（每步一次 `save_trace`）负责，
        这里只改状态与回答。第一版在这里用**给界面看的精简 steps** 覆盖了轨迹，
        把 `call_id` / `arguments` 抹掉了 —— 于是续跑失效（实测撞到）。

        **用请求那个会话**：与 `_agent_run` 同一个理由（见那里的说明）。
        """
        await self._run_repo.finish(
            run_id=run_id,
            status=RUN_FINISHED,
            answer=answer,
            stop_reason=stop_reason,
        )
        await self._uow.commit()

    async def _recent_history(self, *, session_id: uuid.UUID, limit: int = 6) -> list[ChatMessage]:
        """取同一会话里最近几条消息，当作**短期记忆**喂给 Agent。

        ⚠️ 这是本次改动**顺手补上的一个真缺口**：`run_agent` 的 `history`
        参数一直存在、却**从来没人传过** —— 于是 Agent 连"那第 2 张单子呢"
        这种追问都接不上（而 `chat_messages` 表与 `list_messages` 本来就有）。

        `limit` 数的是**消息条数**（一问一答算两条），除 2 才是"几轮对话"。
        取 6 = 最近 3 轮：再多会让上下文变长，而 Agent 的轮次上限本来就只有 4 ——
        把 token 花在更早的对话上不划算。

        工具调用与结果**不在这里**（它们在 `agent_runs.trace`），
        所以重建不出完整的工具上下文；但"上一句问了什么、答了什么"
        已经足够让追问接上，也不会误导模型以为工具结果还在。
        """
        rows = await self._knowledge.list_messages(session_id)
        history: list[ChatMessage] = []
        for record in rows[-limit:]:
            role = "assistant" if record.role == "ASSISTANT" else "user"
            content = (record.content or "").strip()
            if content:
                history.append(ChatMessage(role=role, content=content))
        # 最后一条是**这次刚追加进去的问题**（`_prepare` 里写的）——
        # 它会由 `run_agent` 自己再作为 `user` 消息加一次，留着就重复了。
        # 重复的问题会让模型以为用户问了两遍。
        if history and history[-1].role == "user":
            history.pop()
        return history

    def __init__(
        self,
        *,
        knowledge: KnowledgeRepository,
        object_store: ObjectStorePort,
        chat_model: ChatModelPort,
        embedder: EmbeddingPort,
        uow: UnitOfWork,
        top_k: int,
        min_score: float,
        price_config: str = "",
        purchase: PurchaseCoordinator | None = None,
        approval: ApprovalCoordinator | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        self._knowledge = knowledge
        self._store = object_store
        self._chat = chat_model
        #: 算向量的实现（本地哈希 / 真模型）。**由边界层按配置选**，这里不做判断 ——
        #: 与 `chat_model` 同一个理由：测试要注入一个"故意给错维度"的实现来验证校验，
        #: 而"选哪个实现"写死在协调者里，那条验证就只能靠真模型碰运气。
        self._embedder = embedder
        self._uow = uow
        self._top_k = top_k
        self._min_score = min_score
        #: 模型价格表配置（`model=输入价,输出价;...`），用于成本核算。
        #: **保留原始字符串**而不是解析后的字典：坏配置要在**每次真正用到时**
        #: 报一次警告（而不是启动时静默丢掉），而且改了 .env 重启即生效。
        self._price_config = price_config
        #: Agent 工具要用的两个协调者。**默认 `None` = 关掉 Agent 路径**：
        #: 没有它们时行为与加 Agent 之前**完全一致**（直接回"制度中没有找到相关规定"）。
        #: 这个默认值让"加了 Agent"这件事可以被逐层接上，而不是一次性改完所有装配点。
        self._purchase = purchase
        self._approval = approval
        #: Agent 运行记录（M12++）。**用请求那个会话** —— `agent_runs.session_id`
        #: 外键指向 `chat_sessions`，而那个会话行是 `_prepare` 在同一次请求里
        #: 刚建、还没提交的；独立会话看不到它，插入会直接外键违约（实测撞到过，
        #: 见 `_agent_run` 的说明）。传 `None` 时才退回自己开会话 ——
        #: 那条路只在"没有请求会话"的离线脚本里成立。
        from app.infrastructure.repositories.agent_run_repo import AgentRunRepository

        self._run_repo = AgentRunRepository(session or SessionFactory())

    # ------------------------------------------------------------------ 文档入库

    async def upload_doc(
        self,
        *,
        actor_id: uuid.UUID,
        source_key: str,
        title: str,
        version: int | None,
        effective_from: datetime | None,
        filename: str | None,
        content_type: str,
        raw: bytes,
    ) -> dict[str, Any]:
        """上传一份制度文档：落原文 → 切分 → 算向量 → 存片段。

        `version` 不传就**自动取下一版**（`MAX + 1`）—— 这是最常见的用法：
        "这是这份制度的新版本"。传了就按传的算（允许补录历史版本）。

        ============================ 失败也要留下文档 ============================
        切分 / 向量化失败时，文档**留在 `FAILED` 并带上原因**，而不是根本不建。
        否则用户看到的是"上传失败了"，但不知道是文件的问题还是系统的问题；
        留下一条 `FAILED` 记录，管理页就能显示"为什么这一版没生效"。
        """
        source_key = source_key.strip()
        title = title.strip()
        if not source_key or not title:
            raise BusinessError(
                "source_key 与标题都不能为空（source_key 是「同一份制度」的稳定标识）",
                status_code=422,
                code="KNOWLEDGE_DOC_FIELDS_REQUIRED",
            )
        # 类型：**先看声明，只在声明缺失时**按扩展名补全一次。
        #
        # ⚠️ 顺序不能反。反过来（扩展名优先）会让"声明 application/pdf、文件名 .txt"
        # 这种组合被当成纯文本收下 —— 实测踩到：一条"应当被拒"的用例变成了 201。
        # 声明是本意，扩展名只是它在 multipart 里丢了之后的**兜底**。
        resolved_type: str | None = content_type
        if resolved_type in (None, "", "application/octet-stream"):
            resolved_type = _infer_content_type(filename)
        if resolved_type not in ALLOWED_CONTENT_TYPES:
            raise BusinessError(
                f"制度文档目前只收纯文本与 Markdown（收到 {resolved_type or '未知类型'}）",
                status_code=422,
                code="KNOWLEDGE_DOC_TYPE_UNSUPPORTED",
            )
        # 补全之后的类型要带下去：后面按它选落盘后缀
        content_type = resolved_type

        text = normalize_text(self._decode(raw))
        if not text:
            raise BusinessError(
                "文件里没有可用的文本内容", status_code=422, code="KNOWLEDGE_DOC_EMPTY"
            )
        if len(text) > MAX_DOC_CHARS:
            raise BusinessError(
                f"文本长度 {len(text)} 超过上限 {MAX_DOC_CHARS} 字符"
                "（这一版会全量切分并逐片算向量，太大请拆分后分次上传）",
                status_code=422,
                code="KNOWLEDGE_DOC_TOO_LARGE",
            )

        if version is None:
            version = await self._knowledge.latest_version_of(source_key) + 1
        elif version <= 0:
            raise BusinessError("版本号必须为正整数", status_code=422, code="KNOWLEDGE_VERSION_INVALID")
        # **显式查重**，把"这一版传过了"变成一句人话的 409。
        # 不查的话，唯一的拦截点是切分/向量化之后的 flush —— 那时异常从
        # "插入片段"那条 SQL 冒出来，`except IntegrityError` 接不住，
        # 用户看到的是 503「数据库暂不可用」（实测踩到，指错方向）。
        # 并发下仍然由 `uq_knowledge_doc_version` 兜底（见下面那个 except）。
        if await self._knowledge.version_exists(source_key=source_key, version=version):
            raise ConflictError(
                f"《{title}》的第 {version} 版已经存在了（同一份制度同一版本只能传一次）"
            )

        # 原文落盘（**UUID 命名**，沿用 M9 纪律：永不用用户提供的文件名）
        suffix = ALLOWED_CONTENT_TYPES[content_type]
        storage_key = self._store.save(raw, suffix)

        doc = await self._knowledge.create_doc(
            source_key=source_key,
            title=title,
            version=version,
            effective_from=effective_from or datetime.now(UTC),
            storage_key=storage_key,
            filename=filename,
            content_type=content_type,
            content=text,
            embedding_model=self._embedder.name,
            uploaded_by=actor_id,
        )
        try:
            await self._index(doc.id, text)
        except IntegrityError as exc:
            # `UNIQUE(source_key, version)`：这一版已经传过了。
            # 落盘的原文要删掉 —— 否则文件永远留在磁盘上没人引用
            # （对象存储没有"垃圾回收"，只有人工清理脚本会看它）。
            await self._uow.rollback()
            self._store.delete(storage_key)
            raise ConflictError(
                f"《{title}》的第 {version} 版已经存在了（同一份制度同一版本只能传一次）"
            ) from exc

        await self._uow.commit()
        # 与 `list_docs` **同一个形状**（直接复用它，不手拼第二份）：
        # 上传的响应体与列表里那一行长得一样，前端才能只写一套渲染。
        # 手拼一份的代价是实测过的 —— 少三个字段（`filename` / `failure_reason` /
        # `created_at`）而 Pydantic 对**可选**字段缺失照样报错，成了"上传成功但响应 500"。
        return await self._doc_payload(doc)

    async def _doc_payload(self, doc: Any) -> dict[str, Any]:
        """一条文档的对外形状（上传与列表共用）。"""
        return {
            "id": str(doc.id),
            "source_key": doc.source_key,
            "title": doc.title,
            "version": doc.version,
            "effective_from": doc.effective_from.isoformat(),
            "filename": doc.filename,
            "status": doc.status,
            "embedding_model": doc.embedding_model,
            "chunk_count": await self._knowledge.chunk_count_of(doc.id),
            "content_chars": len(doc.content),
            "failure_reason": doc.failure_reason,
            "created_at": doc.created_at.isoformat(),
        }

    async def _index(self, doc_id: uuid.UUID, text: str) -> int:
        """切分 + 算向量 + 入库。返回片段数。

        向量在**这里**算（而不是数据库里）：数据库只负责"存向量、按距离排序"，
        "怎么把文本变成向量"是模型的事 —— 把它塞进 SQL 会绑定死某一种实现。
        """
        chunks = chunk_text(text)
        if not chunks:
            await self._knowledge.mark_failed(doc_id, reason="切分后没有任何片段")
            raise BusinessError(
                "切分后没有任何片段（文本可能全是空白）",
                status_code=422,
                code="KNOWLEDGE_CHUNK_EMPTY",
            )
        # 向量**批量算**：一份制度几十个片段，逐条发 HTTP 会把"上传一份制度"
        # 变成几十次往返（真模型时这一步是主要耗时）。
        vectors = await self._embedder.embed([chunk.content for chunk in chunks])
        prepared = [
            (
                chunk.chunk_no,
                chunk.content,
                chunk.start_offset,
                chunk.end_offset,
                vector,
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        count = await self._knowledge.replace_chunks(doc_id, prepared)
        await self._knowledge.mark_ready(doc_id)
        return count

    @staticmethod
    def _decode(raw: bytes) -> str:
        """字节 → 文本。**解码失败要报出来**，不能悄悄用替换字符。

        `errors="replace"` 会把乱码变成 `\\ufffd`，于是"文件编码不对"这件事
        就变成了"文本里有一堆问号" —— 切分照样成功、检索照样能跑，
        只是检索出来的是垃圾。GBK 文件（国内制度文档很常见）就会踩这一条。
        """
        for encoding in ("utf-8", "utf-8-sig", "gb18030"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise BusinessError(
            "文件编码无法识别（试过 UTF-8 / GB18030）—— 请另存为 UTF-8 后重传",
            status_code=422,
            code="KNOWLEDGE_DOC_ENCODING",
        )

    # ------------------------------------------------------------------ 管理页读接口

    async def list_docs(self) -> list[dict[str, Any]]:
        docs = await self._knowledge.list_docs()
        return [await self._doc_payload(doc) for doc in docs]

    async def list_chunks(self, *, doc_id: uuid.UUID) -> list[dict[str, Any]]:
        """这份文档的切分结果（**排障入口**：一眼看出是没检索到还是切碎了）。"""
        doc = await self._knowledge.get_doc(doc_id)
        if doc is None:
            raise NotFoundError(f"制度文档不存在：{doc_id}")
        chunks = await self._knowledge.list_chunks(doc_id)
        return [
            {
                "chunk_no": chunk.chunk_no,
                "content": chunk.content,
                "start_offset": chunk.start_offset,
                "end_offset": chunk.end_offset,
                "length": chunk.end_offset - chunk.start_offset,
            }
            for chunk in chunks
        ]

    # ------------------------------------------------------------------ 会话

    async def list_sessions(self, *, user_id: uuid.UUID) -> list[dict[str, Any]]:
        sessions = await self._knowledge.list_sessions(user_id=user_id)
        return [
            {
                "id": str(session.id),
                "title": session.title,
                "created_at": session.created_at.isoformat(),
            }
            for session in sessions
        ]

    async def session_messages(
        self, *, user_id: uuid.UUID, session_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        session = await self._knowledge.get_session(session_id)
        if session is None or session.user_id != user_id:
            # 别人的会话一律 404（不告诉它存在）—— 与全项目的可见范围口径一致
            raise NotFoundError(f"会话不存在：{session_id}")
        messages = await self._knowledge.list_messages(session_id)
        return [
            {
                "id": str(message.id),
                "role": message.role,
                "content": message.content,
                "seq": message.seq,
                "sources": message.sources,
                "citation_problems": message.citation_problems,
                "used_model": message.used_model,
                "top_score": message.top_score,
                "created_at": message.created_at.isoformat(),
            }
            for message in messages
        ]

    # ------------------------------------------------------------------ 提问（核心）

    async def ask(
        self,
        *,
        user_id: uuid.UUID,
        question: str,
        session_id: uuid.UUID | None,
        actor: Principal | None = None,
        run_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """非流式提问：把整段回答拼好再返回（给 curl / 测试用）。

        与 `ask_stream` **共用同一段逻辑**（`_prepare`），
        所以两条路不会出现"流式说没依据、非流式却调了模型"这种不一致。

        ⚠️ 实测踩到：Agent 分流**只加在 `ask_stream` 里**时，
        非流式那条路会照旧回"制度中没有找到相关规定"，
        而 curl 与集成测试打的偏偏是非流式那条 ——
        表现是"功能明明写了却没有效果"。所以两条路的分流由
        `_should_use_agent` **同一个判据**给出。
        """
        prepared = await self._prepare(user_id=user_id, question=question, session_id=session_id)
        prepared["actor"] = actor
        # 用量收在一个单元素列表里：闭包要**写回外层变量**，
        # 而 Python 的嵌套函数对不可变外层变量只能读（`nonlocal` 在这个作用域层级不好用）。
        captured: list[ModelUsage] = []

        model_started = time.perf_counter()

        if self._should_use_agent(prepared):
            (
                answer,
                steps,
                usage,
                stop_reason,
                run_id_used,
                reused_steps,
            ) = await self._collect_agent(prepared, question=question, run_id=run_id)
            model_ms = int((time.perf_counter() - model_started) * 1000)
            return await self._finish(
                prepared,
                answer=answer,
                model_ms=model_ms,
                usage=usage,
                agent_steps=steps,
                agent_stop_reason=stop_reason,
                agent_run_id=str(run_id_used) if run_id_used else None,
                reused_steps=reused_steps,
            )

        if not prepared["decision"].use_model:
            answer = prepared["decision"].fallback_answer
        else:
            chunks: list[str] = []
            async for piece in self._chat.stream(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=prepared["user_prompt"],
                sources=prepared["sources"],
                on_usage=captured.append,
            ):
                chunks.append(piece)
            answer = "".join(chunks)
        model_ms = int((time.perf_counter() - model_started) * 1000)
        return await self._finish(
            prepared,
            answer=answer,
            model_ms=model_ms,
            usage=captured[-1] if captured else None,
        )

    def _should_use_agent(self, prepared: dict[str, Any]) -> bool:
        """这次提问该走 Agent 还是走制度问答。

        ============================ 判据只有一条：**检索给的依据够不够** ============================
            有依据（超过阈值）      → 制度问答（依据原文回答 + 两层引用校验）
            没依据 + 模型会用工具    → Agent（去查真实业务数据）
            没依据 + 模型不会用工具  → 原来的"制度中没有找到相关规定"

        为什么不额外看"问题里有没有关键词"：那要靠规则去猜自然语言，
        猜错的方向有两个（把数据问题当制度问题、反之），而两个方向都很难被发现。
        **相似度是本系统已经算出来的信号**，用它更稳、也更容易解释。

        `ask` 与 `ask_stream` **都调这一个方法** —— 判据只有一处，
        两条路不可能给出不同的结论。
        """
        return (
            not prepared["decision"].use_model
            and self._chat.supports_tools
            and self._purchase is not None
            and self._approval is not None
        )

    def _used_remote(self, decision: Any, *, agent_used: bool = False) -> bool:
        """这次回答有没有真的调用**远端模型**。

        ⚠️ 语义是"远端模型有没有被调用"，**不是**"要不要生成回答"。
        两者在本地兜底时分开：那时 `decision.use_model` 是 True（有依据、可以生成），
        但一次网络调用都没发生 —— 而 `used_model` 这个字段存在的意义，
        正是让"未调用模型"这件事在界面与日志里看得见（一条验收项）。
        实测踩到：把它写成 `decision.use_model`，那条验收项在本地兜底下永远显示错误。

        ============================ 第二个踩到的坑：Agent 走的是另一条路 ============================
        上面那个 `isinstance(self._chat, LocalExtractiveChat)` 判断**只覆盖 `stream()`**。
        Agent 走的是 `stream_turn()`，于是当 `decision.use_model` 是 False
        （检索没依据）而模型**确实被调用了**（去查业务数据）时，
        这个字段会返回 `False` —— **审计字段在说谎**。
        实测踩到：一个明明花了 4 秒模型时间的 Agent 回答，库里记的是"未调用模型"。

        所以调用方要把它知道的 Agent 事实传进来（`agent_used`）。
        """
        if agent_used:
            return True
        return decision.use_model and not isinstance(self._chat, LocalExtractiveChat)

    async def ask_stream(
        self,
        *,
        user_id: uuid.UUID,
        question: str,
        session_id: uuid.UUID | None,
        actor: Principal | None = None,
        run_id: uuid.UUID | None = None,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """流式提问：产出 `(事件名, 数据)`，由边界层翻成 SSE。

        ============================ 事件顺序（协议的一部分） ============================
            `meta`  → 会话 id、来源列表、是否调用模型、为什么
            `delta` → 正文片段（可能很多条；未调用模型时只有一条）
            `done`  → 结束（带落库后的消息 id）
            `error` → 出错（**不静默截断**）

        **`meta` 必须先发**（计划里专门点了这一条）：用户能在正文还没出来时就
        先看到引用来源，信任感强很多；反过来（先出正文再补来源）用户读完一段
        断言才发现没有出处，那时的体感是"它在编"。

        `sources` 在 `meta` 里给全，而不是等正文里的 `[n]` 出现再补 ——
        前端因此可以先把引用卡片渲染出来。
        """
        prepared = await self._prepare(
            user_id=user_id, question=question, session_id=session_id
        )
        # 把身份放进 `prepared`：Agent 分支要用它当**工具调用的权限来源**。
        # 它由边界层传下来（就是那个已登录用户），协调者不自己查 —— 见 `_principal_for`。
        prepared["actor"] = actor
        sources: list[Source] = prepared["sources"]
        decision = prepared["decision"]
        # `used_model` 的语义是"**远端模型**有没有被调用"，不是"要不要生成回答"。
        # 两者的区别在本地兜底时体现出来：那时 `decision.use_model` 是 True
        # （有依据、可以生成），但**一次网络调用都没发生**。
        # 实测踩到：把它写成 `decision.use_model`，于是"未调用模型"这条
        # 验收项在本地兜底下永远显示成"已调用" —— 而它恰恰是要证明这件事的字段。
        #
        # ⚠️ **Agent 分流要先算**：Agent 回答的 `decision.use_model` 是 False
        # （检索没依据），但它**确实调用了模型**（去查业务数据）。
        # 如果这里照旧用 `decision`，`meta` 会告诉前端"未调用模型"，
        # 页面就会显示那个徽标 —— 而这是**一条验收项**，不能给它错的值。
        #
        # ⚠️ 但 `will_use_agent` **不等于"模型一定被使用"**（实测踩到）：
        # Agent 若**一个工具都没调**，`_answer_with_agent` 会退回本地兜底那句确定性话术
        # （模型答不了制度问题，见那边的说明）—— 那时采用的字**不是模型生成的**，
        # `used_model` 就该是 `False`。
        #
        # ============================ 预报 vs 事实 ============================
        # `meta` 必须**在正文之前**发出去（用户要先看到引用来源），所以它只能**预报**；
        # 而"模型到底有没有被采用"要到 Agent 跑完才知道 —— **预报不可能总对**。
        # 所以真相走 `done`：`done.used_model` 由 `_finish` 按事实算
        # （见那里 `agent_used_tools` 的说明）。前端拿 `done` 覆盖 `meta` 的预报。
        will_use_agent = self._should_use_agent(prepared)
        used_remote = self._used_remote(decision, agent_used=will_use_agent)
        yield (
            "meta",
            {
                "session_id": str(prepared["session"].id),
                "sources": [self._source_payload(source) for source in sources],
                "used_model": used_remote,
                "reason": self._reason(
                    decision,
                    used_remote=used_remote,
                    # 预报：这一刻只知道"会不会走 Agent"。**事实**在 `done` 里
                    # （那里 `agent_ran` 按轨迹判断，前端应以 `done` 为准）。
                    agent_ran=will_use_agent,
                ),
                "model": self._chat.name if used_remote else None,
            },
        )

        pieces: list[str] = []
        usage: ModelUsage | None = None

        def capture_usage(reported: ModelUsage) -> None:
            """模型报用量时收下它（流式接口只在最后一帧报，且**可能不报**）。"""
            nonlocal usage
            usage = reported

        # 模型耗时**只量这一段**：不含检索、不含落库。
        # 分开量的理由见 `chat_messages.retrieval_ms` 的注释 ——
        # 合起来只记一个"总耗时"就分不清该改检索还是该改模型。
        model_started = time.perf_counter()

        # ============================ 分流：见 `_should_use_agent` ============================
        # 判据只有一处 —— 这里与 `ask` 调的是同一个方法。
        # 复用上面已经算好的结果（同一个方法调两次虽然无害，但会让"判据只有一处"
        # 这句话在代码里看起来不成立）。
        if will_use_agent:
            async for event in self._answer_with_agent(
                prepared,
                question=question,
                model_started=model_started,
                run_id=run_id,
            ):
                yield event
            return

        if not decision.use_model:
            # **不调用模型**：直接给那句固定回答。文字不进模型、不进网络
            yield "delta", {"text": decision.fallback_answer}
            pieces.append(decision.fallback_answer)
        else:
            try:
                async for piece in self._chat.stream(
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=prepared["user_prompt"],
                    sources=sources,
                    on_usage=capture_usage,
                ):
                    pieces.append(piece)
                    yield "delta", {"text": piece}
            except Exception as exc:  # noqa: BLE001
                # 明确告知，**不静默截断**：已经吐出去的部分保留（用户看到了什么就是什么），
                # 但必须有一条 error 让它知道后面还有内容没出来。
                # 这条例外**不吞**：它要被记进日志，否则线上"偶尔答一半"永远查不出来。
                await self._record_failure(prepared, reason=f"模型调用失败：{exc}")
                yield "error", {"message": f"模型调用失败：{exc}"}
                return
        model_ms = int((time.perf_counter() - model_started) * 1000)

        result = await self._finish(
            prepared, answer="".join(pieces), model_ms=model_ms, usage=usage
        )
        yield "done", {
            "message_id": result["message_id"],
            "citation_problems": result["citation_problems"],
            # 支撑性问题也随 `done` 发给前端与评测脚本 ——
            # **评测脚本靠流式接口也能拿到指标**，不必为了统计再打一次非流式接口。
            "support_problems": result["support_problems"],
            "support_rate": result["support_rate"],
            # 来源随 `done` 再带一次（`meta` 里已经给过）：边界层要在**记账时**
            # 用它估算这次问答的 token 量，而 `meta` 事件早已流过。
            # 上下文的主体是这些片段（5 条 × 600 字 ≈ 3000 token），不是那句问题。
            "sources": result["sources"],
            # token 与成本也随 `done` 出去：**拿得到就用真实值**，
            # 拿不到就是 `None`（边界层会退化成估算上界去记配额）。
            "prompt_tokens": result["prompt_tokens"],
            "completion_tokens": result["completion_tokens"],
            "estimated_cost_micros": result["estimated_cost_micros"],
        }

    # ------------------------------------------------------------------ 内部

    async def _prepare(
        self,
        *,
        user_id: uuid.UUID,
        question: str,
        session_id: uuid.UUID | None,
    ) -> dict[str, Any]:
        """提问的**公共前半段**：存问题 → 检索 → 阈值判定 → 拼 Prompt。

        流式与非流式共用它，所以"要不要问模型"这个决定只有一处。
        """
        question = question.strip()
        if not question:
            raise BusinessError("问题不能为空", status_code=422, code="CHAT_QUESTION_EMPTY")
        if len(question) > 500:
            raise BusinessError(
                "问题过长（上限 500 字）", status_code=422, code="CHAT_QUESTION_TOO_LONG"
            )

        session = await self._ensure_session(
            user_id=user_id, session_id=session_id, question=question
        )
        seq = await self._knowledge.next_seq(session.id)
        await self._knowledge.append_message(
            session_id=session.id, role="USER", content=question, seq=seq
        )

        # 检索耗时**把"算查询向量"也算进去**：真模型时这一步是检索的主要耗时
        # （一次 HTTP 往返 + 一次前向），不算进去的话
        # `retrieval_ms` 会显示成"1 毫秒"，让人以为检索很快、问题在别处。
        retrieval_started = time.perf_counter()
        query_embedding = (await self._embedder.embed([question]))[0]
        if not any(query_embedding):
            # 全零向量：**只有哈希实现在"问题里一个词都没有"时会给出**（例如全是标点）。
            # 此时不检索 —— 全零向量与任何向量的余弦距离没有意义（NULL 或 1），
            # 排序结果不可解释。
            # ⚠️ 真模型不会返回全零向量，所以这个分支对它是死代码；
            # 保留它是因为"哈希 + 离线"仍是支持的配置（见 `LocalHashEmbedding`）。
            sources: list[Source] = []
        else:
            rows = await self._knowledge.search_chunks(
                query_embedding=query_embedding,
                top_k=self._top_k,
                # 只检索**当前模型**算的向量：混着检索的结果不可比（见仓库说明）
                embedding_model=self._embedder.name,
            )
            sources = [self._to_source(index, row) for index, row in enumerate(rows, start=1)]
        retrieval_ms = int((time.perf_counter() - retrieval_started) * 1000)

        decision = decide_grounding(sources, min_score=self._min_score)
        return {
            "session": session,
            "retrieval_ms": retrieval_ms,
            "sources": sources,
            "decision": decision,
            "user_prompt": build_user_prompt(question, sources) if sources else "",
        }

    async def _ensure_session(
        self, *, user_id: uuid.UUID, session_id: uuid.UUID | None, question: str
    ) -> Any:
        """拿到会话；没给就新建一个，标题取问题的前若干字。

        会话**归属校验**在这里：给了别人的 session_id 一律 404，
        不能把问题追加到别人的会话里（那既是越权、也会把两个人的对话混在一起）。
        """
        if session_id is None:
            return await self._knowledge.create_session(
                user_id=user_id, title=question[:TITLE_LENGTH]
            )
        session = await self._knowledge.get_session(session_id)
        if session is None or session.user_id != user_id:
            raise NotFoundError(f"会话不存在：{session_id}")
        return session

    def _reason(
        self, decision: Any, *, used_remote: bool, agent_ran: bool = False
    ) -> str:
        """给人和日志看的一句话：为什么是这个回答方式。

        四种情况要说得出区别，否则排障时分不清"没检索到"、"阈值太高"、
        "用的是兜底而不是模型"、"走了 Agent 但没用工具" —— 它们的修法完全不同。
        """
        if agent_ran and not used_remote:
            # Agent 跑过、但一个工具都没调（于是退回本地兜底那句确定性话术）。
            # ⚠️ 不特判这一种的话，这里会回 `decision.reason`（"检索结果为空"之类）——
            # 那句话对制度问答是对的，对 Agent 却是**指错方向**：
            # 真正发生的是"模型答不了这个问题、也没去查数据"。
            return "已交给 Agent 处理，但它没有调用任何工具；本次用本地兜底回答"
        if not decision.use_model:
            return decision.reason
        if used_remote:
            return decision.reason
        return f"{decision.reason}；本次用**本地兜底**整理原文，未调用远端模型"

    def _to_source(self, number: int, row: tuple[Any, Any, float]) -> Source:
        """检索结果 → 领域层的 `Source`。

        **相似度 = 1 − 余弦距离**（唯一的换算处）：SQL 按**距离升序**排，
        而对外给的是**分数越高越相关**。把换算只留在这里，
        排序与分数就不会各写一遍、各错一次。

        `max(0.0, ...)`：浮点误差可能让相等的向量算出 `1e-16` 的距离，
        于是分数变成 `0.9999999999`；反过来也可能出现略大于 1 的值。
        夹到 `[0, 1]` 让"分数"这个数在界面上始终可解释。
        """
        chunk, doc, distance = row
        return Source(
            number=number,
            doc_id=str(doc.id),
            doc_title=doc.title,
            doc_version=doc.version,
            chunk_no=chunk.chunk_no,
            content=chunk.content,
            score=max(0.0, min(1.0, 1.0 - distance)),
            # 片段主键随来源一起带出去：**评测的锚点**（见 `Source.chunk_id`）。
            # 界面上用不到它，但少带它评测就没法判"期望的那一片有没有被检索到"。
            chunk_id=str(chunk.id),
        )

    @staticmethod
    def _source_payload(source: Source) -> dict[str, Any]:
        return {
            "number": source.number,
            "doc_id": source.doc_id,
            "doc_title": source.doc_title,
            "doc_version": source.doc_version,
            "chunk_no": source.chunk_no,
            "chunk_id": source.chunk_id,
            "content": source.content,
            "score": round(source.score, 4),
        }

    async def _answer_with_agent(
        self,
        prepared: dict[str, Any],
        *,
        question: str,
        model_started: float,
        run_id: uuid.UUID | None = None,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """走 Agent：让模型自己决定调哪些工具，再把结果拼成回答。

        ============================ 这里的三条纪律 ============================
        ① **身份只有一个来源**：`Principal` 由调用方（边界层）传下来，
           与 HTTP 请求里那个是同一份数据 —— 工具因此继承调用者的权限范围，
           不存在"两处身份可能不一致"的空间；
        ② **工具步骤实时推给前端**（`step` 事件）：用户想知道"它在干什么"，
           而"转圈 20 秒然后一次性给答案"会让人以为卡住了；
        ③ **引用校验照旧跑**：Agent 的回答可能引用制度片段（它没有检索结果，
           但可能凭记忆说起制度），所以 `_finish` 的两层校验**一次都不能少**。

        ============================ M12++：先发 run_id，再跑 ============================
        第一个 SSE 事件是 `step`，但我们在跑之前就知道 `run_id` 了 ——
        所以把它放进**最早的** `step` 事件的一个字段里不合适（`step` 的形状是固定的）。
        改成：`run_id` 随 `done` 事件回去（那是权威值），
        而**前端要续跑时是自己带 `run_id` 进来的**（`POST /chat {run_id}`），
        所以它不需要从流里拿 —— 流里回传只是为了排障与前端显示。
        """
        assert self._purchase is not None and self._approval is not None  # noqa: S101
        actor = await self._principal_for(prepared)
        session = prepared["session"]

        pieces: list[str] = []
        steps: list[dict[str, Any]] = []
        usage: ModelUsage | None = None
        stop_reason = "answered"
        rounds = 0
        reused = 0
        actual_run_id = run_id

        async with self._agent_run(
            session_id=session.id,
            actor_id=actor.id,
            question=question,
            run_id=run_id,
        ) as (this_run_id, resume_from, checkpoint):
            actual_run_id = this_run_id
            # 短期记忆：同一会话里最近几轮（见 `_recent_history` 的说明）
            history = await self._recent_history(session_id=session.id)

            async for item in run_agent(
                question=question,
                actor=actor,
                chat_model=self._chat,
                purchase=self._purchase,
                approval=self._approval,
                history=history,
                resume_from=resume_from,
                on_step=checkpoint,
            ):
                if isinstance(item, AgentStep):
                    steps.append(
                        {
                            "round": item.round_no,
                            "kind": item.kind,
                            "detail": item.detail,
                            "tool": item.call.name if item.call else None,
                        }
                    )
                    # 实时推给前端。`tool_call` 与 `tool_result` 分开是两个有用的信号：
                    # "正在查"与"查到了"—— 后者才是用户等的那一下。
                    yield "step", steps[-1]
                    continue
                # AgentResult
                pieces.append(item.answer)
                usage = item.usage
                stop_reason = item.stop_reason
                rounds = item.rounds
                reused = item.reused_steps

            # 跑到这里说明循环正常结束（没有异常、生成器没被关）→ 记为 FINISHED。
            # 用 `RUN_FINISHED` 而不是"删掉这次运行"：**修完的记录本身是证据**
            # （"它跑过、跑到哪、怎么停的"），也是前端列出历史的来源。
            await self._finish_run(
                run_id=this_run_id,
                answer="".join(pieces),
                stop_reason=stop_reason,
            )

        # ============================ 一个工具都没调 → 退回确定性话术（实测踩到）============================
        # **模型没有查制度的工具**（4 个工具全是业务数据类），所以"制度里有没有这条规定"
        # 它答不了 —— 而它不会说自己答不了，只会给一句半截的"系统里没有查到"。
        # 那条路**绕过了本地兜底那句确定性话术**（"制度中没有找到相关规定"），
        # 于是同一个问题在不同后端配置下给出**两套不同的回答**：
        # 没配模型时是那句标准话术，配了模型（Agent 打开）时变成模型随口一句。
        #
        # 判据是"**真的调过工具吗**"：调了工具 = 这次回答的依据来自业务数据，用它；
        # 一个都没调 = 它接不了这个问题 → 走原来那条确定性路径（含 `decide_grounding` 的判定），
        # 与没有 Agent 时的行为**完全一致**。
        #
        # ⚠️ 这同时修掉一个**不一致**：`meta` 事件按"将会走 Agent"报 `used_model=True`，
        # 而 `_finish` 按"模型是否真的被调用"算 —— 一个没调工具的回答会让两者说法相反，
        # 页面于是不显示「未调用模型」徽标、回答却又是本地兜底的原话。
        # 退回确定性路径之后，两条路都不再调用模型，`meta` 与落库结果一致。
        if not agent_used_tools(steps):
            # 判据与说明在 `_settle_agent_answer`（非流式那条路也走它）
            fallback = str(prepared["decision"].fallback_answer)
            yield "delta", {"text": fallback}
            model_ms = int((time.perf_counter() - model_started) * 1000)
            # ⚠️ `_finish` 里 `agent_used` 必须传"**真的调过工具**"而不是"走过 Agent 路径"：
            # 走这条兜底分支时**没有任何模型输出被采用**，落库的 `used_model` 就该是 `False` ——
            # 与 `prepared["decision"]`（本地兜底）的说法一致。
            result = await self._finish(
                prepared,
                answer=fallback,
                model_ms=model_ms,
                usage=usage,
                agent_steps=steps,
                agent_stop_reason=stop_reason,
                agent_run_id=str(actual_run_id) if actual_run_id else None,
            )
            yield "done", self._done_payload(
                result,
                rounds=rounds,
                stop_reason=stop_reason,
                run_id=actual_run_id,
                reused_steps=reused,
            )
            return

        answer = "".join(pieces)
        yield "delta", {"text": answer}

        model_ms = int((time.perf_counter() - model_started) * 1000)
        result = await self._finish(
            prepared,
            answer=answer,
            model_ms=model_ms,
            usage=usage,
            agent_steps=steps,
            agent_stop_reason=stop_reason,
            agent_run_id=str(actual_run_id) if actual_run_id else None,
        )
        yield "done", self._done_payload(
            result,
            rounds=rounds,
            stop_reason=stop_reason,
            run_id=actual_run_id,
            reused_steps=reused,
        )

    @staticmethod
    def _done_payload(
        result: dict[str, Any],
        *,
        rounds: int,
        stop_reason: str,
        run_id: uuid.UUID | None = None,
        reused_steps: int = 0,
    ) -> dict[str, Any]:
        """Agent 两条出口（用到工具 / 没用工具）**共用的 `done` 事件体**。

        抽出来是为了让两个出口的字段**不可能漂移** —— 它们是同一个协议事件，
        少一个字段或改一个名字，前端就要有两套解析。
        """
        return {
            "message_id": result["message_id"],
            "citation_problems": result["citation_problems"],
            "support_problems": result["support_problems"],
            "support_rate": result["support_rate"],
            "sources": result["sources"],
            "prompt_tokens": result["prompt_tokens"],
            "completion_tokens": result["completion_tokens"],
            "estimated_cost_micros": result["estimated_cost_micros"],
            #: **`used_model` 的权威值**（事实，不是预报）。
            #: `meta` 里那个是发正文之前的预报，可能在 Agent 没调工具时偏乐观；
            #: 这里的是 `_finish` 按"模型有没有真的被采用"算出来的，前端应以它为准。
            "used_model": result["used_model"],
            #: 为什么没调用模型 —— 同样以 `done` 为准（与 `used_model` 配对）
            "reason": result["reason"],
            #: **这次 Agent 运行的 id**（M12++）。前端拿它在中断后用
            #: `POST /chat {run_id}` 续跑 —— 续跑时已跑过的步骤不会重新执行。
            "agent_run_id": str(run_id) if run_id else None,
            #: 这次**复用了多少步**已记录的结果（0 = 全新执行）。
            #: 要发出去：续跑到底有没有生效，判据就是它非零 ——
            #: 否则"续跑"这件事只能靠日志猜。
            "agent_reused_steps": reused_steps,
            # Agent 专属信息：轮次与停止原因。**要发出去** ——
            # 它是"这次为什么停了"的唯一线索（超时？轮次用完？工具一直失败？），
            # 而这些情况的用户体感都是"没答好"，不给原因就没法归因。
            "agent_rounds": rounds,
            "agent_stop_reason": stop_reason,
        }

    async def _collect_agent(
        self, prepared: dict[str, Any], *, question: str, run_id: uuid.UUID | None = None
    ) -> tuple[str, list[dict[str, Any]], ModelUsage | None, str, uuid.UUID | None, int]:
        """非流式地把 Agent 跑完（`ask` 用它；`ask_stream` 那条路要逐步推给前端）。

        返回 `(answer, steps, usage, stop_reason, run_id)`。
        `run_id` 要回给边界层：非流式响应里带上它，客户端才能在中断后用同一个 id 续跑。

        ⚠️ **断点在这里必须真的落盘**：非流式没有 SSE 可断，
        但进程一样会在半路被杀 —— 所以同样走 `_agent_run` 生命周期
        （独立的检查点会话 + 每步 commit）。
        """
        assert self._purchase is not None and self._approval is not None  # noqa: S101
        actor = await self._principal_for(prepared)
        session = prepared["session"]

        pieces: list[str] = []
        steps: list[dict[str, Any]] = []
        usage: ModelUsage | None = None
        stop_reason = "answered"
        actual_run_id: uuid.UUID | None = run_id
        reused_steps = 0

        async with self._agent_run(
            session_id=session.id,
            actor_id=actor.id,
            question=question,
            run_id=run_id,
        ) as (this_run_id, resume_from, checkpoint):
            actual_run_id = this_run_id
            history = await self._recent_history(session_id=session.id)

            async for item in run_agent(
                question=question,
                actor=actor,
                chat_model=self._chat,
                purchase=self._purchase,
                approval=self._approval,
                history=history,
                resume_from=resume_from,
                on_step=checkpoint,
            ):
                if isinstance(item, AgentStep):
                    steps.append(
                        {
                            "round": item.round_no,
                            "kind": item.kind,
                            "detail": item.detail,
                            "tool": item.call.name if item.call else None,
                        }
                    )
                    continue
                pieces.append(item.answer)
                usage = item.usage
                stop_reason = item.stop_reason

            await self._finish_run(
                run_id=this_run_id,
                answer="".join(pieces),
                stop_reason=stop_reason,
            )

        answer, steps = self._settle_agent_answer(
            prepared, answer="".join(pieces), steps=steps
        )
        return answer, steps, usage, stop_reason, actual_run_id, reused_steps

    @staticmethod
    def _settle_agent_answer(
        prepared: dict[str, Any], *, answer: str, steps: list[dict[str, Any]]
    ) -> tuple[str, list[dict[str, Any]]]:
        """**一个工具都没调 → 退回确定性话术。**

        流式与非流式**共用这一个判据**，理由见 `_answer_with_agent` 里那段说明：
        模型没有查制度的工具，所以"制度里有没有这条规定"它答不了 —— 而它不会承认，
        只会给一句半截的"系统里没有查到"。那条路会**绕过本地兜底那句确定性话术**，
        于是同一个问题在"配了模型"和"没配模型"两种后端配置下给出**两套不同的回答**。

        判据是"**真的调过工具吗**"：调了 → 这次回答的依据来自业务数据，用它；
        一个都没调 → 它接不了这个问题 → 换成 `decide_grounding` 给出的那句标准话术。

        返回 `(最终回答, 最终轨迹)` —— 轨迹原样带回（它是诊断信息，
        "模型没调工具"这件事本身要能从落库的 `support_details` 里看出来）。
        """
        if agent_used_tools(steps):
            return answer, steps
        return str(prepared["decision"].fallback_answer), steps

    async def _principal_for(self, prepared: dict[str, Any]) -> Principal:
        """取回调用者身份（**工具的权限来源**）。

        ============================ 为什么由调用方传入，而不是在这里查 ============================
        边界层手里已经有 `CurrentUser`（就是那个已登录用户）。在这里再按
        `session.user_id` 反查一次是**多余的数据库往返**，而且引入一个隐患：
        反查结果与边界层手里那个如果因为任何原因不同，工具就会以**另一个身份**
        执行 —— 而"权限来源必须唯一"是这一整块设计的根。

        所以规则是：**谁发起提问，谁把身份交下来**；协调者不自己猜。
        """
        actor = prepared.get("actor")
        if actor is None:
            raise BusinessError(
                "取不到当前用户身份，无法执行工具调用",
                status_code=500,
                code="AGENT_NO_PRINCIPAL",
            )
        return actor

    async def _finish(
        self,
        prepared: dict[str, Any],
        *,
        answer: str,
        model_ms: int = 0,
        usage: ModelUsage | None = None,
        agent_steps: list[dict[str, Any]] | None = None,
        agent_stop_reason: str | None = None,
        agent_run_id: str | None = None,
        reused_steps: int = 0,
    ) -> dict[str, Any]:
        """回答的**公共后半段**：两层引用校验 → 追加提示 → 落库。

        ============================ 为什么是**两层**校验 ============================
        | 层 | 函数 | 查什么 | 防的错 |
        | --- | --- | --- | --- |
        | ① 完整性 | `check_citation_integrity` | 引用的**编号**真不真 | 编造出处、通篇无引用 |
        | ② 支撑性 | `check_citation_support` | 引用的**内容**撑不撑得住 | "编号真、内容假"（把 10 万说成 50 万） |

        只做 ① 会放行后者 —— 而后者在 RAG 评测里对应 **Faithfulness（忠实度）**：
        "回答里的事实能不能从被引用的上下文里得到支持"。
        两层都做，才有资格谈"引用可信"。

        校验结果**落库**（`chat_messages`）而不是只写日志：
        计划里定的第一版策略是"**不阻断，但把问题记下来**"，
        记在库里才能积累样本、决定将来是重试还是拒答；
        ② 的 `support_rate` 还是**评测指标的数据来源**（见 `support_rate` 的口径说明）。
        """
        sources: list[Source] = prepared["sources"]
        problems = check_citation_integrity(answer, sources)
        support_problems = check_citation_support(answer, sources)
        rate = support_rate(answer, sources)

        # ============================ Agent 型回答不该套"制度问答"的引用规则 ============================
        # `check_citation_integrity` 的第二条是"有实质内容却没有任何引用编号"——
        # 它对**制度问答**是对的（回答必须能指到原文）。
        # 但 Agent 查的是**业务数据**（"你提过哪些申请"），那种回答**本来就没有制度引用**，
        # 给它套这条规则会得到一个假问题，并在回答末尾贴上一句
        # **误导性**的"请以制度原文为准"（实测：用户会以为系统在让他去翻制度）。
        #
        # 只保留**与来源相关**的那两条：引用了不存在的编号、检索到来源却完全不用。
        #
        # ⚠️ **判据必须是"真的调过工具"，不是"走了 Agent 路径"**（实测踩到，本行改过一次）：
        # 一开始写的是 `bool(agent_steps)`，而 `agent_steps` 在**走 Agent 路径时必然非空**
        # （至少有一条 `answer` 步）—— 于是这个条件**恒为真**。
        # 后果：一个"**没调任何工具**、直接答了制度问题"的回答也被当成数据型回答，
        # 跳过了"有断言必须带引用"这条检查。实测表现是模型回一句半截的
        # "系统里没有查到"，却**没被标出"通篇断言却零引用"** —— 而那正是这条检查存在的意义。
        #
        # 所以只认 `tool_call` / `tool_result`：那才代表"这次回答的依据来自工具调用"。
        # 判据抽成了纯函数（`agent_used_tools`），因为它**必须能离线单测** ——
        # 这个恒真条件在集成测试里凑不出复现场景（需要"检索命中 + 模型一个工具都不调"）。
        if agent_used_tools(agent_steps):
            problems = [item for item in problems if "没有任何引用编号" not in item]

        # 提示语只有一个来源（`CITATION_WARNING`）—— 两层问题合并后只追加一次，
        # 避免回答末尾出现两行几乎一样的免责声明（那会让提示贬值成噪音）。
        final = answer + (CITATION_WARNING if (problems or support_problems) else "")
        top_score = max((source.score for source in sources), default=None)

        session = prepared["session"]
        seq = await self._knowledge.next_seq(session.id)

        # ---- 成本核算：用量与单价都齐了才算得出，缺一个就是"未知"（不是 0）----
        # ⚠️ **Agent 也算"调用了模型"**：它走的是 `stream_turn`（不是 `stream`），
        # 而 `decision.use_model` 对它是 False（检索没依据）。
        # 不把 Agent 的事实传进来，一个花了几秒模型时间的 Agent 回答
        # 在库里会记成"未调用模型" —— 审计字段说谎（实测踩到过）。
        #
        # ⚠️ 但判据是"**真的调过工具**"，不是"走了 Agent 路径"（同一类错漏的第三处）：
        # Agent 一个工具都没调时会退回本地兜底那句写死的话术 —— 那时采用的字**不是模型生成的**，
        # 记成"调用了模型"同样是说谎。`agent_used_tools` 是这条判据的唯一定义处。
        used_remote = self._used_remote(
            prepared["decision"], agent_used=agent_used_tools(agent_steps)
        )
        cost_micros = estimate_cost_micros(
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
            price=self._price_for_remote(used_remote),
        )

        message = await self._knowledge.append_message(
            session_id=session.id,
            role="ASSISTANT",
            content=final,
            seq=seq,
            sources=[self._source_payload(source) for source in sources],
            citation_problems=problems or None,
            support_details=_merge_details(
                [self._support_payload(problem) for problem in support_problems],
                agent_steps=agent_steps,
                agent_stop_reason=agent_stop_reason,
            ),
            support_rate=rate,
            top_score=top_score,
            retrieval_ms=prepared.get("retrieval_ms"),
            model_ms=model_ms,
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
            estimated_cost_micros=cost_micros,
            used_model=used_remote,
        )
        await self._uow.commit()
        return {
            "message_id": str(message.id),
            "session_id": str(session.id),
            "answer": final,
            "sources": [self._source_payload(source) for source in sources],
            "citation_problems": problems,
            "support_problems": [self._support_payload(p) for p in support_problems],
            # `None` = 这次回答不适用该指标（没有引用句），与 `0.0`（全不可信）不同 ——
            # 评测汇总时必须分开，否则均值会被"不适用"系统性压低。
            "support_rate": rate,
            # ---- 可观测：延迟与成本（`None` = 未知，不是 0）----
            "retrieval_ms": prepared.get("retrieval_ms"),
            "model_ms": model_ms,
            "prompt_tokens": usage.prompt_tokens if usage else None,
            "completion_tokens": usage.completion_tokens if usage else None,
            "estimated_cost_micros": cost_micros,
            "used_model": used_remote,
            "reason": self._reason(
                prepared["decision"],
                used_remote=used_remote,
                # "走过 Agent 路径"这件事由轨迹本身表达（有轨迹 ⇒ Agent 跑过）——
                # 用它区分"制度问答没依据"与"Agent 答不了又没查数据"，否则前者的话术会指错方向
                agent_ran=bool(agent_steps),
            ),
            "top_score": top_score,
            # ---- Agent 轨迹（只在走 Agent 时有值）----
            # 非流式那条路也要把它们给出来：`stop_reason` 是"这次为什么停了"的
            # 唯一线索（超时？轮次用完？工具一直失败？），
            # 而这三种情况的用户体感都是"没答好"，不给原因就没法归因。
            "agent_steps": agent_steps or None,
            "agent_stop_reason": agent_stop_reason,
            #: 这次运行的 id 与复用步数（M12++）。**必须回给客户端** ——
            #: 没有 `agent_run_id`，"续跑"这件事在接口上就不存在
            #: （客户端不知道该拿什么 id 回来）。
            "agent_run_id": agent_run_id,
            "agent_reused_steps": reused_steps,
        }

    def _price_for_remote(self, used_remote: bool) -> ModelPrice | None:
        """这次调用该用哪条单价。

        ============================ 三种情况，必须分开 ============================
        · **本地兜底**（没调模型）→ 不产生费用。这里返回 `None`，
          但注意 `estimate_cost_micros` 也会因为**没有用量**而返回 `None` ——
          两个 `None` 叠加仍是"未知"，语义上正确（"没调用"本来就没有成本可言）；
        · **远端模型，价格表里有它** → 用它算；
        · **远端模型，价格表里没有** → `None`（未知）。**不拿别的模型的价去套** ——
          那会让成本报表看起来完整，实际上是编的。
        """
        if not used_remote:
            return None
        prices, problems = parse_model_prices(self._price_config)
        if problems:
            # 坏配置**不静默**：一条写错的价格会让该模型全部记成"成本未知"，
            # 而"为什么未知"必须能从日志里看出来。
            logger.warning("模型价格表有 %d 条无法解析：%s", len(problems), problems[:3])
        return prices.get(self._chat.name)

    @staticmethod
    def _support_payload(problem: SupportProblem) -> dict[str, Any]:
        """一条支撑性问题的对外形状（落库与响应共用，**不手拼两份**）。"""
        return {
            "kind": problem.kind,
            "detail": problem.detail,
            "sentence_index": problem.sentence_index,
        }

    async def _record_failure(self, prepared: dict[str, Any], *, reason: str) -> None:
        """模型失败时也把这件事落库（用户看到的是 error 事件）。

        不写这条的话，库里只有"用户问了"没有"回答失败了" ——
        事后统计"多少问题没答出来"会漏掉最需要看的那一类。
        """
        session = prepared["session"]
        seq = await self._knowledge.next_seq(session.id)
        await self._knowledge.append_message(
            session_id=session.id,
            role="ASSISTANT",
            content=f"（回答失败：{reason}）",
            seq=seq,
            sources=[self._source_payload(source) for source in prepared["sources"]],
            citation_problems=[reason],
            used_model=self._used_remote(prepared["decision"]),
        )
        await self._uow.commit()


def build_knowledge_coordinator(
    session: Any,
    *,
    object_store: ObjectStorePort,
    chat_model: ChatModelPort,
    embedder: EmbeddingPort,
    top_k: int,
    min_score: float,
    price_config: str = "",
    purchase: PurchaseCoordinator | None = None,
    approval: ApprovalCoordinator | None = None,
) -> KnowledgeCoordinator:
    """按一次请求的会话装配知识库用例的依赖。

    与其它 `build_*_coordinator` 同一个理由：装配只写一份，边界层与测试共用。

    `chat_model` 与 `embedder` 都是**参数**而不是在这里 new 出来的：
    测试要注入"故意引用不存在编号的假模型"与"故意给错维度的假 embedder"
    来验证两条校验，而把选实现的逻辑写死在装配里，那两条验证就只能靠真模型碰运气。
    选哪一个实现由**边界层**决定（见 `boundaries/http/knowledge.py`）。
    """
    return KnowledgeCoordinator(
        knowledge=KnowledgeRepository(session),
        object_store=object_store,
        chat_model=chat_model,
        embedder=embedder,
        uow=session,
        top_k=top_k,
        min_score=min_score,
        price_config=price_config,
        purchase=purchase,
        approval=approval,
        # 请求会话一路传下去：Agent 运行记录要用它（见 `_agent_run` 的说明）
        session=session,
    )


__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "MAX_DOC_CHARS",
    "KnowledgeCoordinator",
    "VECTOR_DIM",
    "build_knowledge_coordinator",
]
