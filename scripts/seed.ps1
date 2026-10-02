<#
.SYNOPSIS
    灌入演示数据：部门 / 品类 / 供应商 / 预算。

.DESCRIPTION
    幂等 —— 已存在的按 code 跳过，可以反复跑，不会覆盖你手工改过的审批策略。
    用同一套 ORM 模型写数据（不手写 SQL），迁移一改种子数据跟着改。

    前提：数据库容器在跑（先执行 .\scripts\dev-up.ps1）。

.EXAMPLE
    .\scripts\seed.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$backend = Join-Path (Split-Path -Parent $PSScriptRoot) 'backend'
$python = Join-Path $backend '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    throw "找不到虚拟环境：$python`n先按 README 建 .venv 并安装依赖。"
}

Set-Location $backend
# Python 在 Windows 上默认用本地编码输出（中文系统是 cp936），
# 而终端多半按 UTF-8 解码 —— 不设这个变量，中文提示会变成乱码。
$env:PYTHONUTF8 = '1'
& $python -m scripts.seed
if ($LASTEXITCODE -ne 0) { throw "seed 失败（exit=$LASTEXITCODE）。" }
