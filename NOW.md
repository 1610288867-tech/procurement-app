# NOW · 当前目标、进展、阻塞、下一步

> **用途**：接续任务时**先读这一份**（它很短）。
> 历史过程不写在这里 —— 阶段验收去 [PROGRESS.md](PROGRESS.md)，决策去 [DECISIONS.md](DECISIONS.md)。
>
> 最后更新：2026-10-14

## 一、当前目标

**主干已完成（M0–M11 + M11+ + M12+），文档规范化也已完成。**
**下一步是"补功能缺口"**（见第四节），或按你的安排转向别的目标。

- 已完成：M0–M11 十一个主干阶段 + 两个增强段（AI 模块增强、Function Calling 与最小 ReAct）
- 已完成：**项目文档规范化**（按职责拆成 10 份根文档 + `docs/` 六个主题子目录，见第六节）
- 明确不做：**M12 桌面壳与移动端**（2026-10-06 决定；它是打包工程，与领域建模不重叠，前置已就位、随时可回头做）

## 二、进展（实测，截至 2026-10-14）

| 维度 | 数值 | 证据 |
| --- | --- | --- |
| 后端源码 | **88** 个 `.py` | `Get-ChildItem backend\app -Recurse -Filter *.py` |
| 数据表 | **25** 张（ORM 模型） | `records/*.py` 里的 `__tablename__` |
| 迁移 | **19** 个，head = `0e9f1c4a77b2` | `alembic heads` |
| HTTP 接口 | **59** 条路径 / **70** 个操作 | `app.openapi()["paths"]` |
| 前端源码 | **38** 个 `.ts/.tsx` | `Get-ChildItem frontend\src -Recurse` |
| 测试 | `pytest -m ""` **680 passed**（unit 428 / integration 252） | 实跑 |
| 一条命令回归 | `.\scripts\回归验收.ps1` **14 步，退出码 0** | 实跑 |

**回归 14 步各自的数字**（最近一次全绿）：

```
三条分层自查        命中 0 处（自证探针通过）
pytest -m ""        680 passed
ruff + mypy         0 问题 / 88 files
前端 tsc            0 错误
前端 vitest         28 passed
前端 build          built
确保前后端在跑       （不在就起，最多等 90 秒）
m6_smoke.py         19/19
regression_probes   20/20
浏览器自检.mjs      17/17
m11-知识库实测.mjs   6/6
窄屏自检.mjs        20/20（375×812）
知识库评测门禁      通过（27 条 Golden QA，真 embedding）
testdata_cleanup    ✅ 干净
```

**单独跑（唯一不在入口里的）**：`node scripts\m6-页面实测.mjs` —— **51/51**。
它需要外部已开好的 Edge（9222 端口），与"自起自收"的生命周期不同。

## 三、阻塞

**当前没有阻塞项。**

两条需要注意的环境前提（不算阻塞，但会让相关步骤红）：

| 前提 | 影响 | 怎么处理 |
| --- | --- | --- |
| 本机 Ollama 没起 | 评测门禁**明确报错**（不会静默退回哈希） | `.\scripts\ollama.ps1 -Serve` |
| **`backend/.env` 是本机文件、不进仓库**（`.gitignore` 挡着） | 新克隆或换机器时它**不存在** → 对话模型没配 → Agent 路径关闭，问业务数据问题只回"制度中没有找到相关规定"（**不报配置错误**，看起来像功能没做） | 按 [RUNBOOK.md](RUNBOOK.md) 第 1.5 节建 `backend/.env`（三项 `KNOWLEDGE_LLM_*` 齐了才开 Agent）；模板见仓库根 [.env.example](.env.example)，它 **14 个键与 `config.py` 逐项对齐** |
| 数据库容器没起 | `pytest -m integration` 与冒烟全红（`ConnectionRefusedError`） | `.\scripts\dev-up.ps1` |

