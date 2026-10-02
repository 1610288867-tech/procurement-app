<#
.SYNOPSIS
    启动前后端（各开一个新窗口），并验证它们真的起来了。

.DESCRIPTION
    与 dev-up.ps1 的分工很清楚：

        dev-up.ps1  ->  Docker 引擎 + PostgreSQL 容器（基础设施）
        start.ps1   ->  后端 + 前端（应用，本脚本）

    两个都是幂等的：端口上已经有服务就跳过，不会起重复进程。

    启动后会**等到两个端口都能访问**，再打印地址和一次真实的后端就绪检查。
    这一步是有意加的 —— 之前吃过亏：页面开着、后端没跑时，Vite 代理会返回
    空响应体的 500，看起来像后端 bug，其实只是没启动。

.PARAMETER SkipWait
    只拉起进程，不等就绪。

.PARAMETER ApiOnly
    只起后端（dev-up.ps1 转发用）。

.PARAMETER WebOnly
    只起前端（dev-up.ps1 转发用）。

.EXAMPLE
    .\scripts\start.ps1

.NOTES
    前提：数据库容器在跑（先执行 .\scripts\dev-up.ps1）。
    本脚本只启动进程，不删任何数据。
#>
[CmdletBinding()]
param(
    [switch]$SkipWait,
    [switch]$ApiOnly,
    [switch]$WebOnly,

    [ValidateRange(5, 300)]
    [int]$TimeoutSeconds = 60
)

$ErrorActionPreference = 'Stop'

$repoRoot  = Split-Path -Parent $PSScriptRoot
$apiPort   = 8000
$webPort   = 5173
$container = 'procurement-db'

if ($ApiOnly -and $WebOnly) {
    throw "-ApiOnly 和 -WebOnly 不能同时给（那等于什么都不起）。"
}

# 默认两边都起；给一个开关就只起那一边
$startApi = -not $WebOnly
$startWeb = -not $ApiOnly

function Test-PortBusy {
    param([Parameter(Mandatory)][int]$Port)
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Start-InNewWindow {
    <#
        在新窗口里跑一个脚本（后端 / 前端）。

        用**当前这个 PowerShell 自己的可执行文件**，不要写死 powershell.exe：
        写死会把子窗口钉死在系统自带的 5.1 上 —— 父脚本在 7 里跑得好好的，
        子窗口却在 5.1 里跑，将来 run-api.ps1 / run-web.ps1 一旦用了 PowerShell 7
        的写法，就会出现"明明启动成功、新窗口却立刻报错"的怪事。

        为什么不用 $PSHOME：不同版本目录下的可执行文件名不一样
        （pwsh.exe / powershell.exe），直接取当前进程的映像路径最稳。
    #>
    param([Parameter(Mandatory)][string]$Script)
    # 取不到就退回 powershell.exe，不让这一步把整个启动流程卡住
    $shell = 'powershell.exe'
    try { $shell = (Get-Process -Id $PID).Path } catch { }
    Start-Process -FilePath $shell -ArgumentList @(
        '-NoExit', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot $Script)
    )
}

function Wait-HttpOk {
    param([Parameter(Mandatory)][string]$Url)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-HttpResponding -Url $Url) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Test-HttpResponding {
    <#
        探测一次"这个地址有没有 HTTP 服务在应答"。

        **不要用 -SkipHttpErrorCheck** —— 那是 PowerShell 7 才有的参数。
        在 Windows PowerShell 5.1 下它会直接报"找不到参数"，又被 catch 吞掉，
        于是探测永远失败：表现为等满 60 秒后假报"未就绪"，而服务其实是好的（实测踩过）。

        5.1 的兼容做法：4xx/5xx 会抛异常，但**抛异常本身就说明服务在监听**，
        从异常里把状态码取出来判断即可 —— 那和"连接被拒绝"是两回事。
    #>
    param([Parameter(Mandatory)][string]$Url)
    try {
        # -UseBasicParsing：5.1 上没有它会去依赖 IE 引擎，可能直接报错；
        # PowerShell 7 也接受这个参数（只为向后兼容而保留）。
        $response = Invoke-WebRequest -Uri $Url -TimeoutSec 3 -UseBasicParsing
        return ($response.StatusCode -lt 500)
    } catch {
        $status = Get-HttpStatusCode -ErrorRecord $_
        if ($null -eq $status) { return $false }
        return ($status -lt 500)
    }
}

