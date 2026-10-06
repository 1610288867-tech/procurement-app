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
    # ⚠️ **这个阈值取决于 embedding 模型**：本地 `local-hash-256` 靠字面重合，
    # 同一主题的相似度普遍在 0.15–0.6；真模型通常 0.5 以上。
    # 换模型时必须一起调 —— 调错的后果是"要么全部拒答，要么全部硬答"。
    knowledge_min_score: float = 0.15
    #
    # 远端模型（**可选**）。三个都为空时用本地兜底（抽取式：把最相关的
    # 制度原文摘出来并标引用），整条链路照样能跑 —— 这是刻意的：
    # 没密钥时助手不该整体不可用，只是答得不如模型好。
    # 按"OpenAI 兼容"协议调用，所以 base_url 可以指向 DashScope 兼容模式、
    # 本地 vLLM/Ollama 等任意同类服务。
    knowledge_llm_base_url: str | None = None
    knowledge_llm_api_key: str | None = None
    knowledge_llm_model: str | None = None

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
