@echo off
rem 启动后端 + 前端，各开一个新窗口，并等到就绪。
rem 真正的逻辑在 start.ps1，这里只做包装。
rem 额外参数会原样传下去：启动-前后端.bat -ApiOnly  只起后端。

chcp 65001 > nul
setlocal
set "REPO=%~dp0.."
title 企业采购审批系统 - 启动前后端

rem 优先用 PowerShell 7（pwsh）；没有就退回系统自带的 5.1，两边都能跑。
set "PSEXE=pwsh.exe"
where pwsh.exe >nul 2>nul
if errorlevel 1 set "PSEXE=powershell.exe"

echo [应用] 启动后端(8000)与前端(5173)…
"%PSEXE%" -NoProfile -ExecutionPolicy Bypass -File "%REPO%\scripts\start.ps1" %*
if errorlevel 1 goto failed

echo.
echo 前后端在自己的窗口里运行 —— 关掉它们就等于停止服务。
pause
exit /b 0

:failed
echo.
echo *** 失败：看上面的报错。数据库没起？先双击 scripts\启动-数据库.bat ***
pause
exit /b 1
