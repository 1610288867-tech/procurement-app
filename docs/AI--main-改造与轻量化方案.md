# `AI--main` 改造与轻量化方案

> 配套文档：[AI--main-项目技术清单.md](AI--main-项目技术清单.md)
> 目标：把这个"功能超载的政务演示项目"裁剪并替换成你自己的项目。
> 生成时间：2026-10-01。

---

## 零、开始之前的两件事

**1) 先确认授权。** 这个项目是对外发布形态较完整的仓库（含部署脚本、Docker secrets 编排、官方日志脱敏配置），建议先确认它的许可协议再二次开发。另外三块有独立条款、不能改造或再分发：

- `android/app/src/main/assets/metastudio/sdk/` 是**华为云 MetaStudio 官方 Web SDK 5.0.6 原样资产**，仓库里还带 `provenance/HwICSUiSdk-5.0.6.zip.cms` 完整性证据，构建脚本会逐文件校验 SHA-256。要么整体保留、要么整体删除，不要修改其中任何文件。
- 华为 AG Connect / HMS Map / Location / Navi 需要你自己的开发者账号与 `agconnect-services.json`。
- DashScope、DeepSeek 的调用受各自服务条款约束，且是**付费点**。

**2) 策略上先"砍"再"换"。** 这个项目最大的问题是功能堆叠，不是技术落后。先删掉不需要的模块能一次性去掉一半代码和 95% 的仓库体积；剩下的再谈技术替换，风险和工作量都小得多。

---

## 一、结论先行：可裁剪空间有多大

### 1.1 体积分布（实测）

| 项 | 数值 | 说明 |
| --- | --- | --- |
| 仓库总计 | **62.8 MB / 376 个文件** | 不含 git 历史 |
| Android MetaStudio 资产 | **59.9 MB / 15 个文件** | 占仓库 **95.4%** |
| 其中 `modelData.js` | 34.1 MB | 数字人模型数据 |
| 其中 `wasmData.js` | 14.9 MB | WASM 数据 |
| 其中 `HwICSUiSdk.js` + `.esm.js` | 8.4 MB | 双份 SDK 打包 |
| 其余全部代码 | 约 2.9 MB | 后端 + Android Java + 两份 Node 服务 |

**结论**：只要不用华为数字人，删掉一个目录就能让仓库从 62.8 MB 降到 2.9 MB。

### 1.2 资源占用（`compose.cloud.yaml` 的实配限额）

| 服务 | mem_limit | 是否可省 |
| --- | --- | --- |
| Milvus | 2560m | ✅ 可换 pgvector |
| material-worker | 1536m | ✅ 不需要 DOCX 生成即可删 |
| API | 1024m | ❌ 核心 |
| rag-import | 1024m | ✅ 不用团队 RAG 即可删 |
| PostgreSQL | 768m | ❌ 核心 |
| MinIO | 768m | ⚠️ 可换本地盘 |
| etcd | 512m | ✅ 只为 Milvus 存在 |
| Redis | 384m | ⚠️ 可换 Valkey，或按用途拆 |
| MCP Server | 384m | ✅ 可合并进 FastAPI |
| Mock API | 256m | ✅ 可合并进 FastAPI |
| **合计上限** | **≈ 8.2 GB** | 裁剪后可降到 **≈ 2 GB** |

---

## 二、可删除 / 可关闭的模块

按"删除收益 ÷ 删除成本"从高到低排列。

### ① 华为 MetaStudio 数字人 —— 收益最大，优先决策

