"""HTTP 路由包。一个分组一个文件。

这一层只做四件事：解析请求、校验输入、做权限/幂等依赖、把结果转成响应。
**不写业务规则**（那在 domain/），**不编排事务**（那在 application/）。

目录约定：
    catalog.py   供应商（M1）
    （M2 起增加 auth.py / requests.py / approvals.py / sourcing.py …）
"""
