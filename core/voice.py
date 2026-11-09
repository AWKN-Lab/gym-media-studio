"""分镜配音：edge-tts（免费在线，默认） / Windows SAPI（完全离线）→ 静音兜底。

零密钥默认值：config.voice.provider = "edge"，不依赖任何付费 TTS Key。
音色用统一 ref 表示（edge:<ShortName> / sapi:<短名>），试听与成片共用
synthesize_preview / _edge_tts / _sapi_audio 同一条合成链，避免「试听好听、成片是另一个声音」。
provider=sapi 是纯本机 COM 合成，不发起任何网络请求，且失败时只降级为静音、绝不偷偷回退到联网引擎。
按分镜单独合成，再用 ffprobe 读取真实音频时长驱动时间轴，保证画面/字幕/配音严格同步。
"""
from __future__ import annotations

import json
import subprocess
import sys
import wave
from pathlib import Path

from .util import (DATA, ROOT, ffprobe_duration, ffmpeg,
                   read_json, write_json)

EDGE_CACHE = DATA / "edge_voices.json"
SAPI_CACHE = DATA / "sapi_voices.json"
DEFAULT_EDGE_VOICE = "zh-CN-YunxiNeural"

# 断网 / 未安装 edge_tts 时的兜底清单（2026-11-06 实测微软在线中文 Neural 音色）
EDGE_FALLBACK = [
    {"id": "zh-CN-YunxiNeural", "gender": "Male", "locale": "zh-CN"},
    {"id": "zh-CN-YunjianNeural", "gender": "Male", "locale": "zh-CN"},
    {"id": "zh-CN-YunxiaNeural", "gender": "Male", "locale": "zh-CN"},
    {"id": "zh-CN-YunyangNeural", "gender": "Male", "locale": "zh-CN"},
    {"id": "zh-CN-XiaoxiaoNeural", "gender": "Female", "locale": "zh-CN"},
    {"id": "zh-CN-XiaoyiNeural", "gender": "Female", "locale": "zh-CN"},
    {"id": "zh-CN-liaoning-XiaobeiNeural", "gender": "Female", "locale": "zh-CN-liaoning"},
    {"id": "zh-CN-shaanxi-XiaoniNeural", "gender": "Female", "locale": "zh-CN-shaanxi"},
    {"id": "zh-HK-HiuGaaiNeural", "gender": "Female", "locale": "zh-HK"},
    {"id": "zh-HK-HiuMaanNeural", "gender": "Female", "locale": "zh-HK"},
    {"id": "zh-HK-WanLungNeural", "gender": "Male", "locale": "zh-HK"},
    {"id": "zh-TW-HsiaoChenNeural", "gender": "Female", "locale": "zh-TW"},
    {"id": "zh-TW-HsiaoYuNeural", "gender": "Female", "locale": "zh-TW"},
    {"id": "zh-TW-YunJheNeural", "gender": "Male", "locale": "zh-TW"},
]


def rate_str(speed) -> str:
    """把 0.5~2.0 倍速转成 edge-tts 需要的 '+5%' / '-10%' 字符串。"""
    try:
        s = float(speed)
    except (TypeError, ValueError):
        s = 1.0
    return f"{round((max(0.5, min(2.0, s)) - 1.0) * 100):+d}%"


def sapi_rate(speed) -> int:
    """把 0.5~2.0 倍速映射到 SAPI 的 -10~10（实测 0.5→-5 / 1.0→0 / 1.5→+5 / 2.0→+10）。"""
    try:
        s = float(speed)
    except (TypeError, ValueError):
        s = 1.0
    return int(round((max(0.5, min(2.0, s)) - 1.0) * 10))


