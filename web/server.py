"""本地控制台服务：Python 标准库 http.server，零额外依赖。

提供 JSON API + MP4/素材 Range 播放 + 本机文件夹直连素材 + 用户自填密钥 + 两阶段审核工作流。
素材本地化：登记的本机目录只建索引，原文件不复制、不上传；播放按索引 ID 映射，
不接收客户端传入的任意路径。
"""
from __future__ import annotations

import base64
import hmac
import json
import mimetypes
import os
import re
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

from core.util import (ROOT, OUTPUT, DATA, ASSETS, WEB, load_config, save_config,
                       read_json, write_json)
from core.pipeline import run_pipeline, render_from_disk

_lock = threading.Lock()
_thumb_lock = threading.Lock()
_state = {"running": False, "run_id": None, "last_result": None, "error": None,
          "phase": None}

# 本地转写（语音→文字）任务状态
_transcribe_state = {"running": False, "id": None, "error": None, "result": None}

# ---------------------------------------------------------------- 访问控制
# 不配置用户时保持本机零门槛；一旦绑到非回环地址，必须配置用户或显式 --allow-insecure。
# 用户文件格式  data/users.json:
#   {"admin": {"password": "xxx", "role": "admin"}, "xiaowang": {"password": "yyy", "role": "user"}}
# 也可命令行 --users "admin:pwd:admin,xiaowang:pwd" 或环境变量 GYMPULSE_USERS。
_AUTH: dict[str, dict] = {}
_OPERATOR: dict[str, str] = {}   # run_id -> 操作者，用于成片归属

# 普通用户可调用的写接口；其余写接口（密钥/配置/上传/素材/账号台账/Cookie/真实发布）仅管理员
_USER_ALLOWED_POST = {"/api/run", "/api/render", "/api/plan", "/api/gateway/test",
                      "/api/voices/preview", "/api/transcribe",
                      # 逐字稿 → 分镜脚本工作流
                      "/api/transcribe/to-script", "/api/script/save",
                      # 账号运营（本地草稿 / 评论 / AI 起草回复 / 待发布清单）
                      "/api/publish/drafts", "/api/publish/comments",
                      "/api/publish/reply", "/api/publish/queue"}


def configure_auth(spec: str = "") -> dict:
    """载入用户表。内存中的口令一律以 PBKDF2 哈希保存，不保留明文。

    历史 users.json 里的明文口令在内存里即时升级（不擅自改写用户文件）。
    """
    from core.credentials import hash_password
    global _AUTH
    raw = read_json(DATA / "users.json", None)
    if isinstance(raw, dict) and raw:
        out = {}
        for k, v in raw.items():
            if not (isinstance(v, dict) and v.get("password")):
                continue
            stored = str(v["password"])
            if not stored.startswith("pbkdf2_sha256$"):
                stored = hash_password(stored)
            out[str(k)] = {"password": stored,
                           "role": "admin" if v.get("role") == "admin" else "user"}
        _AUTH = out
    else:
        spec = spec or os.environ.get("GYMPULSE_USERS", "")
        out = {}
        for part in spec.split(","):
            bits = [x.strip() for x in part.strip().split(":")]
            if len(bits) >= 2 and bits[0] and bits[1]:
                out[bits[0]] = {
                    "password": hash_password(bits[1]),
                    "role": "admin" if (len(bits) > 2 and bits[2] == "admin") or bits[0] == "admin" else "user",
                }
        _AUTH = out
    return _AUTH


