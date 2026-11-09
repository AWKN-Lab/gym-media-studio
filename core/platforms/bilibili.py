"""B 站（哔哩哔哩）平台对接。

读侧（评论 / 搜索 / 热点）：
- 公开视频的搜索与评论接口无需登录即可调用（已实测可达）。
- 登录态（SESSDATA）仅在需要拉取私密/更全数据或发布时必填。

写侧（上传 + 发草稿 / 发布）：
- 采用 bilibili-API-collect / biliupload 的同款 UPOS 分片上传流程。
- ⚠️ 该流程随 B 站接口变动，本机未持有真实账号，**未做真机验证**；
  调用会逐步记录 steps，失败返回结构化诊断，绝不会静默假装成功。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import uuid

from core.util import http_get, http_get_json, http_post_json, net_opener, _UA

VIEW_API = "https://api.bilibili.com/x/web-interface/view"
REPLY_API = "https://api.bilibili.com/x/v2/reply/main"
SEARCH_API = "https://api.bilibili.com/x/web-interface/search/all/v2"
PROFILES_API = "https://member.bilibili.com/x/vu/web/uk/get_user_cap?type=1"
SUBMIT_API = "https://member.bilibili.com/x/vu/web/add/v3"

_HEADERS = {"User-Agent": _UA, "Referer": "https://www.bilibili.com"}


def _cookie_header(cookie: str) -> dict:
    h = dict(_HEADERS)
    if cookie:
        h["Cookie"] = cookie
    return h


def resolve_aid(bvid: str, cookie: str = "") -> int | None:
    """bvid -> aid（评论接口用 aid 作 oid）。"""
    try:
        d = http_get_json(f"{VIEW_API}?bvid={bvid}", timeout=12, headers=_cookie_header(cookie))
        return d.get("data", {}).get("aid")
    except Exception:
        return None


def fetch_comments(bvid: str, cookie: str = "", max_n: int = 20) -> dict:
    """拉取某 B 站视频的真实评论（吸收 bilibili-API-collect 的 reply/main 接口）。"""
    aid = resolve_aid(bvid, cookie)
    if not aid:
        return {"ok": False, "error": "无法解析该 BV 号（检查 bvid 是否正确）", "comments": []}
    out, next_cursor, seen = [], 0, 0
    for _ in range(5):
        if seen >= max_n:
            break
        try:
            url = f"{REPLY_API}?oid={aid}&type=1&mode=3&next={next_cursor}"
            d = http_get_json(url, timeout=12, headers=_cookie_header(cookie))
        except Exception as e:
            return {"ok": False, "error": f"评论接口调用失败：{e}", "comments": out}
        if d.get("code") != 0:
            return {"ok": False, "error": d.get("message") or f"code={d.get('code')}", "comments": out}
        replies = (d.get("data", {}).get("replies") or [])
        if not replies:
            break
        for r in replies:
            member = r.get("member", {})
            content = r.get("content", {})
            out.append({
                "user": member.get("uname", ""),
                "text": (content.get("message") or "").replace("\n", " "),
                "likes": r.get("like", 0),
                "time": r.get("ctime", 0),
            })
            seen += 1
            if seen >= max_n:
                break
        if not d.get("data", {}).get("cursor", {}).get("is_end"):
            next_cursor = d["data"]["cursor"].get("next", 0)
        else:
            break
    return {"ok": True, "bvid": bvid, "aid": aid, "count": len(out), "comments": out}


def search_videos(keyword: str, cookie: str = "", max_n: int = 10) -> dict:
    """按关键词在 B 站搜索视频（兼作「搜热点 / 选题灵感」）。"""
    try:
        d = http_get_json(f"{SEARCH_API}?keyword={urllib.parse.quote(keyword)}&page=1",
                         timeout=12, headers=_cookie_header(cookie))
    except Exception as e:
        return {"ok": False, "error": f"搜索失败：{e}", "items": []}
    if d.get("code") != 0:
        return {"ok": False, "error": d.get("message") or f"code={d.get('code')}", "items": []}
    items = []
    for grp in d.get("data", {}).get("result", []) or []:
        if grp.get("result_type") != "video":
            continue
        for v in grp.get("data", []) or []:
            items.append({
                "title": v.get("title", "").replace("<em class=\"keyword\">", "").replace("</em>", ""),
                "bvid": v.get("bvid", ""),
                "author": v.get("author", ""),
                "play": v.get("play", 0),
                "duration": v.get("duration", ""),
                "url": f"https://www.bilibili.com/video/{v.get('bvid','')}",
            })
            if len(items) >= max_n:
                break
        if len(items) >= max_n:
            break
    return {"ok": True, "keyword": keyword, "count": len(items), "items": items}


def fetch_hot(keyword: str, cookie: str = "", max_n: int = 10) -> dict:
    """热点 / 选题：复用搜索接口。"""
    return search_videos(keyword, cookie, max_n)


def upload_and_publish(path: str, title: str, desc: str = "", tags: list[str] | None = None,
                       cookie: str = "", csrf: str = "", category: int = 21,
                       dynamic: str = "", mode: str = "draft") -> dict:
    """上传视频并发布 / 存草稿（UPOS 分片流程，未真机验证）。

    mode="draft" 存草稿；mode="publish" 直接发布。
    返回结构含 verified=False 与逐步 steps，便于真机调试。
    """
    tags = tags or ["健身"]
    steps = []
    if not cookie or not csrf:
        return {"ok": False, "verified": False,
                "error": "缺 Cookie / csrf（bili_jct）。需在账号设置里填入 SESSDATA 与 bili_jct。",
                "steps": steps}
    try:
        from pathlib import Path
        video = Path(path)
        # 前置校验：成片必须是本机真实文件，否则明确报错，不要抛异常返回 500
        if not path or not video.is_file():
            return {"ok": False, "verified": False,
                    "error": f"成片文件不存在或不是文件：{path or '(空路径)'}。"
                             f"请先在工作台渲染成片，再填写本机 MP4 绝对路径。",
                    "steps": steps}
        size = video.stat().st_size
        # 1) 取上传配置（bucket / upos 域名 / 上传会话）
        prof = http_get_json(PROFILES_API, timeout=15, headers=_cookie_header(cookie))
        upos = prof["data"]["upos"]["params"]
        bucket, upos_url = upos["bucket"], upos["upos_uri"].replace("upos://", "https://")
        upload_id = upos.get("upload_id", str(uuid.uuid4()))
        biz_id = upos.get("biz_id", "")
        steps.append({"step": "profiles", "ok": True, "biz_id": biz_id})
        # 2) 分片上传（每片 2MB）
        chunk = 2 * 1024 * 1024
        total = (size + chunk - 1) // chunk
        with open(video, "rb") as f:
            for i in range(total):
                data = f.read(chunk)
                url = (f"{upos_url}?partNumber={i+1}&uploadId={upload_id}&chunk={i}"
                       f"&size={len(data)}&bizId={biz_id}")
                req = urllib.request.Request(url, data=data,
                                             headers={**_cookie_header(cookie), "Content-Type": "application/octet-stream"},
                                             method="PUT")
                with net_opener().open(req, timeout=60) as r:
                    r.read()
        steps.append({"step": "upload_chunks", "ok": True, "chunks": total})
        # 3) 提交（草稿或发布）
        endpoint = "https://member.bilibili.com/x/vupre/web/draft/add" if mode == "draft" else SUBMIT_API
        payload = {
            "copyright": 1, "videos": [{"filename": f"{biz_id}.mp4", "title": title,
                                        "desc": desc, "cid": biz_id}],
            "source": "js Honor", "tid": category, "title": title, "cover": "",
            "tag": ",".join(tags[:10]), "desc_format_id": 0, "desc": desc,
            "dynamic": dynamic, "subtitle": {"open": 0, "lan": ""},
        }
        resp = http_post_json(endpoint, payload, headers={**_cookie_header(cookie), "Csrf-Token": csrf},
                             timeout=30, raw=True)
        try:
            jr = json.loads(resp)
        except Exception:
            jr = {"raw": resp[:300]}
        steps.append({"step": mode, "ok": jr.get("code") == 0, "resp": jr})
        ok = jr.get("code") == 0
        return {"ok": ok, "verified": False, "mode": mode,
                "bvid_or_draft": jr.get("data", {}).get("bvid") or jr.get("data"), "steps": steps}
    except Exception as e:
        return {"ok": False, "verified": False, "error": f"{type(e).__name__}: {e}", "steps": steps}
