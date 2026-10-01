"""infrastructure —— 外部适配器与组合根。

============================ 目录约定 ============================
    runtime.py       组合根：**唯一的注入点**，把端口接到具体实现
    security.py      JWT + Argon2（M3）
    object_store.py  本地目录对象存储（M7）
    login_throttle.py 登录限流（M3，进程内内存字典，不引 Redis）
    llm.py           httpx 直连 LLM（M9）
    records/         ORM 映射，**一个聚合一个文件**
    repositories/    数据访问，**一个聚合一个文件**

============================ 三条纪律 ============================
1. **仓库层只 flush()，不 commit()**。事务边界在 application/coordinators/。
   自查（应无输出）：
       Select-String -Path backend\\app\\infrastructure\\repositories\\*.py -Pattern "\\.commit\\(\\)"
2. **ORM 映射类一律叫 `*Record`**，与 domain/entities.py 里的业务实体严格分开。
   数据库对象和业务对象混用，早晚会出事。
3. **records/ 和 repositories/ 都是目录，不是一个文件**。
   原项目把 22 个聚合塞进一个 3,589 行的 repositories.py —— 这就是要避免的。

M0 阶段本目录为空，从 M1 起开始填充（M1 先做 SupplierRecord + CatalogRepository）。
"""
