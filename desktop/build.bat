@echo off
chcp 65001 >nul
setlocal
echo Building LYNVOS FIT desktop installer...
python "%~dp0build.py" %*
if errorlevel 1 (
  echo.
  echo [FAILED] See desktop\dist\BUILD_REPORT.json for details.
) else (
  echo.
  echo [OK] Installer is in desktop\dist\
)
pause
