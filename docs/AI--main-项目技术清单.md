# 智慧政务“一网通办”AI 助手（`-AI--main`）项目技术清单

> 说明：项目目录实际名称为 `D:\own-project\-AI--main`（开头带一个连字符），下称 `AI--main`。
> 本文以仓库当前代码为准，逐模块列出用到的技术、版本与作用，并给出整体定位。
> 清单生成时间：2026-10-01。

---

## 一、整体这个项目是做什么的

这是一套**可本地/云端跑通的全栈业务演示系统**，主题是“智慧政务一网通办 AI 助手”。

一句话概括：群众（个人/企业）、工作人员、管理员三类角色，在一个 Android 本地资产门户里完成“找事项 → 资格预检 → 填表上传材料 → 提交 → 工作人员审核办结 → 预约/核验/缴费/邮寄 → AI 咨询与转人工”，后端用 `FastAPI 模块化单体` 承载业务，`PostgreSQL` 是权威数据源，`MCP + Mock 政务 API` 提供只读政务目录，`Redis/Milvus/MinIO` 提供缓存、向量检索和文件存储，`DeepSeek` 只做解释与建议。

### 1.1 它解决的核心问题

| 目标 | 实现方式 |
| --- | --- |
| 让群众“问得清、找得到、办得了” | 公开事项目录 + 受限规则资格预检 + JSON Schema 动态表单 + 材料清单分类 |
| 让 AI 只做“解释和建议”，不越权 | LLM 输出不能直接改办件/审批/缴费/账号/事项状态，所有状态变更必须走显式业务 API |
| 让答案“有据可查” | 回答必须带 `local_catalog`（PostgreSQL）、`mcp`（Mock 政务目录）、`rag`（Milvus 知识/团队语料）三类溯源，取不到来源就不编造 |
| 让演示可重复、可验收 | 幂等键 + 乐观锁版本号 + 不可变事件/审计 + 完整 smoke 脚本 |
| 保护私人数据 | 私人表单、材料、办件不进公共缓存、不进 LLM 上下文；文件走私有对象存储 |

### 1.2 系统拓扑（逻辑）

```
Android 客户端（WebView 本地资产 SPA + 原生网关）
        │  HTTP / SSE / WebSocket（HTTPS，仅原生层持 Token）
        ▼
FastAPI 模块化单体（Uvicorn，Python 3.13）
        │
        ├── PostgreSQL 16        权威业务数据（账号/事项/办件/审核/审计）
        ├── Redis 7              公开检索缓存 v3（只缓存 MCP/RAG 检索上下文）
        ├── Milvus 2.6           向量检索（本地知识 256 维 + 团队 RAG 1024 维）
        ├── MinIO                私有对象存储（材料 / 知识 / 模板 / 生成件）
        ├── MCP Server（Node）   只读政务目录工具（Streamable HTTP）
        │        └── Mock 政务 REST API（六个演示事项）
        └── DeepSeek（LangChain） 仅生成解释性回答

独立进程：material-worker（LibreOffice + 单次 Qwen VL，异步生成可填写 DOCX）
可选：华为云 MetaStudio 数字人（第三方大脑回调模式，默认关闭）
可选：华为 HMS Map / Location / Navi Kit 导航（仅前台、本机定位）
```

### 1.3 重要边界（技术选型的前提）

- 所有账号、身份、企业、材料、政策、缴费、邮寄结果**均为合成演示数据**。
- 没有接入真实政务、短信、支付、人脸、银行、快递平台。
- 仍保留 `Mock API` 与 `Demo Provider`，**不等同于正式政务上线**。
- API 当前口径：**83 个 operation、76 个 path**；就绪检查本地 7 项、云端启用团队 RAG 后 8 项。

---

## 二、技术栈总览

