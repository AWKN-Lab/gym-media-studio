#!/usr/bin/env python3
"""凌扬运动 LYNVOS FIT · 自媒体自动化工作台

用法：
  python run.py                      # 全流程跑 1 条，直接产出 MP4
  python run.py --count 3            # 一次产出 3 条
  python run.py --step hot           # 只采集热点
  python run.py --step scripts       # 到脚本为止（不出片）
  python run.py --step prepare       # 只出分镜方案（人工审核后再渲染）
  python run.py --render <run_id>    # 按已审核的 plan.json 渲染成片
  python run.py --serve              # 本机控制台 http://127.0.0.1:8787（免登录）
  python run.py --serve --host 0.0.0.0 --users "admin:密码:admin,同事:密码"
                                     # 团队/公网部署：多用户口令 + 权限分级
  python run.py --probe              # 环境自检
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.util import (ROOT as CORE_ROOT, load_config, read_json, DATA, OUTPUT,
                       font_path, run_cmd, http_get_json)
from core.pipeline import run_pipeline
from core.llm import available_providers


def probe() -> None:
    print("\n=== 凌扬运动 LYNVOS FIT 环境自检 ===")
    checks = []
    for name, cmd in [("Python", [sys.executable, "--version"]),
                      ("FFmpeg", ["ffmpeg", "-version"]),
                      ("FFprobe", ["ffprobe", "-version"])]:
        try:
            r = run_cmd(cmd, timeout=20)
            first = (r.stdout or r.stderr).splitlines()[0][:70]
            checks.append((name, True, first))
        except Exception as e:
            checks.append((name, False, str(e)[:70]))
    try:
        from PIL import Image
        checks.append(("Pillow", True, Image.__version__))
    except Exception as e:
        checks.append(("Pillow", False, str(e)[:70]))
    checks.append(("中文字体", Path(font_path()).exists(), font_path()))
    try:
        http_get_json("https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/", timeout=10)
        checks.append(("抖音热榜", True, "可达"))
    except Exception as e:
        checks.append(("抖音热榜", False, str(e)[:70]))
    from core.secrets import public_state
    from core.voice import sapi_catalog
    zh = [x for x in sapi_catalog(force=True) if x.get("zh")]
    checks.append(("完全离线配音", bool(zh),
                   f"本机 {len(zh)} 个中文语音包（断网可用）" if zh
                   else "不可用：需 Windows + comtypes + 系统中文语音包"))
    try:
        import edge_tts  # noqa: F401
        checks.append(("免费在线配音", True, "edge-tts 已安装（需联网到微软）"))
    except ImportError:
        checks.append(("免费在线配音", False, "未安装 edge-tts → 仅离线档可用"))
    from core.materials import scan_library
    lib = scan_library()
    checks.append(("真实视频素材库", bool(lib),
                   f"{sum(1 for x in lib if x['kind']=='video')} 条视频 / {len(lib)} 个文件"))
    checks.append(("已配置密钥项", True,
                   ", ".join(s["name"] for s in public_state() if s["configured"]) or "无"))
    cfg = load_config()
    providers = available_providers(cfg, force=True)
    checks.append(("AI 文案模型", bool(providers),
                   ", ".join(p["name"] for p in providers) if providers else "无 → 自动使用内置模板引擎"))
    for name, ok, detail in checks:
        print(f"  [{'OK ' if ok else 'WARN'}] {name:<18} {detail}")
    print("\n结论：核心链路（热搜/画面/配音/合成）不依赖 AI 文案模型；"
          "配音可选完全离线档（本机语音包）或免费在线档，均不需要任何 Key。\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="凌扬运动 LYNVOS FIT 自媒体自动化工作台")
    ap.add_argument("--count", type=int, default=1, help="一次生成几条视频")
    ap.add_argument("--step", choices=["hot", "topics", "scripts", "prepare", "all"], default="all")
    ap.add_argument("--render", metavar="RUN_ID", help="按指定 run_id 的审核方案渲染成片")
    ap.add_argument("--serve", action="store_true", help="启动 Web 控制台")
    # PORT 是 WorkBuddy「发布为应用」及多数 PaaS 注入的标准变量
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("GYMPULSE_PORT") or os.environ.get("PORT") or 8787))
    ap.add_argument("--host", default=os.environ.get("GYMPULSE_HOST", "127.0.0.1"),
                    help="监听地址，默认 127.0.0.1；对外服务用 0.0.0.0")
    ap.add_argument("--users", default=os.environ.get("GYMPULSE_USERS", ""),
                    help='多用户口令："用户名:密码[:admin],..."，admin 可改密钥/配置/素材')
    ap.add_argument("--allow-insecure", action="store_true",
                    help="对外监听但不鉴权（仅限可信内网）")
    ap.add_argument("--probe", action="store_true", help="环境自检")
    args = ap.parse_args()

    if args.probe:
        probe()
        return
    if args.serve:
        from web.server import serve
        serve(port=args.port, host=args.host, users=args.users,
              allow_insecure=args.allow_insecure)
        return

    cfg = load_config()
    # 端到端耗时 / 结果埋点（客观测量，非效果证明）
    from core.metrics import timer, track
    with timer("run", mode="render" if args.render else "pipeline"):
        if args.render:
            from core.pipeline import render_from_disk
            result = render_from_disk(args.render, cfg=cfg)
        else:
            result = run_pipeline(cfg=cfg, count=args.count, max_steps=args.step)
    if isinstance(result, dict):
        track("run_result", run_id=result.get("run_id", ""),
              videos=len(result.get("videos", [])),
              scripts=len(result.get("scripts", [])),
              ok=bool(result.get("ok")))

    print("\n=== 结果 ===")
    if args.step in ("hot", "topics"):
        print(json.dumps(result, ensure_ascii=False, indent=2)[:1600])
    else:
        for v in result.get("videos", []):
            print(f"  成片: {v['video']}")
            print(f"  时长: {v['duration']}s | 大小: {v['size_mb']}MB | {v['resolution']}")
            print(f"  标题: {v['title']}")
        if result.get("scripts") and not result.get("videos"):
            print(f"  已生成 {len(result['scripts'])} 份脚本")
    if not result.get("ok"):
        print(f"  失败: {result.get('error')}")
        sys.exit(1)
    print(f"\n运行日志: {DATA / 'runs' / result['run_id'] / 'run.log'}")


if __name__ == "__main__":
    main()