| 维度 | 内容 |
| --- | --- |
| 规模 | Android 资产 59.9 MB；Android `metastudio/` 包约 15 个类 + `DigitalHumanActivity`（37 KB）另加 8 个视觉类；后端约 1,600 行（`application/metastudio.py` 587 + `boundaries/metastudio.py` 691 + `infrastructure/metastudio.py` 237 + schemas 91）；测试 `test_metastudio.py` 1,680 行 |
| 牵连 | 2 个迁移（`0006_metastudio`）、`compose.metastudio.yaml`、`deploy/scripts/verify-metastudio-smoke.sh`、`docs/metastudio-integration.md`、`app/application/vision.py` 的摄像头链路也主要服务于它 |
| 删除后影响 | 不影响任何政务业务能力；只失去"数字人语音交互" |
| 成本 | 低。它是独立包 + 独立路由 + 独立 compose 覆盖层，`METASTUDIO_ENABLED=false` 时本来就不加载 |
| 建议 | 除非明确要做华为数字人，**整体删除** |

> 注意：MetaStudio 也是唯一会把"端侧不上传音频"边界打破的模块（音频由华为云处理）。删除它同时让项目的隐私故事变干净。

### ② 摄像头视觉链路（CameraX + 双速帧协议）

| 维度 | 内容 |
| --- | --- |
| 规模 | 后端 `infrastructure/vision.py` 1,519 行 + `application/vision.py` 736 行 + `boundaries/vision_protocol.py` 301 行；测试 `test_vision.py` 3,172 行（全仓最大测试）；Android `metastudio/vision/` 8 个类，约 2,000 行 |
| 牵连 | CameraX 依赖、WebSocket 路由、Redis 视觉票据/额度、`VISION_*` 约 20 个环境变量、`infrastructure/vision.py` 的 DashScope 视觉适配器 |
| 删除后影响 | 失去"拍照识图/文档识别辅助"；普通咨询完全不受影响 |
| 成本 | 中。视觉与 MetaStudio 在 Android 端耦合在同一 Activity 里，建议**先删 MetaStudio、再删视觉**，顺序反了会很痛 |
| 建议 | 若无"拍照问政务"需求，**删除**。它是全仓复杂度最高的模块，却只服务一个附加卖点 |

### ③ 团队 RAG（版本化语料 + DashScope 付费导入）

| 维度 | 内容 |
| --- | --- |
| 规模 | `rag_corpus_archive.py` 382 + `rag_corpus_milvus.py` 339 + `rag_corpus_repository.py` 323 + `rag_import.py` 9.4 KB + `dashscope_embedding.py` + `ops/import_rag_corpus.py` + `ops/sanitize_rag_corpus.py` ≈ 1,300 行；测试 4 个文件 |
| 牵连 | 迁移 `0005_rag_corpus`、Milvus 双 collection + 双 alias、`deploy/scripts/import-rag.sh`、1,024 维 embedding 与 Redis 缓存 dataset 版本隔离、`/health/ready` 第 8 项校验 |
| 删除后影响 | 失去"团队知识库"，但**管理员上传知识 + 本地 Milvus 检索（`gov_knowledge_v2`）仍然可用**——这才是主要 RAG 能力 |
| 成本 | 低。默认就是关的（`RAG_GROUP_ENABLED=false`） |
| 建议 | **关闭并删除**。1,024 维 DashScope 是要真金白银付 embedding 费的 |

### ④ 材料 DOCX 异步生成 Worker

| 维度 | 内容 |
| --- | --- |
| 规模 | 后端 `infrastructure/material_documents.py` 858 + `application/material_documents.py` 520 + `ops/material_document_worker.py` + `tools/build_material_template_pack.py`（32 KB）≈ 2,000 行；两个测试文件共 76 KB；`backend/resources/material_templates/` 整套模板包 |
| 牵连 | 独立镜像 `Dockerfile.material-worker`（LibreOffice + poppler + 中文字体，镜像体积 1.5 GB+，运行内存 1536m）、迁移 `0008`/`0009`、专用 MinIO bucket、`MATERIAL_DOCUMENT_*` 约 10 个配置、Android `MaterialDocumentGateway`/`Validator`/`Boundary`（约 29 KB） |
| 删除后影响 | 失去"自动填写 Word 模板"；普通材料上传（PDF/JPEG/PNG）不受影响 |
| 成本 | 中。横跨 Android 保存边界、SAF、OOXML 校验，删起来要一起清 |
| 建议 | 不需要就**删除**。如果只是"想有个生成文档的亮点"，更轻的做法是**用纯模板字符串渲染**（python-docx 直接填充），彻底去掉 LibreOffice + 视觉模型，成本降到 5% |

