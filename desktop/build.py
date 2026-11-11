#!/usr/bin/env python3
"""凌扬运动 LYNVOS FIT · 桌面安装包一键构建。

    python desktop/build.py                 # 精简完整包（推荐）
    python desktop/build.py --with-whisper # 全量离线包（含 faster-whisper 模型链）
    python desktop/build.py --console      # sidecar 保留控制台窗口，便于排障
    python desktop/build.py --skip-sidecar # 只重打 Tauri 壳
    python desktop/build.py --skip-tauri   # 只重打 Python 内核

产物：desktop/dist/
    sidecar/                        Python 内核（PyInstaller onedir）
    凌扬运动工作台_1.0.0_x64-setup.exe   NSIS 安装包（免管理员，当前用户安装）
    BUILD_REPORT.json               本轮构建读数

纪律：构建产物只从白名单来。项目里 864MB 的抖音浏览器 profile / CDP 探针 /
历史成片一律不进包 —— spec 的 datas 是显式白名单，不写任何 data/ 通配，
data/baseline 整个目录永不进包。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

DESKTOP = Path(__file__).resolve().parent
PROJECT = DESKTOP.parent
TAURI = DESKTOP / "src-tauri"
DIST = DESKTOP / "dist"
ICONS = TAURI / "icons"
SIDECAR_DIR = DIST / "sidecar"
SPEC = DESKTOP / "sidecar" / "lynvos.spec"
REPORT = {"started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
          "steps": [], "warnings": [], "artifacts": []}


def step(name: str, fn):
    print(f"\n=== {name} ===", flush=True)
    result = fn()
    REPORT["steps"].append({"name": name, "ok": True, "detail": str(result)[:300]})
    print(f"[OK] {name}: {str(result)[:300]}", flush=True)
    return result


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None,
        timeout: int = 1800) -> str:
    print("  $", " ".join(str(x) for x in cmd), flush=True)
    e = os.environ.copy()
    if env:
        e.update(env)
    r = subprocess.run(cmd, cwd=str(cwd or PROJECT), env=e,
                       capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace")
    if r.stdout:
        print(r.stdout[-2500:], flush=True)
    if r.returncode != 0:
        print(r.stderr[-3000:], flush=True)
        raise RuntimeError(f"命令失败 rc={r.returncode}: {' '.join(map(str, cmd))}")
    return (r.stdout or "").strip()


def preflight() -> dict:
    out = {}
    for name, cmd in (("Python", [sys.executable, "--version"]),
                      ("Rust", ["cargo", "--version"]),
                      ("Node", ["node", "--version"]),
                      ("npm", ["npm", "--version"]),
                      ("FFmpeg", ["ffmpeg", "-version"])):
        try:
            out[name] = run(cmd).splitlines()[0][:80]
        except Exception as exc:
            out[name] = f"缺失：{type(exc).__name__}"
            REPORT["warnings"].append(f"{name} 不可用：{exc}")
    try:
        from PIL import Image  # noqa: F401
        out["Pillow"] = "已安装"
    except ImportError:
        out["Pillow"] = "缺失"
        REPORT["warnings"].append("Pillow 缺失，图标生成会失败")
    base = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) \
        / "Microsoft" / "EdgeWebView" / "Application"
    out["WebView2"] = "已安装" if base.is_dir() and any(base.glob("*/msedgewebview2.exe")) \
        else "未检测到（安装后首次启动会自动引导）"
    out["Python"] = f"{sys.version.split()[0]} @ {sys.executable}"
    return out


def gen_icons() -> dict:
    from PIL import Image, ImageDraw, ImageFont
    ICONS.mkdir(parents=True, exist_ok=True)
    size = 512
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for y in range(size):
        t = y / (size - 1)
        d.line([(0, y), (size, y)], fill=(int(16 + 20 * t), int(40 + 46 * t),
                                          int(62 + 66 * t), 255))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=112, fill=255)
    img.putalpha(mask)
    d = ImageDraw.Draw(img)
    d.arc((58, 58, 454, 454), 198, 342, fill=(255, 190, 64, 255), width=28)
    d.arc((92, 92, 420, 420), 18, 162, fill=(255, 255, 255, 90), width=12)

    def font(px: int):
        for p in ("C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc",
                  "/System/Library/Fonts/PingFang.ttc",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
            try:
                return ImageFont.truetype(p, px)
            except OSError:
                continue
        return ImageFont.load_default()

    d.text((256, 232), "LY", anchor="mm", font=font(196), fill=(255, 255, 255, 255))
    d.text((256, 362), "LYNVOS FIT", anchor="mm", font=font(44), fill=(226, 236, 246, 255))
    img.save(ICONS / "icon.ico",
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                    (128, 128), (256, 256)])
    for s in (32, 128, 256):
        img.resize((s, s), Image.LANCZOS).save(ICONS / f"{s}x{s}.png")
    img.resize((256, 256), Image.LANCZOS).save(ICONS / "128x128@2x.png")
    return {"icons": sorted(p.name for p in ICONS.iterdir() if p.is_file())}


def ensure_pyinstaller(with_whisper: bool, console: bool) -> dict:
    try:
        import PyInstaller  # noqa: F401
        ver = PyInstaller.__version__
    except ImportError:
        run([sys.executable, "-m", "pip", "install", "pyinstaller==6.20.0"])
        import PyInstaller  # noqa: F401
        ver = PyInstaller.__version__
    env = {"LYNVOS_WITH_WHISPER": "1" if with_whisper else "0",
           "LYNVOS_CONSOLE": "1" if console else "0"}
    return {"pyinstaller": ver, "with_whisper": with_whisper, "console": console,
            "env": env}


def build_sidecar(env: dict) -> dict:
    DIST.mkdir(parents=True, exist_ok=True)
    run([sys.executable, "-m", "PyInstaller", "--noconfirm",
         "--distpath", str(DIST), "--workpath", str(DESKTOP / "build" / "pyi"),
         str(SPEC)], env=env)
    exe = SIDECAR_DIR / "lynvos-sidecar.exe"
    if not exe.is_file():
        raise RuntimeError(f"未产出 {exe}")
    web = list(SIDECAR_DIR.rglob("index.html"))
    if not web:
        raise RuntimeError("sidecar 里没有 web/index.html，安装后界面会空白")
    total = sum(p.stat().st_size for p in SIDECAR_DIR.rglob("*") if p.is_file())
    return {"exe": str(exe), "web_index": str(web[0]),
            "files": sum(1 for p in SIDECAR_DIR.rglob("*") if p.is_file()),
            "bytes": total, "mib": round(total / 1024 / 1024, 1)}


def build_tauri() -> dict:
    if not (DESKTOP / "node_modules" / ".bin").exists():
        run(["npm", "install"], cwd=DESKTOP)
    run(["npm", "run", "build"], cwd=DESKTOP, timeout=3600)
    return {"bundle_dir": str(TAURI / "target" / "release" / "bundle" / "nsis")}


def collect() -> dict:
    bundles = sorted((TAURI / "target" / "release" / "bundle" / "nsis").glob("*-setup.exe"))
    if not bundles:
        raise RuntimeError("未找到 NSIS 安装包 *-setup.exe")
    src = bundles[-1]
    dst = DIST / "凌扬运动工作台_1.0.0_x64-setup.exe"
    shutil.copy2(src, dst)
    REPORT["artifacts"] = [
        {"path": str(SIDECAR_DIR.relative_to(PROJECT)), "mib": round(
            sum(p.stat().st_size for p in SIDECAR_DIR.rglob("*") if p.is_file()) / 1048576, 1)},
        {"path": str(dst.relative_to(PROJECT)), "mib": round(dst.stat().st_size / 1048576, 1)},
    ]
    return {"installer": str(dst), "mib": round(dst.stat().st_size / 1048576, 1),
            "source": str(src)}


def forbidden_in_bundle() -> list[str]:
    """清单门禁：安装包里绝不能出现抖音抓取留下的大缓存/探针，
    以及 data/baseline 下的真实素材（GPT 复核 2026-11-10 第 3 项：
    真实 baseline 只活在本机，包里只许有 examples/baseline 示例文件）。"""
    bad = ("browser-profile", "_cdp_probe", "_pipe_probe", "data/runs",
           "tmp_offline_probe", "voice_previews", "cmd.json", "data/baseline")
    hits = []
    for p in SIDECAR_DIR.rglob("*"):
        rel = p.relative_to(SIDECAR_DIR).as_posix()
        if any(b in rel for b in bad):
            hits.append(rel)
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="构建凌扬运动桌面安装包")
    ap.add_argument("--with-whisper", action="store_true", help="打全量离线包")
    ap.add_argument("--console", action="store_true", help="sidecar 保留控制台")
    ap.add_argument("--skip-sidecar", action="store_true")
    ap.add_argument("--skip-tauri", action="store_true")
    args = ap.parse_args()

    REPORT["options"] = vars(args)
    try:
        step("环境自检", preflight)
        step("生成品牌图标", gen_icons)
        if not args.skip_sidecar:
            env = step("准备 PyInstaller", lambda: ensure_pyinstaller(
                args.with_whisper, args.console))["env"]
            step("打包 Python 内核", lambda: build_sidecar(env))
            leaks = forbidden_in_bundle()
            REPORT["forbidden_in_bundle"] = leaks
            if leaks:
                raise RuntimeError(f"安装包混入了被禁止的缓存：{leaks[:5]}")
        if not args.skip_tauri:
            step("打包 Tauri 桌面壳", build_tauri)
            step("收集安装包", collect)
        REPORT["ok"] = True
    except Exception as exc:
        REPORT["ok"] = False
        REPORT["error"] = f"{type(exc).__name__}: {exc}"
        print(f"\n[FAIL] {REPORT['error']}", flush=True)
    finally:
        REPORT["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        DIST.mkdir(parents=True, exist_ok=True)
        (DIST / "BUILD_REPORT.json").write_text(
            json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n构建读数：", json.dumps({k: REPORT[k] for k in
              ("ok", "artifacts", "warnings", "forbidden_in_bundle")
              if k in REPORT}, ensure_ascii=False), flush=True)
    return 0 if REPORT.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
