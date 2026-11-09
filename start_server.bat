@echo off
chcp 65001 >nul 2>&1
REM ============================================================
REM  凌扬运动 LYNVOS FIT - 自媒体自动化工作台 启动脚本
REM  Double-click to launch the local server (Windows).
REM  优先用工程自带的 .venv（已验证在线配音正常）；没有才退回系统 python，并提示风险。
REM ============================================================
cd /d "%~dp0"

REM HuggingFace 镜像，避免直连 huggingface.co 超时（faster-whisper 模型下载用）
set HF_ENDPOINT=https://hf-mirror.com

REM 监听地址与端口（如需局域网/其他设备访问，把 127.0.0.1 改成 0.0.0.0）
set HOST=127.0.0.1
set PORT=8787

set PY=%~dp0.venv\Scripts\python.exe
if exist "%PY%" goto run

echo [WARN] 未找到工程自带运行时 .venv，改用系统 python。
echo [WARN] 系统 python 不保证能正常出声：部分构建的在线配音会超时。
echo [WARN] 建议先双击 setup.bat 建好 .venv。
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] 未找到 python，请先双击 setup.bat，或安装 Python 3.12 并加入 PATH。
  pause
  exit /b 1
)
set PY=python

:run
echo ============================================================
echo  凌扬运动 LYNVOS FIT 自媒体工作台
echo  访问地址: http://%HOST%:%PORT%/
echo  关闭此窗口即停止服务
echo ============================================================
"%PY%" run.py --serve --host %HOST% --port %PORT%
if errorlevel 1 (
  echo [ERROR] 服务启动失败，请查看上方报错。
  pause
)
