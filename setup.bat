@echo off
chcp 65001 >nul 2>&1
REM ============================================================
REM  凌扬运动 LYNVOS FIT - 本机运行时安装
REM  建一个工程自带的 .venv（Python 3.12）并装齐依赖。
REM  配音能不能出声取决于解释器，所以固定用这个运行时，别用系统随手装的。
REM ============================================================
cd /d "%~dp0"

if exist "%~dp0.venv\Scripts\python.exe" (
  echo [SKIP] 已有 .venv，如需重建请先删掉 .venv 目录。
  goto check
)

where uv >nul 2>nul
if errorlevel 1 goto pipvenv

echo [1/2] 用 uv 建 .venv（Python 3.12）
uv venv .venv -p 3.12
if errorlevel 1 goto failed
echo [2/2] 装依赖
uv pip install --python ".venv\Scripts\python.exe" -r requirements.txt
if errorlevel 1 goto failed
goto check

:pipvenv
echo [1/2] 未装 uv，改用系统 python 建 .venv
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] 既没有 uv 也没有 python，无法安装运行时。
  pause
  exit /b 1
)
python -m venv .venv
if errorlevel 1 goto failed
echo [2/2] 装依赖
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed

:check
echo.
echo [3/3] 自检（在线配音会真合成一段）
".venv\Scripts\python.exe" run.py --probe
echo.
echo 完成。以后双击 start_server.bat 即可。
pause
exit /b 0

:failed
echo.
echo [FAILED] 运行时安装失败，请查看上方报错。
pause
exit /b 1
