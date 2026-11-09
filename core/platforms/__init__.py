"""平台对接统一入口。

读操作（评论 / 热点 / 搜索）：
- B 站：公开接口即可，无需登录（已实测）。
- 抖音：热榜公开接口（已实测）；评论需签名 → 走 MediaCrawler。
- 小红书：评论 / 笔记需签名 → 走 MediaCrawler。

写操作（发布 / 草稿）：
- B 站：UPOS 分片上传 + 提交（未真机验证，需 SESSDATA+bili_jct）。
- 抖音 / 小红书：无干净发布 API，受官方企业资质管控 → 明确标注「需资质」。
"""
from __future__ import annotations

from core.platforms.cookies import (save_cookie, get_cookie, has_cookie, masked_state,
                                    cookie_path, COOKIE_PLATFORMS, norm_platform)
from core.platforms import bilibili as _bili
from core.platforms import douyin_xhs as _dx

# 读：评论
def fetch_comments(platform: str, ref: str, acc_id: str = "") -> dict:
    plat = norm_platform(platform)
    c = get_cookie(acc_id, plat) or {} if acc_id else {}
    cookie = c.get("cookie", "")
    if plat == "bilibili":
        return _bili.fetch_comments(ref, cookie)
    if plat == "douyin":
        return _dx.douyin_comments(ref, cookie)
    if plat == "xiaohongshu":
        return _dx.xiaohongshu_comments(ref, cookie)
    return {"ok": False, "error": f"暂不支持拉取「{platform}」的真实评论（需该平台登录态与签名能力）"}


# 读：热点 / 搜索
def fetch_hot(platform: str, keyword: str, acc_id: str = "") -> dict:
    plat = norm_platform(platform)
    c = get_cookie(acc_id, plat) or {} if acc_id else {}
    cookie = c.get("cookie", "")
    if plat == "bilibili":
        return _bili.fetch_hot(keyword, cookie)
    if plat == "douyin":
        return _dx.douyin_hot()
    if plat == "xiaohongshu":
        return _dx.xiaohongshu_notes(keyword, cookie)
    return {"ok": False, "error": f"暂不支持拉取「{platform}」的真实热点"}


# 写：发布 / 草稿
def publish(platform: str, video_path: str, meta: dict, acc_id: str = "") -> dict:
    plat = norm_platform(platform)
    c = get_cookie(acc_id, plat) or {}
    if plat == "bilibili":
        return _bili.upload_and_publish(
            video_path, meta.get("title", ""), meta.get("desc", ""),
            meta.get("tags", ["健身"]), c.get("cookie", ""), c.get("csrf", ""),
            category=int(meta.get("category", 21)), dynamic=meta.get("dynamic", ""),
            mode=meta.get("mode", "draft"))
    # 抖音 / 视频号 / 小红书：无干净发布 API，受官方企业资质管控
    return {"ok": False, "verified": False,
            "error": f"{platform} 真实发布受官方企业开放平台资质管控，本工作台不伪造接口；"
                     f"如需自动发布，请提供该平台的开放平台 API 密钥并另行对接。"}


__all__ = ["save_cookie", "get_cookie", "has_cookie", "masked_state", "cookie_path",
           "COOKIE_PLATFORMS", "norm_platform", "fetch_comments", "fetch_hot", "publish"]
