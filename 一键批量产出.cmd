@echo off
setlocal EnableExtensions
chcp 65001 >nul
title AI Video Automation - Folder Production

set "PROJECT_ROOT=%~dp0"
set "VIDEO_FACTORY_PROJECT_ROOT=%PROJECT_ROOT%"
set "VIDEO_FACTORY_PYTHON=%PROJECT_ROOT%.venv\Scripts\python.exe"
set "VIDEO_FACTORY_RUNTIME_DIR=%PROJECT_ROOT%.local-runtime"
set "VIDEO_FACTORY_STATE_FILE=%VIDEO_FACTORY_RUNTIME_DIR%\folder-production-state.json"
set "VIDEO_FACTORY_MANIFEST_DIR=%VIDEO_FACTORY_RUNTIME_DIR%\batch_40_production"
set "VIDEO_FACTORY_SSH_CONFIG=%VIDEO_FACTORY_RUNTIME_DIR%\folder-production-ssh.txt"
set "VIDEO_FACTORY_SSH_KNOWN_HOSTS=%VIDEO_FACTORY_RUNTIME_DIR%\vast-known-hosts"
if not defined VIDEO_FACTORY_SOURCE_ROOT set "VIDEO_FACTORY_SOURCE_ROOT=D:\脚本"

if not exist "%VIDEO_FACTORY_PYTHON%" (
  echo [错误] 未找到项目 Python：%VIDEO_FACTORY_PYTHON%
  pause
  exit /b 2
)
if not exist "%PROJECT_ROOT%scripts\one_click_folder_production.py" (
  echo [错误] 批量生产程序不存在：%PROJECT_ROOT%scripts\one_click_folder_production.py
  pause
  exit /b 2
)
if not exist "%VIDEO_FACTORY_SOURCE_ROOT%" (
  echo [错误] Word 脚本目录不存在：%VIDEO_FACTORY_SOURCE_ROOT%
  pause
  exit /b 2
)
if not defined VIDEO_FACTORY_API_URL if not exist "%VIDEO_FACTORY_SSH_CONFIG%" (
  echo [错误] 需要设置 VIDEO_FACTORY_API_URL 或配置私有 SSH 目标：%VIDEO_FACTORY_SSH_CONFIG%
  pause
  exit /b 2
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_ROOT%scripts\run_folder_production.ps1"
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo [失败] 批量生产有失败或阻断项，请检查本地 .local-runtime 任务台账。
pause
exit /b %RESULT%