def sapi_catalog(force: bool = False) -> list[dict]:
    """枚举本机 Windows SAPI 语音包（纯 COM，零网络、零 Key），结果落盘缓存。

    英文语音包无法朗读中文文案（实测输出 46 字节空 WAV），故目录里以 zh=False 标注，
    界面只把中文语音开放为可选项，不让用户点到没声音的试听。
    """
    if not force:
        cached = read_json(SAPI_CACHE, None)
        if isinstance(cached, dict) and isinstance(cached.get("voices"), list):
            return cached["voices"]
    items: list[dict] = []
    try:
        import comtypes
        import comtypes.client as cc
        comtypes.CoInitialize()
        try:
            v = cc.CreateObject("SAPI.SpVoice")
            tokens = v.GetVoices()
            for i in range(tokens.Count):
                t = tokens.Item(i)
                desc = str(t.GetDescription())
                try:
                    lang = str(t.GetAttribute("Language"))
                except Exception:
                    lang = ""
                try:
                    gender = str(t.GetAttribute("Gender"))
                except Exception:
                    gender = ""
                words = desc.split()
                items.append({
                    "id": words[1] if len(words) > 1 else desc,
                    "description": desc, "token": str(t.Id),
                    "lang": lang, "gender": gender,
                    "locale": {"804": "普通话", "404": "繁体中文", "409": "英语"}.get(lang, lang),
                    "zh": lang in ("804", "404"),
                })
        finally:
            comtypes.CoUninitialize()
    except Exception:
        items = []
    write_json(SAPI_CACHE, {"source": "sapi-com", "voices": items})
    return items


def sapi_available() -> bool:
    """完全离线档是否真的可用：至少有一个能读中文的本机语音包。"""
    return any(x.get("zh") for x in sapi_catalog())


def _sapi_tts(text: str, out_wav: Path, voice: str = "", speed=1.0) -> bool:
    """SAPI COM 直接写 WAV；整个调用链不 import 任何网络库。"""
    cat = sapi_catalog()
    item = next((x for x in cat if x["id"] == voice and x.get("zh")), None)
    if item is None:
        item = next((x for x in cat if x.get("zh")), None)
    if item is None:
        return False
    try:
        import comtypes
        import comtypes.client as cc
    except ImportError:
        return False
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    comtypes.CoInitialize()
    try:
        v = cc.CreateObject("SAPI.SpVoice")
        tokens = v.GetVoices()
        picked = False
        for i in range(tokens.Count):
            t = tokens.Item(i)
            if str(t.Id) == item["token"] or str(t.GetDescription()) == item["description"]:
                v.Voice = t          # 实测不同语音 md5 不同，音色切换确实生效
                picked = True
                break
        if not picked:
            return False
        v.Rate = sapi_rate(speed)
        stream = cc.CreateObject("SAPI.SpFileStream")
        stream.Open(str(out_wav), 3)   # SSFMCreateForWrite
        v.AudioOutputStream = stream
        v.Speak(text)
        stream.Close()
    except Exception:
        return False
    finally:
        comtypes.CoUninitialize()
    return out_wav.exists() and out_wav.stat().st_size > 1000


def _sapi_audio(text: str, out: Path, voice: str, speed=1.0) -> bool:
    """SAPI 原生 22.05k WAV → 试听用 MP3 / 成片用 48k WAV，统一进既有时间轴。"""
    tmp = out.with_name(out.stem + "_sapi.wav")
    if not _sapi_tts(text, tmp, voice, speed):
        tmp.unlink(missing_ok=True)
        return False
    if out.suffix.lower() == ".mp3":
        ffmpeg(["-i", str(tmp), "-ar", "24000", "-ac", "1",
                "-c:a", "libmp3lame", "-q:a", "4", str(out)])
    else:
        ffmpeg(["-i", str(tmp), "-ar", "48000", "-ac", "1",
                "-c:a", "pcm_s16le", str(out)])
    tmp.unlink(missing_ok=True)
    return out.exists() and out.stat().st_size > 1000