### ⑤ 华为 HMS 导航（Map / Location / Navi Kit）

| 维度 | 内容 |
| --- | --- |
| 规模 | Android `navigation/` 包约 15 个类；后端 `application/navigation.py` 355 行；迁移 `0007_navigation_catalog`；CSV 导入接口与 `backend/fixtures/` |
| 牵连 | HMS Map/Location/Navi 三个依赖 + AG Connect、Android 定位权限、"仅前台定位"整套生命周期控制、`docs/navigation-integration.md` |
| 删除后影响 | 失去"到最近网点怎么办"；目录接口 `/navigation-options` 可一并删 |
| 成本 | 中低。它是自成一体的包 |
| 建议 | 不用华为生态就**删除**，同时把 AG Connect 一起换掉，APK 依赖数量立刻减半 |

### ⑥ 云端部署全家桶

| 维度 | 内容 |
| --- | --- |
| 规模 | `deploy/` 目录：27 个 Bash 脚本 + Nginx 4 个模板 + certbot + systemd 4 个单元；`compose.cloud.yaml` / `compose.candidate.yaml` / `compose.metastudio.yaml` 三份额外编排 |
| 牵连 | `deploy/secrets/`、`deploy/state/`、`deploy/releases/`、`deploy/cloud.env.example`、`docs/cloud-deployment.md` |
| 删除后影响 | 只失去云端 IP-only 部署能力；本地 `compose.yaml` + `scripts/` 完整保留 |
| 成本 | 极低，纯目录删除 |
| 建议 | 只做本地 Demo 或自建云 → **只留 `compose.yaml`**，其余删除。要做正式云部署 → 保留 `deploy.sh`/`health-check.sh`/`rollback.sh`/`backup.sh`，删掉 certbot/IP 证书那一整套（换 Caddy 或云厂商证书） |

### ⑦ MCP Server + Mock 政务 API 两个 Node 服务

| 维度 | 内容 |
| --- | --- |
| 规模 | 两个独立 Node 工程 + 两个 Dockerfile + 两个 package-lock（约 75 KB）+ 两个测试文件 |
| 作用 | MCP 提供 5 个只读政务工具；Mock API 提供 6 个演示事项数据 |
| 判断 | 这两个服务体现的是"**MCP 作为独立只读边界**"的架构意图，很漂亮，但对个人项目是纯负担 |
| 建议 | 分两种：<br>• 想保留 MCP 生态/面试亮点 → 保留<br>• 只想跑通业务 → 把 Mock 数据变成 JSON fixture 直接进 FastAPI，MCP 层的"只读边界"用 `ports.py` 里的 `GovernmentToolRetrievalPort` 换一个本地适配器即可 |

### ⑧ AG Connect 身份

删除 ⑤ 的 HMS 后，`agconnect` 插件与 `agconnect-services.json` 就没有依赖方了，可以一并移除，改用你自己的账号体系。

---

## 三、技术替换对照表

### 3.1 存储层（收益最高）

