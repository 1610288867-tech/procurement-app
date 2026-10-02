@echo off
rem 启动 Docker 引擎 + PostgreSQL 容器（幂等，重复双击安全）。
rem 真正的逻辑在 dev-up.ps1，这里只做包装。

chcp 65001 > nul
setlocal
set "REPO=%~dp0.."
title 企业采购审批系统 - 启动数据库

rem 优先用 PowerShell 7（pwsh）；没有就退回系统自带的 5.1，两边都能跑。
set "PSEXE=pwsh.exe"
where pwsh.exe >nul 2>nul
if errorlevel 1 set "PSEXE=powershell.exe"

echo [数据库] 启动 Docker 引擎与 PostgreSQL 容器…
"%PSEXE%" -NoProfile -ExecutionPolicy Bypass -File "%REPO%\scripts\dev-up.ps1" %*
if errorlevel 1 goto failed

echo.
echo 完成。
pause
exit /b 0

:failed
echo.
echo *** 失败：看上面的报错，或运行 scripts\dev-up.ps1 -ShowWindow 看 Docker 窗口 ***
pause
exit /b 1
