#!/usr/bin/env bash
# Linux 前台启动；开机自启请用 gym-pulse.service
#   ./deploy/start.sh --host 0.0.0.0 --port 8787
set -euo pipefail
cd "$(dirname "$0")/.."

# WorkBuddy / 部分云主机会注入内部代理，导致热搜与素材下载失败；国内源直连即可
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

if [ -f .venv/bin/activate ]; then source .venv/bin/activate; fi
command -v ffmpeg >/dev/null || { echo "缺少 FFmpeg：sudo apt install -y ffmpeg"; exit 1; }
python -c "from PIL import Image" 2>/dev/null || { echo "缺少 Pillow：pip install -r requirements.txt"; exit 1; }

exec python run.py --serve "$@"
