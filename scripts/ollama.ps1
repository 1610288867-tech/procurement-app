<#
.SYNOPSIS
    本地大模型的启停（AI 模块增强阶段用）。

.DESCRIPTION
    Ollama 提供 OpenAI 兼容的接口（`/v1/chat/completions`），所以后端的
    `OpenAiCompatibleChat` **一行代码都不用改** —— 只要把配置指到
    `http://127.0.0.1:11434/v1` 即可。

    ============================ 为什么要有这个脚本 ============================
    模型目录要固定在**项目内** `.ollama\models`（可用 `-ModelsDir` 改），
    而不是散在用户目录里 —— 这些模型是本项目专用的，跟着项目才好备份与交接。
    Ollama 靠 **环境变量 `OLLAMA_MODELS`** 决定模型放哪，而这个变量有几种设法，
    本脚本刻意选了**最不污染环境**的一种：

        ❌ `[Environment]::SetEnvironmentVariable(..., "User")`
           —— 会写进用户环境，影响这台机器上所有项目
        ❌ 注册表 / 系统设置
           —— 同上，而且别人接手时查不到它从哪来
        ✅ **只在本脚本进程里设**（`$env:OLLAMA_MODELS = ...`）
           —— 作用域止于脚本起的那个 `ollama serve`

    ⚠️ **一个必须知道的后果**：从**开始菜单/托盘**启动的 Ollama 是另一个进程，
    它读的是用户级环境变量 —— 所以那种启动方式**不会**用项目内的模型目录。
    想让模型落在项目里，就用本脚本启动（`.\scripts\ollama.ps1 -Serve`）。
    两种方式**不要同时跑**（会抢 11434 端口）。

.PARAMETER Serve
    启动服务（前台阻塞，Ctrl+C 停止）。

.PARAMETER Status
    打印环境与已安装模型，不做任何修改（默认行为）。

.PARAMETER Pull
    拉取模型。默认拉配置里那两个：对话模型 + embedding 模型。

.EXAMPLE
    .\scripts\ollama.ps1                 # 看状态
    .\scripts\ollama.ps1 -Serve          # 启动（模型落在项目内 .ollama\models）
    .\scripts\ollama.ps1 -Pull           # 拉默认的两个模型
    .\scripts\ollama.ps1 -Pull -Model qwen2.5:7b
    .\scripts\ollama.ps1 -ModelsDir E:\models
