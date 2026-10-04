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
      8) 浏览器自检            —— scripts\浏览器自检.mjs（无窗口 Edge + CDP，12 项）
      9) 清理与残留回查        —— testdata_cleanup.py --scope all（残留非 0 就失败）

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
  $hits = @()
  $hits += Select-String -Path (Join-Path $backendDir "app\domain\*.py") `
    -Pattern "import (fastapi|sqlalchemy|redis|httpx|minio)"
  $hits += Select-String -Path (Join-Path $backendDir "app\**\*.py") `
    -Pattern "Mapped\[float\]|Float\("
  $hits += Select-String -Path (Join-Path $backendDir "app\infrastructure\repositories\*.py") `
    -Pattern "\.commit\(\)"
  if ($hits.Count -gt 0) {
    $hits | ForEach-Object { "$($_.Path):$($_.LineNumber): $($_.Line.Trim())" }
    throw "分层纪律被破坏：上面这些命中必须为 0"
  }
  "三条自查合计命中 0 处"
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

Invoke-Step "静默失败与接缝探针 regression_probes.py（14 项，跑完自清理）" {
  $env:PYTHONUTF8 = "1"
  & $python "scripts\regression_probes.py"
  if ($LASTEXITCODE -ne 0) { throw "探针有未通过项" }
} $backendDir

Invoke-Step "浏览器自检 浏览器自检.mjs（12 项，无窗口 Edge）" {
  node "scripts\浏览器自检.mjs"
  if ($LASTEXITCODE -ne 0) { throw "浏览器自检有未通过项" }
}

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
