@echo off
rem 灌入演示数据：部门 / 品类 / 供应商 / 预算 / 7 个演示账号。
rem 幂等 —— 反复双击安全，已存在的会跳过，不会覆盖你手工改过的审批策略。
rem 前提：数据库容器在跑（先双击 scripts\启动-数据库.bat）。

chcp 65001 > nul
setlocal
set "REPO=%~dp0.."
title 企业采购审批系统 - 灌演示数据

rem 优先用 PowerShell 7（pwsh）；没有就退回系统自带的 5.1，两边都能跑。
set "PSEXE=pwsh.exe"
where pwsh.exe >nul 2>nul
if errorlevel 1 set "PSEXE=powershell.exe"

echo [演示数据] 正在写入……
echo.
"%PSEXE%" -NoProfile -ExecutionPolicy Bypass -File "%REPO%\scripts\seed.ps1"
if errorlevel 1 goto failed

echo.
echo ------------------------------------------------------------
echo  完成。账号口令统一为 Dev-Passw0rd!（仅本地演示数据）
echo ------------------------------------------------------------
pause
exit /b 0

:failed
echo.
echo *** 失败：数据库没起？先双击 scripts\启动-数据库.bat ***
pause
exit /b 1