#>
[CmdletBinding(DefaultParameterSetName = 'Status')]
param(
    [Parameter(ParameterSetName = 'Serve')]
    [switch]$Serve,

    [Parameter(ParameterSetName = 'Status')]
    [switch]$Status,

    [Parameter(ParameterSetName = 'Pull')]
    [switch]$Pull,

    #: 单独指定要拉的模型（不传就拉下面两个默认的）
    [string]$Model,

    #: 模型目录。默认 `D:\ollama\models`（本机实际使用的位置）。
    [string]$ModelsDir
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
#: 模型目录默认放 **项目内** `.ollama\models`。
#: 理由：这些模型是**本项目专用的**（对话模型给 Agent 循环与评测生成、
#: embedding 给制度检索），放进项目的规范目录才好一起备份、也让人一眼知道
#: "这个项目依赖哪些模型"。
#: 它已在 `.gitignore` 里排除 —— 模型是**可再生的**（`ollama pull` 重新拉一次即可），
#: 而且 bge-m3 就 1.1 GB，进版本库没有意义。
#: 这里仍然只设**进程作用域**的环境变量，不碰用户环境 —— 见下面那段说明。
#: 想放别处就传 `-ModelsDir`。
$modelsDir = if ($ModelsDir) { $ModelsDir } else { Join-Path $root '.ollama\models' }

# ---- 只在本脚本进程内生效：作用域止于本脚本起的 ollama serve ----
$env:OLLAMA_MODELS = $modelsDir

#: 默认要拉的模型。
#:
#: ============================ embedding 为什么默认用小的那个 ============================
#: `bge-m3` 效果更好（1024 维），但它的模型层有 **1104 MB**，
#: 而本机到 `registry.ollama.ai` 实测只有 **50–210 KB/s**。
#: 实测踩到的坑：拉到 1104 MB 时中断，留下一个**永远无法完成的 `-partial` 文件**
#: （大小正好等于目标大小，但 SHA256 不匹配，Ollama 不会认它，白占 1.1 GB，
#: 续传也不会补完 —— 只能删掉重来）。所以默认不拉它。
#:
#: `quentinz/bge-small-zh-v1.5` 只有 **48 MB**（约 4 分钟拉完），
#: 而且是**中文专用**的检索模型 —— 对"制度问答"这个场景比通用多语言的 bge-m3 更贴。
#: 实测效果（27 条 Golden QA）：MRR 0.879 → **1.000**、能答却拒答 3 条 → **0 条**、
#: 实际回答成功率 0.864 → **1.000**。详见 `docs/AI-评测baseline.md`。
#:
#: ⚠️ **换成 bge-m3 不只是 pull 一个模型**：它是 **1024 维**，而
#: `knowledge_chunks.embedding` 现在是 `vector(512)` —— 列维度要一起迁
#: （写一条迁移改 `vector(1024)` 并重建 HNSW 索引），
#: 而且 `domain/embedding.py` 的 `MODEL_DIMENSIONS` 里要登记它的维度。
#: 只 pull 不改维度的话，插入时会在数据库层报维度不符。
$defaultModels = @('qwen2.5:7b', 'quentinz/bge-small-zh-v1.5')

function Get-OllamaExe {
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    # 安装器默认落点（OllamaSetup.exe 是 Inno Setup 打包的，**不支持自定义安装目录**）
    $fallback = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if (Test-Path -LiteralPath $fallback) { return $fallback }
    return $null
}

$ollama = Get-OllamaExe

Write-Host '=== Ollama（本地大模型）==='
Write-Host "模型目录   : $modelsDir"
Write-Host "进程内变量 : OLLAMA_MODELS=$env:OLLAMA_MODELS"
$userLevel = [Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'User')
Write-Host ("用户级变量 : " + $(if ($userLevel) { "$userLevel  ← ⚠️ 它会让从开始菜单启动的 Ollama 也读这个目录" } else { '（未设置 —— 符合本项目口径）' }))
Write-Host ("可执行文件 : " + $(if ($ollama) { $ollama } else { '未找到（先装 Ollama，见 README）' }))
Write-Host ''

if (-not $ollama) {
    Write-Host '未安装 Ollama。安装方式（本机实测 winget 的下载会被墙，用直连或镜像）：'
    Write-Host '  1) 直连（慢）：curl.exe -L -o OllamaSetup.exe https://github.com/ollama/ollama/releases/latest/download/OllamaSetup.exe'
    Write-Host '  2) 镜像（快些）：curl.exe -L -C - -o OllamaSetup.exe https://gh-proxy.com/https://github.com/ollama/ollama/releases/latest/download/OllamaSetup.exe'
    Write-Host '  然后直接运行 OllamaSetup.exe（安装路径不可改，模型目录由本脚本控制）。'
    exit 1
}

# 服务是否在跑（无论它是谁起的）
$running = $false
try {
    $null = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3
    $running = $true
} catch {
    $running = $false
}

if ($Serve) {
    if ($running) {
        Write-Host '⚠️ 11434 端口已有 Ollama 在跑 —— 端口只能被一个进程占。'
        Write-Host '   如果那是从开始菜单启动的，它用的是**用户级**模型目录（不是项目内这个）。'
        Write-Host '   想改用项目内目录：先在托盘退出 Ollama，再跑本命令。'
        exit 1
    }
    New-Item -ItemType Directory -Force -Path $modelsDir | Out-Null
    Write-Host "启动 ollama serve（模型落到 $modelsDir）… Ctrl+C 停止"
    & $ollama serve
    exit $LASTEXITCODE
}

if ($Pull) {
    if (-not $running) {
        Write-Host '服务没在跑，先起来：另开一个窗口跑 .\scripts\ollama.ps1 -Serve'
        exit 1
    }
    $targets = if ($Model) { @($Model) } else { $defaultModels }
    foreach ($name in $targets) {
        Write-Host "拉取 $name …（7B 量化约 4–5 GB，embedding 约 1–2 GB）"
        & $ollama pull $name
        if ($LASTEXITCODE -ne 0) { throw "拉取 $name 失败" }
    }
    Write-Host '完成。列出已安装模型：'
    & $ollama list
    exit 0
}

# ---- 默认：状态 ----
if (-not $running) {
    Write-Host '服务未运行。启动：.\scripts\ollama.ps1 -Serve'
    exit 0
}
Write-Host '服务在跑。已安装模型：'
& $ollama list
Write-Host ''
Write-Host '=== 后端要用的配置（写在 backend\.env，或直接改 config.py 的默认值）==='
Write-Host '【embedding —— 制度检索用，有默认值，通常不用改】'
Write-Host '  KNOWLEDGE_EMBEDDING_PROVIDER=ollama                       # 或 local（离线哈希）'
Write-Host '  KNOWLEDGE_EMBEDDING_BASE_URL=http://127.0.0.1:11434       # 不带 /v1（走 /api/embed）'
Write-Host '  KNOWLEDGE_EMBEDDING_MODEL=quentinz/bge-small-zh-v1.5'
Write-Host ''
Write-Host '【对话模型 —— 可选】三个都不配时走"本地抽取式兜底"（摘原文、标引用，不调模型）'
Write-Host '  KNOWLEDGE_LLM_BASE_URL=http://127.0.0.1:11434/v1'
Write-Host '  KNOWLEDGE_LLM_API_KEY=ollama      # Ollama 不校验，随便填'
Write-Host '  KNOWLEDGE_LLM_MODEL=qwen2.5:7b'
Write-Host ''
Write-Host '⚠️ 换 embedding 模型会换掉整个向量空间（旧文档按模型名过滤、将检索不到），'
Write-Host '   需要重新上传制度文档；维度不同的模型还要改列定义（见本脚本上方说明）。'
