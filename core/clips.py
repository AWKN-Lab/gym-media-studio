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


def thumbnail(video: Path, out: Path, second: float = 1.0, w: int = 240, h: int = 426) -> Path | None:
    r = run_cmd(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-ss", str(second), "-i", str(video), "-frames:v", "1",
                 "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
                 "-q:v", "4", str(out)], timeout=60)
    return out if r.returncode == 0 and out.exists() else None
