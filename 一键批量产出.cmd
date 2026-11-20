@echo off
setlocal EnableExtensions
chcp 65001 >nul
title AI Video Automation - Batch Production

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_folder_production.ps1"
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo [失败] 请检查上方错误和本地 .local-runtime\folder-production-ssh.*.log。
pause
exit /b %RESULT%
