<#
  回归验收入口（M8）—— 一条命令把"这个项目现在到底行不行"跑一遍。

  ============================ 它跑什么（固定，不接受任何开关）============================
      1) 三条分层自查          —— 应无任何输出
      2) 后端全量测试          —— pytest -m ""（unit + integration，**不跳集成档**）
      3) 后端静态检查          —— ruff（app / tests / scripts）+ mypy
      4) 前端三件              —— tsc --noEmit / vitest run / npm run build
      5) 起服务（若没在跑）     —— 复用 scripts\start.ps1，等就绪
      6) 真实服务冒烟          —— backend\scripts\m6_smoke.py（对着 8000 打 HTTP，跑完自清理）
      7) 静默失败与接缝探针     —— backend\scripts\regression_probes.py（三条"不会报错只会算错"
                                  的约束 + 四条跨阶段接缝，跑完自清理）
      8) 浏览器自检            —— scripts\浏览器自检.mjs（无窗口 Edge + CDP，17 项）
      9) 知识库实测（M11）      —— scripts\m11-知识库实测.mjs（自起 Edge:9333，6 项）
     10) 窄屏自检（375×812）    —— scripts\窄屏自检.mjs（自起 Edge:9334，20 项）
     11) 知识库评测质量门禁     —— backend\scripts\knowledge_eval.py --gate
                                   （27 条 Golden QA，比 Recall/MRR/回答率/拒答率硬门槛；
                                     embedding 走本机 Ollama，见下方说明）
     12) 清理与残留回查        —— testdata_cleanup.py --scope all（残留非 0 就失败）

  > **不在这个入口里的**：`scripts\m6-页面实测.mjs`（51 项，含 M10 全链路）。
  > 它的数据准备较长（建品类 + 预算 + 走完整条审批链），而且**需要外部已经开好的
  > 9222 端口上的 Edge**，与这里"自起自收"的生命周期不一样 —— 单独跑更合适，见 README。
  >
  > 窄屏自检原先也在这里之外（它同样要自己设视口），2026-10-06 修掉它"跑完不退出"
  > 的毛病之后接了进来：自起 Edge、9.5 秒跑完、退出码正确。

  **为什么固定写死、不给 `-OnlyXxx` 之类的开关**：一有开关，就一定会出现
  "这次时间紧、少跑一项"，回归也就失去意义（实施计划 M8 的常见坑里写着这一条）。

  **退出码**：0 = 全过；非 0 = 第几步失败的编号（失败时把**原始输出**整段打出来）。

  跑法（仓库根目录）：

      .\scripts\回归验收.ps1
#>

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $root "backend"
$frontendDir = Join-Path $root "frontend"
$python = Join-Path $backendDir ".venv\Scripts\python.exe"

$script:stepNo = 0
$script:summary = @()

function Write-Head([string]$title) {
  Write-Host ""
  Write-Host ("=" * 72)
  Write-Host ("[{0}] {1}" -f ++$script:stepNo, $title)
  Write-Host ("=" * 72)
}

# 一步一件事：跑、计时、成功只留一行摘要，失败把原始输出整段打出来并退出。
#
# ⚠️ **失败判定只能靠脚本块内部 `throw`，不能靠函数里读 `$LASTEXITCODE`。**
# 实测踩过：`& $Body` 里的外部命令是在**子作用域**里跑的，它写的是子作用域的
# `$LASTEXITCODE`；函数里那句 `$LASTEXITCODE = 0` 恰好又在本作用域建了一个副本，
# 于是 `if ($LASTEXITCODE -ne 0)` 永远比较的是 0 —— **测试挂了也判成通过**。
# （这就是"1 failed, 221 passed"却被标成 ✓ 的原因。）
# 所以每个步骤的脚本块在外部命令之后都要自己检查一次退出码并 throw。
function Invoke-Step {
  param(
    [string]$Title,
    [scriptblock]$Body,
    [string]$WorkDir = $root
  )

  Write-Head $Title
  $started = Get-Date
  $output = @()
  $failed = $false

  Push-Location $WorkDir
  try {
    $output = & $Body 2>&1 | ForEach-Object { "$_" }
  } catch {
    $failed = $true
    $output += "异常：$($_.Exception.Message)"
  } finally {
    Pop-Location
  }

  $seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)

  if ($failed) {
    Write-Host "✗ 失败：$Title（耗时 ${seconds}s）" -ForegroundColor Red
    Write-Host "---- 原始输出 ----"
    $output | ForEach-Object { Write-Host $_ }
    Write-Host "------------------"
    Write-Host ""
    Write-Host "回归中断在第 $script:stepNo 步。修完重跑：.\scripts\回归验收.ps1"
    exit $script:stepNo
  }

  # 成功：挑最后一行非空输出当摘要（各工具都会把结论放最后一行）
  $tail = ($output | Where-Object { $_.Trim() -ne "" } | Select-Object -Last 1)
  Write-Host ("✓ {0}  —— {1}（{2}s）" -f $Title, $tail, $seconds) -ForegroundColor Green
  $script:summary += [pscustomobject]@{ 步骤 = $Title; 摘要 = $tail; 秒 = $seconds }
}