| 层次 | 技术选型 | 版本 | 作用 |
| --- | --- | --- | --- |
| 移动端 | Android（Java）+ Gradle | AGP 8.5.2，compileSdk 35 / minSdk 24 / targetSdk 35，Java 8 | 本地资产门户、原生边界与原生能力宿主 |
| 移动端 UI 层 | WebView + 本地 HTML/CSS/JS 资产 | `WebViewAssetLoader` | 门户 SPA，离线加载，禁止直接联网 |
| 移动端网络 | OkHttp | 4.12.0 | 全部 HTTP/SSE/WebSocket 强类型网关 |
| 移动端序列化 | Gson | 2.11.0 | 合同模型与 JSON 映射 |
| 移动端 UI 框架 | AndroidX（core/appcompat/lifecycle/webkit/camera） | core 1.13.1，appcompat 1.7.0，lifecycle 2.8.4，webkit 1.11.0，camera 1.4.2 | 兼容性、ViewModel/LiveData、安全 WebView、CameraX 视觉采集 |
| 移动端测试 | JUnit4 + MockWebServer + arch core testing | 4.13.2 / 4.12.0 / 2.2.0 | JVM 单测与网关合同测试 |
| 后端语言/框架 | Python + FastAPI + Uvicorn | Python 3.13.7，FastAPI 0.141.1，Uvicorn 0.52.4 | 模块化单体 API、SSE、OpenAPI |
| 后端数据访问 | SQLAlchemy + asyncpg + Alembic | 2.0.52 / 0.31.0 / 1.19.1 | 异步 ORM、迁移（head = `0009_consultation_materials`） |
| 后端配置/校验 | pydantic + pydantic-settings | 2.13.4 / 2.15.0 | 环境变量配置、请求/响应模型 |
| 后端鉴权 | PyJWT + argon2-cffi | 2.10.1 / 25.1.0 | JWT access/refresh、Argon2 口令哈希 |
| 后端缓存 | redis（asyncio） | 7.2.2 | 公开检索缓存 `smart-gov:retrieval:v3:*` |
| 后端向量库 | pymilvus | 2.6.17 | Milvus 读写、collection/alias 管理 |
| 后端对象存储 | minio | 7.2.20 | 私有材料/知识/模板/生成件 bucket |
| 后端 LLM 接入 | langchain-core + langchain-deepseek | 1.6.0 / 1.1.0 | ChatDeepSeek 普通与流式调用 |
| 后端 MCP 客户端 | mcp | 1.29.0 | 作为 MCP Client 调用只读政务工具 |
| 后端文档处理 | python-docx + pypdf + python-multipart | 1.2.0 / 6.6.0 / 0.0.22 | DOCX 渲染、PDF 解析、multipart 上传 |
| 后端 HTTP 客户端 | httpx | 0.28.1 | 调用 DashScope / 视觉 / MCP 健康检查 |
| MCP 服务 | Node.js + Express + MCP SDK + zod | Node >= 20，Express 5.1.0，`@modelcontextprotocol/sdk` 1.30.0，zod 3.25.76 | Streamable HTTP MCP，5 个只读工具 |
| Mock 政务 API | Node.js + Express | Node >= 20，Express 5.1.0 | 六个演示事项目录数据 REST API |
| 关系数据库 | PostgreSQL | 16.4-alpine | 事项、账号、办件、审核、审计、RAG 清单权威源 |
| 缓存 | Redis | 7.4.1-alpine | 公开检索缓存、额度计数、短期会话/意图 |
| 向量数据库 | Milvus | v2.6.22（standalone） | 知识检索与团队 RAG 向量检索 |
| 向量库元数据 | etcd | v3.5.25 | Milvus 依赖 |
| 对象存储 | MinIO | RELEASE.2024-05-28T17-19-04Z | S3 兼容私有对象存储 |
| 容器编排 | Docker + Docker Compose v2 | compose 文件 4 份 | 本地/候选/云端/MetaStudio 覆盖式编排 |
| 反向代理/TLS | Nginx + lego | lego v5.4.0 | HTTPS、限流、SSE 关闭缓冲、Let’s Encrypt IP 证书 |
| 文档渲染 | LibreOffice（`soffice`）+ poppler-utils（`pdftoppm`） | Debian 包 | DOCX→页面图→视觉模型 |
| 外部大模型 | DeepSeek `deepseek-v4-flash`（非思考） | — | 咨询回答生成 |
| 外部向量服务 | 阿里云 DashScope `text-embedding-v4`（1024 维） | — | 团队 RAG 语料 embedding |
| 外部视觉模型 | DashScope `qwen3-vl-flash-2026-01-22` | — | 单次抽取 DOCX 白名单字段 |
| 华为能力 | AG Connect、HMS Map/Location/Navi、MetaStudio Web SDK 5.0.6 | 见 `libs.versions.toml` | 登录身份、地图定位导航、可选数字人 |
| 测试 | pytest + pytest-asyncio + Node test runner + Gradle lint | pytest 9.0.2 | 全栈源码级验收 |

---

## 三、逐个模块清单

### 3.1 `android/` —— Android 客户端

**作用**：单 Activity 安全 WebView 宿主。页面本身是随 APK 打包的本地资产，WebView 不允许直接访问网络；所有 API、Token 和二进制下载只经过原生强类型网关。

**基础技术**

