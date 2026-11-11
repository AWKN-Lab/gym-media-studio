"""拍摄清单与发布包：脚本的下一站是拍摄，不是另一份稿子。

shot_list() 把一条 5 镜脚本翻成 5 个镜头：每镜的地点、画面、口播、人物、
预估时长。地点取自本地配置（地铁站、门店、器械区），人物一律写“教练或
真实新客出镜，不背词”——没有真人，这份清单拍不出来，这是设计，不是遗漏。

jianying_draft() 按剪映草稿 draft_content.json 的公开形态拼一份草稿：
5 条字幕轨道对应 5 镜画面大字。未在剪映里验证打开，须在 Windows 剪映里
打开确认；打不开就以这份清单手工建草稿，不要硬说它能用。

publish_pack() 是发布前的一包东西：标题、封面、文案、拍摄清单、剪映草稿、
POI 与团购占位。团购券未上架时 group_buy 是空，须在抖音来客上架后填链接。
"""
from __future__ import annotations

import uuid


def _est_seconds(narration: str) -> int:
    # 中文口播约每秒 4.5 字；每镜至少 5 秒。只是拍摄排期的估计，不是实测。
    return max(5, round(len(narration) / 4.5))


def shot_list(script: dict, cfg: dict) -> list[dict]:
    local = cfg.get("local") or {}
    store = local.get("store") or "门店"
    transit = local.get("transit") or ""
    door = f"{transit}出来到{store}门口" if transit else f"{store}门口"
    locations = [door, f"{store}店内", "器械区", "器械区", f"{store}门口"]
    shots = []
    for i, scene in enumerate(script["scenes"]):
        shots.append({
            "scene": scene.get("index", i + 1),
            "narration": scene["narration"],
            "on_screen": scene["on_screen"],
            "visual_prompt": scene.get("visual_prompt", ""),
            "location": locations[i] if i < len(locations) else "器械区",
            "people": "教练或真实新客出镜，不背词",
            "est_seconds": _est_seconds(scene["narration"]),
        })
    return shots


def jianying_draft(script: dict, cfg: dict) -> dict:
    shots = shot_list(script, cfg)
    texts, segments = [], []
    start_us = 0
    for i, shot in enumerate(shots):
        dur_us = shot["est_seconds"] * 1_000_000
        mid = f"text-{i}"
        texts.append({"content": shot["on_screen"], "id": mid, "type": "text",
                      "font_size": 60})
        segments.append({"id": f"seg-{i}", "material_id": mid,
                         "target_timerange": {"start": start_us, "duration": dur_us},
                         "type": "text"})
        start_us += dur_us
    return {
        "version": "1.0",
        "canvas_config": {"width": 1080, "height": 1920, "ratio": "9:16"},
        "materials": {"texts": texts},
        "tracks": [{"id": "track-text", "type": "text", "segments": segments}],
        "unverified": "未在剪映里验证打开，须在 Windows 剪映里打开确认",
    }


def publish_pack(script: dict, cfg: dict) -> dict:
    local = cfg.get("local") or {}
    return {
        "title": script["title"],
        "cover_text": script.get("cover_text", ""),
        "caption": script.get("caption", ""),
        "poi": {"store": local.get("store", ""), "transit": local.get("transit", ""),
                "community": local.get("community", "")},
        "group_buy": None,  # 团购券未上架：须在抖音来客上架后填链接
        "shots": shot_list(script, cfg),
        "jianying_draft": jianying_draft(script, cfg),
        "draft_id": f"draft-{uuid.uuid4().hex[:8]}",
    }
