"""成片合成器（平台核心补齐件）。

全库没有现成的「多画面 + 字幕 + 配音 + BGM → 竖屏 MP4」脚本，本模块自建：
  PNG 分镜 → Ken Burns 推镜 → H.264 静音片段 → concat
  WAV 配音 → Python 精确时间轴拼接 → 程序化 BGM 混音
  音视频一次封装 → 1080x1920 MP4 + SRT + 封面
全程无第三方剪辑软件、无需人工导出，可无人值守。
"""
from __future__ import annotations

import array
import math
import wave
from datetime import datetime
from pathlib import Path

from .util import ROOT, OUTPUT, ffmpeg, run_cmd, ffprobe_duration, write_json, write_text

SR = 48000


# ---------------------------------------------------------------- 音频轨

def _wav_frames(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes()), w.getnframes()


def _build_soundtrack(voices: list[dict], workdir: Path, cfg: dict) -> tuple[Path, float]:
    """按配音真实时长排时间轴，段间插入留白，再混入低音量程序化 BGM。"""
    pad = cfg["video"]["pad_seconds"]
    min_dur = cfg["video"]["min_scene_seconds"]
    timeline, cursor = [], 0.0
    for v in voices:
        d = max(v["duration"], min_dur)
        timeline.append({"index": v["index"], "start": cursor, "voice": v["audio"],
                         "voice_dur": v["duration"], "scene_dur": d})
        cursor += d + pad
    total = cursor

    frames = array.array("h")
    for seg in timeline:
        raw, n = _wav_frames(Path(seg["voice"]))
        a = array.array("h")
        a.frombytes(raw)
        frames.extend(a)
        target = int(round((seg["scene_dur"] + pad) * SR))
        if len(a) < target:
            frames.extend(array.array("h", [0]) * (target - len(a)))
        else:
            # 极端情况下配音长于预估，保留完整配音
            pass

    # 程序化 BGM：低频和弦垫音 + 四四拍轻脉冲，音量远低于口播
    bgm_files = sorted((ROOT / "assets" / "bgm").glob("*.wav"))
    if bgm_files:
        raw, n = _wav_frames(bgm_files[0])
        bgm = array.array("h")
        bgm.frombytes(raw)
        if len(bgm) < len(frames):
            reps = math.ceil(len(frames) / max(1, len(bgm)))
            bgm = (bgm * reps)[:len(frames)]
        else:
            bgm = bgm[:len(frames)]
        source = bgm
    else:
        source = array.array("h", [0]) * len(frames)
        chord = (55.0, 110.0, 164.81, 220.0)
        beat = 0.5
        for i in range(len(frames)):
            t = i / SR
            env = 0.65 + 0.35 * math.sin(2 * math.pi * 0.07 * t)
            s = sum(math.sin(2 * math.pi * f * t) for f in chord) / len(chord)
            phase = t % beat
            kick = math.exp(-phase * 26) * math.sin(2 * math.pi * 62 * t) * 0.45
            hat = math.exp(-phase * 90) * math.sin(2 * math.pi * 7400 * t) * 0.10
            source[i] = int(max(-1.0, min(1.0, (s * env * 0.5 + kick + hat) * 0.5)) * 12000)
    # 用 ffmpeg 混音更省事：这里直接把 BGM 以低增益叠加到人声
    gain = cfg["video"].get("bgm_volume", 0.10)
    mixed = array.array("h", [0]) * len(frames)
    for i in range(len(frames)):
        val = frames[i] + int(source[i] * gain)
        mixed[i] = max(-32768, min(32767, val))

    out = workdir / "soundtrack.wav"
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(mixed.tobytes())
    return out, total


# ---------------------------------------------------------------- 视频轨

def _render_clip(img: Path, duration: float, index: int, out: Path,
                 workdir: Path, cfg: dict, run=None) -> None:
    w, h, fps = cfg["video"]["width"], cfg["video"]["height"], cfg["video"]["fps"]
    frames = max(1, int(duration * fps))
    fade = f"fade=t=in:st=0:d=0.18"
    common = ["-framerate", str(fps), "-loop", "1", "-t", f"{duration:.3f}", "-i", str(img)]

    if cfg["video"].get("ken_burns", True):
        # zoompan 的 z 用 on（输出帧序号）线性推进，避免表达式内逗号转义
        z = f"1+0.085*on/{frames}"
        vf = (f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,"
              f"crop={w * 2}:{h * 2},"
              f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
              f":d=1:s={w}x{h}:fps={fps},{fade},format=yuv420p")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + common + [
            "-vf", vf, "-frames:v", str(frames), "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-an", str(out)]
        r = run_cmd(cmd, timeout=300)
        if r.returncode == 0 and out.exists() and out.stat().st_size > 5000:
            return
        if run:
            run.log(f"分镜 {index} 推镜渲染失败，回退静态画面：{r.stderr[-160:]}", "WARN")

    vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
          f"fps={fps},{fade},format=yuv420p")
    ffmpeg(common + ["-vf", vf, "-frames:v", str(frames), "-c:v", "libx264",
                     "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
                     "-an", str(out)], timeout=300)