| 原技术 | 更轻的替代 | 收益 | 迁移成本 | 推荐度 |
| --- | --- | --- | --- | --- |
| **Milvus 2.6 + etcd + 复用 MinIO** | **pgvector**（PostgreSQL 扩展） | 省掉 Milvus 2.5 GB + etcd 512 MB 两个容器；复用已有 PG，运维归零 | 低。只需写一个新的 `KnowledgeVectorPort` 适配器（原实现见 `backend/app/milvus.py`，共 6.8 KB，接口很窄） | ⭐⭐⭐⭐⭐ |
| Milvus | Qdrant（单容器，Rust） | 单容器 ~100 MB，比 Milvus 轻 20 倍，API 更简单 | 低 | ⭐⭐⭐⭐ |
| Milvus | Chroma / FAISS（嵌入式） | 零容器，纯进程内 | 低，但并发/持久化要自己兜 | ⭐⭐⭐（仅个人 Demo） |
| MinIO（S3 网关） | 本地文件系统 + Python `pathlib` | 省 768 MB 容器 | 低。`object_store.py` 只有 6.5 KB，写个 `LocalObjectStore` 即可 | ⭐⭐⭐⭐ |
| MinIO | 云厂商 OSS/COS（正式环境） | 无运维、有生命周期规则 | 中 | ⭐⭐⭐⭐ |
| Redis 7 | 保留（推荐）/ 换 Valkey、Dragonfly | 内存占用略降 | 极低（协议兼容） | ⭐⭐⭐ |
| Redis（全部用途） | 按用途拆分：缓存→进程内 LRU；额度→PG 计数表 | 彻底去掉一个容器 | **中高**。Redis 不只做缓存：检索缓存、聊天额度（Lua 脚本）、MetaStudio 会话、视觉票据/额度都在用 | ⭐⭐（先删功能再考虑） |

> **最有价值的一条**：`Milvus → pgvector`。以 `application/ports.py` 的 `KnowledgeVectorPort` 为界，写一个新适配器 + 一个 `CREATE EXTENSION vector` 迁移，就能删掉两个容器和 3 GB 内存预算，代码改动集中在 1 个文件。

### 3.2 服务层

| 原技术 | 更轻的替代 | 收益 | 迁移成本 | 推荐度 |
| --- | --- | --- | --- | --- |
| **langchain-core + langchain-deepseek** | **httpx 直连 DeepSeek 的 OpenAI 兼容接口** | 去掉 2 个依赖。实际只用到 `ChatDeepSeek`，`llm.py` 只有 8.3 KB，流式就是 SSE 逐行解析 | 低 | ⭐⭐⭐⭐⭐ |
| MCP Server（独立 Node） | FastAPI 内部工具调用 | 少一个容器、一个 Dockerfile、一个 package-lock | 低 | ⭐⭐⭐⭐ |
| Mock 政务 API（独立 Node） | JSON fixture 直接进 FastAPI | 少一个容器 | 低 | ⭐⭐⭐⭐ |
| FastAPI 0.141 | 保留 | — | — | ⭐⭐⭐⭐⭐（不建议换） |
| SQLAlchemy 2.0 async | 保留 / SQLModel（同一作者，更简洁） | SQLModel 更少样板 | 中（`records.py` 39 KB） | ⭐⭐⭐ |
| Alembic | 保留 | — | — | ⭐⭐⭐⭐⭐ |

> **`langchain-deepseek` → `httpx`** 是性价比第二高的一条：删掉依赖不等于删掉能力，DeepSeek 的流式就是标准 SSE。

### 3.3 AI / 检索层

| 原技术 | 更轻的替代 | 说明 |
| --- | --- | --- |
| 本地知识 256 维 hash embedding（`knowledge.py`） | 换成任意 embedding API（BGE / text-embedding-v4） | 现在是**确定性哈希**，不是真实语义向量。检索质量很差，但它零成本、可离线。做真产品必须换 |
| 双路 RAG + RRF 融合 | 单路 pgvector + 真实 embedding | 删掉 `composite_retriever.py` 的复杂度 |
| Qwen VL 抽取 DOCX 字段 | 纯模板填充，不用模型 | 省掉一条付费链路 + LibreOffice 容器 |
| MetaStudio 的 LLM 回调 | 删除 | — |

### 3.4 移动端

