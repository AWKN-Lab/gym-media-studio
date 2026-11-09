"""本地语音转文字：faster-whisper（免费 · 本地 · 零密钥），输出中文逐字稿。

抖音 / 视频号下载的视频 → 提取音频 → Whisper 转写 → 带时间戳的分段逐字稿 + 纯文本 + SRT/VTT。
默认 base 模型（比 tiny 同音字更准，首次使用自动从 HF 镜像下载，之后本地缓存）；tiny 作为无效输入时的离线兜底；small 更准但更慢、体积更大。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import wave
from pathlib import Path

from .util import DATA, ffmpeg, write_json

TRANSCRIBE_DIR = DATA / "transcripts"
# 国内 HF 镜像：避免直连 huggingface.co 在多数网络下超时
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

MODEL_CHOICES = ["tiny", "base", "small"]
# 缓存已加载模型，避免每条视频都重新初始化 CTranslate2
_MODEL_LOCK = threading.Lock()
_MODEL_CACHE: dict[str, object] = {}

_AUDIO_EXT = {".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg", ".opus"}
_VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".ts", ".m2ts"}
MEDIA_EXT = _AUDIO_EXT | _VIDEO_EXT


def _is_media(p: Path) -> bool:
    return p.suffix.lower() in MEDIA_EXT


def _model_cache_dir() -> Path:
    # faster-whisper 默认缓存到 ~/.cache/huggingface；这里只探测是否已存在
    base = Path(os.environ.get("HF_HUB_CACHE")
                or Path.home() / ".cache" / "huggingface" / "hub")
    return base


def available_models() -> list[dict]:
    out = []
    cache = _model_cache_dir()
    for m in MODEL_CHOICES:
        # HF 缓存布局：models--Systran--faster-whisper-<size>/snapshots/<hash>/
        ready = cache.exists() and any(
            cache.glob(f"models--Systran--faster-whisper-{m}/snapshots/*"))
        out.append({"id": m, "ready": bool(ready),
                    "note": "已就绪" if ready else "首次使用自动下载"})
    return out


def _load_model(model_size: str):
    model_size = model_size if model_size in MODEL_CHOICES else "tiny"
    with _MODEL_LOCK:
        if model_size not in _MODEL_CACHE:
            from faster_whisper import WhisperModel
            _MODEL_CACHE[model_size] = WhisperModel(
                model_size, device="cpu", compute_type="int8")
    return _MODEL_CACHE[model_size]


def _to_srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _read_wav_f32(wav: Path) -> "object":
    """用标准库 wave 读 16k 单声道 wav → float32/32768 的 numpy 数组。

    绕过 faster-whisper 内置音频加载器（av/soundfile 在新版本下
    `metadata_errors` 关键字不兼容），直接喂 16k 数组给模型。"""
    import numpy as np
    with wave.open(str(wav), "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _extract_audio(src: Path, out_wav: Path) -> bool:
    """视频 → 16k 单声道 wav；音频文件直接软链/复制无需转码。"""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() in _AUDIO_EXT:
        # 已是音频：直接重采样为 16k wav，供 whisper 统一读取
        try:
            ffmpeg(["-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", str(out_wav)])
            return out_wav.exists() and out_wav.stat().st_size > 1000
        except Exception:
            return False
    try:
        ffmpeg(["-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", str(out_wav)])
        return out_wav.exists() and out_wav.stat().st_size > 1000
    except Exception:
        return False


def transcribe(path: str, model_size: str = "base",
               language: str = "zh") -> dict:
    """把本地视频/音频转成逐字稿。

    返回 {ok, id, model, language, duration, text, segments:[{start,end,text}], files:{...}}
    失败返回 {ok:False, error:...}。转写的产物（txt/srt/vtt/json）落盘到 data/transcripts/<id>/，
    不复制、不改动源视频。
    """
    src = Path(str(path).strip())
    if not src.exists() or not src.is_file():
        return {"ok": False, "error": "文件不存在或不是普通文件"}
    if not _is_media(src):
        return {"ok": False, "error": "不支持的媒体格式"}
    model_size = model_size if model_size in MODEL_CHOICES else "tiny"

    import tempfile
    tmp = Path(tempfile.gettempdir()) / "lynvos_whisper"
    tmp.mkdir(parents=True, exist_ok=True)
    wav = tmp / (src.stem + "_16k.wav")
    if not _extract_audio(src, wav):
        return {"ok": False, "error": "音频提取失败（FFmpeg 未安装或文件损坏）"}

    lang = language if language in ("zh", "en") else None  # None = 自动检测
    try:
        model = _load_model(model_size)
        audio = _read_wav_f32(wav)
        segments_iter, info = model.transcribe(
            audio, language=lang, vad_filter=True, beam_size=5)
        segments = [{"start": round(s.start, 2), "end": round(s.end, 2),
                     "text": s.text.strip()} for s in segments_iter]
    except Exception as e:
        return {"ok": False, "error": f"转写失败：{type(e).__name__}: {str(e)[:160]}"}
    finally:
        wav.unlink(missing_ok=True)

    text = "\n".join(s["text"] for s in segments).strip()
    detected = (info.language or "zh") if info else (lang or "auto")

    tid = src.stem + "_" + os.urandom(3).hex()
    d = TRANSCRIBE_DIR / tid
    d.mkdir(parents=True, exist_ok=True)

    # SRT
    srt = "\n".join(
        f"{i}\n{_to_srt_time(s['start'])} --> {_to_srt_time(s['end'])}\n{s['text']}\n"
        for i, s in enumerate(segments, 1))
    (d / "transcript.srt").write_text(srt, encoding="utf-8")
    (d / "transcript.txt").write_text(text, encoding="utf-8")
    (d / "transcript.json").write_text(
        json.dumps({"source": str(src), "model": model_size,
                    "language": detected, "segments": segments,
                    "text": text}, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": True, "id": tid, "model": model_size, "language": detected,
        "duration": round(info.duration, 1) if info else 0.0,
        "segment_count": len(segments), "text": text,
        "segments": segments,
        "files": {
            "txt": f"/media/transcripts/{tid}/transcript.txt",
            "srt": f"/media/transcripts/{tid}/transcript.srt",
            "json": f"/media/transcripts/{tid}/transcript.json",
        },
    }


def list_transcripts() -> list[dict]:
    if not TRANSCRIBE_DIR.exists():
        return []
    out = []
    for d in sorted(TRANSCRIBE_DIR.iterdir(), key=lambda p: p.stat().st_mtime,
                   reverse=True):
        if not d.is_dir():
            continue
        meta = read_meta(d)
        out.append(meta)
    return out


def to_script(tid: str, scenes_per: int = 2) -> dict:
    """把一次转写结果转换成可渲染的分镜脚本草稿。

    按时间顺序每 `scenes_per` 个转写分段合并成一镜，自动算出每镜时长，
    落盘到 data/scripts/<tid>.json，供「一键成片」或人工二次编辑。
    """
    d = TRANSCRIBE_DIR / tid
    j = d / "transcript.json"
    if not j.exists():
        return {"ok": False, "error": "转写不存在"}
    m = json.loads(j.read_text(encoding="utf-8"))
    segs = m.get("segments", [])
    if not segs:
        return {"ok": False, "error": "该转写无分段"}

    scenes, buf, start = [], [], None
    for i, s in enumerate(segs, 1):
        if start is None:
            start = s["start"]
        buf.append(s["text"])
        if i % scenes_per == 0 or i == len(segs):
            end = s["end"]
            scenes.append({
                "index": len(scenes) + 1,
                "text": " ".join(buf).strip(),
                "duration": round(max(2.0, end - start), 1),
            })
            buf, start = [], None

    title = (segs[0]["text"][:20] + "…") if segs[0]["text"] else tid
    script = {
        "id": tid, "source": m.get("source", ""),
        "title": title, "language": m.get("language", ""),
        "scenes": scenes,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    out_dir = DATA / "scripts"
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / f"{tid}.json", script)
    return {"ok": True, "script": script}


def _script_path(tid: str) -> Path | None:
    """防目录穿越：逐字稿脚本 id 只允许安全文件名。"""
    safe = "".join(c for c in str(tid) if c not in '\\/:*?"<>|').strip()
    if not safe or safe in (".", ".."):
        return None
    return (DATA / "scripts" / f"{safe}.json")


def list_scripts() -> list[dict]:
    """逐字稿转出的分镜脚本摘要列表（按修改时间倒序）。"""
    sdir = DATA / "scripts"
    if not sdir.is_dir():
        return []
    out = []
    for f in sorted(sdir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(s, dict) and s.get("scenes") is not None:
            out.append({"id": s.get("id", f.stem), "title": s.get("title", ""),
                        "scenes": len(s.get("scenes", [])),
                        "created": s.get("created", ""),
                        "source": s.get("source", "")})
    return out


def read_script(tid: str) -> dict | None:
    p = _script_path(tid)
    if not p or not p.exists():
        return None
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
        return s if isinstance(s, dict) else None
    except Exception:
        return None


def save_script(tid: str, script: dict) -> dict:
    """保存前端人工编辑后的分镜脚本（只接受白名单字段，防注入）。"""
    p = _script_path(tid)
    if not p:
        return {"ok": False, "error": "非法脚本 id"}
    if not isinstance(script, dict) or not isinstance(script.get("scenes"), list):
        return {"ok": False, "error": "脚本格式错误"}
    scenes = []
    for i, x in enumerate(script["scenes"], 1):
        if not isinstance(x, dict):
            continue
        try:
            dur = float(x.get("duration", 3.0))
        except (TypeError, ValueError):
            dur = 3.0
        scenes.append({"index": i,
                       "text": str(x.get("text", ""))[:500],
                       "duration": round(max(1.0, min(dur, 120.0)), 1)})
    clean = {"id": p.stem,
             "title": str(script.get("title", ""))[:80],
             "source": str(script.get("source", ""))[:200],
             "language": str(script.get("language", ""))[:20],
             "scenes": scenes,
             "created": str(script.get("created", "")),
             "edited": time.strftime("%Y-%m-%d %H:%M:%S")}
    p.parent.mkdir(parents=True, exist_ok=True)
    write_json(p, clean)
    return {"ok": True, "script": clean}


def read_meta(d: Path) -> dict:
    j = (d / "transcript.json")
    if j.exists():
        try:
            m = json.loads(j.read_text(encoding="utf-8"))
            return {"id": d.name, "source": m.get("source", ""),
                    "model": m.get("model", ""), "language": m.get("language", ""),
                    "segment_count": len(m.get("segments", [])),
                    "duration": m.get("duration", 0), "at": d.stat().st_mtime}
        except Exception:
            pass
    return {"id": d.name, "source": "", "model": "", "language": "",
            "segment_count": 0, "duration": 0, "at": d.stat().st_mtime}
