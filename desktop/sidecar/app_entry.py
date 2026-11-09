#!/usr/bin/env python3
"""凌扬运动 LYNVOS FIT · 桌面安装版后端入口（Tauri sidecar）。

四条硬契约：
  1. **顺序**：必须在 import core.* / web.* 之前设好 LYNVOS_HOME。core.util 在导入
     瞬间就把 ROOT / CONFIG_PATH / DATA / OUTPUT / ASSETS / WEB 固化成常量，晚一步全错。
  2. **只读程序目录**：骨架从 BUNDLE_ROOT（PyInstaller 的 _MEIPASS）读，所有可写数据
     落在 %LOCALAPPDATA%\\LynvosFit。装进 Program Files 不会因无写权限而崩。
  3. **白名单引导**：只播种 config / web / assets 程序资源 / data/baseline 方法库。
     抖音抓取留下的浏览器 profile、CDP 探针、runs 产物一律不进包、不复制。
     用户数据只补缺失，程序资源按哈希覆盖 —— 升级不冲掉用户改过的方案库。
  4. **不留后台**：--parent-pid 守护线程，父进程消失即清掉 port.json 并 os._exit(0)。
     Tauri 侧另有一层 kill 兜底。这是「关掉窗口不留服务」的硬保证。

开发态可直接 python desktop/sidecar/app_entry.py 运行（不传 parent-pid 时不启用守护）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- 根目录决策（必须先于 core 导入）
def _setup_home() -> Path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from core.paths import default_user_home  # 刻意只导入不依赖 util 的路径模块
    raw = str(os.environ.get("LYNVOS_HOME") or "").strip()
    home = Path(raw).expanduser().resolve() if raw else default_user_home()
    os.environ["LYNVOS_HOME"] = str(home)
    home.mkdir(parents=True, exist_ok=True)
    return home


# 刻意不在模块导入时调用 _setup_home()：门禁测试要 import 本模块验证 bootstrap，
# 不能在 import 的副作用里真的创建用户目录、改写进程环境变量。
HOME: Path | None = None

# ---------------------------------------------------------------- 首次启动引导（纯文件操作，可单测）
DIRS = (
    "output", "data", "data/baseline", "data/transcripts", "data/runs",
    "data/drafts", "data/scripts", "data/plans", "data/metrics",
    "assets", "assets/gym", "assets/fonts", "assets/bgm", "web",
)

# 程序资源：每次启动按内容哈希同步，升级时保证 UI 与随包资源是最新的
PROGRAM_GLOBS = ("web/*.html", "web/*.css", "web/*.js",
                 "assets/fonts/*", "assets/bgm/*", "assets/*.png")

# 用户数据：仅在目标不存在时播种，绝不覆盖（设置、方案库、确认台账、示例素材）
USER_SEED_GLOBS = ("config.json", "data/baseline/*.json", "assets/gym/*")

# 明确禁止进包/复制的缓存与探针目录 —— 门禁会断言 bootstrap 报告里永不出现这些前缀
NEVER_BUNDLE = (
    "data/browser", "data/_cdp_probe", "data/_pipe_probe", "data/_pipe_",
    "data/runs/", "data/tmp_", "data/voice_previews", "data/*.mp4",
)


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def bootstrap(bundle_root: Path, home: Path) -> dict:
    """把只读骨架播种到可写用户目录，返回可审计的报告。

    不删除、不遍历整个 data/ —— 白名单之外的一切（浏览器缓存、探针、历史成片）
    既不会被复制，也不会被清理。用户重装后旧数据原样保留。
    """
    bundle_root, home = Path(bundle_root), Path(home)
    rep = {"created_dirs": [], "synced": [], "seeded": [], "kept": [], "missing": []}
    for rel in DIRS:
        d = home / rel
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            rep["created_dirs"].append(rel)

    def _items(globs: tuple[str, ...]):
        seen = set()
        for pattern in globs:
            for src in sorted(bundle_root.glob(pattern)):
                if not src.is_file() or src in seen:
                    continue
                seen.add(src)
                yield src.relative_to(bundle_root).as_posix(), src

    for rel, src in _items(PROGRAM_GLOBS):
        dst = home / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.is_file() or _sha(src) != _sha(dst):
            shutil.copy2(src, dst)
            rep["synced"].append(rel)

    for rel, src in _items(USER_SEED_GLOBS):
        dst = home / rel
        if dst.exists():
            rep["kept"].append(rel)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        rep["seeded"].append(rel)

    for rel in ("config.json", "web/index.html"):
        if not (bundle_root / rel).is_file():
            rep["missing"].append(rel)
    return rep


# ---------------------------------------------------------------- 端口与进程存活
def choose_port(preferred: int = 8787, attempts: int = 12) -> int:
    """优先固定端口；被占用时向后顺延，让第二个实例能复用同一份 port.json。"""
    for port in range(preferred, preferred + attempts):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError(f"127.0.0.1:{preferred}~{preferred + attempts - 1} 均被占用")


def _parent_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        k = ctypes.windll.kernel32
        handle = k.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return k.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT = 仍存活
        finally:
            k.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def watch_parent(pid: int, home: Path, interval: float = 1.5) -> None:
    """父进程（Tauri 壳）消失 → 清理端点文件 → 硬退出，不留后台服务。"""
    while True:
        time.sleep(interval)
        if not _parent_alive(pid):
            try:
                (home / "port.json").unlink(missing_ok=True)
            except OSError:
                pass
            os._exit(0)


# ---------------------------------------------------------------- 入口
def main() -> int:
    global HOME
    # 必须在 import core.util 之前定好根目录：core.util 导入即固化 ROOT。
    HOME = _setup_home()
    # 让 config 里诸如 "assets/gym" 的相对写法也解析到用户数据目录，
    # 而不是快捷方式的「起始位置」（安装后通常指向只读的 Program Files）。
    os.chdir(HOME)

    ap = argparse.ArgumentParser(description="LYNVOS FIT 桌面版后端")
    ap.add_argument("--port", type=int, default=int(os.environ.get("LYNVOS_PORT") or 8787))
    ap.add_argument("--parent-pid", type=int, default=int(os.environ.get("LYNVOS_PARENT_PID") or 0))
    args = ap.parse_args()

    from core.util import BUNDLE_ROOT, write_json
    rep = bootstrap(BUNDLE_ROOT, HOME)
    port = choose_port(args.port)
    write_json(HOME / "port.json", {
        "port": port,
        "pid": os.getpid(),
        "parent_pid": args.parent_pid,
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "home": str(HOME),
        "bootstrap": {"synced": len(rep["synced"]), "seeded": len(rep["seeded"]),
                      "kept": len(rep["kept"]), "missing": rep["missing"]},
    })

    if args.parent_pid:
        threading.Thread(target=watch_parent, args=(args.parent_pid, HOME),
                         daemon=True).start()

    # 延迟到端口与目录都就绪后再启动 HTTP 服务；serve() 内部会启动调度线程并阻塞。
    from web.server import serve
    print(f"[LYNVOS] home={HOME} port={port} bundle={BUNDLE_ROOT}", flush=True)
    serve(port=port, host="127.0.0.1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