def edge_catalog(force: bool = False) -> list[dict]:
    """动态枚举免费音色（联网拉一次即缓存），失败时回退内置中文清单。"""
    if not force:
        cached = read_json(EDGE_CACHE, {}) or {}
        if cached.get("voices"):
            return cached["voices"]
    voices: list[dict] = []
    try:
        import edge_tts
        raw = asyncio.run(edge_tts.list_voices())
        voices = [{"id": str(v.get("ShortName") or ""),
                   "gender": str(v.get("Gender") or ""),
                   "locale": str(v.get("Locale") or "")}
                  for v in raw if v.get("ShortName")]
    except Exception:
        voices = []
    source = "live" if voices else "fallback"
    if not voices:
        voices = list(EDGE_FALLBACK)
    write_json(EDGE_CACHE, {"source": source, "voices": voices})
    return voices


def _silent_wav(path: Path, seconds: float = 3.0) -> Path:
    rate = 48000
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


_EDGE_LAST_ERROR = ""


def edge_last_error() -> str:
    """最近一次 edge 合成失败的原因（给日志和自检用，空串表示没出错）。"""
    return _EDGE_LAST_ERROR


def _edge_tts(text: str, out: Path, cfg: dict,
              voice: str = "", rate: str = "") -> bool:
    """指定音色 / 语速合成；voice、rate 留空时读当前默认配置。

    asyncio 合成放在子进程里跑并用硬超时等：Windows 上部分 Python 构建建不出
    本地回环（socket.socketpair 回退实现卡在 socket.accept），在主进程里直接
    asyncio.run 会永久挂住，整条出片流程跟着死等。子进程超时就杀掉，只算这一段失败。
    """
    global _EDGE_LAST_ERROR
    _EDGE_LAST_ERROR = ""
    try:
        import edge_tts  # noqa: F401  只用来确认依赖在，实际合成在子进程
    except ImportError:
        _EDGE_LAST_ERROR = "未安装 edge-tts"
        return False
    v = voice or cfg["voice"].get("edge_voice") or DEFAULT_EDGE_VOICE
    r = rate or rate_str(cfg["voice"].get("speed", 1.0))
    try:
        timeout = float(cfg["voice"].get("timeout") or 40)
    except (TypeError, ValueError):
        timeout = 40.0

    job = {"text": text, "voice": v, "rate": r, "out": str(out)}
    job_path = out.with_suffix(".job.json")
    try:
        job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    except OSError:
        _EDGE_LAST_ERROR = f"写不了合成任务文件：{job_path}"
        return False

    # 用 -m 启动：脚本目录一旦落在 core\ 下会把 sys.path[0] 带偏，edge_tts 的
    # 相对导入会报 "attempted relative import with no known parent package"。
    cmd = [sys.executable, "-m", "core.edge_worker", str(job_path)]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              creationflags=flags, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        _EDGE_LAST_ERROR = f"edge-tts 合成超时（{timeout:.0f}s），已强制终止"
        return False
    except OSError as e:
        _EDGE_LAST_ERROR = f"起不了合成子进程：{str(e)[:80]}"
        return False
    finally:
        try:
            job_path.unlink()
        except OSError:
            pass

    if proc.returncode != 0:
        tail = (proc.stderr or b"").decode("utf-8", "ignore").strip().splitlines()
        why = f"退出码 {proc.returncode}"
        if tail:
            why += "：" + tail[-1][:100]
        _EDGE_LAST_ERROR = f"edge-tts 合成失败（{why}）"
        return False
    ok = out.exists() and out.stat().st_size > 1000
    if not ok:
        _EDGE_LAST_ERROR = "edge-tts 没产出有效音频"
    return ok


