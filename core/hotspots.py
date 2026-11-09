"""多源热搜采集 + 健身垂类相关性打分。

已实测（2026-11-06，禁用代理直连）：
  百度 board API / 抖音 billboard / B站 popular / 头条 hot-board / 贴吧 topicList 均可用；
  微博 ajax 403、知乎 401、抖音 web 热搜被风控，故不纳入。
另复用项目预置的 hot-search-cn 脚本作为补充源（其自身按平台容错）。
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime

from .util import http_get_json, write_json, DATA

BAIDU_URL = "https://top.baidu.com/api/board?platform=wise&tab=realtime"
DOUYIN_URL = "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/"
BILI_URL = "https://api.bilibili.com/x/web-interface/popular?ps=50&pn=1"
TOUTIAO_URL = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
TIEBA_URL = "https://tieba.baidu.com/hottopic/browse/topicList"
BILI_SPORT_URL = "https://api.bilibili.com/x/web-interface/ranking/v2?rid=234&type=all"

RISK_WORDS = ["去世", "死亡", "遇难", "判刑", "被捕", "诈骗", "战争", "坠机", "地震",
              "塌房", "离婚", "股价", "涨停", "通报", "处分", "涉政", "遗体", "枪击"]
# 与健身无直接字面关联、但存在真实内容桥接空间的泛生活热点
BRIDGE_WORDS = ["熬夜", "睡眠", "久坐", "压力", "情绪", "焦虑", "饮食", "外卖", "控糖",
                "健康", "体检", "亚健康", "自律", "打卡", "开学", "假期", "降温",
                "上班", "加班", "中年", "学生", "步数", "天气", "年龄"]


def _item(title, hot, platform, rank, url="") -> dict:
    return {"title": str(title).strip(), "hot": hot, "platform": platform,
            "rank": rank, "url": url, "fetched_at": datetime.now().isoformat(timespec="seconds")}


def _baidu(limit: int) -> list[dict]:
    d = http_get_json(BAIDU_URL, timeout=15)
    cards = d.get("data", {}).get("cards", [])
    out = []
    for card in cards:
        for entry in card.get("content", []):
            # 百度 board 有两种嵌套：外层嵌套 content 数组 / 直接是词条
            rows = entry.get("content", []) if isinstance(entry, dict) and "content" in entry else [entry]
            for row in rows:
                if isinstance(row, dict) and row.get("word"):
                    out.append(_item(row["word"], str(row.get("hotScore", "")),
                                     "百度", len(out) + 1, row.get("url", "")))
                if len(out) >= limit:
                    return out
    return out[:limit]


def _douyin(limit: int) -> list[dict]:
    d = http_get_json(DOUYIN_URL, timeout=15)
    return [_item(w.get("word", ""), w.get("hot_value", 0), "抖音", i + 1)
            for i, w in enumerate(d.get("word_list", [])[:limit]) if w.get("word")]


def _bilibili(limit: int) -> list[dict]:
    d = http_get_json(BILI_URL, timeout=15)
    return [_item(v.get("title", ""), v.get("stat", {}).get("view", 0), "B站", i + 1,
                  v.get("short_link_v2", ""))
            for i, v in enumerate(d.get("data", {}).get("list", [])[:limit]) if v.get("title")]


def _toutiao(limit: int) -> list[dict]:
    d = http_get_json(TOUTIAO_URL, timeout=15)
    return [_item(v.get("Title", ""), v.get("HotValue", 0), "头条", i + 1, v.get("Url", ""))
            for i, v in enumerate(d.get("data", [])[:limit]) if v.get("Title")]


def _tieba(limit: int) -> list[dict]:
    d = http_get_json(TIEBA_URL, timeout=15)
    rows = d.get("data", {}).get("bang_topic", {}).get("topic_list", [])
    return [_item(v.get("topic_name", ""), v.get("discuss_num", 0), "贴吧", i + 1)
            for i, v in enumerate(rows[:limit]) if v.get("topic_name")]


def _bilibili_sport(limit: int) -> list[dict]:
    """B站运动分区排行榜：真实的健身垂类内容供给，补全网热榜垂类命中率低的问题。"""
    d = http_get_json(BILI_SPORT_URL, timeout=15)
    rows = d.get("data", {}).get("list", [])
    return [_item(v.get("title", ""), v.get("stat", {}).get("view", 0), "B站运动榜", i + 1,
                  v.get("short_link_v2", ""))
            for i, v in enumerate(rows[:limit]) if v.get("title")]


def _hot_search_cn(cfg, limit: int) -> list[dict]:
    script = cfg["hotspot"].get("hot_search_cn_script", "")
    if not script:
        return []
    import os
    from pathlib import Path
    if not Path(script).exists():
        return []
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(k, None)
    try:
        r = subprocess.run([sys.executable, script, "all", "--limit", str(limit)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=50, env=env)
        import json
        data = json.loads(r.stdout)
        out = []
        for platform, rows in data.items():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict) and row.get("title"):
                    out.append(_item(row["title"], row.get("hot", 0), "hot_search:" + platform,
                                     row.get("rank", 0)))
        return out
    except Exception:
        return []


FETCHERS = {
    "baidu": _baidu, "douyin": _douyin, "bilibili": _bilibili,
    "toutiao": _toutiao, "tieba": _tieba, "bilibili_sport": _bilibili_sport,
}


def fetch_all(cfg: dict, run=None) -> tuple[list[dict], dict]:
    limit = cfg["hotspot"]["limit_per_source"]
    raw, stats = [], {}
    for name in cfg["hotspot"]["sources"]:
        t0 = time.time()
        try:
            rows = _hot_search_cn(cfg, limit) if name == "hot_search_cn" else FETCHERS[name](limit)
            bad = 0
        except Exception as e:
            rows, bad = [], 1
            if run:
                run.log(f"热搜源 {name} 失败：{type(e).__name__} {str(e)[:110]}", "WARN")
        stats[name] = {"count": len(rows), "ok": not bad, "ms": int((time.time() - t0) * 1000)}
        raw.extend(rows)
        if run:
            run.log(f"热搜源 {name}: {len(rows)} 条 / {stats[name]['ms']}ms")
    return raw, stats


def score_item(item: dict, cfg: dict, cross_count: int = 1) -> tuple[float, list[str]]:
    title = item["title"]
    keywords = cfg["niche"]["keywords"]
    hits = [k for k in keywords if k in title]
    score = 0.0
    if hits:
        score += 4.0 + min(len(hits), 3) * 1.2
    try:
        rank = int(item.get("rank") or 99)
    except (ValueError, TypeError):
        rank = 99
    score += max(0.0, 2.2 - rank / 12.0)
    if cross_count > 1:
        score += min(cross_count - 1, 3) * 1.5
    risk = [w for w in RISK_WORDS if w in title]
    score -= len(risk) * 4.0
    if len(title) < 5:
        score -= 1.0
    # 强相关热点优先；纯泛热点仅作为"蹭热点"候选
    return round(score, 2), hits


def collect(cfg: dict, run=None) -> dict:
    raw, stats = fetch_all(cfg, run)
    cross: dict[str, int] = {}
    for it in raw:
        cross[it["title"]] = cross.get(it["title"], 0) + 1

    seen, ranked = set(), []
    for it in raw:
        key = it["title"]
        if key in seen or not key:
            continue
        seen.add(key)
        s, hits = score_item(it, cfg, cross.get(key, 1))
        enriched = dict(it, score=s, keyword_hits=hits, cross_count=cross.get(key, 1),
                        risk=[w for w in RISK_WORDS if w in it["title"]])
        ranked.append(enriched)
    ranked.sort(key=lambda x: (-x["score"], -x["cross_count"]))

    fitness = [x for x in ranked if x["keyword_hits"] and not x["risk"]]
    fit_titles = {x["title"] for x in fitness}
    bridged = [x for x in ranked
               if x["title"] not in fit_titles and not x["risk"]
               and any(w in x["title"] for w in BRIDGE_WORDS)][:20]
    result = {
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "total_raw": len(raw), "unique": len(ranked), "sources": stats,
        "fitness_related": fitness, "bridge_candidates": bridged,
        "top": ranked[:40],
    }
    write_json(DATA / "hot" / f"{datetime.now():%Y-%m-%d}.json", result)
    if run:
        run.save("hotspots.json", result)
        run.step("热点采集", "done", f"{len(raw)} 条原始 / {len(ranked)} 条去重 / "
                                    f"{len(result['fitness_related'])} 条健身相关 / "
                                    f"{sum(1 for v in stats.values() if v['ok'])} 个源可用")
    return result
