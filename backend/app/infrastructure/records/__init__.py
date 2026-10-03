"""ORM 映射包。

一个聚合一个文件 —— 不要学参考项目把 22 个聚合塞进一个 3,589 行的文件。

    base.py       DeclarativeBase（只有 Base）
    catalog.py    部门 / 品类 / 供应商 / 预算（M1–M2）
    auth.py       账号与角色（M3）
    purchase.py   采购申请 / 明细 / 事件流 / 幂等记录（M5）
    approval.py   审批流 / 审批任务（M6）

新模型必须让 `migrations/env.py` 能 import 到，否则 Alembic autogenerate
看不到它，会生成一个空迁移。
"""
