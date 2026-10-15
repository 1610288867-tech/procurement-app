"""Alembic 运行环境。

相对模板改了三处：

1. 导入 Base 与**所有**模型 —— 模型没被 import 时 autogenerate 看不到它，
   会生成一个空迁移（最坑的失败模式：看起来成功，其实什么都没建）。
2. 数据库地址从 app.config 读，不在 alembic.ini 里写死连接串。
3. 用 async 引擎跑迁移（应用用 asyncpg，同步引擎连不上）。
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings
from app.infrastructure.records import agent_run as _agent_run  # noqa: F401  autogenerate
from app.infrastructure.records import approval as _approval  # noqa: F401  autogenerate
from app.infrastructure.records import attachment as _attachment  # noqa: F401  autogenerate
from app.infrastructure.records import auth as _auth  # noqa: F401  autogenerate
from app.infrastructure.records import catalog  # noqa: F401  autogenerate
from app.infrastructure.records import fulfillment as _fulfillment  # noqa: F401  autogenerate
from app.infrastructure.records import invoice as _invoice  # noqa: F401  autogenerate
from app.infrastructure.records import knowledge as _knowledge  # noqa: F401  autogenerate
from app.infrastructure.records import payment as _payment  # noqa: F401  autogenerate
from app.infrastructure.records import purchase as _purchase  # noqa: F401  autogenerate
from app.infrastructure.records import sourcing as _sourcing  # noqa: F401  autogenerate
from app.infrastructure.records.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 连接串只有一个事实来源：app/config.py（可被 .env 覆盖）
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
