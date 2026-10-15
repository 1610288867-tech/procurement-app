<#
.SYNOPSIS
    从本地 GGUF 导入一个对话模型（必要时先合并分片）。

.DESCRIPTION
    ============================ 为什么需要它 ============================
    本机到 `registry.ollama.ai` 拉模型实测只有 **50–210 KB/s**，
    而 `ollama pull qwen2.5:7b-instruct` 试过 **4 次**全部失败：
    blob 曾到 100%（4,683,073,952 字节，与清单逐字节一致），
    但 Ollama 卡在"校验 + 登记 manifest"这一步，重试 4 次、每次 13–25 分钟零进展，
    最后一次失败后 Ollama 把那个 4.4 GB 的 blob 与 `-partial` 一起清掉了。
    **结论：那条路本机走不通**（见 RISKS 第五节）。

    所以换这条路：从 **ModelScope** 下 GGUF（实测约 850 KB/s），
    再让 Ollama 直接读本地文件 —— 这正是当初 3B 模型装进来的方式。

    ============================ 它会做四件事 ============================
    1. **合并分片**（给了多个 GGUF 时）：ModelScope 官方仓库的 q4_k_m 是
       `-00001-of-00002` + `-00002-of-00002`，而 **Ollama 不支持多文件 GGUF 导入**
       （[ollama/ollama#5245](https://github.com/ollama/ollama/issues/5245)，
       本机 Ollama 0.40.3 安装目录里也没有 `llama-gguf-split`）。
       合并交给 `backend\scripts\merge_gguf.py`（纯 stdlib，带分片完整性校验）。
    2. **写 Modelfile** 到 `.ollama\modelfiles\<名字>.Modelfile`（进版本库，
       下次换机器照着复现；里面的 `FROM` 是本机绝对路径，见下面 ⚠️）。
    3. **确保 Ollama 在跑**（没跑就报错让你先 `.\scripts\ollama.ps1 -Serve`，
       不替你起常驻进程）。
    4. **`ollama create`** 导入，并核对导入结果的体积与参数量。

.PARAMETER Gguf
    一个或多个 GGUF 路径。多个 = 按给定顺序合并（顺序不能错）。

.PARAMETER Name
    导入后的模型名（默认：从文件名推，去掉 `-0000N-of-0000M` 与扩展名）。

.PARAMETER ModelfilePath
    只写 Modelfile、**不导入**（用来先看一眼它长什么样）。

.EXAMPLE
    # 分片：先合并再导入
    .\scripts\导入GGUF.ps1 -Gguf D:\develop\models\qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf,
                               D:\develop\models\qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf

.EXAMPLE
    # 单文件
    .\scripts\导入GGUF.ps1 -Gguf D:\develop\models\some-model-q4_k_m.gguf -Name some-model

.NOTES
    只读 GGUF 文件；在 `.ollama\modelfiles\` 写一个 Modelfile、在 Ollama 模型目录写模型。
    **不修改 backend\.env** —— 换用哪个模型是配置决定，要你自己改（脚本会打印该改哪一行）。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]]$Gguf,

    [string]$Name,

    [switch]$ModelfilePath
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$modelsDir = Join-Path $root '.ollama\models'
$modelfileDir = Join-Path $root '.ollama\modelfiles'
#: 与 3B 那份保持一致：`num_ctx` **必须显式给** —— Ollama 默认 2048，
#: 而本项目的问答 Prompt 带着检索到的制度片段（5 条 × 600 字）与 10 份种子制度，
#: 2048 会被截断。表现是"模型看不到后面的来源"，而它**不会报错**。
$numCtx = 8192

# ---- 解析输入路径（去重、验存在、按给定顺序保留）----
$files = @()
foreach ($item in $Gguf) {
    foreach ($piece in ($item -split ',')) {
        $trimmed = $piece.Trim().Trim('"')
        if (-not $trimmed) { continue }
        if (-not (Test-Path -LiteralPath $trimmed)) { throw "找不到 GGUF：$trimmed" }
        $files += (Resolve-Path -LiteralPath $trimmed).Path
    }
}
if ($files.Count -eq 0) { throw "没有给定任何 GGUF 路径" }

# ---- 推模型名 ----
if (-not $Name) {
    $leaf = [System.IO.Path]::GetFileNameWithoutExtension($files[0])
    $Name = $leaf -replace '-\d{5}-of-\d{5}$', ''
    Write-Host "未指定 -Name，从文件名推出：$Name"
}

Write-Host '=== 导入本地 GGUF ==='
Write-Host "GGUF 数量 : $($files.Count)"
foreach ($f in $files) { Write-Host ("  · {0}（{1:N2} GB）" -f $f, ((Get-Item -LiteralPath $f).Length / 1GB)) }
Write-Host "模型名     : $Name"
Write-Host "模型目录   : $modelsDir"
Write-Host ''

# ---- 1) 需要时先合并 ----
if ($files.Count -gt 1) {
    $merged = Join-Path (Split-Path -Parent $files[0]) "$Name.gguf"
    if (Test-Path -LiteralPath $merged) {
        Write-Host "已存在合并结果，跳过合并：$merged"
    } else {
        Write-Host '--- 合并分片（Ollama 不支持多文件 GGUF，见脚本头说明）---'
        $python = Join-Path $root 'backend\.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $python)) { throw "找不到虚拟环境：$python" }
        $mergeScript = Join-Path $root 'backend\scripts\merge_gguf.py'
        & $python $mergeScript $merged @files
        if ($LASTEXITCODE -ne 0) { throw "合并失败（退出码 $LASTEXITCODE）" }
    }
    $source = $merged
} else {
    $source = $files[0]
}

# ---- 2) 写 Modelfile ----
if (-not (Test-Path -LiteralPath $modelfileDir)) {
    New-Item -ItemType Directory -Path $modelfileDir -Force | Out-Null
}
$modelfile = Join-Path $modelfileDir "$Name.Modelfile"
$content = @"
# 从本地 GGUF 导入 $Name（由 scripts\导入GGUF.ps1 生成）。
#
# ⚠️ 这个 FROM 是**本机绝对路径** —— 它进版本库是为了让人看懂
# "这个模型是怎么装进来的"（以及 num_ctx 为什么是 8192），
# 换机器时这一行必须改成那台机器上的实际路径。
#
# ⚠️ `num_ctx` 必须显式给：Ollama 默认 2048，而本项目的问答 Prompt
# 带着检索到的制度片段（5 条 × 600 字），2048 会被截断 ——
# 表现是"模型看不到后面的来源"，而它不会报错。
FROM $($source -replace '\\', '\')

PARAMETER num_ctx $numCtx
PARAMETER temperature 0.2
PARAMETER top_p 0.9
"@
Set-Content -LiteralPath $modelfile -Value $content -Encoding utf8NoBOM
Write-Host "已写 Modelfile：$modelfile"

if ($ModelfilePath) {
    Write-Host ''
    Write-Host '（-ModelfilePath：只写文件、不导入）'
    Write-Host "看内容：Get-Content '$modelfile'"
    return
}
Write-Host ''

# ---- 3) Ollama 必须在跑 ----
$running = $false
try {
    Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 5 | Out-Null
    $running = $true
} catch { $running = $false }
if (-not $running) {
    throw "Ollama 没在跑。先开一个窗口常驻：.\scripts\ollama.ps1 -Serve"
}

$ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
if (-not $ollama) {
    $fallback = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if (Test-Path -LiteralPath $fallback) { $ollama = $fallback }
}
if (-not $ollama) { throw "找不到 ollama 可执行文件" }

# ---- 4) 导入 ----
# ⚠️ `OLLAMA_MODELS` 只在**本次进程里**设：模型要落到项目目录，
# 而**不碰用户环境变量**（与 ollama.ps1 同一条口径 —— 那会让从开始菜单
# 启动的 Ollama 也读这个目录，换项目时互相看不到模型）。
$env:OLLAMA_MODELS = $modelsDir
Write-Host '--- ollama create ---'
& $ollama create $Name -f $modelfile
if ($LASTEXITCODE -ne 0) { throw "ollama create 失败（退出码 $LASTEXITCODE）" }

Write-Host ''
Write-Host '--- 核对 ---'
& $ollama list

Write-Host ''
Write-Host '下一步（**要让后端用上它，必须自己改配置**）：'
Write-Host "  backend\.env 里把这一行改成：KNOWLEDGE_LLM_MODEL=$Name"
Write-Host '  ⚠️ 改完要**重启后端** —— 运行中的实例不会重读 .env。'
Write-Host ''
Write-Host '验证（两件都值得做）：'
Write-Host "  1) 工具调用：问「我提过哪些采购申请？」，看它是否复述单号与金额"
Write-Host "  2) 端到端：.\scripts\回归验收.ps1"
