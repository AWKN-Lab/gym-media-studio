"""平台登录态（Cookie）安全存储。

关键约束（与发布账号凭证一致）：
- Cookie 只落本机 data/publish/<acc_id>/cookies.json，永不随 API 回传前端明文。
- 前端只能拿到「是否已配置」的布尔，填新 Cookie 时留空 = 保留原值（__KEEP__ 语义）。
- 读取 Cookie 仅供本机后端调用平台接口使用。
"""
from __future__ import annotations

import time
from pathlib import Path

from core.util import DATA, read_json, write_json
from core.credentials import (seal, unseal, needs_migration,
                              protection_mode, harden)

COOKIE_PLATFORMS = ["bilibili", "douyin", "xiaohongshu", "kuaishou", "weibo",
                    "wechat_channels"]

# 前端账号台账用中文平台名，读侧/写侧接口用英文别名；统一归一化到 slug，
# 否则「B站」大小写或中英混用会导致 Cookie 存了读不到、平台分支匹配不上。
_PLATFORM_ALIASES = {
    "bilibili": "bilibili", "b站": "bilibili", "bili": "bilibili", "哔哩哔哩": "bilibili",
    "douyin": "douyin", "抖音": "douyin",
    "xiaohongshu": "xiaohongshu", "xhs": "xiaohongshu", "小红书": "xiaohongshu",
    "kuaishou": "kuaishou", "快手": "kuaishou",
    "weibo": "weibo", "微博": "weibo",
    "wechat_channels": "wechat_channels", "视频号": "wechat_channels",
    "wechat": "wechat_channels", "微信视频号": "wechat_channels",
}


def norm_platform(name: str) -> str:
    """把任意中英平台别名归一化为 COOKIE_PLATFORMS 里的 slug。"""
    return _PLATFORM_ALIASES.get(str(name or "").strip().lower(),
                                 str(name or "").strip().lower())


def cookie_path(acc_id: str) -> Path:
    return DATA / "publish" / str(acc_id) / "cookies.json"


def _load(acc_id: str) -> tuple[Path, dict]:
    p = cookie_path(acc_id)
    return p, read_json(p, {}) or {}


def _migrate(p: Path, store: dict) -> bool:
    """把历史明文 Cookie / csrf 就地升级为加密存储。返回是否发生迁移。"""
    changed = False
    for _, rec in store.items():
        if not isinstance(rec, dict):
            continue
        for k in ("cookie", "csrf"):
            if needs_migration(rec.get(k, "")):
                rec[k] = seal(rec[k])
                changed = True
    if changed:
        write_json(p, store)
        harden(p)
    return changed


def save_cookie(acc_id: str, platform: str, cookie: str, csrf: str = "") -> dict:
    """保存登录态。cookie / csrf 落盘前一律加密；留空表示保留原值。"""
    p, store = _load(acc_id)
    _migrate(p, store)
    platform = norm_platform(platform) or "bilibili"
    prev = store.get(platform, {}) or {}
    new_cookie = (cookie or "").strip()
    new_csrf = (csrf or "").strip()
    store[platform] = {
        "cookie": seal(new_cookie) if new_cookie else prev.get("cookie", ""),
        "csrf": seal(new_csrf) if new_csrf else prev.get("csrf", ""),
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "protection": protection_mode(),
    }
    write_json(p, store)
    harden(p)
    return {"ok": True, "platform": platform,
            "configured": bool(store[platform]["cookie"]),
            "protection": protection_mode()}


def get_cookie(acc_id: str, platform: str) -> dict | None:
    """读取并解密登录态；历史明文自动迁移为密文。"""
    p, store = _load(acc_id)
    if _migrate(p, store):
        p, store = _load(acc_id)
    rec = store.get(norm_platform(platform))
    if not isinstance(rec, dict):
        return None
    return {"cookie": unseal(rec.get("cookie", "")),
            "csrf": unseal(rec.get("csrf", "")),
            "updated": rec.get("updated", ""),
            "protection": rec.get("protection", "legacy")}


def has_cookie(acc_id: str, platform: str) -> bool:
    c = get_cookie(acc_id, platform)
    return bool(c and c.get("cookie"))


def masked_state(acc_id: str) -> dict:
    """返回该账号各平台「是否已配置 Cookie」的布尔映射，绝不带明文。"""
    _, store = _load(acc_id)
    out = {}
    for p in COOKIE_PLATFORMS:
        rec = store.get(p, {})
        out[p] = bool(isinstance(rec, dict) and rec.get("cookie"))
    return out
