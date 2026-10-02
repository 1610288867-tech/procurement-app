"""ORM 映射包。

一个聚合一个文件 —— 不要学参考项目把 22 个聚合塞进一个 3,589 行的文件。

    base.py       DeclarativeBase（只有 Base）
    catalog.py    SupplierRecord（M1）

新模型必须让 `migrations/env.py` 能 import 到，否则 Alembic autogenerate
看不到它，会生成一个空迁移。
"""