| 原技术 | 替代 | 收益 | 成本 | 推荐度 |
| --- | --- | --- | --- | --- |
| Android Java | **Kotlin** | 代码量降 30–40%，空安全，协程替代 ViewModel 回调 | 中（可逐文件迁移，Kotlin/Java 可共存） | ⭐⭐⭐⭐ |
| WebView + 本地 HTML/JS 门户（`portal-app-v2.js` 69 KB + `index.html` 26 KB） | **保留** | 这是该项目最聪明的设计：UI 改版不用重编 APK | — | ⭐⭐⭐⭐⭐（别动） |
| WebView SPA | Flutter / React Native | 跨平台 | **极高**。要重写全部原生边界、Keystore、SAF、Bridge | ⭐（不建议） |
| 手写 OkHttp 网关 + 手写合同类 | OpenAPI 生成客户端（`openapi-generator`） | 合同改动自动同步 | 中 | ⭐⭐⭐ |
| AG Connect / HMS 三件套 | 删除，用系统 Intent 调高德/百度地图 | 依赖减半 | 低 | ⭐⭐⭐⭐ |

### 3.5 基础设施 / 运维

| 原技术 | 替代 | 收益 | 推荐度 |
| --- | --- | --- | --- |
| Nginx + lego（4 套配置模板 + 证书脚本 + systemd timer） | **Caddy** | 一个 Caddyfile 自动 HTTPS，配置量降 95% | ⭐⭐⭐⭐⭐ |
| 4 份 compose 文件 + 27 个部署脚本 | 1 份 `compose.yaml` + 1 份 `compose.prod.yaml` | 认知负担大降 | ⭐⭐⭐⭐⭐ |
| PowerShell 脚本（Windows 专属） | 改成 Python 或 Makefile / Task | 跨平台 | ⭐⭐⭐ |
| pytest 手写 | testcontainers-python | 测试用真实 PG/Redis，不用 mock | ⭐⭐⭐ |
| 无 CI | GitHub Actions | 自动跑 lint + test + build | ⭐⭐⭐⭐ |
| 无监控 | OpenTelemetry + Prometheus | 可观测性 | ⭐⭐⭐（正式产品再加） |

---

## 四、必须保留的核心骨架（不要动）

这几块是这个项目的**真正价值**，删了就不是"改造"而是"重写"了。

| 骨骼 | 位置 | 为什么保留 |
| --- | --- | --- |
| BCE 四层分层 | `domain/` `application/` `boundaries/` `infrastructure/` | 领域层是纯 Python，不依赖任何框架。这是整个项目最值钱的设计 |
| 端口抽象 | `application/ports.py` | 所有外部依赖（DB/向量/对象存储/MCP/LLM）都从这里替换 |
| 组合根 | `infrastructure/runtime.py` | 唯一注入点，替换实现只改这一个文件 |
| 业务策略 | `domain/policies.py` | 状态机、资格评估、材料规则、命令授权、溯源策略，纯 Python 可独立测试 |
| 幂等 + 乐观锁 | `IdempotencyCoordinator` + `Application.version` | 并发的正确做法，别退化成"先查后写" |
| 审计只追加 | `ApplicationEvent` / `BusinessAuditEvent` | 政务/金融/医疗类系统的通用要求，改了将来要还债 |
| 溯源强制策略 | `GroundedAnswerPolicy` | "没有来源就不调模型"，这是 AI 产品的信任基础 |
| Android 安全边界 | 窄 JS Bridge + Keystore + 私有 bucket + 无公共直链 | 移动端 + AI 最容易出事的地方，它已经做对了 |
| 三类型值分离 | `Record`（ORM）/ 实体（dataclass）/ DTO | 避免数据库懒加载泄漏到业务规则 |

---

## 五、需要重构的大文件（无论是否换技术栈）