| 项 | 值 |
| --- | --- |
| 包名 / 身份 | `com.example.aicompanion`（沿用 AG Connect 身份，APK 被视为旧应用同包） |
| 语言 | Java（source/target 1.8） |
| 构建 | Gradle + Android Gradle Plugin 8.5.2（本机使用 gradle 8.14.5） |
| SDK | compileSdk 35 / minSdk 24 / targetSdk 35 |
| 当前版本 | versionCode 7，versionName `0.3.0-material-docgen-test` |
| 默认后端 | `https://123.249.68.176`，可用 `-PgovApiBase=` 覆盖（必须是纯 HTTPS origin） |
| 网络安全 | debug/release 都拒绝明文 HTTP（`network_security_config`） |

**内部结构（分层）**

| 子包 | 技术/角色 | 主要类 | 作用 |
| --- | --- | --- | --- |
| `portal/boundary` | 边界类 | `PortalJsBoundary`、`SecureWebViewHost`、`DocumentPickerBoundary`、`MaterialDocumentSaveBoundary`、`VoiceCaptureBoundary`、`TextToSpeechBoundary`、`ServiceNavigationBoundary` | 隔离 JS 与原生能力，只暴露枚举化命令和受控参数 |
| `portal/business` | 业务策略 | `PortalCommandPolicy`、`DynamicFormValidator`、`MaterialCompletionPolicy`、`RoleNavigationPolicy`、`SensitiveDisplayPolicy` | 命令授权、表单校验、材料完备性、角色导航、敏感信息显示 |
| `portal/coordinator` | 协调者 | `PortalCoordinatorViewModel`、`AuthCoordinator`、`CitizenCoordinator`、`StaffTaskCoordinator`、`AdminCoordinator`、`VoiceConsultationCoordinator` | 编排页面用例，结果通过可观察 ViewModel 状态回传 |
| `portal/gateway` | 网关/适配器 | `NativeApiClient`、各 `OkHttp*Gateway`、`AndroidKeystoreSessionStore`、`SecureSessionBrokerProvider`、`MaterialDocumentGateway/Validator` | HTTP/SSE 调用、Token 加密存储与跨进程 broker、DOCX 下载与完整性校验 |
| `portal/entity`、`portal/model` | 实体/合同 | `PortalEntities`、`PortalContract` | 强类型账户、事项、办件、待办、流式事件 |
| `assistant` | 旧版聊天遗留 | `AssistantViewModel`、`GovAssistantRepository`、`ChatContract` | 保留的聊天数据源与合同 |
| `navigation` | 导航能力 | `ServiceNavigationActivity`、`ServiceNavigationController`、`HuaweiNaviEngineAdapter`、`ForegroundLocationSource`、`OkHttpNavigationOptionsGateway`、`AndroidNavigationSpeechOutput` | 事项网点导航页：本机定位排序最近三点、Navi Kit 路线预览与逐向 TTS |
| `metastudio` | 数字人能力 | `DigitalHumanActivity`、`DigitalHumanWebViewHost`、`DigitalHumanCoordinator`、`OkHttpDigitalHumanGateway`、`DigitalHuman*Policy` | 独立 `:digital_human` 进程与隔离 WebView，加载官方 SDK 包装页 |
| `metastudio/vision` | 摄像头视觉 | `CameraXVisionController`、`VisionWebSocketGateway`、`VisionFrameSelector`、`Jpeg/Yuv` 编码器 | CameraX 采集、限帧筛选、WebSocket 送帧、内存前置缓存 |
| `web` | WebView 安全 | `SecureAssetWebViewClient` | 只允许本地资产加载 |

**JS 边界与隐私约束**

- JS 只能提交**枚举化命令**，不能指定任意 URL、HTTP 方法、原生类名、坐标或 Intent。
- JS Bridge 仅暴露：校验后的命令封包、材料选择、语音开始/停止、`openServiceNavigation(service_id)`、`saveGeneratedDocument(generation_id)`。
- Token 用 Android Keystore 加密存储，不进入 WebView、日志或明文 SharedPreferences。
- 定位只在用户点击后于前台运行，当前位置不上传后端、不持久化。
- MetaStudio 包装页只允许麦克风权限，不发定位/地图/Navi 权限；AK/SK、App Key 永不进入 APK/WebView。
- 构建期强制校验 MetaStudio Web SDK 5.0.6 的 11 个资产 SHA-256 与 CMS 证据，缺失即构建失败。

**测试**：`app/src/test/java` 下约 30 个 JVM 测试类（网关合同、命令策略、视觉协议、导航生命周期等）+ `app/src/test/js` 两个 Node 合同测试 + `lintDebug` / `assembleDebug`。

---

### 3.2 `backend/` —— 后端模块化单体（核心）

**作用**：全部业务写入口；BCE（Boundary–Control–Entity）分层，领域层是纯 Python，可独立测试。

