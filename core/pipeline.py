"""流水线编排（两阶段工作台）。

prepare：热点 → 选题 → 脚本 → 合规 → 配音 → 真实素材匹配 → 分镜方案 plan.json（停住待人工审核）
render ：按审核通过的方案裁切实拍视频 → 叠加文字 → 配音/BGM → 成片

对标 chopify 的 `--action prepare / render` 两阶段、NarratoAI 的视频审查界面：
AI 给初稿，人工可逐镜换片、改画面文案，确认后才渲染，避免「一键出片但没法改」。
"""
from __future__ import annotations

import traceback
from datetime import datetime
from pathlib import Path

from .util import Run, load_config, read_json, write_json
from .hotspots import collect
from .topics import build_topics
from .llm import generate_scripts
from .voice import synthesize_scenes
from .assets import build_scene_images, make_overlay, make_cover
from .compose import compose
from .materials import (scan_library, match_materials, ensure_demo_clips, local_mode,
                        load_plan, save_plan)
from .secrets import public_state


def _plan_path(run: Run) -> Path:
    return run.dir / "plan.json"


def prepare(cfg: dict | None = None, count: int = 1, run: Run | None = None,
            use_demo: bool | None = None) -> dict:
    """只产出分镜方案，不渲染。用户可在控制台换片/改文案后再 render。"""
    cfg = cfg or load_config()
    run = run or Run()
    run.log(f"=== 准备阶段启动 | 品牌 {cfg['brand']['name']} | 真实素材剪辑 ===")
    result: dict = {"run_id": run.id, "videos": [], "ok": False, "phase": "prepare"}
    try:
        hot = collect(cfg, run)
        result["hotspots"] = {"unique": hot["unique"], "fitness": len(hot["fitness_related"]),
                              "sources": hot["sources"]}
        topics = build_topics(hot, cfg, run, count=max(count, 1))
        scripts = generate_scripts(topics, cfg, run)
        run.save("scripts_final.json", scripts)
        result["topics"] = [t["title"] for t in topics]

        # 本地模式默认禁止联网补素材：只用项目内 + 本机登记文件夹里的实拍
        mcfg = cfg.get("materials", {})
        auto = mcfg.get("auto_demo", True) if use_demo is None else use_demo
        offline = local_mode()
        if offline:
            run.log("本地模式：素材仅来自项目内目录与本机登记文件夹，不会联网下载")
        library = scan_library()
        if auto and not offline and not library:
            r = ensure_demo_clips(run)
            run.log(f"演示素材入库 {r['ok']} 条，失败 {len(r['failed'])} 条")
            library = scan_library()
        run.log(f"素材库索引：{len(library)} 个文件，其中视频 "
                f"{sum(1 for x in library if x['kind'] == 'video')} 条")
        result["library"] = library

        plans = []
        for i, script in enumerate(scripts, 1):
            workdir = run.dir / f"video_{i:02d}"
            workdir.mkdir(parents=True, exist_ok=True)
            run.log(f"--- 准备第 {i}/{len(scripts)} 条：{script['title']} ---")
            clips = match_materials(script, cfg, library)
            has_real = all(c["source"] for c in clips)
            voices = synthesize_scenes(script, workdir, cfg, run)
            cover_photo = None
            if has_real and clips and clips[0].get("source"):
                from .clips import thumbnail
                cover_photo = thumbnail(Path(clips[0]["source"]),
                                        workdir / "cover_frame.jpg", second=0.6,
                                        w=cfg["video"]["width"], h=cfg["video"]["height"])
            images, cover, overlays = build_scene_images(
                script, cfg, workdir, run, static=not has_real, cover_photo=cover_photo)
            p = {
                "run_id": run.id, "index": i, "created_at": datetime.now().isoformat(timespec="seconds"),
                "status": "prepared", "script": script, "voices": voices, "clips": clips,
                "library": library, "images": [str(x) for x in images],
                "overlays": [str(x) for x in overlays], "cover": str(cover),
                "workdir": str(workdir), "has_real": has_real,
                "material_mode": "real_video" if has_real else "static_card",
                "secret_state": [{"name": s["name"], "configured": s["configured"]}
                                 for s in public_state()],
            }
            save_plan(run.dir / f"plan_{i:02d}.json", p)
            plans.append(p)
            run.log(f"方案 {i}：{'真实实拍 ' + str(len(clips)) + ' 段' if has_real else '静态卡片兜底'}")

        save_plan(_plan_path(run), plans[0])
        run.step("分镜方案", "done",
                 f"{len(scripts)} 份方案 | 首条素材模式 {plans[0]['material_mode']} | 待人工审核")
        result.update(ok=True, plans=plans, plan=plans[0], scripts=scripts)
        write_json(run.dir / "result_all.json", result)
        return result
    except Exception as e:
        run.log(f"准备阶段失败：{type(e).__name__}: {e}", "ERROR")
        run.log(traceback.format_exc()[-1600:], "ERROR")
        run.step("分镜方案", "failed", f"{type(e).__name__}: {str(e)[:180]}")
        result["error"] = f"{type(e).__name__}: {e}"
        write_json(run.dir / "result_all.json", result)
        return result


