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


def _gain_envelope(timeline: list[dict], total: float, base_gain: float,
                   duck_gain: float, step: float = 0.05,
                   ramp: float = 0.15) -> list[float]:
    """BGM 让位包络：人声段压到 duck_gain，段间留白回到 base_gain，边界 0.15s 淡入淡出。

    替代原来全程固定低音量。timeline 元素含 start / voice_dur（人声实际时长）。
    返回每 step 秒一个增益值（供 _build_soundtrack 按采样点查表，避免逐采样算区间）。
    """
    n = max(1, int(total / step) + 1)
    env = [base_gain] * n
    for seg in timeline:
        vs = float(seg["start"])
        ve = vs + float(seg.get("voice_dur", 0) or 0)
        if ve <= vs:
            continue
        for k in range(n):
            t = k * step
            if vs - ramp <= t < vs:
                f = (t - (vs - ramp)) / ramp
                env[k] = min(env[k], base_gain + (duck_gain - base_gain) * f)
            elif vs <= t < ve:
                env[k] = min(env[k], duck_gain)
            elif ve <= t < ve + ramp:
                f = (t - ve) / ramp
                env[k] = min(env[k], duck_gain + (base_gain - duck_gain) * f)
    return env


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
    # BGM 让位：人声段压低、段间留白恢复，替代原来全程固定低音量。
    # cfg["video"]["bgm_volume"] 为留白段音量，bgm_duck_volume 为人声段音量。
    gain = cfg["video"].get("bgm_volume", 0.10)
    duck_gain = cfg["video"].get("bgm_duck_volume", 0.03)
    envelope = _gain_envelope(timeline, total, gain, duck_gain)
    step = 0.05
    mixed = array.array("h", [0]) * len(frames)
    for i in range(len(frames)):
        g = envelope[min(int((i / SR) / step), len(envelope) - 1)]
        val = frames[i] + int(source[i] * g)
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
        # zoompan 的 z 用 on（输出帧序号）线性推进，避免表达式内逗号转义。
        # 推镜方向按分镜奇偶轮换（奇数推进、偶数拉回），避免每镜都是同一个推镜。
        if index % 2 == 1:
            z = f"1+0.085*on/{frames}"
        else:
            z = f"1.085-0.085*on/{frames}"
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


def _split_clauses(text: str, limit: int = 8) -> list[str]:
    """把口播按标点断成短句，过长的短句再按字数硬切（不断词义、只断长度）。

    字幕逐词卡点的基础：每条不超过 limit 字，念起来一句一卡。
    """
    import re as _re
    parts = [p for p in _re.split(r"[，。！？；、\n]", text) if p.strip()]
    out: list[str] = []
    for p in parts:
        p = p.strip()
        while len(p) > limit:
            out.append(p[:limit])
            p = p[limit:]
        if p:
            out.append(p)
    return out or [text]


def _distribute(duration: float, weights: list[float]) -> list[tuple[float, float]]:
    """按权重把 duration 切成不重叠、不越界的时间段（相对本镜起点）。

    每段至少 0.3 秒（字幕太短看不清），但总时长不够分时下限自动让位、
    按比例切；最后一段结尾强制等于总时长，消化舍入余量。
    返回 [(start, end)]，相邻两段首尾相接、末段结尾恒等于 duration。
    """
    n = len(weights)
    if n == 0 or duration <= 0:
        return []
    total = sum(weights) or 1.0
    floor = 0.3 if duration >= 0.3 * n else 0.0
    raw = [max(floor, duration * w / total) for w in weights]
    over = sum(raw) - duration
    if over > 0:
        # 从超出下限的部分按比例扣回，下限不动
        slack = [r - floor for r in raw]
        s = sum(slack) or 1.0
        raw = [r - over * sl / s for r, sl in zip(raw, slack)]
    out, cursor = [], 0.0
    for d in raw[:-1]:
        out.append((round(cursor, 3), round(cursor + d, 3)))
        cursor = round(cursor + d, 3)
    out.append((round(cursor, 3), round(duration, 3)))
    return out


def _word_timings(narration: str, start: float, duration: float,
                  words: list[dict] | None = None) -> list[tuple[float, float, str]]:
    """给一句口播算逐词时间轴。

    voices 里若带有真实词级时间戳（words: [{text, start, end}]，相对本镜起点），
    直接用，但先夹到 [0, duration] 区间内：完全越界的丢弃，越界部分截断，
    与上一条重叠的起点推到上一条结尾（GPT 第二次复核第 5 节第 3 项）；
    否则按字数比例估算（faster-whisper 转写时间戳可替换这份估算，
    见 core/transcribe.py 的 segments）。返回 [(start, end, text)]，
    所有条目起止都在 [start, start+duration] 内、不重叠、末段结尾恒等于
    start+duration。
    """
    if words:
        rel = []
        for w in words:
            try:
                ws, we = float(w["start"]), float(w["end"])
                text = str(w["text"])
            except (KeyError, TypeError, ValueError):
                continue
            ws = max(0.0, min(ws, duration))
            we = max(0.0, min(we, duration))
            if we <= ws:
                continue
            if rel and ws < rel[-1][1]:
                ws = rel[-1][1]
                if we <= ws:
                    continue
            rel.append((ws, we, text))
        if rel:
            return [(round(start + s, 3), round(start + e, 3), t)
                    for s, e, t in rel]
    clauses = _split_clauses(narration)
    spans = _distribute(duration, [len(c) for c in clauses])
    return [(round(start + s, 3), round(start + e, 3), c)
            for (s, e), c in zip(spans, clauses)]


def _build_srt(script: dict, timeline: list[dict],
               voices: list[dict] | None = None) -> str:
    """逐词字幕：每镜口播按短句切分、按时间轴逐条出字幕，不再整句贴一张。

    仍输出标准 SRT；voices[i] 若带 words 词级时间戳则用真实时间，否则按字数比例估算。
    """
    parts = []
    for n, (seg, scene) in enumerate(zip(timeline, script["scenes"])):
        dur = max(seg["voice_dur"], 0.5)
        words = (voices or [{}] * len(timeline))[n].get("words") if voices else None
        for s, e, text in _word_timings(scene["narration"], seg["start"], dur, words):
            parts.append(f"{len(parts) + 1}\n{_srt_time(s)} --> {_srt_time(e)}\n{text}\n")
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
    subcuts = bool(cfg["video"].get("subcuts", True))
    for i, (seg, img) in enumerate(zip(timeline, images)):
        d = seg["scene_dur"] + pad
        clip = workdir / f"clip_{seg['index']:02d}.mp4"
        if use_real:
            from .clips import render_clip, render_clip_segments
            narration = script["scenes"][i].get("narration", "") if i < len(script["scenes"]) else ""
            if subcuts and d >= 6:
                # 每分镜按配音断句切 2~3 小段，段间推镜方向轮换
                render_clip_segments(plan[i], d, overlays[i], clip, cfg, run,
                                     narration=narration)
            else:
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
    write_text(srt_path, _build_srt(script, timeline, voices))
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