**依赖方向**：`边界类 → 协调者类 → 领域实体/业务逻辑/端口`，基础设施适配器实现端口。

| 子目录 | 技术 | 作用 |
| --- | --- | --- |
| `app/boundaries/` | FastAPI 路由、Pydantic Schema、SSE、Multipart、WebSocket 协议 | 输入校验、鉴权、协议转换；`http.py` 是主业务路由（约 45 KB） |
| `app/application/` | 协调者（纯 Python 用例编排） | `coordinators.py`（约 117 KB）包含咨询/办件/审核/目录/转人工/预约/缴费/核验/邮寄/知识/管理/幂等协调者；另有 `metastudio.py`、`vision.py`、`material_documents.py`、`navigation.py`、`ports.py`（端口定义）、`dtos.py` |
| `app/domain/` | dataclass + enum + 策略 | `entities.py`、`enums.py`、`policies.py`：状态机、资格评估、材料规则、命令授权、溯源策略 |
| `app/infrastructure/` | SQLAlchemy / MinIO / Redis / Milvus / MCP / LLM / DashScope | `repositories.py`（约 186 KB）、`records.py`（ORM 映射，`*Record` 命名）、`runtime.py`（组合根）、`object_store.py`、`material_documents.py`、`vision.py`、`rag_corpus_*.py`、`dashscope_embedding.py`、`composite_retriever.py`、`metastudio.py`、`providers.py`、`security_adapter.py`、`catalog_seed.py` |
| `app/ops/` | 运维脚本入口 | `material_document_worker.py`（Worker 主循环）、`import_rag_corpus.py`、`sanitize_rag_corpus.py`、`check_rag_embedding.py`、`material_template_import.py`、`material_template_check.py` |
| `app/` 根文件 | 配置/安全/缓存/知识/配额 | `config.py`、`main.py`、`services.py`、`database.py`、`cache.py`、`knowledge.py`、`llm.py`、`mcp_client.py`、`milvus.py`、`security.py`、`quota.py`、`models.py`、`schemas.py`、`errors.py` |
| `migrations/versions/` | Alembic | `0001_initial` → `0009_consultation_materials`（身份目录、办件运营、知识审计、团队语料、MetaStudio、导航目录、材料文档、会话模板意图） |
| `tests/` | pytest + pytest-asyncio | 约 20 个测试模块；`test_vision.py`（约 120 KB）、`test_metastudio.py`（约 65 KB）最大 |
| `fixtures/` | CSV | 导航目录模板/演示/真机测试数据 |
| `resources/material_templates/v1/` | DOCX + JSON 清单 | 5 个允许的模板源文件、`manifest.json`（成品哈希）、`source-map.json`（18 个材料项审查映射） |
| `tools/` | Python 脚本 | 模板包构建（`build_material_template_pack.py`）、样例渲染 |

**API 分组（`/api/v1` 前缀）**

| 分组 | 认证 | 说明 |
| --- | --- | --- |
| `/health/live`、`/health/ready` | 否 | 进程存活 / 全栈依赖就绪（7 或 8 项） |
| `/chat`、`/chat/stream` | 公开事项可匿名 | 普通/SSE 多轮咨询，可带 `service_id`，私人办件查询需登录 + `application_id` |
| `/auth/*` | 按接口 | 演示验证码、个人/企业注册、登录、刷新、登出、当前账号 |
| `/services/*` | 公开 | 事项、资格预检、材料、流程、表单、窗口、导航网点选项 |
| `/applications/*` | 群众 | 草稿、表单、材料、提交、补正、撤回、丢弃、时间线、邮寄、材料模板与生成件 |
| `/consultations/*` | 登录 | 历史、消息恢复、材料意图确认、反馈、转人工、发起人取消 |
| `/staff/*` | 工作人员 | 待办、认领、审核决定、办结、人工咨询 |
| `/admin/*` | 管理员 | 组织/窗口/人员/账号、事项版本生命周期、知识上传/重试/归档、导航目录 CSV 导入、审计、指标 |
| `/appointments/*`、`/payments/*`、`/verifications/*`、`/deliveries/*` | 按操作 | 预约与本地模拟能力 |
| `/integrations/metastudio/*` | 混合 | 数字人会话、唯一 LLM 回调（JSON/SSE 同路径）、动作意图交换 |

**后端硬性技术约束**

- 所有写操作需要 `Idempotency-Key`，并保存 `(actor, scope, key, payload hash)`，同键不同载荷报冲突。
- 并发编辑用响应中的 `version` 乐观锁，冲突返回 `409`。
- `ApplicationEvent`、`BusinessAuditEvent` 只追加；已提交办件不物理删除。
- 资格/条件材料只解释受限 JSON DSL：`eq/ne/in/exists/lt/lte/gt/gte/all/any/not`，不执行 Python/JS/SQL。
- 领域层禁止导入 FastAPI、SQLAlchemy、Redis、Milvus、MCP、Android 类型。