| 文件 | 行数 | 建议拆法 |
| --- | --- | --- |
| `backend/app/infrastructure/repositories.py` | **3,589** | 按聚合拆：`identity_repo` / `catalog_repo` / `application_repo` / `consultation_repo` / `operations_repo` / `knowledge_repo` |
| `backend/app/application/coordinators.py` | **2,698** | 按用例域拆（项目里已经声明了 14 个协调者类，只是塞在一个文件）：`auth.py` / `catalog.py` / `application.py` / `review.py` / `consultation.py` / `operations.py` / `admin.py` |
| `backend/app/infrastructure/vision.py` | 1,519 | 删（若不用视觉） |
| `backend/app/boundaries/http.py` | 685 | 已经按 `APIRouter(prefix=...)` 分了 6 组，物理拆成 6 个文件即可 |
| `android/.../assets/portal-app-v2.js` | 1,227 | 按视图拆 ES module；现在是一个大文件 |

---

## 六、可以新增的模块

| 新增项 | 为什么需要 | 轻量做法 |
| --- | --- | --- |
| **多租户 / 组织隔离** | 现在只有 `Department` 维度的粗粒度隔离，做 SaaS 必须加 `tenant_id` | 在 PG 上加一列 + 行级安全（RLS） |
| **CI/CD** | 现在只有本地 PowerShell 脚本 | GitHub Actions：3 个 job（backend pytest / node check / android assemble） |
| **OpenAPI 客户端生成** | Android 现在手写合同类，容易漂移 | `openapi-generator` 生成 Kotlin/Java 客户端 |
| **文件病毒扫描** | 项目文档自己承认"演示环境无病毒扫描"，正式产品必须有 | ClamAV 容器 + 上传后异步扫描状态 |
| **真实身份/短信** | 现在是 `DEMO_SMS_CODE=000000` | 接运营商或云厂商短信服务 |
| **可观测性** | 现在只有结构化日志 | OpenTelemetry SDK + Prometheus + Grafana 或直接用云厂商 |
| **速率限制下沉** | 现在依赖 Nginx 配置 | SlowAPI 或网关层，换 Caddy 后一起处理 |
| **配置中心** | 现在 60+ 个环境变量 | 小规模用 `.env` 就够；规模化再上 |
| **PG 行级备份/时间点恢复** | 现在靠 `backup.sh` 全量 dump | WAL 归档 + PITR |

---

## 七、三套推荐组合

### 方案 A：个人作品 / 轻量 Demo —— 目标约 1.5 GB 内存、4 个容器

```
保留：FastAPI + PostgreSQL + Redis + MinIO(或本地盘)
删除：MetaStudio、视觉、团队RAG、DOCX Worker、HMS 导航、deploy/、MCP、Mock API
替换：Milvus → pgvector     langchain-deepseek → httpx
      Nginx+lego → Caddy     4 份 compose → 1 份
保留：BCE 分层、幂等、审计、溯源策略、Android 安全边界
```

工作量：约 1–2 周。适合作为作品集 / 毕设 / 内部演示。

### 方案 B：真实小规模产品 —— 目标约 4 GB 内存、7 个容器

```
在方案 A 基础上：
保留：MCP（作为只读边界）、独立 Worker（若确需文档生成）
替换：本地 hash embedding → 真实 embedding API + pgvector
      手写合同 → OpenAPI 生成客户端
      本地文件 → 云 OSS + 生命周期规则
新增：多租户、ClamAV 病毒扫描、CI/CD、真实短信、可观测性
保留：云端部署脚本（但改用 Caddy + 云厂商证书）
```

工作量：约 1–2 个月。

### 方案 C：保留政务演示原貌，只做瘦身 —— 一周内可完成

```
第 1 步：删 deploy/ + compose.cloud/candidate/metastudio（3 份编排）
第 2 步：删 MetaStudio（含 59.9 MB 资产）
第 3 步：删视觉链路
第 4 步：删团队 RAG
第 5 步：删 DOCX Worker
结果：约 30 个文件、60 MB 资产、2 个容器的净减少
```

---

## 八、分阶段推进路线（推荐按此顺序）

### 阶段 0：决策（1 天）

先回答 4 个问题，其余全部可以由答案推导：

