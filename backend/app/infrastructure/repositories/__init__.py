"""数据访问层。

三条纪律：

1. **只 flush()，不 commit()** —— 事务边界在 `application/coordinators/`。
   这样一次用例里的多个仓库操作才能处于同一个事务。
   自查（应无输出）：
       Select-String -Path backend\\app\\infrastructure\\repositories\\*.py -Pattern "\\.commit\\(\\)"
2. **一个聚合一个文件**。
3. 只做数据进出，**不在这里做协议转换**（那是 boundaries/ 的事）。
"""