function Get-HttpStatusCode {
    <#
        从 Invoke-WebRequest 抛出的异常里取 HTTP 状态码；取不到返回 $null。

        "取不到"= 连接被拒 / DNS 失败 / 超时 —— 那才是真的没起来。
        注意别写成 [int]$null：PowerShell 里 [int]$null 等于 0，
        0 比 500 小，会把"连不上"误判成"已就绪"。
    #>
    param([Parameter(Mandatory)][System.Management.Automation.ErrorRecord]$ErrorRecord)
    try {
        $response = $ErrorRecord.Exception.Response
        if ($null -eq $response -or $null -eq $response.StatusCode) { return $null }
        return [int]$response.StatusCode
    } catch {
        return $null
    }
}

# ---- 前置检查：数据库容器 ----

$dbHealthy = $false
try {
    $status = docker inspect --format '{{.State.Health.Status}}' $container 2>$null
    $dbHealthy = ($LASTEXITCODE -eq 0 -and $status -eq 'healthy')
} catch {
    $dbHealthy = $false
}

if (-not $dbHealthy) {
    Write-Warning "数据库容器 $container 不是 healthy（可能 Docker 没启动）。"
    Write-Warning "先跑一次：.\scripts\dev-up.ps1"
    Write-Warning "仍然继续启动前后端，但后端会连不上库。"
    Write-Host ""
}

# ---- 启动后端 ----

if ($startApi) {
    if (Test-PortBusy -Port $apiPort) {
        Write-Host "[1/2] 后端已在 $apiPort 运行，跳过"
    } else {
        Start-InNewWindow -Script 'run-api.ps1'
        Write-Host "[1/2] 后端已在新窗口启动（uvicorn --reload）"
    }
}

# ---- 启动前端 ----

if ($startWeb) {
    if (Test-PortBusy -Port $webPort) {
        Write-Host "[2/2] 前端已在 $webPort 运行，跳过"
    } else {
        Start-InNewWindow -Script 'run-web.ps1'
        Write-Host "[2/2] 前端已在新窗口启动（Vite）"
    }
}

if ($SkipWait) {
    Write-Host ""
    Write-Host "（-SkipWait：没有等待就绪）"
    return
}

# ---- 等就绪 ----

Write-Host ""
Write-Host "等待就绪（最多 $TimeoutSeconds 秒）…"

$apiOk = if ($startApi) { Wait-HttpOk -Url "http://127.0.0.1:$apiPort/health/live" } else { $null }
# 前端必须用 localhost 而不是 127.0.0.1：Vite 默认只监听 IPv6 的 ::1，
# 用 IPv4 地址探测会一直失败（实测踩过）。
$webOk = if ($startWeb) { Wait-HttpOk -Url "http://localhost:$webPort/" } else { $null }

Write-Host ""
if ($startApi) {
    $apiVerdict = if ($apiOk) { "OK" } else { "未就绪 —— 看新窗口里的报错" }
    Write-Host "  后端  http://127.0.0.1:$apiPort/docs          [$apiVerdict]"
}
if ($startWeb) {
    $webVerdict = if ($webOk) { "OK" } else { "未就绪 —— 看新窗口里的报错" }
    Write-Host "  前端  http://localhost:$webPort                [$webVerdict]"
}

# ---- 后端对数据库的真实就绪状态 ----

# 数据库就绪只有在真的起了后端、且后端活着时才查得到
if ($startApi -and $apiOk) {
    try {
        $ready = Invoke-RestMethod -Uri "http://127.0.0.1:$apiPort/health/ready" -TimeoutSec 5
        Write-Host "  数据库连接 ${container} 对应的库          [$($ready.database)]"
        if ($ready.database -ne 'up') {
            Write-Host ""
            Write-Host "  数据库不可用 —— 跑一次 .\scripts\dev-up.ps1 再刷新页面。"
        }
    } catch {
        Write-Host "  数据库连接                               [检查失败：$($_.Exception.Message)]"
    }
}

Write-Host ""
if ($startWeb) {
    Write-Host "浏览器打开：http://localhost:$webPort"
} else {
    Write-Host "接口文档：http://127.0.0.1:$apiPort/docs"
}
Write-Host "（详细步骤见仓库根目录的「浏览器测试-启动说明.txt」）"