---

### 3.3 `mcp-server/` —— MCP 只读政务目录服务

**作用**：把“模拟政务目录”包装成标准 MCP 工具，给后端 AI 提供**只读**的政务数据来源。

| 项 | 说明 |
| --- | --- |
| 技术 | Node.js >= 20、Express 5.1.0、`@modelcontextprotocol/sdk` 1.30.0、`zod` 3.25.76、`smart-gov-mcp-server`（本地 vendor tgz 1.0.1） |
| 传输 | Streamable HTTP（`/mcp`），另有 `/health` |
| 认证 | 内部 Bearer Token，`crypto.timingSafeEqual` 恒定时间比较 |
| 会话 | `mcp-session-id`，TTL 默认 15 分钟，最多 128 会话，过期自动清理 |
| 工具（5 个） | `search_services`、`get_service_details`、`get_material_checklist`、`get_process_navigation`、`get_window_info` |
| 上游 | Mock 政务 REST API，独立 Bearer Token |
| 关键边界 | 只有只读工具，**不提供 `generate_docx` 等写工具**；不接触 DOCX、对象键或下载地址 |

---

### 3.4 `mock-gov-api/` —— 模拟政务 REST API

**作用**：提供六个演示事项的目录数据（事项详情、材料清单、流程、窗口），供 MCP 服务读取。

| 项 | 说明 |
| --- | --- |
| 技术 | Node.js >= 20、Express 5.1.0，纯内存数据 |
| 数据 | 6 个事项：社保卡、身份证补领、个体户登记、企业社保登记、劳动合同备案、公积金缴存 |
| 接口 | `/health`、`/services`、`/services/:id`、`/services/:id/materials`、`/services/:id/process`、`/services/:id/windows` |
| 认证 | Bearer Token（`GOV_API_TOKEN`） |
| 数据标记 | 所有响应带 `is_demo: true`，坐标标为 `DEMO_GCJ02` |

---

### 3.5 `infra/` 与 Compose 编排

**作用**：把本地与云端的全套依赖一键拉起。

| 文件 | 技术 | 作用 |
| --- | --- | --- |
| `compose.yaml` | Docker Compose | 本地演示栈：PostgreSQL、Redis、etcd、MinIO、Milvus、Mock API、MCP、API、material-worker |
| `compose.cloud.yaml` | Docker Compose | 云端单机演示：API 只绑 `127.0.0.1:18000`，Docker secrets 注入，Nginx/lego，资源限制 |
| `compose.candidate.yaml` | Docker Compose | 灰度候选：并行 `api-candidate` / `material-worker-candidate`，Nginx 切流前验收 |
| `compose.metastudio.yaml` | Docker Compose | 可选 MetaStudio 覆盖层，强制校验官方 SDK 5.0.6 资产与 IAM/App secrets |
| `infra/milvus-user.yaml` | Milvus 配置 | 把日志级别降到 `warn`，避免对象存储凭据进入 stdout |

**本地端口**：API `127.0.0.1:8000`，PostgreSQL 调试 `127.0.0.1:15432`，其余仅在 Compose 网络内可达。

---

### 3.6 `deploy/` —— 云部署与运维脚本

**作用**：在单机 Linux x86_64 上完成 IP-only HTTPS 演示部署、RAG 付费导入、备份回滚与流量切换。

| 子目录 | 技术 | 作用 |
| --- | --- | --- |
| `deploy/nginx/` | Nginx 配置模板 | HTTP→HTTPS 跳转、TLS 1.2/1.3、按 IP/路径限流、SSE `proxy_buffering off`、MetaStudio 专用脱敏日志格式 |
| `deploy/certbot/` | Let’s Encrypt / lego v5.4.0 | `shortlived` IP 证书签发与 HTTP-01 无停机续期 |
| `deploy/systemd/` | systemd service + timer | 证书自动续期定时任务 |
| `deploy/scripts/` | Bash | `preflight.sh`、`deploy.sh`、`health-check.sh`、`import-rag.sh`、`backup.sh`/`restore-legacy.sh`、`rollback.sh`、`deploy-candidate.sh`、`verify-cloud-smoke.sh`、`verify-metastudio-smoke.sh`、`lib.sh` 等约 25 个脚本 |
| `deploy/container-secret-entrypoint.sh` | Shell | 把 `*_FILE` secret 注入为容器内环境变量 |
| `deploy/cloud.env.example` | 环境模板 | 云端非秘密配置 |

---

