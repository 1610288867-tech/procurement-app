@echo off
rem 只启动后端（uvicorn --reload）。这个窗口会被后端占住，Ctrl+C 停止。
rem 额外参数会原样传下去：启动-后端.bat -Port 8001

chcp 65001 > nul
setlocal
set "REPO=%~dp0.."
title 企业采购审批系统 - 后端 8000

rem 优先用 PowerShell 7（pwsh）；没有就退回系统自带的 5.1，两边都能跑。
set "PSEXE=pwsh.exe"
where pwsh.exe >nul 2>nul
if errorlevel 1 set "PSEXE=powershell.exe"

echo [后端] uvicorn 启动中…… 地址 http://127.0.0.1:8000/docs
echo        按 Ctrl+C 停止。
echo.
"%PSEXE%" -NoProfile -ExecutionPolicy Bypass -File "%REPO%\scripts\run-api.ps1" %*
if errorlevel 1 goto failed
exit /b 0

:failed
echo.
echo *** 后端启动失败：看上面的报错（虚拟环境或端口占用是最常见的两条）***
pause
exit /b 1
