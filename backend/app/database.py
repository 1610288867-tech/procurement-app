"""异步数据库引擎与会话。

两条纪律：

1. **仓库层只 flush()，不 commit()** —— 事务边界在用例层（`application/coordinators/`）。
   这样多个仓库操作才能处于同一个事务里。
2. `expire_on_commit=False` —— 否则 commit 之后再访问对象属性会触发额外查询，
   异步下会直接报错（这就是"接口返回 200 但报 500"的典型原因）。
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(settings.database_url, pool_pre_ping=True)

SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：一个请求一个会话，请求结束自动关闭。"""
    async with SessionFactory() as session:
        yield session
