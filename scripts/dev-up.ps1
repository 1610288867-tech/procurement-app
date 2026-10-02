<#
.SYNOPSIS
    一键拉起开发环境：Docker 引擎 -> PostgreSQL 容器（可选：后端 / 前端）。

.DESCRIPTION
    每次开工跑一次即可。全部操作幂等，重复执行安全。

    本项目刻意**不启用 Docker Desktop 的开机自启**，改由本脚本按需拉起：
    引擎没跑就启动它并等它就绪，然后 docker compose up -d 起数据库，
    最后等到容器 healthy 才返回。

    加 -WithApi / -WithFrontend 会顺便把后端 / 前端也拉起来。
    **这部分逻辑只有一份，在 start.ps1 里** —— 本脚本只做转发，
    所以也会一并得到"等到就绪 + 检查数据库连接"。

.PARAMETER TimeoutSeconds
    等待 Docker 引擎 / 数据库就绪的上限秒数，默认 180。

.PARAMETER ShowWindow
    Docker Desktop 默认最小化到托盘。加这个开关会显示它的主窗口，
    排查"引擎起不来"时用。

.PARAMETER WithApi
    顺便启动后端（转发给 start.ps1 -ApiOnly）。

.PARAMETER WithFrontend
    顺便启动前端（转发给 start.ps1 -WebOnly）。

.EXAMPLE
    .\scripts\dev-up.ps1

.EXAMPLE
    .\scripts\dev-up.ps1 -WithApi -WithFrontend

.NOTES
    只做检查与启动，不删除任何数据，不会执行 docker compose down -v。
#>
[CmdletBinding()]
param(
    [ValidateRange(30, 1800)]
    [int]$TimeoutSeconds = 180,

    [switch]$ShowWindow,
    [switch]$WithApi,
    [switch]$WithFrontend
)

$ErrorActionPreference = 'Stop'

$repoRoot  = Split-Path -Parent $PSScriptRoot
$container = 'procurement-db'

# Docker Desktop 的候选路径：本机是用户级安装（不在 Program Files）
$desktopCandidates = @(
    (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\Docker Desktop.exe'),
    (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe')
)

function Test-DockerEngine {
    try {
        docker info --format '{{.ServerVersion}}' *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Test-DatabaseHealthy {
    try {
        $status = docker inspect --format '{{.State.Health.Status}}' $container 2>$null
        return ($LASTEXITCODE -eq 0 -and $status -eq 'healthy')
    } catch {
        return $false
    }
}

function Wait-For {
    param(
        [Parameter(Mandatory)][scriptblock]$Condition,
        [Parameter(Mandatory)][string]$What
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (& $Condition) { return $true }
        Start-Sleep -Seconds 3
    }
    return $false
}

# ---- 1/2 Docker 引擎 ----

if (Test-DockerEngine) {
    $version = docker info --format '{{.ServerVersion}}'
    Write-Host "[1/2] Docker 引擎已在运行（$version）"
} else {
    $desktop = $desktopCandidates |
        Where-Object { Test-Path -LiteralPath $_ } |
        Select-Object -First 1

    if (-not $desktop) {
        throw "找不到 Docker Desktop。请手动启动它，或把安装路径加进脚本里的 `$desktopCandidates。"
    }

    Write-Host "[1/2] Docker 引擎未运行，正在启动 Docker Desktop（首次通常要 1-2 分钟）…"
    if ($ShowWindow) {
        Start-Process -FilePath $desktop
    } else {
        Start-Process -FilePath $desktop -WindowStyle Hidden
    }

    if (-not (Wait-For -Condition { Test-DockerEngine } -What 'Docker 引擎')) {
        throw "等待 Docker 引擎超时（$TimeoutSeconds 秒）。加 -ShowWindow 重跑一次，看看 Docker Desktop 报了什么。"
    }
    Write-Host "      Docker 引擎就绪"
}

# ---- 2/2 数据库容器 ----

# 通过仓库根目录的薄封装起库，与 README 里的命令走同一条路径
Push-Location $repoRoot
try {
    docker compose up -d
    if ($LASTEXITCODE -ne 0) { throw "docker compose up 失败。" }
} finally {
    Pop-Location
}

if (-not (Wait-For -Condition { Test-DatabaseHealthy } -What '数据库容器')) {
    throw "数据库容器 $container 未在 $TimeoutSeconds 秒内变为 healthy。用 docker compose logs -f db 查看原因。"
}

Write-Host "[2/2] 数据库就绪：$container (healthy)"

# ---- 可选：顺便拉起应用 ----
#
# 应用的启动逻辑**只有一份**，在 start.ps1 里（它还会等到就绪、并检查数据库连接）。
# 这里只把开关翻译成它的参数，不重复实现 —— 否则改一处漏一处。

Write-Host ""
if ($WithApi -or $WithFrontend) {
    # 用哈希表 splatting 才能按**名字**传参；数组 splatting 是按位置传的
    # （踩过：'-ApiOnly' 被当成 TimeoutSeconds 的值，直接报类型转换错）。
    $appArgs = @{}
    if ($WithApi -and -not $WithFrontend) { $appArgs['ApiOnly'] = $true }
    if ($WithFrontend -and -not $WithApi) { $appArgs['WebOnly'] = $true }
    & (Join-Path $PSScriptRoot 'start.ps1') @appArgs
} else {
    Write-Host "接下来各开一个终端："
    Write-Host "  后端  .\scripts\run-api.ps1"
    Write-Host "  前端  .\scripts\run-web.ps1"
    Write-Host "  或者一次带上：.\scripts\start.ps1"
}