function Test-Http([string]$url) {
  try {
    $response = Invoke-WebRequest -Uri $url -TimeoutSec 4 -UseBasicParsing
    return $response.StatusCode -eq 200
  } catch {
    return $false
  }
}

# ---------------------------------------------------------------- 前置检查

Write-Host ""
Write-Host "回归验收 —— 企业采购审批系统（M0–M7 全量复跑）"
Write-Host "仓库根：$root"
Write-Host "时间：$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"

if (-not (Test-Path $python)) {
  Write-Host "找不到后端虚拟环境：$python" -ForegroundColor Red
  Write-Host "先按 README 的「初始化与启动」建好 .venv。"
  exit 1
}

# ---------------------------------------------------------------- 1–8 步

Invoke-Step "三条分层自查（应无任何输出）" {
  # ============================ 为什么这段被重写了 ============================
  # 原写法用的是 `-Path "$backendDir\app\**\*.py"`。**实测证明它不递归**：
  # PowerShell 的 `-Path` 只把 `**` 当**一层**通配（不像 bash 的 globstar），
  # 于是它只扫到 `app\*\*.py` 这一深度 ——
  # 而 `app\infrastructure\records\knowledge.py`（深度 3）从来没被扫过。
  #
  # 后果很严重：**自查一直是假通过**，而"0 处命中"这个结论让人以为纪律被守住了。
  # 实测证据：`chat_messages.top_score` 早就写了 `Mapped[float | None]`，
  # 而自查一直报 0 命中。这与 Q70 记录的"回归入口假通过"是**同一类毛病** ——
  # **验证工具本身坏了，而它坏的方式是说一切正常**。
  #
  # 所以这一步现在做三件事：① 真递归扫；② 口径收窄到"ORM 记录里的金额列"；
  # ③ 末尾用探针**自证扫描器真的能抓到违约**。
  $appDir = Join-Path $backendDir "app"
  $hits = @()

  # ① domain 层不许 import 框架（递归扫，因为 domain 下可能再分子目录）
  $hits += Get-ChildItem (Join-Path $appDir "domain") -Recurse -Filter *.py |
    Select-String -Pattern "import (fastapi|sqlalchemy|redis|httpx|minio)"

  # ② 不许有**浮点金额列**。口径收窄到"ORM 记录里的 Mapped[float" ——
  #    原口径 `Float\(` 会把 `float()` 这个**内置函数调用**也算进来，
  #    而领域层正当地用它处理**评分**（如 citation_support 的概率）。
  #    ⚠️ 允许清单是**显式**的：评分类列（不是金额）逐个列出来，
  #    新加一个必须来这里加一行 —— 这比"放宽正则"安全（放宽会漏掉真的金额列）。
  $allowedFloatColumns = @("support_rate", "top_score")
  $floatColumns = Get-ChildItem (Join-Path $appDir "infrastructure\records") -Recurse -Filter *.py |
    Select-String -Pattern "^\s*(\w+):\s*Mapped\[float" |
    Where-Object { $name = ($_.Line -split ":")[0].Trim(); $name -notin $allowedFloatColumns }
  $hits += $floatColumns

  # ③ 仓库层不许 commit（递归扫）
  $hits += Get-ChildItem (Join-Path $appDir "infrastructure\repositories") -Recurse -Filter *.py |
    Select-String -Pattern "\.commit\(\)"

  if ($hits.Count -gt 0) {
    $hits | ForEach-Object { "$($_.Path):$($_.LineNumber): $($_.Line.Trim())" }
    throw "分层纪律被破坏：上面这些命中必须为 0"
  }

  # ④ **自证**：造一个必然违约的探针，确认扫描器真的抓得到。
  #    没有这一步的话，"0 命中"可能只是"什么都没扫到" —— 而上面那条 bug 正是如此。
  $probe = Join-Path $backendDir "app\infrastructure\records\__probe_violation.py"
  try {
    Set-Content -Path $probe -Value "class Probe:`n    amount_fen: Mapped[float] = None`n" -Encoding UTF8
    $probeHits = Get-ChildItem (Join-Path $appDir "infrastructure\records") -Recurse -Filter *.py |
      Select-String -Pattern "^\s*(\w+):\s*Mapped\[float" |
      Where-Object { $name = ($_.Line -split ":")[0].Trim(); $name -notin $allowedFloatColumns }
    if ($probeHits.Count -eq 0) {
      throw "自查扫描器失效：探针违约文件没被抓到（说明这次 0 命中不可信）"
    }
  } finally {
    Remove-Item $probe -Force -ErrorAction SilentlyContinue
  }

  "三条自查合计命中 0 处（扫描器自证通过：探针违约能被抓到）"
}