def render(run: Run, plan: dict | None = None, cfg: dict | None = None) -> dict:
    """按（可能被人工改过的）方案渲染成片。"""
    cfg = cfg or load_config()
    p = plan or load_plan(_plan_path(run))
    if not p:
        raise RuntimeError("找不到分镜方案，请先执行准备阶段")
    script, workdir = p["script"], Path(p["workdir"])
    workdir.mkdir(parents=True, exist_ok=True)
    run.log(f"=== 渲染阶段启动 | {script['title']} | {p.get('material_mode')} ===")

    # 画面文案被人工改过 → 重绘透明文字层；首镜素材可能被换过 → 同步重绘实拍封面
    overlays = []
    for s in script["scenes"]:
        overlays.append(make_overlay(s, cfg, s["index"], workdir, len(script["scenes"])))
    cover_photo = None
    if p.get("has_real") and p.get("clips") and p["clips"][0].get("source"):
        from .clips import thumbnail
        cover_photo = thumbnail(Path(p["clips"][0]["source"]), workdir / "cover_frame.jpg",
                                second=0.6, w=cfg["video"]["width"], h=cfg["video"]["height"])
    p["cover"] = str(make_cover(script, cfg, workdir, cover_photo))

    # 口播被改过 → 重新配音（时间轴随配音真实时长重算）
    orig = load_plan(run.dir / f"plan_{p.get('index', 1):02d}.json") or p
    changed = any(a.get("narration") != b.get("narration")
                  for a, b in zip(orig["script"]["scenes"], script["scenes"]))
    if changed:
        run.log("检测到口播文案修改，重新合成配音")
        voices = synthesize_scenes(script, workdir, cfg, run)
    else:
        voices = p["voices"]

    images = [Path(x) for x in overlays] if p.get("has_real") else [Path(x) for x in p["images"]]
    meta = compose(script, voices, images, Path(p["cover"]), cfg, run, workdir,
                   plan=p["clips"] if p.get("has_real") else None,
                   overlays=overlays if p.get("has_real") else None)
    p["status"] = "rendered"
    p["result"] = meta
    save_plan(_plan_path(run), p)
    return {"ok": True, "run_id": run.id, "videos": [meta], "phase": "render"}


def render_from_disk(run_id: str, plan_override: dict | None = None,
                     cfg: dict | None = None) -> dict:
    run = Run(run_id)
    return render(run, plan_override, cfg)


def run_pipeline(cfg: dict | None = None, count: int = 1, run: Run | None = None,
                 max_steps: str = "all") -> dict:
    """一键全流程（CLI / 定时任务用）；控制台可改用 prepare → 审核 → render。"""
    cfg = cfg or load_config()
    run = run or Run()
    if max_steps == "prepare":
        return prepare(cfg=cfg, count=count, run=run)
    if max_steps in ("hot", "topics", "scripts"):
        return _legacy_partial(cfg, count, run, max_steps)

    prep = prepare(cfg=cfg, count=count, run=run)
    if not prep.get("ok"):
        return prep
    out = {"run_id": run.id, "videos": [], "ok": True, "phase": "all",
           "hotspots": prep.get("hotspots"), "topics": prep.get("topics")}
    try:
        for p in prep["plans"]:
            r = render(run, p, cfg)
            out["videos"].extend(r["videos"])
        run.log(f"=== 流水线完成 | {len(out['videos'])} 条成片 ===")
        write_json(run.dir / "result_all.json", out)
        return out
    except Exception as e:
        run.log(f"渲染阶段失败：{type(e).__name__}: {e}", "ERROR")
        run.log(traceback.format_exc()[-1600:], "ERROR")
        run.step("成片输出", "failed", f"{type(e).__name__}: {str(e)[:180]}")
        out.update(ok=False, error=f"{type(e).__name__}: {e}")
        write_json(run.dir / "result_all.json", out)
        return out


def _legacy_partial(cfg, count, run, stop):
    """保留 --step hot/topics/scripts 的调试入口。"""
    result = {"run_id": run.id, "videos": [], "ok": False}
    try:
        hot = collect(cfg, run)
        result["hotspots"] = {"unique": hot["unique"], "fitness": len(hot["fitness_related"])}
        if stop == "hot":
            result["ok"] = True
            return result
        topics = build_topics(hot, cfg, run, count=max(count, 1))
        result["topics"] = [t["title"] for t in topics]
        if stop == "topics":
            result["ok"] = True
            return result
        scripts = generate_scripts(topics, cfg, run)
        run.save("scripts_final.json", scripts)
        result.update(ok=True, scripts=scripts)
        return result
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result
