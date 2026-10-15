"""应用配置。

纪律：**配置项保持个位数**。原项目有 60 多个环境变量，那是长期演进的结果，
不要一开始就抄。每加一个配置项之前，先问自己"能不能不给它开关"。

注意这里**故意没有 cors_origins**：前端走 Vite 代理（同源），用不上 CORS。
等 M12 打包成 APK、前后端真正分居两个源时再加回来。

基础设施（PostgreSQL 容器）在 D:\\develop\\procurement\\docker-compose.yml，
与本项目代码解耦：容器是容器，代码是代码。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # PostgreSQL（容器见 D:\develop\procurement\docker-compose.yml）
    database_url: str = "postgresql+asyncpg://app:devpassword@localhost:5432/procurement"

    # M3 起用。本地开发默认值不要用于任何真实环境；
    # 长度不短于 32 字节（HS256 的密钥短了 PyJWT 会警告）。
    jwt_secret: str = "dev-only-change-me-before-any-real-use"

    # M9 起**真的有人读它**：附件落盘的根目录，使用者是 `infrastructure/object_store.py`。
    # 之所以不给它更多开关（大小上限、允许的类型）：那些是**规则**不是**环境差异**，
    # 写在 `domain/attachments.py` 里才能被测试钉住，放进环境变量就没人测了。
    upload_dir: str = "uploads"

    # M10 起用：**比价线**（分）。金额 >= 这条线才要求至少 3 家有效报价
    # （家数常量在 `domain/sourcing_policy.MIN_QUOTATIONS`），低于它一家也能定标。
    #
    # 值取 1_000_000 分 = **1 万元**：这是文档里唯一出现过的数
    # （`docs/企业采购审批系统-领域设计.md` 5.3「金额超过阈值（比如 10000 元）」），
    # 照抄它就不用改文档。真实制度里这个数由采购金额分布与议价收益决定，可按需调整。
    #
    # 为什么是**全局一条线**而不是按品类配：见 PROGRESS.md「M10 定稿的两处口径」——
    # 一句话是"不为将来预留走不到的分支"，真要分档时第一步是给 categories 加一列。
    sourcing_threshold_fen: int = 1_000_000

    # M11 起用：**知识库与问答**。四项配置，各自守一件事（见下面的注释）。
    #
    # 知识库.md / 计划里给的检索阈值。为什么会"低于阈值就不调用模型"而不是
    # "有结果就调用"：检索**永远会返回 top-k**（哪怕全是无关内容），
    # 只判"结果为空"的话，问一个制度里没有的问题会把最不相关的片段递上去，
    # 模型于是拿着不相干的材料硬答 —— 那正是"一本正经地编造"最常见的触发路径。
    knowledge_top_k: int = 5
    #
    # ⚠️ **这个阈值取决于 embedding 模型，换模型必须重新标定** ——
    # 它是"要不要回答"的**唯一判据**，调错的后果是"要么全部拒答、要么全部硬答"。
    # 两者都是实测踩到过的，不是假想：
    #
    #     哈希 @ 0.15：口语化问题的 top_score 只有 0.025 → **3 条能答的也被拒**；
    #     真模型 @ 0.15：语义相近的无关片段也拿到 0.4+ → **5 条无答案问题全被硬答**。
    #
    # 0.50 是按**实测分数分布**标定的（27 条 Golden QA，`quentinz/bge-small-zh-v1.5`）
    # —— 注意标定值是 0.5228–0.8405（能答）/ 0.3562–0.6533（无答案），
    # 而 0.50 实测评出"误拒 0 条、拦住 3/5"，0.55 反而**误拒 3 条**（实测对比过）。
    # 说明分数在不同运行间有零点几的抖动，所以阈值要取在**离两侧都有余量**的位置，
    # 而不是贴着实测最低分（0.5228）去卡。
    #
    # 两类**仍有重叠**（U-05"一亿元要不要董事会审批"是 0.6533，比能答的最低分还高），
    # 所以不存在"0 误拒 + 5/5 拦住"的阈值。剩下那两条是**阈值解决不了**的语义问题 ——
    # 制度里确实提了采购金额与审批，只是没有"一亿元"那一档，
    # 要靠更强的模型或"回答前先反问"来兜（见 docs/AI-评测baseline.md）。
    #
    # 换 embedding 后请用 `scripts\knowledge_eval.py --min-score X` 重新扫一遍。
    knowledge_min_score: float = 0.50
    #
    # ============================ "依据够硬吗"的第二道线（M12++）============================
    # **为什么需要第二个阈值**：实测（2026-10-15，10 份种子制度入库后）
    # 业务数据问题与制度问题的 top_score **是重叠的**：
    #
    #     业务数据问句："我提过哪些采购申请？"      0.624  ← 已越过 0.50
    #                  "我有哪些待办？"              0.405
    #                  "查一下我的预算还有多少"       0.450
    #     制度问句：    "廉洁从业禁止哪些行为？"      0.584
    #                  "采购金额超过十万元…"          0.597
    #                  "合同用印需要经过谁审查？"      0.736
    #                  "紧急采购事后说明…"            0.753
    #
    # 0.624（业务）比 0.584（制度）还高 —— **任何单点阈值都分不开这两类**。
    # 所以不看"分不分得开"，改看"**答错的代价**"：
    #
    #     用制度片段回答业务数据问题 → 答非所问，且**看起来像回答**（最糟）
    #     用 Agent 处理制度问题       → 它没有查制度的工具 → **诚实地拒答**
    #
    # 后者是安全的那一侧（这个项目的铁律："没有来源就不回答"）。
    # 所以判据改成**三档**：
    #
    #     score ≥ 强阈值        → 制度问答（依据够硬，直接答）
    #     min ≤ score < 强阈值  → **先让 Agent 试**（它可能查得到真实数据）；
    #                             它一个工具都没调 → 落回制度问答原本那条路
    #     score < min           → 没依据，Agent 或标准拒答（原有行为，不变）
    #
    # ⚠️ **代价要说清**：顶分在 [0.50, 0.70) 的**真制度问题**（例如 0.597、0.584）
    # 会先浪费一次 Agent 轮次才落到制度问答 —— 多花一点时间，答案不变。
    # 反过来若把强阈值调到 0.60 以下，"我提过哪些采购申请"（0.624）就会重新走错分支。
    #
    # 取 0.70 的依据：10 份种子制度里**顶分高于它的**问句（0.703~0.771）
    # 都是"该用制度答"的（见 docs/AI-评测baseline.md 的实测表）。
    knowledge_strong_score: float = 0.70
    #
    # 远端模型（**可选**）。三个都为空时用本地兜底（抽取式：把最相关的
    # 制度原文摘出来并标引用），整条链路照样能跑 —— 这是刻意的：
    # 没密钥时助手不该整体不可用，只是答得不如模型好。
    # 按"OpenAI 兼容"协议调用，所以 base_url 可以指向 DashScope 兼容模式、
    # 本地 vLLM/Ollama 等任意同类服务。
    knowledge_llm_base_url: str | None = None
    knowledge_llm_api_key: str | None = None
    knowledge_llm_model: str | None = None
    #
    # 模型单价（**元 / 百万 token**），用于成本核算。格式：
    #     `模型名=输入价,输出价`，多个模型用分号隔开
    # 例如：
    #     KNOWLEDGE_LLM_PRICES=qwen2.5:7b=0,0;gpt-4o-mini=0.15,0.6
    #
    # **为什么本地模型也要显式配一条 `0,0`**：`0` 与"没配"在成本核算里是两回事 ——
    # 前者是"确实不花钱"，后者是"不知道花多少"。留空会让本地调用被记成"用量未知"，
    # 于是"这个月花了多少钱"永远算不清（见 `domain/cost.py` 的口径说明）。
    #
    # 不配任何价格时所有调用都记 `NULL`（未知）—— **不会伪造一个估算值**。
    knowledge_llm_prices: str = ""

    #
    # ============================ embedding（算向量）============================
    # 取值：
    #     `local`   → 项目自带的哈希实现（离线、无需模型，但**没有语义能力**）
    #     `ollama`  → 通过 Ollama 调真 embedding 模型
    #
    # **为什么必须显式选，而不是"模型不可用就退回哈希"**：
    # 哈希与真模型的向量**不可比**，混在同一列里会让检索静默变差；
    # 而且检索是按 `knowledge_docs.embedding_model` 过滤的 ——
    # 退回哈希之后，真模型建的文档会**一条都检索不到**。
    # 那不是"差一点"，是"全没了"。所以模型坏了就报错，不偷偷换。
    #
    # 默认 `ollama`：实测（27 条 Golden QA）真 embedding 明显更好 ——
    #     MRR 0.879 → **1.000**、NDCG 0.910 → **1.000**、
    #     能答却拒答 3 条 → **0 条**、实际回答成功率 0.864 → **1.000**
    # 哈希仍是受支持的配置（没有模型的机器上跑链路与 CI 用它）。
    knowledge_embedding_provider: str = "ollama"
    #
    # embedding 服务的地址。**刻意与 `knowledge_llm_base_url` 分开**：
    # 它们常常是同一个服务（本机 Ollama 同时提供对话与 embedding），
    # 但**不是必须**——对话可以用云端 API、embedding 用本地小模型（省钱且不出网），
    # 反过来也成立。合成一个配置的话，"只想换 embedding"就必须把对话模型
    # 也指到同一个地址。
    #
    # 默认 `127.0.0.1:11434`（Ollama 的惯例端口），**不带 `/v1`** ——
    # embedding 走的是 Ollama 原生的 `/api/embed`，不是 OpenAI 兼容路径。
    # 实测踩到：默认 embedding 是 ollama 而这里没有默认值，会让
    # **默认配置直接 500**（"选了 ollama 但没配地址"）——
    # 一个默认配置不可用的项目，等于每个新接手的人都要先猜一遍配置。
    knowledge_embedding_base_url: str = "http://127.0.0.1:11434"
    #
    # 用哪个 embedding 模型（`provider=ollama` 时生效）。
    # ⚠️ **换它会换掉整个向量空间**：旧文档的向量不再被检索到（按名字过滤），
    # 需要重新上传/重灌。可用值与各自维度见 `domain/embedding.py` 的 `MODEL_DIMENSIONS`。
    #
    # 默认选 `quentinz/bge-small-zh-v1.5`（512 维、48 MB）而不是 bge-m3：
    # bge-m3 的模型层 1.1 GB，本机到 registry.ollama.ai 实测只有 50–210 KB/s，
    # 拉取要数小时、且中途失败会留下**永远无法完成**的 partial 文件（实测踩到，
    # 白占 1.1 GB）。要更好效果就 `ollama pull bge-m3` 再改这里 ——
    # 注意 bge-m3 是 **1024 维**，列维度要一起迁移。
    knowledge_embedding_model: str = "quentinz/bge-small-zh-v1.5"

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
