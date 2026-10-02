@echo off
rem ============================================================
rem  一键启动（双击即可）
rem
rem  它只做三件事，真正的逻辑都在 scripts\*.ps1 里：
rem      1) scripts\dev-up.ps1  Docker 引擎 + PostgreSQL 容器
rem      2) scripts\start.ps1   后端(8000) + 前端(5173)
rem      3) 打开浏览器
rem  这里**不重复实现**任何启动逻辑，改一处就够。
rem ============================================================

rem 中文要能正常显示，得先把控制台切到 UTF-8
chcp 65001 > nul
setlocal
set "REPO=%~dp0"
title 企业采购审批系统 - 一键启动

rem 优先用 PowerShell 7（pwsh），找不到就退回系统自带的 5.1。
rem 两边都能跑：start.ps1 的就绪探测特意写成 5.1/7 通吃
rem （5.1 没有 -SkipHttpErrorCheck，早先会在这里假报"未就绪"）。
set "PSEXE=pwsh.exe"
where pwsh.exe >nul 2>nul
if errorlevel 1 (
    set "PSEXE=powershell.exe"
    echo [!] 没找到 PowerShell 7（pwsh），改用系统自带的 Windows PowerShell 5.1。
    echo     同样能跑，只是建议装一下 PowerShell 7。
    echo.
)

echo ============================================================
echo   企业采购审批系统 · 一键启动
echo ============================================================
echo.

echo [1/3] 启动 Docker 引擎与数据库（首次可能要 1-2 分钟，之后约 10 秒）…
"%PSEXE%" -NoProfile -ExecutionPolicy Bypass -File "%REPO%scripts\dev-up.ps1"
if errorlevel 1 goto failed

echo.
echo [2/3] 启动后端与前端（各自会开一个新窗口，那两个窗口别关）…
"%PSEXE%" -NoProfile -ExecutionPolicy Bypass -File "%REPO%scripts\start.ps1"
if errorlevel 1 goto failed

echo.
echo [3/3] 打开浏览器…
start "" "http://localhost:5173/"

echo.
echo ------------------------------------------------------------
echo  启动完成。
echo    页面      http://localhost:5173/       （登录入口在 /login）
echo    接口文档  http://127.0.0.1:8000/docs
echo    演示账号  admin / Dev-Passw0rd!        （其余见登录页）
echo.
echo  这个窗口可以关掉；关掉前后端那两个窗口就等于停止服务。
echo  还没有演示账号或数据？双击 scripts\灌演示数据.bat。
echo ------------------------------------------------------------
echo.
pause
exit /b 0

:failed
echo.
echo ============================================================
echo  启动失败 —— 上面有具体报错
echo  常见原因和排查步骤见「浏览器测试-启动说明.txt」
echo ============================================================
echo.
pause
exit /b 1