> ⚠️ **`.env` 一配上对话模型，Agent 路径就开了 —— 有几条验收的前提会跟着变**
> （原来写着"本机没配 LLM 密钥 → 走本地兜底"）。已处理的见 [RISKS.md](RISKS.md) 第五节；
> **新写测试时注意**：不要断言"本机配置"，要**读后端真实状态**再决定断言什么。

## 四、下一步（按优先级）

| # | 事项 | 性质 | 说明 |
| --- | --- | --- | --- |
| 1 | **前端接"确认创建"按钮** | 功能缺口 | 后端 `POST /api/v1/chat/confirm-draft` 已可用并有集成测试；前端还没有入口，于是 Agent 会说"请去确认"但用户找不到地方 |
| 2 | **换更稳的对话模型** | 可选 | 3B 在"该不该调工具"上会摇摆。⚠️ **`ollama pull qwen2.5:7b-instruct` 已试 4 次都失败、4.4 GB 的 blob 被清掉了**；要走 **GGUF + Modelfile 导入**这条路（见 [RISKS.md](RISKS.md) 第五节） |
| 3 | **重启 8000 上的后端** | 环境动作 | `.env` 已验证生效，但运行中的实例不会重读它 —— Agent 要重启后才在前端可用 |
| 4 | 重排 / 混合检索 | 可选增强 | `Precision@5 = 0.500` 的靶子已立、对比基准就位 |
| 5 | 更早文件的链接巡检 | 文档卫生 | 本次规范化移动了一批文档，见下面第六节 |

**不建议现在做**（已判定不值得）：M12 桌面壳与移动端、并行工具调用、长期记忆、MCP。

## 五、证据入口（复核用）

```powershell
cd D:\procurement-app
.\scripts\回归验收.ps1                              # 14 步，退出码 0 = 全过
cd backend
.\.venv\Scripts\python.exe scripts\knowledge_eval.py --gate    # 只看 AI 质量门禁
.\.venv\Scripts\python.exe scripts\regression_probes.py        # 只看静默失败探针（20 项）
```

详细过程与逐条验收证据在 [PROGRESS.md](PROGRESS.md)；"学到了什么、为什么这么选"在
[Development progress.md](Development progress.md) 与 [DECISIONS.md](DECISIONS.md)。

## 六、本次规范化的进行状态

| 动作 | 状态 |
| --- | --- |
| 建 6 个新文档骨架（本文件、`PROJECT_INDEX`、`MAP`、`RUNBOOK`、`DECISIONS`、`RISKS`）+ `history/` | ✅ 完成 |
| `AGENTS.md` 补"文档结构与读写规则"、移走环境知识（341 → 416 行） | ✅ 完成 |
| `README.md` 瘦身（667 → 222 行；业务规则/权限矩阵/API 移入 `docs/`） | ✅ 完成 |
| 移动求职与 AI 文档到 `docs/{interview,ai,reference}/` | ✅ 完成 |
| `resume/` 清理 24 个渲染检查残留目录（**9.7 MB → 0.5 MB**） | ✅ 完成 |
| 精简两份大文档（`PROGRESS` **1415 → 228**、`Development progress` **1598 → 318**） | ✅ 完成（原文已归档到 `docs/history/`） |
| 新增 `scripts\检查文档链接.mjs`（相对链接巡检；`history/` 默认跳过） | ✅ 完成 |

**完成后的数字**：根目录 10 个规范文档 + `docs/` 六个主题子目录；
Markdown 共 30 个（含归档）；**146 个内部链接 0 断链**；回归 14 步退出码 0、`pytest` **680 passed**。

## 七、维护规则

- **状态实质变化时更新**（不是每次提交都改）——它是"当前快照"，不是日志
- 旧的快照**直接改写**，不追加；有价值的过程挪进 `docs/history/`
- 数字必须**实测**（跑一次或读一次），不写"应该差不多"
- **换窗口 / 换会话 / 收工前，把状态更新到这里**（目标 / 进展 / 阻塞 / 下一步各一行）——
  新会话读不到这次对话，只读得到 `NOW.md`。读的顺序见
  [AGENTS.md](AGENTS.md) 第 3.5 节