1. 项目定位是**作品集**、**内部工具**，还是**要上线的产品**？
2. 业务领域还是**政务**吗？如果换领域，`domain/policies.py` 的资格规则 DSL（`eq/ne/in/exists/lt/lte/...`）和 6 个事项种子是全部要换的。
3. 目标平台只有 **Android** 吗？
4. 有没有**华为云**预算和账号？没有就直接删 MetaStudio + HMS。

### 阶段 1：减法（2–5 天）

按第二节①→⑧的顺序删除，**每删一个就跑一次 `scripts/test-all.ps1 -SkipAndroid`**。这一步不用改任何技术栈，纯删。

关键顺序约束：**先 MetaStudio 后视觉**（两者在 Android 端耦合），其余互不依赖。

### 阶段 2：技术替换（1–2 周）

按收益排序做：

1. `Milvus → pgvector`（写新适配器实现 `KnowledgeVectorPort`）
2. `langchain-deepseek → httpx`（重写 `llm.py`，8.3 KB）
3. `Nginx+lego → Caddy`；compose 合并成 2 份
4. `MinIO → 本地盘` 或云 OSS（写新适配器实现对象存储端口）
5. `Mock API → JSON fixture`；MCP 决定去留

每步做完跑一次 `smoke.ps1` 验证业务闭环没断。

### 阶段 3：重构（1–2 周）

拆 `repositories.py`（3,589 行）和 `coordinators.py`（2,698 行）。**在拆之前先补测试覆盖率**，否则会很痛。

### 阶段 4：加你自己的东西

改领域模型、改 UI、加多租户、加 CI/CD、接真实服务。

---

## 九、风险与坑

| 坑 | 说明 | 规避 |
| --- | --- | --- |
| **迁移链只前进** | `migrate.ps1` 只做 `upgrade head`，脚本永不 downgrade。删功能时如果顺手删了迁移文件，老数据库会直接崩 | 删功能时保留迁移文件，只删业务代码；或彻底重建数据库 |
| **`/health/ready` 会因缺依赖而 503** | `main.py` 对全部 check 做 all-ok 判定；删掉 Milvus 等组件后若不改 readiness 聚合实现，接口会**永久 503**。部署脚本还会按"本地 7 项 / 云端 8 项"做精确断言 | 删每个后端依赖时同步改 `services.py` 的 readiness 聚合；云端还要改 `deploy/scripts/health-check.sh --expect-rag` 的期望值 |
| **MetaStudio 资产不可改** | 构建脚本逐文件校验 11 个 SHA-256。改一个字节就构建失败 | 要么整删要么整留 |
| **Android 合同两端同步** | `PortalContract`/`PortalEntities` 与 FastAPI Schema 是一对一关系 | 改接口时两边一起改，靠 `NativeGatewaysTest` 兜底 |
| **删 MetaStudio 前先删视觉** | 视觉的进入入口在数字人页 | 顺序别反 |
| **`.env` 已生成过** | `init-env.ps1` 只补缺项，不改已有值（保护已有数据卷密码） | 大改配置时手动重建 `.env` 并重建数据卷 |
| **两个 Node 服务的锁文件** | `package-lock.json` 各 30–44 KB | 删服务时把 lock 一起删，别留孤儿 |
| **DashScope 是付费的** | embedding 导入 15,858 条 + Qwen VL 抽取都是付费调用 | 改造期间保持 `*_PROVIDER=mock` |

---

## 十、一句话总结

这个项目**架构是好架构，功能是多余的功能**。

最该做的三件事，按顺序：**删掉华为数字人（省 95% 体积）、把 Milvus 换成 pgvector（省 3 GB 内存）、把 langchain 换成 httpx（少 2 个依赖）**。做完这三件，它就从"8 GB 内存、9 个容器、60 MB 仓库的政务演示"变成"2 GB 内存、4 个容器、3 MB 仓库的干净骨架"，而 BCE 分层、幂等、审计、溯源、Android 安全边界这些真正值钱的东西一个都没丢。
