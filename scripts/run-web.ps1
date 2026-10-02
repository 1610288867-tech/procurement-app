<#
.SYNOPSIS
    启动前端（Vite 开发服务器）。

.DESCRIPTION
    没装依赖会提前拦下并告诉你怎么办；端口占用同理。

.EXAMPLE
    .\scripts\run-web.ps1
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 5173
)

$ErrorActionPreference = 'Stop'

$frontend = Join-Path (Split-Path -Parent $PSScriptRoot) 'frontend'

if (-not (Test-Path -LiteralPath (Join-Path $frontend 'node_modules'))) {
    throw "前端依赖还没装：cd frontend; npm install"
}

$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    throw "$Port 端口已被占用（PID $($busy[0].OwningProcess)）。先停掉它，或用 -Port 换一个端口。"
}

Write-Host "前端启动中 → http://localhost:$Port   （Ctrl+C 停止）"
Set-Location $frontend
npm run dev -- --port $Port
