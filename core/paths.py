"""路径决策：桌面安装版与开发版共用的唯一入口。

这个模块刻意**不依赖 core.util**，因为 core.util 在导入瞬间就会把
ROOT / CONFIG_PATH / DATA / OUTPUT / ASSETS / WEB 固化成模块级常量；
桌面 sidecar 必须在 import core.* 之前就决定好根目录。

契约：
  A. 严格回退 —— 不设 LYNVOS_HOME 时 runtime_root() 与改造前逐字一致
     （Path(__file__).resolve().parent.parent），开发环境零感知。
  B. 安装态 —— 设 LYNVOS_HOME 后所有可写资源迁到用户目录，
     程序目录（Program Files）只读；静态骨架从 BUNDLE_ROOT 拷贝。
  C. 两侧约定同一个默认目录：%LOCALAPPDATA%\\LynvosFit，
     Rust 壳与 Python 内核不得各自算一套，否则会裂成两份数据。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_HOME = "LYNVOS_HOME"
APP_DIR_NAME = "LynvosFit"


def default_user_home(environ: dict | None = None) -> Path:
    """安装后的用户数据目录。与 Tauri main.rs 的 LOCALAPPDATA/LynvosFit 一致。"""
    env = os.environ if environ is None else environ
    if sys.platform == "win32":
        base = str(env.get("LOCALAPPDATA") or "").strip()
        if not base:
            base = str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    base = str(env.get("XDG_DATA_HOME") or "").strip()
    return (Path(base) if base else Path.home() / ".local" / "share") / APP_DIR_NAME


def user_home_enabled(environ: dict | None = None) -> bool:
    env = os.environ if environ is None else environ
    return bool(str(env.get(ENV_HOME) or "").strip())


def runtime_root(environ: dict | None = None) -> Path:
    """core.util.ROOT 的取值。纯函数，便于门禁在不 reload 模块的前提下取证。"""
    env = os.environ if environ is None else environ
    v = str(env.get(ENV_HOME) or "").strip()
    if v:
        return Path(v).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def bundle_root() -> Path:
    """程序自带骨架根：PyInstaller 下是 _MEIPASS，源码态是项目根。只读。

    PyInstaller 6 的 onedir 把 datas 放进 _MEIPASS 指向的 _internal；但个别版本
    会放在 exe 同级目录。以 web/index.html 实际所在为准做一次回退，避免安装后
    界面空白这种「源码好、产物坏」的问题。
    """
    meipass = str(getattr(sys, "_MEIPASS", "") or "").strip()
    root = Path(meipass) if meipass else Path(__file__).resolve().parent.parent
    if not (root / "web" / "index.html").is_file():
        sibling = root.parent
        if (sibling / "web" / "index.html").is_file():
            return sibling
    return root


def is_packaged() -> bool:
    return bool(str(getattr(sys, "_MEIPASS", "") or "").strip())


def port_file(home: Path) -> Path:
    return Path(home) / "port.json"
