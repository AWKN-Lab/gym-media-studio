"""真实素材库：本机文件夹直连 / 项目内实拍 / 可选联网素材。

对标 MoneyPrinterTurbo `app/services/material.py`（本地目录 + 图库 API + 缓存）、
ShortGPT `getBestVideo(used_vids=[])`（跨镜去重）、
ai-shorts-generator `library/inventory.csv`（素材索引 + 语义/标签复用）。

核心原则：
1. 真实视频是主路径；Pillow 静态卡片只在素材库为空时兜底。
2. 本地化优先：登记本机任意文件夹后只读索引（路径/时长/分辨率/标签），
   **不复制、不上传、不改动原文件**，剪辑时 FFmpeg 直接读绝对路径。
3. 本地模式（默认开）下禁止一切联网素材下载，素材文件永远不出本机。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from .util import (ASSETS, DATA, ffprobe_duration, ffprobe_info, http_get,
                   net_opener, run_cmd, write_json, read_json)
from .secrets import get_secret

LIB = ASSETS / "gym"
DEMO_JSON = ASSETS / "demo_sources.json"
RAW = DATA / "demo_raw"
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# 分镜画面提示词 → 素材标签（抄 ShortGPT 的「按叙述内容找素材」思路）
TAG_RULES = [
    (["门口", "进入", "推门", "门店", "氛围", "背影", "第一视角", "收尾", "微笑",
      "面对镜头", "logo", "前台", "全景"], ["门店", "氛围", "背影", "教练"]),
    (["教练", "示范", "中景", "动作", "训练", "下拉", "背部", "侧面", "稳定器",
      "深蹲", "硬拉", "卧推", "推举"], ["训练", "教练", "背部", "下拉"]),
    (["特写", "细节", "关节", "角度", "器械", "杠铃", "哑铃", "配重", "重量"],
     ["器械", "特写", "杠铃", "配重"]),
    (["引体", "悬垂", "单杠"], ["引体"]),
    (["有氧", "跑步", "单车", "椭圆"], ["有氧", "设备"]),
]

ALL_TAGS = ["门店", "氛围", "背影", "教练", "训练", "背部", "下拉", "器械", "特写",
            "杠铃", "配重", "引体", "女性", "有氧", "设备", "卧推", "深蹲", "跑步"]

# ---------------------------------------------------------------- 本机文件夹直连
LOCAL_CFG = DATA / "local_folders.json"     # 登记的本机目录 + 本地模式开关
LOCAL_INDEX = DATA / "local_index.json"     # 只读索引缓存，避免反复 ffprobe 大目录
LIB_INDEX = DATA / "lib_index.json"         # 项目内素材的 ffprobe 结果缓存
THUMB = DATA / "thumbs"                     # 按需生成的 360x640 小缩略图（派生缓存，不是素材副本）
MAX_LOCAL_DEPTH = 3                         # 递归层数，避免误选大目录卡死
MAX_LOCAL_FILES = 400
_SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information", "node_modules",
              "__pycache__", ".git", ".svn", "AppData", "Windows",
              "Program Files", "Program Files (x86)", ".workbuddy", "Temp"}


def local_config() -> dict:
    d = read_json(LOCAL_CFG, {}) or {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("folders", [])
    d.setdefault("local_only", True)   # 默认纯本地：不联网拉任何素材
    return d


def local_mode() -> bool:
    return bool(local_config().get("local_only", True))


def _norm_folder(raw) -> tuple[Path | None, str]:
    s = str(raw or "").strip().strip('"').strip("'")
    if not s:
        return None, "请填写本机文件夹的完整路径"
    try:
        p = Path(os.path.expandvars(s)).expanduser().resolve()
    except Exception:
        return None, "路径无法解析"
    if not p.exists():
        return None, "文件夹不存在（控制台跑在哪台机器，就填那台机器上的路径）"
    if not p.is_dir():
        return None, "这不是文件夹"
    if p.parent == p or p == Path.home().resolve():
        return None, "请指定具体的素材文件夹，不要填磁盘根目录或用户主目录"
    if p.name in _SKIP_DIRS:
        return None, "该目录是系统/缓存目录，请另选素材文件夹"
    if p == LIB.resolve() or LIB.resolve() in p.parents:
        return None, "这个目录已经在项目素材库里，无需重复登记"
    return p, ""


def list_folders() -> list[dict]:
    """已登记目录 + 各自扫到的视频/图片数（不扫描文件，只读缓存）。"""
    idx = read_json(LOCAL_INDEX, {}) or {}
    entries = idx.get("entries", {}) if isinstance(idx, dict) else {}
    out = []
    for raw in local_config()["folders"]:
        p = Path(str(raw))
        files = [v.get("item", {}) for v in entries.values()
                 if str(v.get("item", {}).get("folder", "")) == str(p)]
        out.append({"path": str(p), "exists": p.is_dir(),
                    "count": len(files),
                    "videos": sum(1 for f in files if f.get("kind") == "video")})
    return out


def browse_dirs(raw: str = "", show_all: bool = False) -> dict:
    """平台内「选择本机目录」的数据源：只枚举目录名，不读取任何文件内容。

    控制台跑在哪台机器，列出的就是那台机器的目录。返回的绝对路径仅供用户
    确认后写入本地文件夹登记，不会复制、上传或改动任何素材。
    """
    import string
    home = Path.home()
    out = {"ok": True, "error": "", "path": "", "parent": "", "root": True,
           "dirs": [], "drives": [], "shortcuts": [], "media": 0,
           "hidden": 0, "truncated": False, "selectable": False, "reason": ""}

    drives = []
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            root = Path(letter + ":\\")
            try:
                if root.exists():
                    drives.append({"name": f"本地磁盘 ({letter}:)", "path": str(root)})
            except OSError:
                continue
    else:
        drives = [{"name": "根目录 /", "path": "/"},
                  {"name": "主目录", "path": str(home)}]
    out["drives"] = drives

    shortcuts = []
    for name, p in (("桌面", home / "Desktop"), ("文档", home / "Documents"),
                    ("下载", home / "Downloads"), ("视频", home / "Videos"),
                    ("图片", home / "Pictures"), ("主目录", home),
                    ("项目目录", ASSETS.parent)):
        try:
            if p.is_dir():
                shortcuts.append({"name": name, "path": str(p)})
        except OSError:
            continue
    out["shortcuts"] = shortcuts

    s = str(raw or "").strip().strip('"').strip("'")
    if not s:
        return out
    try:
        p = Path(os.path.expandvars(s)).expanduser().resolve(strict=True)
    except Exception as e:
        out.update(ok=False, error=f"路径不存在或无法访问：{str(e)[:90]}")
        return out
    if not p.is_dir():
        out.update(ok=False, error="这不是文件夹，请选择一个目录")
        return out

    dirs, media, hidden = [], 0, 0
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    if (e.is_file(follow_symlinks=False) and
                            Path(e.name).suffix.lower() in VIDEO_EXT | IMAGE_EXT):
                        media += 1
                    if not e.is_dir(follow_symlinks=False):
                        continue
                    if not show_all and (e.name.startswith((".", "$")) or
                                         e.name in _SKIP_DIRS):
                        hidden += 1
                        continue
                    dirs.append({"name": e.name, "path": str(Path(e.path))})
                except OSError:
                    continue
    except PermissionError:
        out["error"] = "该目录没有读取权限（可换一个目录或直接填写路径）"
    except OSError as e:
        out["error"] = f"读取目录失败：{str(e)[:90]}"

    dirs.sort(key=lambda x: x["name"].lower())
    truncated = len(dirs) > 400
    _, why = _norm_folder(str(p))
    out.update(path=str(p), parent="" if p.parent == p else str(p.parent),
               root=False, dirs=dirs[:400], media=media, hidden=hidden,
               truncated=truncated, selectable=not bool(why), reason=why)
    return out


def add_folder(raw) -> dict:
    p, err = _norm_folder(raw)
    if not p:
        return {"ok": False, "error": err, "folders": list_folders()}
    d = local_config()
    if any(Path(str(x)).resolve() == p for x in d["folders"]):
        return {"ok": False, "error": "这个文件夹已经登记过了", "folders": list_folders()}
    d["folders"].append(str(p))
    write_json(LOCAL_CFG, d)
    items = scan_local_folders(force=True)
    mine = [x for x in items if x["folder"] == str(p)]
    return {"ok": True, "added": str(p), "count": len(mine),
            "error": "" if mine else "目录已登记，但里面没找到视频/图片（支持 mp4/mov/mkv/avi/m4v/webm/jpg/png）",
            "folders": list_folders(), "local_only": d["local_only"]}


def remove_folder(raw) -> dict:
    s = str(raw or "").strip().strip('"').strip("'")
    p, _ = _norm_folder(s)
    d = local_config()
    before = len(d["folders"])
    def same(x):
        return str(Path(str(x)).resolve()) == str(p) if p else str(x) == s
    d["folders"] = [x for x in d["folders"] if not same(x)]
    write_json(LOCAL_CFG, d)
    items = scan_local_folders(force=True)
    return {"ok": len(d["folders"]) < before, "error": "" if len(d["folders"]) < before else "未找到该目录",
            "folders": list_folders(), "total": len(items), "local_only": d["local_only"]}


def set_local_mode(enabled: bool) -> dict:
    d = local_config()
    d["local_only"] = bool(enabled)
    write_json(LOCAL_CFG, d)
    return {"ok": True, "local_only": d["local_only"], "folders": list_folders()}


def _local_item(p: Path, root: Path) -> dict | None:
    is_video = p.suffix.lower() in VIDEO_EXT
    meta = _probe(p) if is_video else {"duration": 0, "width": 0, "height": 0,
                                      "orientation": "图片"}
    if is_video and not meta.get("duration"):
        return None   # ffprobe 读不出时长，跳过损坏/非标准文件
    mid = hashlib.md5(str(p).encode("utf-8")).hexdigest()[:12]
    item = {
        "id": mid, "name": p.name, "path": str(p),
        "kind": "video" if is_video else "image",
        "label": p.stem, "tags": _filename_tags(p.stem),
        "origin": "本地文件夹", "external": True, "folder": str(root),
        "size_mb": round(p.stat().st_size / 1024 / 1024, 2),
        "url": "/media/local/" + mid,
        **meta,
    }
    item["thumb"] = "/media/thumbs/" + mid + ".jpg" if is_video else item["url"]
    return item


def scan_local_folders(force: bool = False) -> list[dict]:
    """只读扫描已登记的本机目录：建索引，绝不复制/移动/改写原文件。

    按 (大小, mtime) 缓存 ffprobe 结果；换片或新增文件才会重新探测。
    """
    folders = [str(x) for x in local_config()["folders"]]
    old = read_json(LOCAL_INDEX, {}) or {}
    entries = old.get("entries", {}) if isinstance(old, dict) else {}
    out, fresh = [], {}
    for raw in folders:
        root = Path(raw)
        if not root.is_dir():
            continue
        try:
            for cur, dirs, files in os.walk(root):
                try:
                    depth = len(Path(cur).relative_to(root).parts)
                except ValueError:
                    depth = 0
                dirs[:] = [d for d in dirs
                           if not d.startswith(".") and d not in _SKIP_DIRS
                           and depth < MAX_LOCAL_DEPTH]
                for name in sorted(files):
                    p = Path(cur) / name
                    if name.startswith(".") or p.suffix.lower() not in VIDEO_EXT | IMAGE_EXT:
                        continue
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    key, sig = str(p), f"{st.st_size}:{int(st.st_mtime)}"
                    rec = entries.get(key)
                    item = dict(rec["item"]) if (rec and rec.get("sig") == sig
                                                 and not force and rec.get("item")) else None
                    if item is None:
                        item = _local_item(p, root)
                        if item is None:
                            continue
                    # 派生字段每次重算：旧版缓存没有 thumb 时也能自动补齐
                    item["url"] = "/media/local/" + item["id"]
                    item["thumb"] = ("/media/thumbs/" + item["id"] + ".jpg"
                                     if item["kind"] == "video" else item["url"])
                    fresh[key] = {"sig": sig, "item": item}
                    out.append(item)
                    if len(out) >= MAX_LOCAL_FILES:
                        break
                if len(out) >= MAX_LOCAL_FILES:
                    break
        except OSError:
            continue
        if len(out) >= MAX_LOCAL_FILES:
            break
    if fresh != entries:   # 没变化就不写盘，界面刷新不产生磁盘写入
        write_json(LOCAL_INDEX, {"entries": fresh,
                                 "updated": datetime.now().isoformat(timespec="seconds")})
    return out


def find_media_by_id(mid: str) -> dict | None:
    """缩略图路由用：只接受索引里的 ID，不接受客户端传路径。"""
    for item in scan_library():
        if item["id"] == mid:
            return item
    return None


def find_local_media(mid: str) -> Path | None:
    """按索引 ID 找绝对路径；不接收客户端传来的路径，避免任意文件读取。"""
    d = read_json(LOCAL_INDEX, {}) or {}
    allowed = {str(Path(str(f)).resolve()) for f in local_config()["folders"]}
    for rec in (d.get("entries") or {}).values():
        item = rec.get("item") or {}
        if item.get("id") != mid:
            continue
        p = Path(item["path"])
        folder = Path(item.get("folder", "")).resolve()
        if p.is_file() and str(folder) in allowed and folder in [p.parent, *p.parents]:
            return p
    return None


def _demo_meta() -> dict:
    return read_json(DEMO_JSON, {"clips": [], "license": ""}) or {"clips": []}


def _filename_tags(name: str) -> list[str]:
    low = name.lower()
    tags = [t for t in ALL_TAGS if t in name]
    for en, zh in [("gym", "门店"), ("coach", "教练"), ("squat", "深蹲"),
                   ("dumbbell", "器械"), ("treadmill", "有氧"), ("bench", "卧推")]:
        if en in low and zh not in tags:
            tags.append(zh)
    return tags


def _probe(path: Path) -> dict:
    info = ffprobe_info(path)
    dur, w, h = 0.0, 0, 0
    try:
        dur = float(info.get("format", {}).get("duration", 0) or 0)
    except (TypeError, ValueError):
        pass
    for s in info.get("streams", []):
        if s.get("codec_type") == "video" and not w:
            w, h = int(s.get("width", 0) or 0), int(s.get("height", 0) or 0)
    return {"duration": round(dur, 2), "width": w, "height": h,
            "orientation": "竖屏" if h > w else ("横屏" if w > h else "未知")}


def scan_library() -> list[dict]:
    """建素材索引（时长/分辨率/方向/标签），供匹配与审核台使用。

    项目内素材也按 (大小, mtime) 缓存 ffprobe 结果，避免界面每次刷新都重扫。
    """
    LIB.mkdir(parents=True, exist_ok=True)
    demo_tags = {c["id"]: c for c in _demo_meta().get("clips", [])}
    cached = read_json(LIB_INDEX, {}) or {}
    entries = cached.get("entries", {}) if isinstance(cached, dict) else {}
    fresh: dict[str, dict] = {}
    out = []
    for p in sorted(LIB.iterdir()):
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXT | IMAGE_EXT:
            continue
        is_video = p.suffix.lower() in VIDEO_EXT
        st = p.stat()
        sig = f"{st.st_size}:{int(st.st_mtime)}"
        rec = entries.get(p.name)
        meta = rec["meta"] if (rec and rec.get("sig") == sig and rec.get("meta")) else None
        if meta is None:
            meta = _probe(p) if is_video else {"duration": 0, "width": 0, "height": 0,
                                               "orientation": "图片"}
        fresh[p.name] = {"sig": sig, "meta": meta}
        m = re.search(r"(?:demo|pexels)_(\d+)", p.stem)
        src = demo_tags.get(m.group(1), {}) if m else {}
        tags = list(src.get("tags") or _filename_tags(p.stem))
        item = {
            "id": hashlib.md5(p.name.encode("utf-8")).hexdigest()[:10],
            "name": p.name, "path": str(p), "kind": "video" if is_video else "image",
            "label": src.get("label") or p.stem,
            "tags": tags, "origin": "演示素材" if p.stem.startswith("demo_") else
                     ("Pexels 检索" if p.stem.startswith("pexels_") else "自有上传"),
            "size_mb": round(st.st_size / 1024 / 1024, 2),
            "url": "/media/assets/gym/" + urllib.parse.quote(p.name),
            **meta,
        }
        item["thumb"] = "/media/thumbs/" + item["id"] + ".jpg" if is_video else item["url"]
        out.append(item)
    if fresh != entries:
        write_json(LIB_INDEX, {"entries": fresh})
    # 本机登记的文件夹：直连索引，不复制原文件
    return out + scan_local_folders()


def _download(url: str, dest: Path, timeout: int = 90) -> Path:
    tmp = dest.with_suffix(".part")
    r = net_opener().open(urllib.request.Request(url, headers=UA), timeout=timeout)
    with open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, length=262144)
    r.close()
    if not tmp.exists() or tmp.stat().st_size < 60000:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("下载文件异常（过小或空）")
    tmp.replace(dest)  # 原子发布，不留半截文件
    return dest


def ensure_demo_clips(run=None, limit: int = 0) -> dict:
    """把内置免 Key 直链拉成本地素材；顺带压成 720 宽 / 12 秒 / 无音轨，省磁盘且剪辑更快。"""
    if local_mode():
        return {"ok": 0, "failed": [], "files": [], "license": "",
                "error": "本地模式已开启：禁止联网下载演示素材。请在「素材」页登记本机文件夹。"}
    LIB.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    clips = _demo_meta().get("clips", [])
    if limit:
        clips = clips[:limit]
    done, failed = [], []
    for c in clips:
        cid = str(c["id"])
        tag = "_".join(c["tags"][:3])
        out = LIB / f"demo_{cid}_{tag}.mp4"
        if out.exists() and out.stat().st_size > 60000:
            done.append(out.name)
            continue
        try:
            raw = RAW / f"{cid}.mp4"
            if not raw.exists() or raw.stat().st_size < 60000:
                _download(c["url"], raw)
            # 保留原始构图，剪辑阶段再做 9:16 cover 裁切；此处只统一尺寸与时长
            r = run_cmd(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                         "-i", str(raw), "-t", "12", "-an",
                         "-vf", "scale='min(720,iw)':-2:flags=lanczos",
                         "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
                         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
                        timeout=240)
            if r.returncode != 0 or not out.exists() or out.stat().st_size < 60000:
                raise RuntimeError(f"转码失败: {r.stderr[-160:]}")
            done.append(out.name)
            if run:
                run.log(f"演示素材入库：{out.name} ({round(out.stat().st_size/1024/1024,1)} MB)")
        except Exception as e:
            failed.append({"id": cid, "error": str(e)[:120]})
            if run:
                run.log(f"演示素材 {cid} 失败：{str(e)[:100]}", "WARN")
    return {"ok": len(done), "failed": failed, "files": done,
            "license": _demo_meta().get("license", "")}


def search_pexels(query: str, per_page: int = 5, run=None) -> dict:
    """生产路径：用用户在控制台填的 Pexels Key 按关键词检索并下载正版素材。"""
    if local_mode():
        return {"ok": 0, "files": [],
                "error": "本地模式已开启：禁止联网检索/下载素材。关闭本地模式后才可用。"}
    key = get_secret("PEXELS_API_KEY")
    if not key:
        return {"ok": 0, "error": "未配置 Pexels API Key（控制台 → 密钥设置）", "files": []}
    url = ("https://api.pexels.com/v1/videos/search?query=" +
           urllib.parse.quote(query) + f"&per_page={max(1, min(per_page, 15))}&orientation=portrait")
    try:
        body = http_get(url, timeout=20, headers={"Authorization": key}).decode("utf-8", "ignore")
        data = json.loads(body)
    except Exception as e:
        return {"ok": 0, "error": f"Pexels 检索失败: {str(e)[:140]}", "files": []}
    out_files = []
    for v in data.get("videos", [])[:per_page]:
        files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4"]
        # 优先接近竖屏、分辨率够用且体积适中的版本
        files.sort(key=lambda f: (abs((f.get("width") or 0) / max(1, f.get("height") or 1) - 0.5625),
                                  -abs((f.get("width") or 0) - 720)))
        if not files:
            continue
        dest = LIB / f"pexels_{v['id']}_{hashlib.md5(query.encode()).hexdigest()[:4]}.mp4"
        if dest.exists() and dest.stat().st_size > 60000:
            out_files.append(dest.name)
            continue
        try:
            _download(files[0]["link"], dest)
            out_files.append(dest.name)
            if run:
                run.log(f"Pexels 素材入库：{dest.name}（关键词 {query}）")
        except Exception as e:
            if run:
                run.log(f"Pexels {v['id']} 下载失败：{str(e)[:80]}", "WARN")
    return {"ok": len(out_files), "files": out_files,
            "error": "" if out_files else "未拿到可用视频，试试别的英文关键词"}


def _wanted_tags(scene: dict) -> list[str]:
    text = (scene.get("visual_prompt", "") + " " + scene.get("on_screen", "") + " " +
            scene.get("sub", ""))
    wanted: list[str] = []
    for keys, tags in TAG_RULES:
        if any(k in text for k in keys):
            wanted.extend(tags)
    return list(dict.fromkeys(wanted)) or ["训练", "器械"]


def _score(clip: dict, wanted: list[str], used: set[str], index: int) -> float:
    s = sum(2.5 for t in clip["tags"] if t in wanted)
    if clip["kind"] != "video":
        s -= 6  # 真实视频优先
    if clip["name"] in used:
        s -= 8  # 跨镜去重（ShortGPT used_vids 思路）
    if clip.get("orientation") == "竖屏":
        s += 1.2
    if clip.get("duration", 0) >= 5:
        s += 0.8
    # 同分时按分镜序号轮转，避免所有镜头都挑同一条
    s += (index * 0.137) % 0.5
    return s


def match_materials(script: dict, cfg: dict, library: list[dict] | None = None) -> list[dict]:
    """为每个分镜挑真实视频片段 + 入点，并给出 3 个候选供人工换片。"""
    lib = library if library is not None else scan_library()
    videos = [c for c in lib if c["kind"] == "video" and c.get("duration", 0) > 0.5]
    plan, used = [], set()
    for scene in script["scenes"]:
        idx = scene["index"]
        wanted = _wanted_tags(scene)
        ranked = sorted(videos, key=lambda c: -_score(c, wanted, used, idx))
        if not ranked:
            plan.append({"index": idx, "source": "", "start": 0, "label": "无可用视频（将用静态卡片）",
                         "tags": wanted, "candidates": []})
            continue
        chosen = ranked[0]
        used.add(chosen["name"])
        dur = max(0.5, chosen.get("duration", 0) or 0)
        # 不同分镜从原片不同位置起切，避免重复画面
        start = round((idx * 2.3 + 0.6) % max(0.2, dur - 1.2), 2)
        plan.append({
            "index": idx, "source": chosen["path"], "name": chosen["name"],
            "label": chosen["label"], "start": start, "clip_duration": dur,
            "tags": wanted,
            "candidates": [{"source": c["path"], "name": c["name"], "label": c["label"]}
                           for c in ranked[:4]],
        })
    return plan


def save_plan(path, plan: dict) -> Path:
    return write_json(path, plan)


def load_plan(path) -> dict | None:
    return read_json(path, None)
