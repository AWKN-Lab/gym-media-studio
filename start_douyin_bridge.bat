@echo off
chcp 65001 >nul
title LYNVOS 抖音浏览器桥
cd /d "%~dp0"

rem Prefer the node that ships with this machine; fall back to PATH.
set "NODE_EXE=C:\Users\10919\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"
if not exist "%NODE_EXE%" set "NODE_EXE=node"

echo [LYNVOS] 正在打开抖音浏览器窗口，请稍候...
echo [LYNVOS] 本窗口是浏览器桥的宿主，最小化即可，关闭它会同时关闭抖音窗口。
echo [LYNVOS] 登录态保存在 data\browser-profile-v2，重新打开不需要再登录。
echo.

"%NODE_EXE%" tools\douyin_bridge.mjs

echo [LYNVOS] 桥已退出。若需要重新打开，双击本文件即可。
timeout /t 5 >nul