def _videos():
    out = []
    for p in sorted(OUTPUT.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        d = read_json(p, {})
        if d.get("video"):
            d["_meta_path"] = str(p)
            d["_mp4_url"] = "/media/output/" + Path(d["video"]).name
            d["_srt_url"] = "/media/output/" + Path(d["srt"]).name
            d["_cover_url"] = "/media/output/" + Path(d["cover"]).name
            out.append(d)
    return out


def _runs():
    base = DATA / "runs"
    if not base.exists():
        return []
    return [p.name for p in sorted(base.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)
            if p.is_dir()][:20]


def _hot_today():
    p = DATA / "hot" / f"{datetime.now():%Y-%m-%d}.json"
    return read_json(p, {"top": [], "fitness_related": [], "sources": {}, "unique": 0})


def _plan_of(run_id: str):
    if not run_id or not re.fullmatch(r"[\w\-]+", run_id):
        return None
    return read_json(DATA / "runs" / run_id / "plan.json", None)


def _publish_pending() -> int:
    try:
        from core.studio import list_queue
        return sum(1 for x in list_queue() if x.get("status") == "pending")
    except Exception:
        return 0


def _account_count() -> int:
    try:
        from core.studio import list_accounts
        return len(list_accounts())
    except Exception:
        return 0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ------------------------------------------------------------ helpers
    def _principal(self):
        """返回 (用户名, 角色)；未开启鉴权时视为本机管理员；失败返回 None。"""
        from core.credentials import verify_password
        if not _AUTH:
            return ("local", "admin")
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                raw = base64.b64decode(h[6:]).decode("utf-8")
                user, _, pw = raw.partition(":")
                rec = _AUTH.get(user)
                if rec and verify_password(pw, rec["password"]):
                    return (user, rec.get("role", "user"))
            except Exception:
                pass
        # 便于脚本/API 调用：X-LYNVOS-Token: user:password（兼容旧头 X-GymPulse-Token）
        alt = self.headers.get("X-LYNVOS-Token") or self.headers.get("X-GymPulse-Token", "")
        if ":" in alt:
            user, _, pw = alt.partition(":")
            rec = _AUTH.get(user)
            if rec and verify_password(pw, rec["password"]):
                return (user, rec.get("role", "user"))
        return None

    def _deny(self, code: int, msg: str, auth_challenge: bool = False):
        body = json.dumps({"error": msg}, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        if auth_challenge:
            self.send_header("WWW-Authenticate", 'Basic realm="LYNVOS FIT", charset="UTF-8"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _thumb_media(self, mid: str, range_header: str | None):
        """按需生成 360x640 小缩略图（派生缓存）；只读原片，不复制原视频。"""
        if not re.fullmatch(r"[0-9a-f]{10,12}", mid):
            return self._json({"error": "非法素材 ID"}, 404)
        from core.materials import find_media_by_id, THUMB
        item = find_media_by_id(mid)
        if not item:
            return self._json({"error": "素材不存在"}, 404)
        src = Path(item["path"])
        if not src.is_file():
            return self._json({"error": "原片不存在"}, 404)
        if item["kind"] != "video":
            return self._stream(src, range_header)
        from core.clips import thumbnail
        THUMB.mkdir(parents=True, exist_ok=True)
        dest = THUMB / (mid + ".jpg")
        stale = (lambda: not dest.is_file() or dest.stat().st_mtime < src.stat().st_mtime)()
        if stale:
            with _thumb_lock:
                if not dest.is_file() or dest.stat().st_mtime < src.stat().st_mtime:
                    tmp = THUMB / f"{mid}.{threading.get_ident()}.tmp.jpg"
                    made = thumbnail(src, tmp, second=0.6, w=360, h=640)
                    if made:
                        tmp.replace(dest)
                    else:
                        tmp.unlink(missing_ok=True)
        if not dest.is_file():
            return self._json({"error": "缩略图生成失败"}, 404)
        return self._stream(dest, range_header)

    def _local_media(self, mid: str, range_header: str | None):
        """按索引 ID 播放本机登记目录里的原片；客户端无法指定任意路径。"""
        from core.materials import find_local_media
        target = find_local_media(mid)
        if target is None:
            return self._json({"error": "素材不存在或已移出登记目录"}, 404)
        return self._stream(target, range_header)

    def _media(self, rel: str, range_header: str | None, base=None):
        base = (base or ROOT).resolve()
        parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
        target = (base / Path(*parts)).resolve()
        allowed = [OUTPUT.resolve(), ASSETS.resolve(), (DATA / "runs").resolve(),
                   WEB.resolve(), (DATA / "voice_previews").resolve(),
                   (DATA / "transcripts").resolve()]
        if not any(target == a or a in target.parents for a in allowed):
            return self._json({"error": "路径不允许"}, 403)
        if not target.is_file():
            return self._json({"error": "文件不存在"}, 404)
        return self._stream(target, range_header)

    def _stream(self, target: Path, range_header: str | None):
        size = target.stat().st_size
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix.lower() in (".mp4", ".mov", ".m4v", ".webm"):
            ctype = "video/mp4"

        start, end = 0, size - 1
        ranged = False
        if range_header:
            m = re.match(r"bytes=(\d*)-(\d*)", range_header)
            if m:
                ranged = True
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
                if start > end:
                    start = 0
        length = end - start + 1
        with open(target, "rb") as f:
            f.seek(start)
            chunk = f.read(length)
        extra = {"Accept-Ranges": "bytes"}
        if ranged:
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        self._send(206 if ranged else 200, chunk, ctype, extra)

    # ------------------------------------------------------------ GET
    def do_GET(self):
        u = urlparse(self.path)
        path, q = unquote(u.path), parse_qs(u.query)
        principal = self._principal()
        if principal is None:
            return self._deny(401, "需要登录", True)
        self._user, self._role = principal
        try:
            if path == "/api/secrets" and self._role != "admin":
                return self._json({"error": "仅管理员可查看密钥状态"}, 403)
            if path in ("/", "/index.html"):
                return self._send(200, (WEB / "index.html").read_bytes(),
                                  "text/html; charset=utf-8")
            if path.startswith("/media/thumbs/"):
                mid = path[len("/media/thumbs/"):].strip("/")
                if mid.endswith(".jpg"):
                    mid = mid[:-4]
                return self._thumb_media(mid, self.headers.get("Range"))
            if path.startswith("/media/local/"):
                return self._local_media(path[len("/media/local/"):].strip("/"),
                                         self.headers.get("Range"))
            if path.startswith("/media/transcripts/"):
                # 逐字稿产物落在 DATA/transcripts（即 ROOT/data/transcripts），
                # 不在 ROOT 下，需显式以 DATA/transcripts 为基准解析，否则被 allowlist 拒掉 403
                return self._media(path[len("/media/transcripts/"):],
                                   self.headers.get("Range"),
                                   base=(DATA / "transcripts"))
            if path.startswith("/media/"):
                return self._media(path[len("/media/"):], self.headers.get("Range"))
            if path == "/api/local/folders":
                from core.materials import list_folders, local_mode, scan_local_folders
                items = scan_local_folders()
                return self._json({"folders": list_folders(), "local_only": local_mode(),
                                   "count": len(items),
                                   "videos": sum(1 for x in items if x["kind"] == "video")})
            if path == "/api/local/browse":
                from core.materials import browse_dirs
                raw = (q.get("path") or [""])[0]
                show_all = (q.get("all") or [""])[0] in ("1", "true", "yes")
                return self._json(browse_dirs(raw, show_all))
            if path == "/api/state":
                from core.util import font_path
                from core.materials import scan_library, local_mode, list_folders
                from core.secrets import public_state, load_secrets, env_summary
                from core.llm import gateway_status
                lib = scan_library()
                cfg = load_config()
                brand = cfg["brand"]
                gst = gateway_status(cfg)
                return self._json({
                    "running": _state["running"], "run_id": _state["run_id"],
                    "phase": _state["phase"], "error": _state["error"],
                    "videos": _videos(), "runs": _runs(),
                    "hot": {"unique": _hot_today().get("unique", 0),
                            "fitness": len(_hot_today().get("fitness_related", [])),
                            "top": _hot_today().get("top", [])[:12]},
                    "config_brand": brand.get("full_name") or brand["name"],
                    "assets_count": len(lib),
                    "video_assets": sum(1 for x in lib if x["kind"] == "video"),
                    "external_assets": sum(1 for x in lib if x.get("external")),
                    "local_mode": local_mode(),
                    "local_folders": len(list_folders()),
                    "keys_configured": sum(1 for s in public_state() if s["configured"]),
                    "allow_env": bool(load_secrets().get("allow_env")),
                    "env_detected": env_summary()["detected"],
                    "gateway": {"online": bool(gst.get("online")),
                                "name": gst.get("name", "Magpie 喜鹊"),
                                "model": gst.get("selected", ""),
                                "free_count": len(gst.get("free", []))},
                    "plan_ready": bool(_plan_of(_state["run_id"] or "")),
                    "font": font_path(),
                    "user": self._user, "role": self._role,
                    "multi_user": bool(_AUTH),
                    "operator": _OPERATOR.get(_state["run_id"] or "", ""),
                    "tasks_count": len(_runs()),
                    "pending_publish": _publish_pending(),
                    "accounts_count": _account_count(),
                })
            if path == "/api/config":
                return self._json(load_config())
            if path == "/api/offers":
                from core.offer import public_state
                return self._json(public_state(load_config()))
            if path == "/api/secrets":
                from core.secrets import public_state
                return self._json(public_state())
            if path == "/api/gateway":
                from core.llm import gateway_status
                from core.secrets import env_summary
                force = (q.get("force") or [""])[0] in ("1", "true", "yes")
                st = dict(gateway_status(load_config(), force=force))
                st["env"] = env_summary()
                return self._json(st)
            if path == "/api/materials":
                from core.materials import scan_library, _demo_meta
                return self._json({"items": scan_library(),
                                   "license": _demo_meta().get("license", "")})
            if path == "/api/plan":
                rid = (q.get("run_id") or [_state["run_id"] or ""])[0]
                p = _plan_of(rid)
                return self._json(p) if p else self._json({"error": "暂无分镜方案"}, 404)
            if path == "/api/hot":
                return self._json(_hot_today())
            if path == "/api/videos":
                return self._json(_videos())
            if path == "/api/run/status":
                rid = (q.get("run_id") or [_state["run_id"] or ""])[0]
                d = DATA / "runs" / rid
                status = read_json(d / "status.json", {"steps": [], "finished": False})
                log = ""
                lp = d / "run.log"
                if lp.exists():
                    log = "\n".join(lp.read_text(encoding="utf-8", errors="replace").splitlines()[-70:])
                result = read_json(d / "result_all.json", None)
                return self._json({"run_id": rid, "status": status, "log": log,
                                   "result": result, "running": _state["running"],
                                   "plan_ready": bool(_plan_of(rid))})
            if path == "/api/scripts":
                rid = (q.get("run_id") or [(_runs() or [""])[0]])[0]
                return self._json(read_json(DATA / "runs" / rid / "scripts_final.json", []))
            if path == "/api/voices":
                from core.studio import list_voices
                force = (q.get("refresh") or [""])[0] in ("1", "true", "yes")
                return self._json(list_voices(force=force))
            if path == "/api/tasks":
                from core.studio import list_tasks
                return self._json(list_tasks())
            if path == "/api/publish/accounts":
                from core.studio import list_accounts
                accs = list_accounts()
                from core.platforms import masked_state, norm_platform
                for a in accs:
                    # 凭证只存本机 data/publish，绝不回传前端明文：
                    # 仅保留一个布尔标记表示「是否已配置」，并剔除 credential 字段
                    a["credential_masked"] = bool((a.get("credential") or "").strip())
                    a.pop("credential", None)
                    # 该账号对应平台的登录态是否已配置（布尔，绝不回传明文 Cookie）
                    a["cookie_masked"] = masked_state(a.get("id", "")).get(
                        norm_platform(a.get("platform", "")), False)
                return self._json(accs)
            if path == "/api/publish/queue":
                from core.studio import list_queue
                return self._json(list_queue())
            if path == "/api/publish/drafts":
                from core.studio import list_drafts
                return self._json(list_drafts((q.get("account_id") or [""])[0]))
            if path == "/api/publish/comments":
                from core.studio import list_comments
                return self._json(list_comments((q.get("account_id") or [""])[0]))
            if path == "/api/transcripts":
                from core.transcribe import list_transcripts, available_models
                return self._json({"items": list_transcripts(),
                                   "models": available_models()})
            if path == "/api/transcribe/status":
                return self._json(dict(_transcribe_state))

            # 逐字稿转出的分镜脚本（独立资源，不与成片分镜 /api/scripts 冲突）
            if path == "/api/metrics/summary":
                from core.metrics import summary
                return self._json(summary())

            if path == "/api/metrics/events":
                from core.metrics import read_events
                try:
                    n = max(1, min(int(q.get("limit", ["200"])[0]), 5000))
                except Exception:
                    n = 200
                return self._json({"items": read_events(limit=n)})

            if path == "/api/transcript-scripts":
                from core.transcribe import list_scripts, read_script
                sid = (q.get("id") or [""])[0]
                if sid:
                    s = read_script(sid)
                    if not s:
                        return self._json({"ok": False, "error": "脚本不存在"}, 404)
                    return self._json(s)
                return self._json({"items": list_scripts()})

            # ---- 平台真实读操作（吸收 bilibili-API-collect / MediaCrawler）----
            if path.startswith("/api/platform/"):
                parts = path[len("/api/platform/"):].split("/")
                plat = unquote(parts[0])
                kind = parts[1] if len(parts) > 1 else ""
                acc_id = (q.get("account_id") or [""])[0]
                if kind == "hot":
                    from core.platforms import fetch_hot
                    return self._json(fetch_hot(plat, (q.get("keyword") or [""])[0], acc_id))
                if kind == "comments":
                    from core.platforms import fetch_comments
                    return self._json(fetch_comments(plat, (q.get("ref") or [""])[0], acc_id))
                return self._json({"ok": False, "error": "未知平台操作"}, 404)
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    # ------------------------------------------------------------ POST
    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()   # 必须先消费请求体，否则 keep-alive 连接会错乱
        principal = self._principal()
        if principal is None:
            return self._deny(401, "需要登录", True)
        self._user, self._role = principal
        try:
            if self._role != "admin" and path not in _USER_ALLOWED_POST:
                return self._deny(403, "仅管理员可修改密钥、配置与素材库")
            if path == "/api/config":
                cfg = load_config()
                for k, v in body.items():
                    if k in cfg and isinstance(cfg[k], dict) and isinstance(v, dict):
                        cfg[k].update(v)
                    else:
                        cfg[k] = v
                save_config(cfg)
                return self._json({"ok": True})

            if path == "/api/offers/set":
                # 只改 local.offer.active / overrides，不动 playbook、锚点池等其它配置，
                # 避免前端表单的浅 patch 把整段 local 覆盖掉。
                from core.offer import apply_selection, public_state
                cfg = load_config()
                try:
                    cfg = apply_selection(cfg, body)
                except ValueError as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
                save_config(cfg)
                return self._json({"ok": True, **public_state(load_config())})

            if path == "/api/offers/confirm":
                # 门店书面确认写 append-only 台账，不改 config、不删历史；
                # 撤销另写一条 revoke 记录，立即让含具体承诺的结构位回退。
                from core.offer import apply_confirmation, public_state
                cfg = load_config()
                try:
                    result = apply_confirmation(cfg, body)
                except ValueError as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
                return self._json({"ok": True, "record": result["record"],
                                   "state": result["state"],
                                   **public_state(load_config())})

            if path == "/api/secrets":
                from core.secrets import save_secrets
                save_secrets(body)
                # Key 变更后让模型探测缓存失效
                (DATA / "llm_status.json").unlink(missing_ok=True)
                return self._json({"ok": True})

            if path == "/api/secrets/test":
                from core.secrets import test_connection
                ok, msg = test_connection(body.get("key", ""), body.get("model", ""))
                return self._json({"ok": ok, "detail": msg})

            if path == "/api/gateway/test":
                from core.llm import gateway_test
                return self._json(gateway_test(load_config(), body.get("model", "")))

            if path == "/api/gateway/set":
                cfg = load_config()
                g = cfg.setdefault("llm", {}).setdefault("gateway", {})
                if "base_url" in body:
                    u = str(body["base_url"]).strip()
                    if u and not u.startswith(("http://", "https://")):
                        return self._json({"error": "网关地址必须以 http:// 或 https:// 开头"}, 400)
                    g["base_url"] = u.rstrip("/")
                for k in ("enabled", "model", "prefer_free"):
                    if k in body:
                        g[k] = body[k]
                if body.get("api_key"):
                    g["api_key"] = str(body["api_key"]).strip()
                if body.get("clear_api_key"):
                    g["api_key"] = ""
                save_config(cfg)
                # 地址/模型变更后旧探测结论立即作废，避免继续用过期缓存
                (DATA / "gateway_status.json").unlink(missing_ok=True)
                (DATA / "llm_status.json").unlink(missing_ok=True)
                return self._json({"ok": True})

            if path == "/api/local/folders":
                from core.materials import (add_folder, remove_folder, set_local_mode,
                                           scan_local_folders, list_folders, local_mode)
                act = str(body.get("action", "scan")).strip().lower()
                if act == "add":
                    return self._json(add_folder(body.get("path", "")))
                if act == "remove":
                    return self._json(remove_folder(body.get("path", "")))
                if act == "mode":
                    return self._json(set_local_mode(body.get("enabled", True)))
                items = scan_local_folders(force=True)
                return self._json({"ok": True, "folders": list_folders(),
                                   "local_only": local_mode(), "count": len(items),
                                   "videos": sum(1 for x in items if x["kind"] == "video")})

            if path == "/api/materials/demo":
                from core.materials import ensure_demo_clips
                return self._json(ensure_demo_clips())

            if path == "/api/materials/pexels":
                from core.materials import search_pexels
                return self._json(search_pexels(body.get("query", "gym workout"),
                                                int(body.get("count", 4))))

            if path == "/api/run":
                if _state["running"]:
                    return self._json({"ok": False, "error": "已有任务在运行"}, 409)
                from core.util import Run
                count = max(1, min(int(body.get("count", 1)), 5))
                step = body.get("step", "all")
                if step not in ("hot", "topics", "scripts", "prepare", "all"):
                    step = "all"
                run = Run()
                _state.update(running=True, run_id=run.id, error=None,
                              last_result=None, phase=step)
                _OPERATOR[run.id] = self._user
                threading.Thread(target=lambda: _run_existing(run, count, step), daemon=True).start()
                return self._json({"ok": True, "run_id": run.id, "phase": step})

            if path == "/api/render":
                if _state["running"]:
                    return self._json({"ok": False, "error": "已有任务在运行"}, 409)
                rid = str(body.get("run_id") or _state["run_id"] or "")
                if not _plan_of(rid):
                    return self._json({"ok": False, "error": "该运行没有分镜方案"}, 404)
                _state.update(running=True, run_id=rid, error=None, phase="render")
                _OPERATOR[rid] = self._user
                threading.Thread(target=lambda: _render_worker(rid), daemon=True).start()
                return self._json({"ok": True, "run_id": rid, "phase": "render"})

            if path == "/api/plan":
                rid = str(body.get("run_id") or "")
                plan = body.get("plan")
                if not _plan_of(rid) or not isinstance(plan, dict):
                    return self._json({"ok": False, "error": "方案不存在或格式错误"}, 400)
                plan["status"] = "reviewed"
                plan["reviewed_by"] = self._user
                write_json(DATA / "runs" / rid / "plan.json", plan)
                return self._json({"ok": True})

            if path == "/api/voices/preview":
                from core.studio import preview_voice
                return self._json(preview_voice(str(body.get("ref") or body.get("voice_id", "")),
                                                str(body.get("text", "")),
                                                body.get("speed")))
            if path == "/api/voices/set":
                from core.studio import set_voice
                return self._json(set_voice(str(body.get("ref") or body.get("voice_id", "")),
                                            body.get("speed")))
            if path == "/api/publish/accounts":
                from core.studio import save_accounts
                return self._json(save_accounts(body.get("items", [])))
            if path == "/api/publish/queue":
                from core.studio import queue_add, queue_update, queue_remove
                act = body.get("action", "add")
                if act == "add":
                    return self._json(queue_add(body.get("video", {}),
                                                str(body.get("account_id", "")),
                                                str(body.get("note", ""))))
                if act == "status":
                    return self._json(queue_update(str(body.get("id", "")),
                                                   str(body.get("status", "pending"))))
                if act == "remove":
                    return self._json(queue_remove(str(body.get("id", ""))))
                return self._json({"ok": False, "error": "未知操作"}, 400)

            if path == "/api/publish/drafts":
                from core.studio import save_drafts
                return self._json(save_drafts(str(body.get("account_id") or ""),
                                            body.get("items", [])))
            if path == "/api/publish/comments":
                from core.studio import save_comments
                return self._json(save_comments(str(body.get("account_id") or ""),
                                              body.get("items", [])))
            if path == "/api/publish/reply":
                from core.studio import draft_reply
                return self._json(draft_reply(str(body.get("account_id") or ""),
                                             str(body.get("comment", "")),
                                             str(body.get("tone", ""))))

            if path == "/api/publish/cookies":
                if self._role != "admin":
                    return self._json({"ok": False, "error": "仅管理员可保存登录态"}, 403)
                from core.platforms import save_cookie
                return self._json(save_cookie(str(body.get("account_id") or ""),
                                             str(body.get("platform") or "bilibili"),
                                             str(body.get("cookie") or ""),
                                             str(body.get("csrf") or "")))

            if path.startswith("/api/platform/") and path.endswith("/publish"):
                if self._role != "admin":
                    return self._json({"ok": False, "error": "仅管理员可发布"}, 403)
                plat = unquote(path[len("/api/platform/"):-len("/publish")].strip("/"))
                from core.platforms import publish
                # video_path 指向本机已渲染成片
                return self._json(publish(plat, str(body.get("video_path") or ""),
                                          dict(body.get("meta", {}) or {}),
                                          str(body.get("account_id") or "")))

            if path == "/api/transcribe":
                if _transcribe_state["running"]:
                    return self._json({"ok": False, "error": "已有转写任务在运行"}, 409)
                from core.transcribe import transcribe
                model_size = str(body.get("model_size") or "base")
                language = str(body.get("language") or "zh")
                src = None
                name = body.get("name", "")
                payload = body.get("data", "")
                if name and "," in payload:
                    safe = "".join(c for c in Path(name).name if c not in '\\/:*?"<>|')
                    udir = DATA / "transcribe_uploads"
                    udir.mkdir(parents=True, exist_ok=True)
                    dest = udir / safe
                    if dest.exists():
                        dest = dest.with_name(dest.stem + "_"
                                             + datetime.now().strftime("%H%M%S") + dest.suffix)
                    dest.write_bytes(base64.b64decode(payload.split(",", 1)[1]))
                    src = dest
                else:
                    p = str(body.get("path") or "").strip()
                    if p and Path(p).exists() and Path(p).is_file():
                        src = Path(p)
                if not src:
                    return self._json({"ok": False,
                                       "error": "请提供本机视频/音频路径，或上传一个文件"}, 400)
                _transcribe_state.update(running=True, id=None, error=None, result=None)

                def _transcribe_work():
                    try:
                        _transcribe_state["result"] = transcribe(str(src), model_size, language)
                        if not _transcribe_state["result"].get("ok"):
                            _transcribe_state["error"] = _transcribe_state["result"].get("error")
                    except Exception as e:
                        _transcribe_state["error"] = f"{type(e).__name__}: {e}"
                        traceback.print_exc()
                    finally:
                        _transcribe_state["running"] = False

                threading.Thread(target=_transcribe_work, daemon=True).start()
                return self._json({"ok": True, "message": "转写已启动，轮询 /api/transcribe/status 获取结果"})

            if path == "/api/transcribe/to-script":
                from core.transcribe import to_script
                r = to_script(str(body.get("id") or ""), int(body.get("scenes_per", 2)))
                return self._json(r)

            if path == "/api/script/save":
                from core.transcribe import save_script
                return self._json(save_script(str(body.get("id") or ""),
                                             dict(body.get("script") or {})))

            if path == "/api/upload":
                name = body.get("name", "")
                payload = body.get("data", "")
                if not name or "," not in payload:
                    return self._json({"ok": False, "error": "缺少文件名或数据"}, 400)
                safe = "".join(c for c in Path(name).name if c not in '\\/:*?"<>|')
                dest = ASSETS / "gym" / safe
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    dest = dest.with_name(dest.stem + "_" + datetime.now().strftime("%H%M%S") + dest.suffix)
                dest.write_bytes(base64.b64decode(payload.split(",", 1)[1]))
                return self._json({"ok": True, "path": str(dest), "size": dest.stat().st_size})
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def _run_existing(run, count, step):
    try:
        result = run_pipeline(cfg=load_config(), count=count, run=run, max_steps=step)
        _state["last_result"] = result
        _state["error"] = result.get("error")
    except Exception as e:
        _state["error"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    finally:
        _state["running"] = False


def _render_worker(run_id):
    try:
        result = render_from_disk(run_id, cfg=load_config())
        _state["last_result"] = result
        _state["error"] = result.get("error")
    except Exception as e:
        _state["error"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    finally:
        _state["running"] = False


_SCHED_STATE = DATA / "schedule_state.json"


def _scheduler_loop():
    """可选：控制台常驻时，每天定点自动跑一次全流程。"""
    from core.util import Run
    while True:
        try:
            cfg = load_config()
            s = cfg.get("schedule", {})
            if s.get("enabled"):
                parts = str(s.get("cron", "0 9 * * *")).split()
                minute, hour = int(parts[0]), int(parts[1])
                now = datetime.now()
                state = read_json(_SCHED_STATE, {})
                today = now.strftime("%Y-%m-%d")
                due = now.hour * 60 + now.minute >= hour * 60 + minute
                if due and state.get("last") != today and not _state["running"]:
                    write_json(_SCHED_STATE, {"last": today, "at": now.isoformat(timespec="seconds")})
                    run = Run()
                    _state.update(running=True, run_id=run.id, error=None, phase="all")
                    print(f"[调度] {today} 自动出片启动 {run.id}", flush=True)
                    _run_existing(run, 1, "all")
        except Exception:
            traceback.print_exc()
        time.sleep(60)


def serve(port: int = 8787, host: str = "127.0.0.1", users: str = "",
          allow_insecure: bool = False):
    auth = configure_auth(users)
    external = host not in ("127.0.0.1", "localhost", "::1")
    if external and not auth and not allow_insecure:
        raise SystemExit(
            "\n[安全中止] 对外监听却未配置访问口令，密钥面板会被任何人读取。\n"
            "任选其一后重启：\n"
            "  1) python run.py --serve --host 0.0.0.0 --users \"admin:你的密码:admin,同事:他的密码\"\n"
            "  2) 创建 data/users.json，格式见 web/server.py 顶部注释\n"
            "  3) 确属可信内网且不需鉴权时，追加 --allow-insecure\n")
    srv = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    cfg = load_config()
    print(f"凌扬运动 LYNVOS FIT 工作台已启动： http://{host}:{port}  (Ctrl+C 停止)")
    if external:
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("223.5.5.5", 80))
            print(f"局域网访问地址： http://{s.getsockname()[0]}:{port}")
            s.close()
        except Exception:
            pass
    print("访问身份：" + (", ".join(f"{u}({v['role']})" for u, v in auth.items())
                      if auth else "本机免登录（仅 127.0.0.1 可达）"))
    print("素材模式：本地化直连（assets/gym/ + 控制台登记的本机文件夹）；默认不联网、不上传素材")
    print("每日自动出片：" + ("已开启" if cfg.get("schedule", {}).get("enabled")
                          else "未开启（可在控制台设置中打开）"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
