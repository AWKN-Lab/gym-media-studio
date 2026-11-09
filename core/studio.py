"""工作台资产管理：音色库、任务中心、发布账号台账与待发布队列。

设计边界：
- 音色：与厂商解耦，用统一 ref（sapi:<短名> / edge:<ShortName>）标识；
  目录按「实际可用引擎」动态生成：Windows SAPI 完全离线档 + edge-tts 免费在线档（默认）；
  试听与成片共用 core/voice.py 的合成链并落盘缓存，不需要任何 TTS Key，
  也没有任何引擎会把用户的音频发到外网。
- 发布：只做「账号台账 + 待发布清单」，不托管平台登录态，也不做无人值守自动发布。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

from .util import DATA, ASSETS, load_config, save_config, read_json, write_json, timestamp, http_post_json

# 配音音色目录由 core/voice.py 按本机可用引擎动态生成（SAPI 离线 + edge-tts 免费在线），
# 不再内置任何需付费 Key 的商业音色。
PREVIEW_DIR = DATA / "voice_previews"
ACCOUNTS_PATH = DATA / "publish_accounts.json"
QUEUE_PATH = DATA / "publish_queue.json"
DEFAULT_PREVIEW_TEXT = "欢迎来到凌扬运动，科学训练，看得见的改变。"

# edge-tts 免费中文音色的人工标注（id 取自 edge_tts.list_voices() 实测清单）
EDGE_META = {
    "zh-CN-YunxiNeural": ("云希", "自然亲和", "新手科普、日常口播"),
    "zh-CN-YunjianNeural": ("云健", "浑厚有力", "促销钩子、力量训练"),
    "zh-CN-YunxiaNeural": ("云夏", "年轻明快", "年轻向、轻内容"),
    "zh-CN-YunyangNeural": ("云扬", "播报专业", "知识干货、资讯解读"),
    "zh-CN-XiaoxiaoNeural": ("晓晓", "温暖亲和", "团课、会员沟通"),
    "zh-CN-XiaoyiNeural": ("晓伊", "轻快活泼", "引流、互动话题"),
    "zh-CN-liaoning-XiaobeiNeural": ("晓北", "东北方言", "地域化、幽默内容"),
    "zh-CN-shaanxi-XiaoniNeural": ("晓妮", "陕西方言", "地域化、幽默内容"),
    "zh-HK-HiuGaaiNeural": ("曉佳", "粤语亲和", "粤语内容"),
    "zh-HK-HiuMaanNeural": ("曉曼", "粤语温和", "粤语内容"),
    "zh-HK-WanLungNeural": ("雲龍", "粤语沉稳", "粤语内容"),
    "zh-TW-HsiaoChenNeural": ("曉臻", "繁中温和", "繁体中文内容"),
    "zh-TW-HsiaoYuNeural": ("曉雨", "繁中轻快", "繁体中文内容"),
    "zh-TW-YunJheNeural": ("雲哲", "繁中沉稳", "繁体中文内容"),
}
LOCALE_LABEL = {"zh-CN": "普通话", "zh-HK": "粤语", "zh-TW": "繁体中文",
                "zh-CN-liaoning": "东北话", "zh-CN-shaanxi": "陕西话"}


# ------------------------------------------------------------------ 音色
def _engine_status() -> list[dict]:
    """引擎能力探测：离线档、免费在线档零密钥可用。"""
    from .voice import sapi_catalog
    edge_ok = bool(importlib.util.find_spec("edge_tts"))
    zh_count = sum(1 for x in sapi_catalog() if x.get("zh"))
    return [
        {"id": "sapi", "name": "Windows 本机离线语音", "free": True, "offline": True,
         "available": zh_count > 0,
         "detail": f"零密钥 · 断网可用 · 本机 {zh_count} 个中文语音包" if zh_count
                   else "仅 Windows：需 comtypes 与系统中文语音包（本项目不下载任何模型）"},
        {"id": "edge", "name": "edge-tts 免费音色", "free": True, "available": edge_ok,
         "detail": "零密钥 · 开源免费 · 需联网到微软语音服务" if edge_ok
                   else "未安装 edge-tts（pip install edge-tts）"},
    ]


def _sapi_items(force: bool = False) -> list[dict]:
    """本机 SAPI 语音包目录；英文包读不了中文，明确禁用而不是给一个空试听。"""
    from .voice import sapi_catalog
    cat = sapi_catalog(force=force)
    zh_ok = any(x.get("zh") for x in cat)
    meta = {
        "Huihui": ("慧慧", "温和清晰", "知识科普、日常口播"),
        "Hanhan": ("涵涵", "繁中亲和", "繁体中文内容"),
    }
    out = []
    for x in cat:
        vid = x["id"]
        name, style, use = meta.get(vid, (vid, "系统语音", "通用口播"))
        usable = bool(x.get("zh")) and zh_ok
        out.append({
            "ref": "sapi:" + vid, "id": vid, "engine": "sapi", "name": name,
            "gender": "女声" if x.get("gender") == "Female" else "男声",
            "style": style, "use": use, "locale": x.get("locale", ""),
            "free": True, "offline": True, "available": usable,
            "reason": "" if usable else ("英文语音包无法朗读中文文案" if cat
                                         else "本机没有可用的中文语音包"),
            "group": "完全离线 · 断网可用",
        })
    return out


def _edge_items(force: bool = False) -> list[dict]:
    from .voice import edge_catalog
    status = {x["id"]: x for x in _engine_status()}["edge"]
    out = []
    for v in edge_catalog(force=force):
        loc = v.get("locale", "")
        if not loc.lower().startswith("zh"):
            continue
        vid = v["id"]
        name, style, use = EDGE_META.get(
            vid, (vid.split("-")[-1].replace("Neural", ""), "自然", "通用口播"))
        out.append({
            "ref": "edge:" + vid, "id": vid, "engine": "edge", "name": name,
            "gender": "女声" if v.get("gender") == "Female" else "男声",
            "style": style, "use": use, "locale": LOCALE_LABEL.get(loc, loc),
            "free": True, "available": status["available"],
            "reason": "" if status["available"] else status["detail"],
            "group": "免费音色 · 零密钥",
        })
    out.sort(key=lambda x: (0 if x["locale"] == "普通话" else 1, x["gender"], x["name"]))
    return out


def _all_voices(force: bool = False) -> list[dict]:
    return _sapi_items(force) + _edge_items(force)


def list_voices(force: bool = False) -> dict:
    cfg = load_config()
    v = cfg["voice"]
    provider = v.get("provider", "edge")
    if v.get("voice_ref"):
        cur = v["voice_ref"]
    elif provider == "sapi":
        cur = "sapi:" + (v.get("sapi_voice") or "Huihui")
    else:
        cur = "edge:" + (v.get("edge_voice") or "zh-CN-YunxiNeural")
    voices = _all_voices(force)
    for x in voices:
        x["current"] = x["ref"] == cur
    engines = _engine_status()
    return {
        "current": cur, "speed": v.get("speed", 1.0),
        "provider": provider, "engines": engines,
        "zero_key": any(e["free"] and e["available"] for e in engines),
        "offline_ready": any(e.get("offline") and e["available"] for e in engines),
        "voices": voices, "preview_text": DEFAULT_PREVIEW_TEXT,
        "note": "离线档为本机 Windows 语音包（断网、零 Key 可用）；在线档 edge-tts 免费但需联网。"
                "试听与成片共用同一条合成链，音频按「音色 + 文本 + 语速」缓存，不重复合成。",
    }


def preview_voice(ref: str, text: str = "", speed=None) -> dict:
    """零密钥试听：edge 免费音色与 SAPI 离线语音均可直接合成。"""
    from .voice import synthesize_preview, rate_str
    item = {x["ref"]: x for x in _all_voices()}.get(ref)
    if not item:
        return {"ok": False, "error": "该音色不在当前音色目录内"}
    if not item["available"]:
        return {"ok": False, "error": item["reason"] or "该引擎当前不可用"}
    engine = item["engine"]
    cfg = json.loads(json.dumps(load_config()))
    if speed is not None:
        try:
            cfg["voice"]["speed"] = max(0.5, min(2.0, float(speed)))
        except (TypeError, ValueError):
            pass
    say = (text or DEFAULT_PREVIEW_TEXT).strip()[:60] or DEFAULT_PREVIEW_TEXT
    digest = hashlib.md5(
        (ref + "|" + say + "|" + rate_str(cfg["voice"]["speed"])).encode("utf-8")
    ).hexdigest()[:16]
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    out = PREVIEW_DIR / f"{engine}_{digest}.mp3"
    cached = out.exists() and out.stat().st_size > 1000
    if not cached:
        r = synthesize_preview(ref, say, out, cfg)
        if not r.get("ok") or not out.exists() or out.stat().st_size < 1000:
            out.unlink(missing_ok=True)
            return {"ok": False, "error": r.get("error") or "合成失败"}
    return {"ok": True, "url": "/media/data/voice_previews/" + out.name,
            "ref": ref, "engine": engine, "cached": cached,
            "size": out.stat().st_size}


def set_voice(ref: str, speed: float | None = None) -> dict:
    """写入真正生效的引擎与音色。"""
    engine, _, vid = (ref or "").partition(":")
    if engine not in ("edge", "sapi") or not vid:
        return {"ok": False, "error": "音色格式错误，应为 sapi:<短名> / edge:<音色ID>"}
    item = {x["ref"]: x for x in _all_voices()}.get(ref)
    if not item:
        return {"ok": False, "error": "该音色不在当前音色目录内"}
    if not item.get("available"):
        return {"ok": False, "error": item.get("reason") or "该引擎当前不可用"}
    cfg = load_config()
    cfg["voice"]["voice_ref"] = ref
    cfg["voice"]["provider"] = engine
    if engine == "edge":
        cfg["voice"]["edge_voice"] = vid
    else:
        cfg["voice"]["sapi_voice"] = vid
    if speed is not None:
        try:
            cfg["voice"]["speed"] = max(0.5, min(2.0, float(speed)))
        except (TypeError, ValueError):
            pass
    save_config(cfg)
    return {"ok": True, "current": ref, "provider": engine,
            "speed": cfg["voice"]["speed"]}


# ------------------------------------------------------------------ 任务中心
def list_tasks(limit: int = 30) -> list[dict]:
    base = DATA / "runs"
    if not base.exists():
        return []
    dirs = sorted((p for p in base.iterdir() if p.is_dir()),
                  key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out = []
    for d in dirs:
        st = read_json(d / "status.json", {}) or {}
        res = read_json(d / "result_all.json", {}) or {}
        plan = read_json(d / "plan.json", None)
        steps = st.get("steps", [])
        videos = res.get("videos", []) or []
        scripts = read_json(d / "scripts_final.json", []) or []
        failed = any(s.get("status") == "failed" for s in steps)
        if failed:
            state = "failed"
        elif videos:
            state = "done"
        elif plan:
            state = "prepared"
        elif scripts:
            state = "scripts"
        elif steps:
            state = "running"
        else:
            state = "empty"
        out.append({
            "run_id": d.name, "state": state, "at": d.name,
            "elapsed": st.get("elapsed", 0), "finished": bool(st.get("finished")),
            "steps": [{"name": s.get("name", ""), "status": s.get("status", ""),
                       "detail": s.get("detail", "")} for s in steps],
            "video_count": len(videos), "script_count": len(scripts),
            "titles": [v.get("title", "") for v in videos],
            "plan_ready": bool(plan), "plan_status": (plan or {}).get("status", ""),
            "reviewed_by": (plan or {}).get("reviewed_by", ""),
            "error": res.get("error") or "",
        })
    return out


# ------------------------------------------------------------------ 发布账号与队列
DEFAULT_ACCOUNTS = [
    {"id": "douyin-main", "platform": "抖音", "name": "凌扬运动主号", "owner": "", "note": "主阵地，日更", "status": "active"},
    {"id": "xhs-main", "platform": "小红书", "name": "凌扬运动", "owner": "", "note": "图文+短视频", "status": "active"},
    {"id": "sph-main", "platform": "视频号", "name": "凌扬运动", "owner": "", "note": "", "status": "active"},
]


def list_accounts() -> list[dict]:
    d = read_json(ACCOUNTS_PATH, None)
    if d is None:
        d = DEFAULT_ACCOUNTS
        write_json(ACCOUNTS_PATH, d)
    return d


def list_queue() -> list[dict]:
    return read_json(QUEUE_PATH, []) or []


def queue_add(video: dict, account_id: str = "", note: str = "") -> dict:
    items = list_queue()
    vid = str(video.get("video", ""))
    if any(x.get("video") == vid and x.get("account_id") == account_id for x in items):
        return {"ok": False, "error": "该作品已在此账号的待发布清单中"}
    items.insert(0, {
        "id": f"q-{len(items)+1}-{timestamp().replace(' ', '').replace(':', '')[-6:]}",
        "video": vid, "title": str(video.get("title", ""))[:80],
        "caption": str(video.get("caption", ""))[:2000],
        "cover": str(video.get("cover", "")),
        "account_id": account_id, "note": str(note)[:200],
        "at": timestamp(), "status": "pending",
    })
    write_json(QUEUE_PATH, items)
    return {"ok": True, "count": len(items)}


def queue_update(qid: str, status: str) -> dict:
    items = list_queue()
    hit = False
    for x in items:
        if x.get("id") == qid:
            x["status"] = status
            x["updated"] = timestamp()
            hit = True
    if not hit:
        return {"ok": False, "error": "未找到该条目"}
    write_json(QUEUE_PATH, items)
    return {"ok": True}


def queue_remove(qid: str) -> dict:
    items = [x for x in list_queue() if x.get("id") != qid]
    write_json(QUEUE_PATH, items)
    return {"ok": True, "count": len(items)}


# ------------------------------------------------------------------ 账号运营（本地优先）
PLATFORMS = ["抖音", "视频号", "小红书", "快手", "B站", "微博"]
LOGIN_TYPES = ["暂不实连", "Cookie", "Token", "开放平台 OAuth"]

PUB_DIR = DATA / "publish"


def account_dir(acc_id: str) -> Path:
    d = PUB_DIR / str(acc_id or "unknown")
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_accounts(items: list) -> dict:
    """真实账号台账：平台 / 账号名 / 登录方式 / 本地凭证 / 状态。

    凭证（Cookie/Token）只存在本机 data/publish 下，永不回传前端明文，
    也绝不代持平台登录态或做无人值守发布。
    """
    clean = []
    existing = {a["id"]: a for a in list_accounts()}
    for x in items:
        if not isinstance(x, dict):
            continue
        cred = str(x.get("credential", "") or "")
        if cred == "__KEEP__":
            cred = (existing.get(x.get("id"), {}) or {}).get("credential", "")
        clean.append({
            "id": str(x.get("id") or f"acc-{len(clean)+1}")[:48],
            "platform": str(x.get("platform", "抖音"))[:20],
            "name": str(x.get("name", "") or x.get("owner", ""))[:40],
            "login_type": str(x.get("login_type") or "暂不实连")[:20],
            "credential": cred[:4000],
            "owner": str(x.get("owner", ""))[:40],
            "note": str(x.get("note", ""))[:200],
            "status": "paused" if x.get("status") == "paused" else "active",
        })
    write_json(ACCOUNTS_PATH, clean)
    return {"ok": True, "count": len(clean)}


def list_drafts(acc_id: str) -> list:
    return read_json(account_dir(acc_id) / "drafts.json", []) or []


def save_drafts(acc_id: str, items: list) -> dict:
    clean = []
    for x in items:
        if not isinstance(x, dict):
            continue
        clean.append({
            "id": str(x.get("id") or f"d-{len(clean)+1}-{timestamp()[-6:]}"),
            "title": str(x.get("title", ""))[:120],
            "body": str(x.get("body", ""))[:4000],
            "tags": str(x.get("tags", ""))[:200],
            "status": str(x.get("status") or "draft")[:12],
            "at": x.get("at") or timestamp(),
        })
    write_json(account_dir(acc_id) / "drafts.json", clean)
    return {"ok": True, "count": len(clean)}


def list_comments(acc_id: str) -> list:
    return read_json(account_dir(acc_id) / "comments.json", []) or []


def save_comments(acc_id: str, items: list) -> dict:
    clean = []
    for x in items:
        if not isinstance(x, dict):
            continue
        clean.append({
            "id": str(x.get("id") or f"c-{len(clean)+1}-{timestamp()[-6:]}"),
            "author": str(x.get("author", ""))[:40],
            "text": str(x.get("text", ""))[:2000],
            "reply": str(x.get("reply", ""))[:2000],
            "replied": bool(x.get("replied")),
            "at": x.get("at") or timestamp(),
        })
    write_json(account_dir(acc_id) / "comments.json", clean)
    return {"ok": True, "count": len(clean)}


def _guard_reply(text: str, source: str, cfg: dict) -> dict:
    """所有面向公众的回复统一过合规闸门。

    闸门本身不可用时**不静默放行**：直接返回 ok=False 并拦截内容，
    避免「审核依赖挂了 = 无审核文案直接发布」。
    """
    try:
        from .compliance import check_text
        r = check_text(text, cfg)
        return {"ok": True, "reply": r["text"], "source": source,
                "compliance": r["report"]}
    except Exception as e:
        return {"ok": False, "source": source,
                "error": f"合规闸门不可用，回复已拦截：{type(e).__name__}: {e}"}


def draft_reply(acc_id: str, comment: str, tone: str = "") -> dict:
    """用平台免费网关起草一条评论回复（本地草稿，不自动发布）。

    无可用模型时回退到模板；两条路径的产出都必须过统一合规闸门。
    """
    cfg = load_config()
    # CTA「计划」是 brand.cta 定义的转化钩子：命中即记为一条线索事件
    try:
        from .metrics import track
        if "计划" in str(comment or ""):
            track("lead", source="comment", account_id=acc_id,
                  signal="cta_plan", text=str(comment)[:120])
    except Exception:
        pass  # 埋点失败不得影响回复主流程

    acc = next((a for a in list_accounts() if a["id"] == acc_id), None)
    name = acc["name"] if acc else "品牌账号"
    prompt = (f"你是「{name}」的私信/评论回复助手。"
              f"{('品牌调性：' + tone + '。') if tone else ''}"
              f"请用亲切、口语化、不超过 40 字的中文回复这条评论，不要硬广、不要反问：\n评论：{comment}")
    try:
        from . import llm
        g = llm.gateway_cfg(cfg)
        if g.get("enabled") and g.get("base_url") and g.get("selected"):
            body = {"model": g["selected"],
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 120, "temperature": 0.7}
            r = http_post_json(llm._gw_url(cfg, "/chat/completions"),
                               body, llm._gw_headers(cfg), timeout=40)
            content = (r.get("choices") or [{}])[0].get("message", {}).get("content", "")
            content = (content or "").strip()
            if content:
                return _guard_reply(content, "gateway", cfg)
    except Exception:
        pass
    # 离线模板兜底（评论原文会被截断带进回复，因此同样必须过闸门）
    t = (comment or "").replace("\n", " ")
    reply = f"谢谢{(t[:10] + '…') if len(t) > 10 else t}的支持～有任何训练问题随时留言，教练在线解答💪"
    return _guard_reply(reply, "template", cfg)