### 3.7 `scripts/` —— Windows 本地开发脚本

**技术**：PowerShell 5.1+/7（全部通过 AST 语法校验）。

| 脚本 | 作用 |
| --- | --- |
| `init-env.ps1` | 从外层 `key-list.txt` 映射 DeepSeek 密钥，随机生成数据库/JWT/MinIO/内部服务凭据，写入 `.env`（不打印密钥、只补齐缺项） |
| `dev-up.ps1` / `dev-down.ps1` | 启动/停止 Compose 栈；停止保留所有命名卷 |
| `logs.ps1` | 查看服务日志 |
| `migrate.ps1` | `alembic upgrade head` 与 `-StatusOnly`，不做 downgrade |
| `show-demo-accounts.ps1` | 显式显示本地演示账号凭据 |
| `business-smoke.ps1` | 不调用 DeepSeek 的完整业务 smoke（注册→预检→上传→提交→审核→办结→时间线→指标→取消类操作） |
| `smoke.ps1` | 需要付费的 AI smoke：真实调用一次 DeepSeek，校验三类溯源与 Redis 缓存键 |
| `material-document-smoke.ps1` | 材料 DOCX 生成链路 smoke |
| `test-all.ps1` | 全栈源码级检查：PowerShell AST、pytest、Node check/test、Android lint+test+assemble |

---

### 3.8 `docs/` —— 设计文档

| 文档 | 内容 |
| --- | --- |
| `architecture.md` | 架构目标、拓扑、分层、关键数据流、降级边界、迁移边界 |
| `business-design.md` | BCE 类别清单、领域聚合关系、规则引擎、五个状态机、团队 RAG 数据集 |
| `role-permissions.md` | 三类角色身份模型、API 权限矩阵、Android 命令边界、失败语义 |
| `api-examples.md` | API 示例（约 24 KB） |
| `material-document-generation.md` | DOCX 模板生成的范围、流程、数据清理、部署门禁 |
| `metastudio-integration.md` | 数字人接入合同、控制台参数、SIS 委托边界、隐私控制 |
| `navigation-integration.md` | Navi Kit 导航边界、CSV 管理、Android 流程 |
| `cloud-deployment.md` | 云端 IP-only 部署、RAG 导入、回滚、APK 交付顺序 |

---

## 四、数据存储与基础设施组件

| 组件 | 镜像/版本 | 存什么 | 不存什么 |
| --- | --- | --- | --- |
| PostgreSQL | `postgres:16.4-alpine` | 账号、部门/窗口、事项与版本、办件与材料元数据、审核、事件、审计、咨询会话、预约/缴费/核验/邮寄、知识任务、团队 RAG 清单与检查点、幂等记录 | 不存原始密码、不存生物特征 |
| Redis | `redis:7.4.1-alpine`（AOF） | 公开检索缓存 v3、聊天额度计数、MetaStudio 短期上下文、动作意图、视觉票据 | 不存私人表单/材料/办件上下文 |
| Milvus | `milvusdb/milvus:v2.6.22` | `gov_knowledge_v2`（本地知识，256 维 hash embedding）；版本化团队语料 collection/alias（DashScope 1024 维） | 不存原图、不存私人办件 |
| MinIO | `RELEASE.2024-05-28T17-19-04Z` | 4 个**互不相同**的私有 bucket：材料 / 知识 / 模板源 / 24 小时生成件 | 不生成预签名或公共直链 |
| etcd | `quay.io/coreos/etcd:v3.5.25` | Milvus 元数据 | — |

**向量检索技术细节**

- 本地知识 embedding：`app/knowledge.py` 的确定性 signed-hashing（字符 1–3 gram，SHA-256），256 维，余弦距离，无外部调用。
- 团队 RAG embedding：DashScope `text-embedding-v4`，1024 维，按 `(content_hash, model, dimension)` 去重缓存。
- 融合策略：`CompositeKnowledgeRetriever` 对本地知识与团队语料执行 RRF（reciprocal rank fusion），任一侧失败返回降级 warning。
- 固定数据集：`team-2026-08-22-v1`，29 个主题、**15,858 条正文分块 + 1,012 条文档路由向量**，标注为“未核验演示参考”。

---

## 五、外部云服务与第三方 SDK

