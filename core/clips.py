"""真实视频剪辑器：按分镜时长从实拍原片裁切、竖屏 cover 裁切、去原声、烧录文字层。

对标 NarratoAI `app/services/clip_video.py`（`-ss` 前置快速定位 + `-t` 精确裁切 + `-an`
去原声）与 MoneyPrinterTurbo `video.py:_fit_clip_to_canvas()`（cover 缩放居中裁剪）。
所有输出统一 1080x1920 / 30fps / yuv420p / H.264 / 无音轨，保证 concat -c copy 安全。
"""
from __future__ import annotations

from pathlib import Path

from .util import ffmpeg, ffprobe_duration, run_cmd


def render_clip(item: dict, duration: float, overlay: Path, out: Path,
                cfg: dict, run=None) -> Path:
    """从一条真实视频切出 duration 秒，并叠加该分镜的透明文字层。"""
    src = Path(item["source"])
    if not src.is_file():
        raise RuntimeError(f"素材不存在: {src}")
    w, h, fps = cfg["video"]["width"], cfg["video"]["height"], cfg["video"]["fps"]
    src_dur = float(item.get("clip_duration") or ffprobe_duration(src) or 0)
    start = max(0.0, float(item.get("start", 0) or 0))

    # 素材剩余长度不足时循环播放该片，而不是冻结最后一帧
    loop = src_dur <= 0 or (src_dur - start) < duration + 0.15
    if loop:
        video_inputs = ["-stream_loop", "-1", "-i", str(src)]
    else:
        video_inputs = ["-ss", f"{start:.3f}", "-i", str(src)]
    inputs = video_inputs + ["-loop", "1", "-i", str(overlay)]

    # 竖屏 cover 裁切 + 轻微压暗去饱和，保证白色字幕在实拍画面上可读
    bg = (f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
          f"crop={w}:{h},setsar=1,fps={fps},"
          f"eq=contrast=1.03:brightness=-0.035:saturation=0.94[bg]")
    ov = f"[1:v]format=rgba,scale={w}:{h}[ov]"
    mix = ("[bg][ov]overlay=0:0:shortest=1:format=auto,"
           "fade=t=in:st=0:d=0.18,format=yuv420p[v]")

    cmd = (["-hide_banner", "-loglevel", "error", "-y"] + inputs +
           ["-filter_complex", f"{bg};{ov};{mix}", "-map", "[v]",
            "-t", f"{duration:.3f}", "-an", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
            "-r", str(fps), "-movflags", "+faststart", str(out)])

    r = run_cmd(["ffmpeg"] + cmd, timeout=420)
    if r.returncode != 0 or not out.exists() or out.stat().st_size < 5000:
        # 退路：不叠加文字层也要出真实视频画面（避免整条流水线挂掉）
        if run:
            run.log(f"分镜 {item.get('index')} 叠加渲染失败，改用纯画面裁切：{r.stderr[-180:]}", "WARN")
        simple = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + video_inputs +
                  ["-t", f"{duration:.3f}", "-an",
                   "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                          f"crop={w}:{h},setsar=1,fps={fps},format=yuv420p",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                   "-pix_fmt", "yuv420p", "-r", str(fps), str(out)])
        r2 = run_cmd(simple, timeout=420)
        if r2.returncode != 0:
            raise RuntimeError(f"真实素材裁切失败: {r2.stderr[-400:]}")
    return out


def split_segments(duration: float, narration: str = "") -> list[dict]:
    """把一镜按配音断句切成 2~3 小段（FunClip“连续片段”思路的本地版，不调 LLM）。

    有口播文本时按标点断句、按字数比例分时长；没有时均分。每段至少 1 秒，
    段数：时长 < 8 秒切 2 段，否则 3 段。返回 [{"start_offset", "duration"}]。
    """
    import re as _re
    duration = max(0.0, float(duration))
    if duration <= 0:
        return []
    if duration < 2.0:
        # 凑不够 2 段、每段至少 1 秒：只返回一段，调用方回退整段渲染，
        # 不硬切出负时长或不足 1 秒的段（GPT 第二次复核第 3 节）。
        return [{"start_offset": 0.0, "duration": round(duration, 3)}]
    n = 2 if duration < 8 else 3
    clauses = [p for p in _re.split(r"[，。！？；、\n]", narration or "") if p.strip()]
    if len(clauses) >= 2:
        # 断句数不多于段数时，每句一段；多于段数时按字数均衡合并成 n 个连续组。
        if len(clauses) <= n:
            groups = [[c] for c in clauses]
        else:
            total_chars = sum(len(c) for c in clauses)
            bounds: list[int] = []
            acc = 0
            for idx, c in enumerate(clauses[:-1]):
                acc += len(c)
                if acc >= total_chars * (len(bounds) + 1) / n:
                    bounds.append(idx)
            groups, start = [], 0
            for b in bounds:
                groups.append(clauses[start:b + 1])
                start = b + 1
            groups.append(clauses[start:])
        total = sum(len(c) for g in groups for c in g) or 1
        # 每段至少 1 秒且各段之和恒等于 duration：先按比例算，再把超出
        # 1 秒下限的部分按比例扣回，下限不动（否则长句段会把末段挤成负数）。
        raw = [max(1.0, duration * sum(len(c) for c in g) / total) for g in groups]
        over = sum(raw) - duration
        if over > 0:
            slack = [r - 1.0 for r in raw]
            s = sum(slack) or 1.0
            raw = [r - over * sl / s for r, sl in zip(raw, slack)]
        out, cursor = [], 0.0
        for d in raw[:-1]:
            out.append({"start_offset": round(cursor, 3), "duration": round(d, 3)})
            cursor = round(cursor + d, 3)
        # 最后一组吃掉舍入余量（此时必 ≥ 1 秒）
        out.append({"start_offset": round(cursor, 3),
                    "duration": round(duration - cursor, 3)})
        return out
    per = duration / n
    return [{"start_offset": round(i * per, 3), "duration": round(per, 3)}
            for i in range(n)]


def render_clip_segments(item: dict, duration: float, overlay: Path, out: Path,
                         cfg: dict, run=None, narration: str = "") -> Path:
    """分段渲染：一镜切 2~3 小段，每段推镜方向轮换（推进/拉回交替），再拼成一镜。

    段与段之间用同一编码参数渲染，concat demuxer 直接拼；拼失败时回退到
    整段 render_clip（不断整条流水线）。
    """
    parts = split_segments(duration, narration)
    if len(parts) <= 1:
        return render_clip(item, duration, overlay, out, cfg, run)
    w, h, fps = cfg["video"]["width"], cfg["video"]["height"], cfg["video"]["fps"]
    src = Path(item["source"])
    src_dur = float(item.get("clip_duration") or ffprobe_duration(src) or 0)
    base_start = max(0.0, float(item.get("start", 0) or 0))
    tmpdir = out.parent
    rendered: list[Path] = []
    try:
        for k, part in enumerate(parts):
            seg_dur = part["duration"]
            seg_start = base_start + part["start_offset"]
            loop = src_dur <= 0 or (src_dur - seg_start) < seg_dur + 0.15
            # 循环输入也要先 -ss 到本段起点再循环：只写 -stream_loop 会从
            # 源文件 0 秒处重播，丢掉原始起点（GPT 第二次复核第 3 节）。
            video_inputs = (["-ss", f"{seg_start:.3f}", "-stream_loop", "-1",
                             "-i", str(src)] if loop
                            else ["-ss", f"{seg_start:.3f}", "-i", str(src)])
            inputs = video_inputs + ["-loop", "1", "-i", str(overlay)]
            frames = max(1, int(seg_dur * fps))
            # 段间推镜方向轮换：偶数段推进、奇数段拉回
            z = (f"1+0.06*on/{frames}" if k % 2 == 0
                 else f"1.06-0.06*on/{frames}")
            bg = (f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
                  f"crop={w}:{h},setsar=1,fps={fps},"
                  f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                  f":d=1:s={w}x{h},"
                  f"eq=contrast=1.03:brightness=-0.035:saturation=0.94[bg]")
            ov = f"[1:v]format=rgba,scale={w}:{h}[ov]"
            fade = "fade=t=in:st=0:d=0.18," if k == 0 else ""
            mix = (f"[bg][ov]overlay=0:0:shortest=1:format=auto,"
                   f"{fade}format=yuv420p[v]")
            piece = tmpdir / f"{out.stem}_part{k}.mp4"
            cmd = (["-hide_banner", "-loglevel", "error", "-y"] + inputs +
                   ["-filter_complex", f"{bg};{ov};{mix}", "-map", "[v]",
                    "-t", f"{seg_dur:.3f}", "-an", "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
                    "-r", str(fps), "-movflags", "+faststart", str(piece)])
            r = run_cmd(["ffmpeg"] + cmd, timeout=420)
            if r.returncode != 0 or not piece.exists() or piece.stat().st_size < 5000:
                raise RuntimeError(f"分段 {k} 渲染失败: {r.stderr[-200:]}")
            rendered.append(piece)
        listfile = tmpdir / f"{out.stem}_parts.txt"
        listfile.write_text("".join("file '" + str(p).replace("\\", "/") + "'\n"
                                    for p in rendered), encoding="utf-8")
        r = run_cmd(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                     "-f", "concat", "-safe", "0", "-i", str(listfile),
                     "-c", "copy", str(out)], timeout=300)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f"分段拼接失败: {r.stderr[-300:]}")
        return out
    except Exception as e:
        if run:
            run.log(f"分段渲染失败，回退整段裁切：{str(e)[:160]}", "WARN")
        return render_clip(item, duration, overlay, out, cfg, run)
    finally:
        for p in rendered:
            p.unlink(missing_ok=True)


def thumbnail(video: Path, out: Path, second: float = 1.0, w: int = 240, h: int = 426) -> Path | None:
    r = run_cmd(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-ss", str(second), "-i", str(video), "-frames:v", "1",
                 "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
                 "-q:v", "4", str(out)], timeout=60)
    return out if r.returncode == 0 and out.exists() else None
