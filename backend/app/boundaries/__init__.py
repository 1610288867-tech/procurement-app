"""boundaries —— 边界层（HTTP 接口）。

这一层只做四件事：解析请求、校验输入、做权限/幂等依赖、把结果转成响应。
**不写业务规则**——那在 domain/；**不编排事务**——那在 application/。

============================ 目录约定 ============================
    deps.py           当前用户、权限校验、幂等键依赖
    http/             一个分组一个路由文件
        auth.py  catalog.py  requests.py  approvals.py
        sourcing.py  receiving.py  settlement.py  admin.py  chat.py

============================ 两条纪律 ============================
1. **权限校验必须在这里独立做**，不能只靠前端隐藏菜单——
   用 Postman 直接打接口就绕过了。
2. **越权语义**：可见范围内没权限 → 403；不在可见范围 → 404。

M0 阶段本目录为空，从 M1 起开始填充（M1 先做 suppliers 的 GET/POST）。
"""
