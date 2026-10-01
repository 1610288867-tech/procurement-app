"""application —— 用例编排层（协调者）。

这一层负责**决定事务边界**并编排多个仓库/端口，但自己不做业务判断、
也不碰 HTTP。

============================ 职责边界 ============================
允许：
    - 调用 domain/ 的纯函数做业务判断
    - 调用 ports.py 定义的端口（仓库、对象存储、LLM）
    - 决定"什么时候 commit"（事务边界在用例层，不在仓库层）

不允许：
    - 直接 import sqlalchemy 写 SQL（那是 infrastructure 的事）
    - 出现 Request / Response / HTTPException（那是 boundaries 的事）
    - 把事务边界交给仓库层（仓库只 flush()）

============================ 目录约定 ============================
    ports.py        端口抽象（仓库、对象存储、LLM 的接口）
    dto.py          用例的输入输出对象
    idempotency.py  幂等协调者（M-09）：IN_PROGRESS 占位 + payload_hash
    coordinators/   一个用例域一个文件
        auth.py  catalog.py  purchase.py  approval.py
        sourcing.py  receiving.py  settlement.py  admin.py  chat.py

M0 阶段本目录为空，从 M2 起开始填充。
"""