| 服务 | 用途 | 启用方式 | 默认状态 |
| --- | --- | --- | --- |
| DeepSeek `deepseek-v4-flash` | 咨询回答（普通 + 流式），显式关闭 thinking | `DEEPSEEK_API_KEY` + `LLM_MODE=deepseek` | 本地 smoke 默认不调用；`business-smoke` 不付费 |
| 阿里云 DashScope `text-embedding-v4` | 团队 RAG 语料 embedding | `deploy/scripts/import-rag.sh --confirm-paid-import` | 关闭（`RAG_GROUP_ENABLED=false`） |
| 阿里云 DashScope `qwen3-vl-flash-2026-01-22` | 从 DOCX 页面图抽取白名单字段 | `MATERIAL_TEMPLATE_PROVIDER=dashscope` + secret 文件 | 本地 `mock` |
| 阿里云 DashScope 视觉（scene/document） | 摄像头视觉理解 | `VISION_PROVIDER=dashscope` | `mock`（无外呼） |
| 华为云 MetaStudio（北京四） | 数字人实时交互，固定“第三方大脑回调”方案 | `compose.metastudio.yaml` + `METASTUDIO_ENABLED=true` | 关闭 |
| 华为 AG Connect | 应用身份与 SDK 配置 | `agconnect-services.json`（忽略提交） | 构建期校验 |
| 华为 HMS Map Kit | 网点地图与轨迹绘制 | `huaweiMaps 6.15.1.322` | 本地 PoC |
| 华为 HMS Location Kit | 前台定位（最近三网点排序） | `huaweiLocation 6.15.1.300` | 需用户显式开启 |
| 华为 HMS Navi Kit | 步行/驾车路线与逐向 TTS | `huaweiNavi 6.13.0.300` | 延迟初始化，仅本地 PoC |
| 华为 HMS Site Kit | 站点能力 | `huaweiSite 6.2.0.301` | 仅在 `libs.versions.toml` 声明版本，未加入 `app/build.gradle` 依赖 |
| 华为 HMS Awareness | 场景感知 | `huaweiAwareness 3.1.0.302` | 仅在 `libs.versions.toml` 声明版本，未加入 `app/build.gradle` 依赖 |

---

## 六、关键业务机制

| 机制 | 实现 | 作用 |
| --- | --- | --- |
| 资格预检 | 受限 JSON DSL + `EligibilityEvaluator` | 返回 `ELIGIBLE/INELIGIBLE/NEEDS_INFORMATION`，始终声明“仅供预检，非正式审批结论” |
| 材料分类 | `MaterialRuleEngine` | `REQUIRED/CONDITIONAL_REQUIRED/OPTIONAL/NOT_APPLICABLE/NEEDS_INFORMATION` + 触发原因 |
| 动态表单 | JSON Schema 子集 + 服务端二次校验 | 不信任 Android 本地校验 |
| 事项版本 | `ServiceVersion` 不可变 + 生命周期策略 | 已发布版本不可原地改写，新版本显式切换 |
| 办件状态机 | `ApplicationStateMachine` | 草稿→提交→认领→补正/驳回/批准→缴费→办结；提交后不可物理删除 |
| 审核 | 条件更新认领 + `ReviewDecisionPolicy` + `AssignmentPolicy` | 并发只有一个工作人员认领成功；管理员不能代替审批 |
| 转人工 | `HandoffStateMachine` + `HandoffAccessPolicy` | 群众可取消本人未终结工单，仅所属部门可回复与解决 |
| 预约/缴费/核验/邮寄 | 四个独立状态机 + Demo Provider | 全为模拟，仅 `ENVIRONMENT=local` 且显式启用时可推进 |
| 知识索引 | `KnowledgeStateMachine` + Milvus | `DRAFT→INDEXING→ACTIVE/INDEX_FAILED→SUPERSEDED→ARCHIVED`，重试/归档会清除公开缓存 |
| 材料 DOCX 生成 | 独立 Worker + `SKIP LOCKED` 租约 + LibreOffice + 单次 Qwen VL | 模型只返回白名单字段，正文/OOXML 由受控渲染器生成；24 小时过期 |
| 咨询问答 | `ConsultationCoordinator` + `GroundedAnswerPolicy` | 歧义先澄清；三类溯源独立标识；无有效来源不调模型 |
| SSE 流 | 固定 `meta/delta/done/error` 事件 | Android 端侧 TTS 播报，原始音频不上传 |
| 幂等 | `IdempotencyCoordinator` | `(actor, scope, key, payload hash)`，同键不同载荷冲突 |
| 并发 | `Application.version` 乐观锁 | 旧版本提交返回 `409` |
| 审计 | `BusinessAuditEvent` 只追加 | 与办件事件同事务写入 |

---

## 七、安全与数据隔离（技术实现方式）

