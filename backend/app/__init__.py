"""企业采购审批系统 —— 后端应用包。

这个文件的作用不只是标记包：**没有它，mypy 会把 `app/config.py` 同时识别成
`config` 和 `app.config` 两个模块**，直接报
"Source file found twice under different module names" 并停止检查。

分层见各子包：
    domain/          纯 Python 业务逻辑（禁止 import 任何框架）
    application/     用例编排（事务边界在这一层）
    boundaries/      HTTP 边界（只解析、校验、鉴权）
    infrastructure/  适配器与组合根（一个聚合一个文件）
"""

__version__ = "0.1.0"