Invoke-Step "后端全量测试 pytest -m `"`"（unit + integration）" {
  & $python -m pytest -m "" -q
  # 失败时 pytest 退出码非 0。这里必须自己检查：见 Invoke-Step 上面的说明。
  if ($LASTEXITCODE -ne 0) { throw "pytest 退出码 $LASTEXITCODE（上面是完整输出）" }
} $backendDir

Invoke-Step "后端静态检查 ruff + mypy" {
  & $python -m ruff check app tests scripts
  if ($LASTEXITCODE -ne 0) { throw "ruff 有命中" }
  & $python -m mypy app --ignore-missing-imports
  if ($LASTEXITCODE -ne 0) { throw "mypy 有问题" }
} $backendDir

Invoke-Step "前端 tsc --noEmit" {
  npx tsc --noEmit
  if ($LASTEXITCODE -ne 0) { throw "tsc 报错" }
  "类型检查无错误"
} $frontendDir

Invoke-Step "前端 vitest run" {
  npx vitest run
  if ($LASTEXITCODE -ne 0) { throw "vitest 有用例失败" }
} $frontendDir

Invoke-Step "前端 npm run build（生产构建）" {
  npm run build
  if ($LASTEXITCODE -ne 0) { throw "生产构建失败" }
} $frontendDir

Invoke-Step "确保前后端在跑（不在就起，最多等 90 秒）" {
  $backendUp = Test-Http "http://127.0.0.1:8000/health/ready"
  $frontendUp = Test-Http "http://localhost:5173/"
  if ($backendUp -and $frontendUp) { "后端与前端都已在跑"; return }

  "服务没起（后端=$backendUp 前端=$frontendUp），调用 scripts\start.ps1 …"
  & (Join-Path $PSScriptRoot "start.ps1") | ForEach-Object { "$_" }

  $deadline = (Get-Date).AddSeconds(90)
  while ((Get-Date) -lt $deadline) {
    if ((Test-Http "http://127.0.0.1:8000/health/ready") -and (Test-Http "http://localhost:5173/")) {
      "服务已就绪"; return
    }
    Start-Sleep -Seconds 3
  }
  throw "90 秒内没等到服务就绪（后端 $backendUp / 前端 $frontendUp）"
}

Invoke-Step "真实服务冒烟 m6_smoke.py（19 项，跑完自清理）" {
  $env:PYTHONUTF8 = "1"
  & $python "scripts\m6_smoke.py"
  if ($LASTEXITCODE -ne 0) { throw "冒烟有未通过项" }
} $backendDir

Invoke-Step "静默失败与接缝探针 regression_probes.py（21 项，跑完自清理）" {
  $env:PYTHONUTF8 = "1"
  & $python "scripts\regression_probes.py"
  if ($LASTEXITCODE -ne 0) { throw "探针有未通过项" }
} $backendDir

Invoke-Step "浏览器自检 浏览器自检.mjs（17 项，无窗口 Edge）" {
  node "scripts\浏览器自检.mjs"
  if ($LASTEXITCODE -ne 0) { throw "浏览器自检有未通过项" }
}

# M11 的知识库实测：上传制度 → 看切分 → 提问 → 引用卡片 → 拒答 → 会话。
# **它自己也起一个无窗口 Edge（端口 9333）**，所以与上一步不冲突、可以连着跑。
# 放在"清理"之前：它造的制度文档正好由下一步收掉。
Invoke-Step "知识库实测 m11-知识库实测.mjs（6 项，无窗口 Edge）" {
  node "scripts\m11-知识库实测.mjs"
  if ($LASTEXITCODE -ne 0) { throw "知识库实测有未通过项" }
}

# 窄屏（375×812）：验的是"窄屏不塌"——横向不溢出、侧边栏折叠成横条、
# 面包屑/本页导航按断点隐藏。它自起一个 Edge（端口 9334）并设视口，所以能连着跑。
Invoke-Step "窄屏自检 窄屏自检.mjs（20 项，375×812）" {
  node "scripts\窄屏自检.mjs"
  if ($LASTEXITCODE -ne 0) { throw "窄屏自检有未通过项" }
}

# 知识库评测质量门禁（M11 增强）：跑 27 条 Golden QA，比硬门槛。
#
# **为什么这一步重要**：它是"AI 功能好不好"第一次有了**可回归的量化判据**——
# 之前改 prompt、改切分、换 embedding 都只能凭感觉说"好像更好了"。
# 门槛阈值写在 `backend\scripts\knowledge_eval.py` 的 `GATES` 里（含"为什么定得松"的说明）。
#
# ⚠️ **它依赖本机 Ollama**：默认 embedding 是真模型 `quentinz/bge-small-zh-v1.5`，
# 走 `http://127.0.0.1:11434/api/embed`。启动方式：`.\scripts\ollama.ps1 -Serve`。
# 服务不在时这一步会**明确报 embedding 调用失败**，而不是悄悄退回哈希 ——
# 那会让指标看起来正常、实则测的是另一套向量空间（见 `infrastructure/embeddings.py`）。
#
# 想在没有 Ollama 的机器上跑：给这一步加 `--embedding local`，
# 但那时 `grounded_rate` / `refusal_rate` 会掉到哈希的水平（0.864 / 0.60），
# 门槛要跟着放宽 —— 见 `docs/AI-评测baseline.md` 的两套数字对比。
#
# 放在"清理"之前：它造的 `EVAL-` 制度文档正好由下一步收掉。
Invoke-Step "知识库评测质量门禁（27 条 Golden QA，embedding 走 Ollama）" {
  $env:PYTHONUTF8 = "1"
  # `--quiet`：关掉应用的每请求日志。不关的话报告会被几十行 INFO 淹掉，
  # 而失败时回归入口只打尾部输出 —— 那就看不到**到底哪个指标没过门槛**。
  & $python "scripts\knowledge_eval.py" --gate --quiet
  if ($LASTEXITCODE -ne 0) { throw "知识库评测未过质量门禁" }
} $backendDir

Invoke-Step "测试数据清理与残留回查（testdata_cleanup.py --scope all）" {
  $env:PYTHONUTF8 = "1"
  & $python "scripts\testdata_cleanup.py" --scope all
  # 有残留时清理脚本自己返回 1（回查不为 0），这里照样要检查
  if ($LASTEXITCODE -ne 0) { throw "清理后仍有残留" }
} $backendDir

# ---------------------------------------------------------------- 汇总

Write-Host ""
Write-Host ("=" * 72)
Write-Host "回归结果：全部通过"
Write-Host ("=" * 72)
$script:summary | Format-Table -AutoSize | Out-String | Write-Host
Write-Host "退出码 0。上面每一行的摘要都可以单独重跑复核。"
exit 0
