@echo off
rem 只启动前端（Vite）。这个窗口会被前端占住，Ctrl+C 停止。
rem 额外参数会原样传下去：启动-前端.bat -Port 5174

chcp 65001 > nul
setlocal
set "REPO=%~dp0.."
title 企业采购审批系统 - 前端 5173

rem 优先用 PowerShell 7（pwsh）；没有就退回系统自带的 5.1，两边都能跑。
set "PSEXE=pwsh.exe"
where pwsh.exe >nul 2>nul
if errorlevel 1 set "PSEXE=powershell.exe"

echo [前端] Vite 启动中…… 地址 http://localhost:5173/
echo        按 Ctrl+C 停止。
echo.
"%PSEXE%" -NoProfile -ExecutionPolicy Bypass -File "%REPO%\scripts\run-web.ps1" %*
if errorlevel 1 goto failed
exit /b 0

:failed
echo.
echo *** 前端启动失败：看上面的报错（没装依赖就先 cd frontend 跑 npm install）***
pause
exit /b 1
