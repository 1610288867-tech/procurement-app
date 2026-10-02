<#
.SYNOPSIS
    启动后端（uvicorn，带热重载）。

.DESCRIPTION
    用 backend 目录下的虚拟环境跑 uvicorn。两种常见错误会被提前拦下，
    而不是给你一个"看起来启动了、其实没监听"的进程：

      - 虚拟环境不存在  → 告诉你先怎么建
      - 端口已被占用    → 告诉你被哪个 PID 占了

    热重载需要 watchfiles（已在 requirements.txt 的「开发工具」里）。

.EXAMPLE
    .\scripts\run-api.ps1

.EXAMPLE
    .\scripts\run-api.ps1 -Port 8001
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'

$backend = Join-Path (Split-Path -Parent $PSScriptRoot) 'backend'
$python = Join-Path $backend '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    throw "找不到虚拟环境：$python`n先建一个：cd backend; py -3.12 -m venv .venv; .\.venv\Scripts\Activate.ps1; python -m pip install -r requirements.txt --only-binary=:all:"
}

$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    throw "$Port 端口已被占用（PID $($busy[0].OwningProcess)）。先停掉它，或用 -Port 换一个端口。"
}

Write-Host "后端启动中 → http://127.0.0.1:$Port/docs   （Ctrl+C 停止）"
Set-Location $backend
& $python -m uvicorn app.main:app --reload --port $Port
