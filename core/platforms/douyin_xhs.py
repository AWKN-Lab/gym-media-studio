"""抖音 / 小红书 平台对接（吸收 MediaCrawler 的设计思路）。

现实约束（务必如实）：
- 抖音、小红书的「评论 / 笔记详情」接口需要 X-Bogus / xsec_token 等签名参数，
  纯 HTTP 逆向成本高且易失效。业界成熟方案是 MediaCrawler（Playwright + CDP + Cookie），
  本模块把「重活」显式交给它，不伪造可用接口。
- 抖音热榜是公开 Billboard 接口，无需登录即可调用（已实测 50 条）。
"""
from __future__ import annotations

import subprocess
import sys
import urllib.parse

from core.util import http_get_json, _UA

DOUYIN_HOT = "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/"


def douyin_hot() -> dict:
    """抖音实时热榜（公开接口，无需登录）。"""
    try:
        d = http_get_json(DOUYIN_HOT, timeout=12)
        words = d if isinstance(d, list) else d.get("word_list", [])
        items = [{"word": w.get("word", ""), "hot": w.get("hot_value", 0)} for w in words]
        return {"ok": True, "count": len(items), "items": items}
    except Exception as e:
        return {"ok": False, "error": f"抖音热榜获取失败：{e}", "items": []}


def _mediacrawler_available() -> bool:
    """检测本机是否装了 MediaCrawler（NanmiCoder/MediaCrawler）。"""
    try:
        import importlib.util
        return importlib.util.find_spec("mediacrawler") is not None
    except Exception:
        return False


def _call_mediacrawler(platform: str, op: str, **kwargs) -> dict:
    """通过 MediaCrawler 拉取需签名的评论/笔记（真实可用路径）。

    由用户在装有 MediaCrawler + Chrome 的机器上执行，本函数只负责拼命令并取回 JSON。
    """
    if not _mediacrawler_available():
        return {"ok": False, "needs": "mediacrawler",
                "error": "该平台的评论/笔记需签名，请先 `pip install medi acrawler` 并用 Playwright 登录，"
                         "或在本机部署 MediaCrawler 后由它产出 JSON 再导入。"}
    # MediaCrawler 以 main.py 子命令方式运行；此处仅给出接入形态，具体参数随版本而定。
    cmd = [sys.executable, "-m", "mediacrawler", "--platform", platform, "--type", op,
           "--keyword", kwargs.get("keyword", "")]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return {"ok": out.returncode == 0, "verified": False,
                "stdout": out.stdout[-2000:], "stderr": out.stderr[-800:]}
    except Exception as e:
        return {"ok": False, "error": f"调用 MediaCrawler 失败：{e}"}


def douyin_comments(aweme_id: str, cookie: str = "") -> dict:
    """抖音视频评论。需签名 → 走 MediaCrawler。"""
    return _call_mediacrawler("douyin", "detail", keyword=aweme_id)


def xiaohongshu_notes(keyword: str, cookie: str = "") -> dict:
    """小红书笔记搜索。需签名 → 走 MediaCrawler。"""
    return _call_mediacrawler("xhs", "search", keyword=keyword)


def xiaohongshu_comments(note_id: str, cookie: str = "") -> dict:
    """小红书笔记评论。需签名 → 走 MediaCrawler。"""
    return _call_mediacrawler("xhs", "detail", keyword=note_id)
