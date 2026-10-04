<#
  回归验收入口（M8）—— 一条命令把"这个项目现在到底行不行"跑一遍。

  ============================ 它跑什么（固定，不接受任何开关）============================
      1) 三条分层自查          —— 应无任何输出
      2) 后端全量测试          —— pytest -m ""（unit + integration，**不跳集成档**）
      3) 后端静态检查          —— ruff（app / tests / scripts）+ mypy
      4) 前端三件              —— tsc --noEmit / vitest run / npm run build
      5) 起服务（若没在跑）     —— 复用 scripts\start.ps1，等就绪
      6) 真实服务冒烟          —— backend\scripts\m6_smoke.py（对着 8000 打 HTTP，跑完自清理）
      7) 浏览器自检            —— scripts\浏览器自检.mjs（无窗口 Edge + CDP，12 项）
      8) 清理与残留回查        —— testdata_cleanup.py --scope all（残留非 0 就失败）

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
  # ⚠️ 必须先把 $LASTEXITCODE 归零：它是**上一条外部命令**留下的，
  # 纯 PowerShell 的步骤（例如三条分层自查）不碰它，留着 $null 时会
  # 被 `-ne 0` 判成失败 —— 实测踩过，表现为"明明 0 命中却说第 1 步失败"。
  $LASTEXITCODE = 0

  Push-Location $WorkDir
  try {
    $output = & $Body 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE -ne 0) { $failed = $true }
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
} $backendDir

Invoke-Step "后端静态检查 ruff + mypy" {
  & $python -m ruff check app tests scripts
  if ($LASTEXITCODE -ne 0) { throw "ruff 有命中" }
  & $python -m mypy app --ignore-missing-imports
} $backendDir

Invoke-Step "前端 tsc --noEmit" {
  npx tsc --noEmit
  if ($LASTEXITCODE -eq 0) { "类型检查无错误" }
} $frontendDir

Invoke-Step "前端 vitest run" {
  npx vitest run
} $frontendDir

Invoke-Step "前端 npm run build（生产构建）" {
  npm run build
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
} $backendDir

Invoke-Step "浏览器自检 浏览器自检.mjs（12 项，无窗口 Edge）" {
  node "scripts\浏览器自检.mjs"
}

Invoke-Step "测试数据清理与残留回查（testdata_cleanup.py --scope all）" {
  $env:PYTHONUTF8 = "1"
  & $python "scripts\testdata_cleanup.py" --scope all
} $backendDir

# ---------------------------------------------------------------- 汇总

Write-Host ""
Write-Host ("=" * 72)
Write-Host "回归结果：全部通过"
Write-Host ("=" * 72)
$script:summary | Format-Table -AutoSize | Out-String | Write-Host
Write-Host "退出码 0。上面每一行的摘要都可以单独重跑复核。"
exit 0
