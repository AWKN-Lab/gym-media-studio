@echo off
chcp 65001 >nul 2>&1
REM ============================================================
REM  凌扬运动 LYNVOS FIT - 自媒体自动化工作台 启动脚本
REM  Double-click to launch the local server (Windows).
REM ============================================================
cd /d "%~dp0"

REM HuggingFace 镜像，避免直连 huggingface.co 超时（faster-whisper 模型下载用）
set HF_ENDPOINT=https://hf-mirror.com

REM 监听地址与端口（如需局域网/其他设备访问，把 127.0.0.1 改成 0.0.0.0）
set HOST=127.0.0.1
set PORT=8787

REM 可选：若本机已开 Clash 类代理且需走代理下载模型，可取消下一行注释
REM set HTTPS_PROXY=http://127.0.0.1:7890

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] 未找到 python，请先安装 Python 3.10+ 并加入 PATH。
  pause
  exit /b 1
)

echo ============================================================
echo  凌扬运动 LYNVOS FIT 自媒体工作台
echo  访问地址: http://%HOST%:%PORT%/
echo  关闭此窗口即停止服务
echo ============================================================
python run.py --serve --host %HOST% --port %PORT%
if errorlevel 1 (
  echo [ERROR] 服务启动失败，请查看上方报错。
  pause
)
