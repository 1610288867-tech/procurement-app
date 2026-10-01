"""应用配置。

纪律：**配置项保持个位数**。原项目有 60 多个环境变量，那是长期演进的结果，
不要一开始就抄。每加一个配置项之前，先问自己"能不能不给它开关"。

注意这里**故意没有 cors_origins**：前端走 Vite 代理（同源），用不上 CORS。
等 M10 打包成 APK、前后端真正分居两个源时再加回来。

基础设施（PostgreSQL 容器）在 D:\\develop\\procurement\\docker-compose.yml，
与本项目代码解耦：容器是容器，代码是代码。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # PostgreSQL（容器见 D:\develop\procurement\docker-compose.yml）
    database_url: str = "postgresql+asyncpg://app:devpassword@localhost:5432/procurement"

    # M3 起用；本地开发默认值不要用于任何真实环境
    jwt_secret: str = "dev-only-change-me"

    # M7 起用：附件落盘目录
    upload_dir: str = "uploads"

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
