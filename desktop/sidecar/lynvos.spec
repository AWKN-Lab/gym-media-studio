# -*- mode: python ; coding: utf-8 -*-
"""凌扬运动 LYNVOS FIT · PyInstaller 构建描述。

由 desktop/build.py 调用（`python desktop/build.py [--with-whisper] [--console]`），
产物进 desktop/dist/sidecar/（COLLECT 名须与 Tauri resources 对齐，见下）。

纪律（审核 H2 钉死，GPT 复核 2026-11-10 第 3 项收紧）：
  - datas 只写显式白名单，绝不写整包 data/ —— data/ 里有 864MB 抖音浏览器
    profile / CDP 探针 / 历史成片，打进去安装包就废了。
  - data/baseline 整个目录永不打包：里面有 gitignored 的真实素材与经营数据，
    打进去等于把本机真实数据塞进发布物。方法库只打包 examples/baseline 下
    4 个示例文件；运行时缺真实文件时，各加载器回退到示例文件
    （core/common_materials 回退 + is_example 标记；playbook / offer 的
    method_source / benchmark_source / presets_source 在配置里本就指向示例）。
  - 精简包默认排除 faster-whisper（重依赖）；全量离线包用 --with-whisper 打开，
    build.py 会把 LYNVOS_WITH_WHISPER=1 传进来。
  - 配置只带 config.example.json（可入库）；新装没有 config.json 时，
    core.util.load_config() 会回退到它，装完就能跑，用户在界面保存后才落自己的
    config.json。config.json 本身永不入库。
  - 控制台窗口由 LYNVOS_CONSOLE=1 打开（排障用），默认不带。
"""

import os
from pathlib import Path

PROJECT = Path(SPECPATH).resolve().parents[1]

# 白名单：只打包可入库的示例数据文件。data/baseline 绝不出现在这里。
EXAMPLE_BASELINE = [
    "method_playbook.example.json",
    "local_benchmark.example.json",
    "offer_presets.example.json",
    "common_materials.example.json",
]

datas = [
    (str(PROJECT / "config.example.json"), "."),
    (str(PROJECT / "web" / "index.html"), "web"),
    (str(PROJECT / "assets"), "assets"),
] + [(str(PROJECT / "examples" / "baseline" / name), "examples/baseline")
     for name in EXAMPLE_BASELINE]

_missing = [str(p) for p in (PROJECT / "config.example.json",
                             PROJECT / "web" / "index.html",
                             PROJECT / "assets",
                             *[PROJECT / "examples" / "baseline" / name
                               for name in EXAMPLE_BASELINE]) if not p.exists()]
if _missing:
    raise SystemExit("spec 缺源文件（先确认仓库完整）：" + "、".join(_missing))

# 精简包：faster-whisper 是重依赖，默认排除；全量离线包用 --with-whisper 打开。
excludes = []
if os.environ.get("LYNVOS_WITH_WHISPER", "0") != "1":
    excludes = ["faster_whisper"]

a = Analysis(
    [str(PROJECT / "desktop" / "sidecar" / "app_entry.py")],
    pathex=[str(PROJECT)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="lynvos-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=os.environ.get("LYNVOS_CONSOLE", "0") == "1",
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="sidecar",
)