| 数据 | 存储/处理 | 禁止 |
| --- | --- | --- |
| 公开事项/知识 | PostgreSQL、Milvus、Redis v3、MCP/Mock | 不宣称真实政策 |
| 账号/JWT refresh hash | PostgreSQL；Android 侧 Keystore 加密 | 不进 WebView/日志/明文偏好 |
| 私人表单/办件 | PostgreSQL 授权查询 | 不进公共缓存或 LLM 上下文 |
| 上传材料 | MinIO 私有 bucket + PostgreSQL 哈希元数据 | 不生成公共直链、不在 WebView 直接打开 |
| 生成 DOCX | 独立私有 bucket；快照用完即清；24 小时过期 | 模型不生成证件/证明/OOXML |
| 生物/支付/邮寄 | 仅存状态与模拟引用 | 不采集生物数据、无真实资金/快递 |
| 凭据 | 被忽略的 `.env` / Docker secrets（`*_FILE`） | 不进 APK、API 响应、提交历史 |
| MetaStudio 音频/交互 | 官方 SDK 与华为云处理 | 未告知同意不得启用；AK/SK 不进日志或 WebView |

**认证技术细节**：JWT access 默认 15 分钟、refresh 默认 7 天且只存哈希；冻结账号递增 `token_version` 并撤销 refresh，使旧 token 立即失效。

**降级边界**：PostgreSQL 挂 → 结构化 `503`；DeepSeek 挂 → 结构化 `502`（不用模板冒充）；Redis 挂 → 公开咨询绕过缓存；MCP/Milvus 单点挂 → 用剩余来源并返回 warning；MinIO 挂 → 文件操作失败并反映到 readiness；材料模型/LibreOffice 挂 → 该任务 `FAILED`，不重试不降级。

---

## 八、测试与验收体系

| 层次 | 工具 | 覆盖 |
| --- | --- | --- |
| PowerShell | AST 语法解析 | `scripts/*.ps1` 全部 |
| 后端 | pytest 9.0.2 + pytest-asyncio 1.3.0 | 领域业务、API、缓存、MCP 客户端、知识、RAG（archive/milvus/repo/import）、材料文档、MetaStudio、导航、配额、视觉、安全 |
| MCP / Mock | Node 原生 test runner + `node --check` | 两个 Node 工程 |
| Android | `lintDebug` + `testDebugUnitTest` + `assembleDebug` + Node 包装页诊断 | JVM 单测、lint、构建、MetaStudio 包装页隐私合同 |
| 业务验收 | `scripts/business-smoke.ps1` | 不调用 DeepSeek 的端到端业务闭环 |
| AI 验收 | `scripts/smoke.ps1` | 一次真实 DeepSeek 调用 + 三类溯源 + Redis 缓存键校验（可选第二次验证 `cache_hit`） |

---

## 九、后续做技术改造 / 重建项目时，重点关注这些耦合点

1. **后端分层是硬约束**：`domain` 不能依赖任何框架。替换 Web 框架、ORM 或向量库时，改动集中在 `boundaries/` 与 `infrastructure/`，不要动 `domain/policies.py`。
2. **组合根只有一个**：`backend/app/infrastructure/runtime.py` 负责把所有适配器注入协调者，是替换外部依赖（Milvus/MinIO/MCP/LLM）的主要入口。
3. **端口抽象在 `application/ports.py`**：换实现只需新增适配器并改 runtime 注入。
4. **数据库迁移只前进**：`migrate.ps1` 只做 `upgrade head`，脚本不 downgrade、不删表、不删数据卷。重建项目时需重新设计迁移链而非回退。
5. **Android 与后端的合同面**：`PortalContract` / `PortalEntities` 对应 FastAPI 的 DTO/Schema；接口改名要两边同步，`NativeGatewaysTest`、`PortalContractWireTest` 会兜底。
6. **安全边界不可削**：JS 命令白名单、Token 只在原生层、私有 bucket 隔离、幂等键、乐观锁、溯源强制，是这套演示系统的核心卖点。
7. **可选能力都是开关式的**：团队 RAG（`RAG_GROUP_ENABLED`）、MetaStudio（`METASTUDIO_ENABLED`）、材料 DOCX（`MATERIAL_DOCUMENTS_ENABLED`）、视觉（`VISION_ENABLED`）默认关闭，重建时建议沿用这种“默认最小暴露”的策略。
8. **外部付费点**：DeepSeek 调用、DashScope embedding 导入、Qwen VL 生成都需显式确认，脚本默认不触发。
9. **目录名注意**：项目文件夹实际是 `-AI--main`，路径引用和脚本中的相对路径需要一并调整。
10. **当前最大文件**：`backend/app/infrastructure/repositories.py`（约 186 KB）、`backend/app/application/coordinators.py`（约 117 KB）、`android/app/src/main/assets/portal-app-v2.js`（约 69 KB）、`backend/app/infrastructure/vision.py`（约 65 KB）。若做重构，这几个是优先拆分对象。
