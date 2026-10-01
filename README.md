# 企业采购审批系统

个人学习项目。**Windows 浏览器优先，不上线。**

> 定位：从零重写一套采购审批系统，用来练"动态多级审批 + 预算并发预留 + 三单匹配"。
> 参考项目（只当参考书，不改动它）：`D:\own-project\-AI--main`
> 规划文档：`D:\own-project\企业采购审批系统-实施计划.md`、`企业采购审批系统-领域设计.md`

## 目录结构

```
procurement-app/                ← 本项目（代码）
├── backend/
│   ├── app/
│   │   ├── main.py             应用入口 + 三个健康检查 + request_id 中间件
│   │   ├── config.py           配置（只有 4 项，刻意的）
│   │   ├── database.py         异步引擎与会话
│   │   ├── errors.py           统一错误类型与响应体
│   │   ├── domain/             纯 Python 业务逻辑 —— 禁止 import 任何框架
│   │   ├── application/        用例编排 —— 事务边界在这一层
│   │   ├── boundaries/         HTTP 边界 —— 只解析/校验/鉴权
│   │   └── infrastructure/     适配器与组合根 —— 一个聚合一个文件
│   ├── migrations/             Alembic（M1 初始化）
│   ├── tests/                  pytest（unit 默认档 / integration）
│   ├── pyproject.toml          工具配置（pytest 分档、ruff、mypy）
│   └── requirements.txt        pip freeze 的结果，唯一事实来源
├── frontend/                   React + Vite + TS（M1）
├── docs/                       M0 设计产物
│   ├── ER-图.md                15 张表 + 三条关系约束
│   ├── 业务规则.md             8 条核心规则
│   └── 金额-审批链对照表.md     ★ 需求文档 = 测试用例 = 验收标准
├── docker-compose.yml          薄封装，指向 ../procurement
├── PROGRESS.md                 阶段验收清单（做完一项勾一项）
├── .env.example
└── .gitignore
```

**注意代码与容器是分开的**：

| | 位置 | 说明 |
| --- | --- | --- |
| 本项目代码 | `D:\own-project\procurement-app` | 这个仓库 |
| 基础设施 | `D:\develop\procurement` | 只有 PostgreSQL 容器与数据 |

这样职责清楚：容器是容器，代码是代码。数据库坏了不影响代码，代码重写不用动容器。

## 环境要求

| 项 | 版本 | 本机现状 |
| --- | --- | --- |
| Docker Desktop | 29.8.1 + Compose v5.5.1 | ✅ 已装（用户级），**引擎需手动启动** |
| PostgreSQL | 16.15 + pgvector 0.8.6 | ✅ 容器已跑（`pgvector/pgvector:pg16`） |
| Python | 3.12（**不是 3.13**） | ✅ 3.12.2 |
| Node.js | ≥ 20 | ✅ v24.18.0 |
| Git | 任意 | ✅ |

**两个必须知道的坑**：

1. **Docker Hub 在本机不可达**（`registry-1.docker.io` 的 A/AAAA 都连不上）。
   已配镜像加速（`~/.docker/daemon.json` 的 `registry-mirrors`）。
   如果不生效，拉镜像会直接超时。
2. **pip 直连 pypi.org 会挂起**。必须用镜像源：
   ```powershell
   python -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
   ```

## 起库

```powershell
# Docker Desktop 必须先启动（设置里 AutoStart 关着，刻意不开机自启）
cd D:\develop\procurement
docker compose up -d
docker compose ps          # 等 STATUS 出现 (healthy)
```

也可以在本项目目录里用薄封装：

```powershell
cd D:\own-project\procurement-app
docker compose up -d
```

## 起后端

```powershell
cd D:\own-project\procurement-app\backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

uvicorn app.main:app --reload --port 8000
```

三个地址：

| 地址 | 用途 |
| --- | --- |
| http://127.0.0.1:8000/docs | **Swagger 交互文档**，写前端之前先在这里验证接口 |
| http://127.0.0.1:8000/health/live | 存活检查（不碰数据库） |
| http://127.0.0.1:8000/health/ready | 就绪检查（数据库连不上返回 503） |

收工：

```powershell
cd D:\develop\procurement
docker compose stop        # 停止但保留数据
```

**绝对不要用 `docker compose down -v`** —— 那会连数据卷一起删掉。
（本项目用 bind mount，`down -v` 其实删不掉 `data\postgres`，但别养成这个习惯。）

## 测试与自查

```powershell
cd backend
.\.venv\Scripts\Activate.ps1

pytest -m unit          # 纯函数，毫秒级（默认档，pytest 不带参数就是它）
pytest -m integration   # 需要 PostgreSQL
pytest -m ""            # 全量
```

三条自查命令（提交前跑一遍，应全部无输出）：

```powershell
Select-String -Path backend\app\domain\*.py -Pattern "import (fastapi|sqlalchemy|redis|httpx|minio)"
Select-String -Path backend\app\**\*.py -Pattern "Float|: float"
Select-String -Path backend\app\infrastructure\repositories\*.py -Pattern "\.commit\(\)"
```

## 八条业务规则（M0 定稿）

1. 预算提交时预留，驳回/撤回时释放，付款时按**实付**转已花并**释放差额**
2. 按金额分级审批，规则存在品类上，**改规则不改代码**
3. 金额超比价线需至少 **3 家有效报价**才能定标
4. 只能用合格且未禁用的供应商（资质过期自动不可选）
5. 同一人同品类短期拆单要有警示（第一版只 warning）
6. 三单匹配：超收/金额超容差/供应商不一致 → 拒绝付款并列出问题
7. **收货完成后才能付款**（状态机强制）
8. 金额一律**整数「分」**，禁止 float

详见 `docs/业务规则.md`；审批层级见 `docs/金额-审批链对照表.md`。
