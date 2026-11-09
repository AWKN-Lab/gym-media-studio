@echo off
chcp 65001 >nul
setlocal
REM 打包用的解释器决定安装版能不能出声，优先用工程自带 .venv。
set PY=%~dp0..\.venv\Scripts\python.exe
if exist "%PY%" goto build
set PY=python
echo [WARN] 未找到 ..\.venv，改用系统 python 打包（在线配音可能不可用）。

:build
echo Building LYNVOS FIT desktop installer...
"%PY%" "%~dp0build.py" %*
if errorlevel 1 (
  echo.
  echo [FAILED] See desktop\dist\BUILD_REPORT.json for details.
) else (
  echo.
  echo [OK] Installer is in desktop\dist\
)
pause