def synthesize_preview(ref: str, text: str, out: Path, cfg: dict) -> dict:
    """试听专用入口：与成片共用 _edge_tts / _sapi_audio，保证「试听即所得」。

    ref 形如 edge:zh-CN-YunxiNeural / sapi:Huihui。
    """
    engine, _, vid = (ref or "").partition(":")
    if engine == "sapi":
        ok = _sapi_audio(text, out, vid, cfg["voice"].get("speed", 1.0))
        return {"ok": bool(ok), "engine": engine,
                "error": "" if ok else "Windows 离线语音不可用（需 Windows + comtypes，或该语音读不了中文）"}
    if engine != "edge":
        return {"ok": False, "engine": engine or "unknown", "error": "未知配音引擎"}
    ok = _edge_tts(text, out, cfg, voice=vid)
    return {"ok": bool(ok), "engine": engine,
            "error": "" if ok else (edge_last_error() or "edge-tts 合成失败（需要联网到微软语音服务）")}


def synthesize_scenes(script: dict, workdir: Path, cfg: dict, run=None) -> list[dict]:
    workdir.mkdir(parents=True, exist_ok=True)
    provider = cfg["voice"]["provider"]
    ref = str(cfg["voice"].get("voice_ref") or "")
    ref_engine, _, ref_voice = ref.partition(":")
    edge_voice = ref_voice if ref_engine == "edge" else ""
    sapi_voice = ref_voice if ref_engine == "sapi" else ""
    speed = cfg["voice"].get("speed", 1.0)
    rate = rate_str(speed)   # 默认 edge 引擎的语速此前被忽略
    used_counts: dict[str, int] = {}
    results = []
    edge_broken = False   # 一旦在线合成超时，剩余分镜不再一段段干等

    for scene in script["scenes"]:
        idx = scene["index"]
        text = scene["narration"].strip()
        mp3 = workdir / f"voice_{idx:02d}.mp3"
        wav = workdir / f"voice_{idx:02d}.wav"
        ok, used = False, "silent"

        if provider == "sapi":
            # 完全离线档：失败只落静音并留痕，绝不偷偷回退到需要联网的 edge-tts
            try:
                ok = _sapi_audio(text, mp3, sapi_voice, speed)
                if ok:
                    used = "sapi:" + (sapi_voice or "default")
            except Exception as e:
                if run:
                    run.log(f"分镜 {idx} 离线配音失败：{str(e)[:100]}", "WARN")
        else:
            if edge_broken:
                if run:
                    run.log(f"分镜 {idx} 跳过在线配音：本机在线合成通道已判定不可用", "WARN")
            else:
                try:
                    ok = _edge_tts(text, mp3, cfg, voice=edge_voice, rate=rate)
                    if ok:
                        used = "edge-tts:" + (edge_voice or "default")
                    else:
                        why = edge_last_error() or "未知原因"
                        if run:
                            run.log(f"分镜 {idx} 在线配音失败：{why}，本段降级", "WARN")
                        if "超时" in why:
                            edge_broken = True
                            if run:
                                run.log("在线配音超时，本次剩余分镜不再逐段等待，请改用离线档或换解释器", "WARN")
                except Exception as e:
                    if run:
                        run.log(f"分镜 {idx} edge-tts 异常：{str(e)[:80]}", "WARN")
        if ok and mp3.exists() and mp3.stat().st_size > 500:
            # 统一转 48k 单声道 wav，后续 ffmpeg 拼接无需重采样
            ffmpeg(["-i", str(mp3), "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)])
        else:
            _silent_wav(wav, cfg["video"]["min_scene_seconds"])
            used = "silent"

        dur = ffprobe_duration(wav)
        if dur <= 0.1:
            _silent_wav(wav, cfg["video"]["min_scene_seconds"])
            dur = ffprobe_duration(wav)
            used = "silent"
        used_counts[used] = used_counts.get(used, 0) + 1
        results.append({"index": idx, "audio": str(wav), "duration": round(dur, 3),
                        "provider": used, "text": text})

    if run:
        run.step("配音合成", "done", f"{len(results)} 段 | {used_counts} | "
                                     f"总时长 {sum(r['duration'] for r in results):.1f}s")
    return results
