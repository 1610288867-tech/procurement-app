"""SQLAlchemy 声明式基类。

只放 Base。所有模型分文件放在本包内，由 `migrations/env.py` 统一导入。
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
