"""用例协调者。一个用例域一个文件。

============================ 这一层最重要的职责 ============================
**决定事务边界。**

仓库只 flush()、路由只解析请求，只有这里知道"一次用例到哪算结束"，
于是多个仓库操作才能处在同一个事务里。

============================ 允许 / 不允许 ============================
允许：调用 domain/ 的纯函数做业务判断；调用 ports.py 里的端口；
      决定什么时候 commit。
不允许：import sqlalchemy 写 SQL（那是 infrastructure 的事）；
        出现 Request / Response / HTTPException（那是 boundaries 的事）。

目录约定：
    catalog.py   采购目录（M1）
    （M2 起增加 purchase.py / approval.py / sourcing.py / settlement.py …）
"""