def _concat_clips(clips: list[Path], out: Path, workdir: Path, run=None) -> None:
    listfile = workdir / "concat.txt"
    listfile.write_text("".join("file '" + str(p).replace("\\", "/") + "'\n" for p in clips),
                        encoding="utf-8")
    r = run_cmd(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
                 "-safe", "0", "-i", str(listfile), "-c", "copy", str(out)], timeout=300)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"片段拼接失败: {r.stderr[-500:]}")


# ---------------------------------------------------------------- 字幕 / 元数据

def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _build_srt(script: dict, timeline: list[dict]) -> str:
    parts = []
    for seg, scene in zip(timeline, script["scenes"]):
        end = seg["start"] + max(seg["voice_dur"], 0.5)
        parts.append(f"{len(parts) + 1}\n{_srt_time(seg['start'])} --> {_srt_time(end)}\n"
                     f"{scene['narration']}\n")
    return "\n".join(parts)


def _slug(title: str) -> str:
    keep = "".join(c for c in title if c.isalnum() or "\u4e00" <= c <= "\u9fff")
    return keep[:14] or "video"


# ---------------------------------------------------------------- 主编排

def compose(script: dict, voices: list[dict], images: list[Path], cover: Path,
            cfg: dict, run, workdir: Path, plan: list[dict] | None = None,
            overlays: list[Path] | None = None) -> dict:
    pad = cfg["video"]["pad_seconds"]
    min_dur = cfg["video"]["min_scene_seconds"]
    timeline, cursor = [], 0.0
    for v in voices:
        d = max(v["duration"], min_dur)
        timeline.append({"index": v["index"], "start": cursor,
                         "voice_dur": v["duration"], "scene_dur": d})
        cursor += d + pad

    soundtrack, total = _build_soundtrack(voices, workdir, cfg)
    run.log(f"音轨构建完成：{total:.1f}s，分镜时长 {[round(t['scene_dur'], 1) for t in timeline]}")

    use_real = (bool(plan) and bool(overlays) and len(plan) >= len(timeline)
                and len(overlays) >= len(timeline)
                and all(p.get("source") and Path(p["source"]).is_file()
                        for p in plan[:len(timeline)]))
    if run:
        run.log("剪辑模式：" + ("真实实拍视频裁切 + 文字叠加" if use_real
                              else "静态卡片兜底（素材库为空或素材不可用）"))
        run.step("素材匹配", "done",
                 f"{len(timeline)} 个分镜 | {'真实视频 ' + str(len(plan)) + ' 段' if use_real else '无真实素材'}")

    clips = []
    for i, (seg, img) in enumerate(zip(timeline, images)):
        d = seg["scene_dur"] + pad
        clip = workdir / f"clip_{seg['index']:02d}.mp4"
        if use_real:
            from .clips import render_clip
            render_clip(plan[i], d, overlays[i], clip, cfg, run)
        else:
            _render_clip(img, d, seg["index"], clip, workdir, cfg, run)
        clips.append(clip)
        run.log(f"分镜 {seg['index']}/{len(timeline)} "
                f"{'实拍裁切' if use_real else '卡片渲染'}完成 {d:.2f}s")
    if len(clips) != len(images):
        raise RuntimeError("分镜数与画面数不一致")

    joined = workdir / "joined.mp4"
    _concat_clips(clips, joined, workdir, run)

    date = datetime.now().strftime("%Y%m%d")
    slug = _slug(script["title"])
    stem = f"{date}_{slug}"
    OUTPUT.mkdir(parents=True, exist_ok=True)
    final = OUTPUT / f"{stem}.mp4"

    ffmpeg(["-i", str(joined), "-i", str(soundtrack),
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
            "-b:a", "192k", "-ar", "48000", "-shortest", "-movflags", "+faststart",
            str(final)], timeout=300)

    srt_path = OUTPUT / f"{stem}.srt"
    write_text(srt_path, _build_srt(script, timeline))
    cover_out = OUTPUT / f"{stem}_cover.png"
    cover_out.write_bytes(Path(cover).read_bytes())
    meta = {
        "title": script["title"], "caption": script["caption"],
        "cover_text": script.get("cover_text", ""), "generator": script["generator"],
        "topic": script.get("topic", {}), "compliance": script.get("compliance", {}),
        "scenes": [{"index": s["index"], "narration": s["narration"],
                    "on_screen": s["on_screen"], "visual_prompt": s.get("visual_prompt", "")}
                   for s in script["scenes"]],
        "timeline": timeline, "video": str(final), "srt": str(srt_path),
        "cover": str(cover_out),
        "material_mode": "real_video" if use_real else "static_card",
        "clip_plan": [{"index": p.get("index"), "name": p.get("name", ""),
                       "label": p.get("label", ""), "start": p.get("start", 0)}
                      for p in (plan or [])[:len(timeline)]],
        "duration": round(ffprobe_duration(final), 2),
        "size_mb": round(final.stat().st_size / 1024 / 1024, 2),
        "resolution": f"{cfg['video']['width']}x{cfg['video']['height']}",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    write_json(OUTPUT / f"{stem}.json", meta)
    run.save("result.json", meta)
    run.step("成片输出", "done",
             f"{final.name} | {meta['duration']}s | {meta['size_mb']}MB | {meta['resolution']}")
    return meta
