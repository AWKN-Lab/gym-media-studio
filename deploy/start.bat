@echo off
chcp 65001 >nul
cd /d "%~dp0.."
rem 清掉会话注入的内部代理，否则热搜/素材下载会失败
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "http_proxy="
set "https_proxy="
set "ALL_PROXY="
echo.
echo   凌扬运动 LYNVOS FIT 工作台:  http://127.0.0.1:8787
echo   团队共享请改用:   python run.py --serve --host 0.0.0.0 --users "admin:密码:admin,同事:密码"
echo.
python run.py --serve --port 8787 %*
pause
