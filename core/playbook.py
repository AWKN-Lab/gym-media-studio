"""参考素材方法库 / 同城对标打法：单一加载入口。

所有内容都来自 data/baseline 下已落盘的真实卡片抽取结果：
  - method_playbook.json  B 端获客卡片提炼的脚本骨架、钩子句式、形式、反例、C 端转译选题
  - local_benchmark.json  同城 17 条：自家达人钩子、差异化可用范围、竞品钩子类型

设计约束（A/B 严格回退依赖）：
  仅在 config.local.enabled 且 config.local.playbook.enabled 时生效；
  删掉 local 段，本模块所有函数一律返回空，产出逐字回到改前。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .util import ROOT, read_json


def playbook_cfg(cfg: dict) -> dict:
    local = cfg.get("local") or {}
    if not local.get("enabled"):
        return {}
    pb = local.get("playbook") or {}
    return pb if pb.get("enabled") else {}


def _load(rel: str) -> dict:
    if not rel:
        return {}
    return read_json(ROOT / rel, {}) or {}


def load_method(cfg: dict) -> dict:
    pb = playbook_cfg(cfg)
    return _load(pb.get("method_source", "")) if pb else {}


def load_benchmark(cfg: dict) -> dict:
    pb = playbook_cfg(cfg)
    return _load(pb.get("benchmark_source", "")) if pb else {}


def topic_transfers(cfg: dict) -> list[dict]:
    return list(load_method(cfg).get("topic_transfers") or [])


def pick_anchor(topic: dict, local: dict) -> str:
    """按标题稳定轮换人群锚点：同一选题每次生成同一句，不同选题可覆盖不同分支。

    锚点只描述「在哪活动 / 什么状态」，不描述「住在哪个小区」。
    """
    anchors = list(local.get("audience_anchors") or [])
    if not anchors:
        return ""
    key = str(topic.get("title") or topic.get("hook") or "")
    idx = int(hashlib.md5(key.encode("utf-8")).hexdigest()[:8], 16) % len(anchors)
    return anchors[idx]


def format_for(topic: dict, cfg: dict) -> str:
    """按选题稳定选一条实拍形式，给 visual_prompt 用。"""
    forms = [f for f in load_method(cfg).get("formats", []) if f.get("usable")]
    if not forms:
        return ""
    key = str(topic.get("title") or "")
    idx = int(hashlib.md5(("fmt:" + key).encode("utf-8")).hexdigest()[:8], 16) % len(forms)
    return forms[idx].get("visual_prompt", "")


def method_block(cfg: dict) -> str:
    """给 LLM 的 user 消息注入的方法说明；关掉 playbook 时返回空串。"""
    m, b = load_method(cfg), load_benchmark(cfg)
    if not m:
        return ""
    lines = ["参考素材方法（学结构与形式，不是照播 B 端内容）："]
    sk = [s["name"] for s in m.get("script_skeleton", [])]
    if sk:
        lines.append("脚本骨架：" + " → ".join(sk))
    hooks = [f'{h["pattern"]}（我方转写示例：{h["c_rewrite"]}）'
             for h in m.get("hook_patterns", [])]
    if hooks:
        lines.append("可用钩子句式：" + "；".join(hooks))
    forms = [f["name"] for f in m.get("formats", []) if f.get("usable")]
    if forms:
        lines.append("可用实拍形式：" + "；".join(forms))
    anti = [a["name"] for a in m.get("anti_patterns", [])]
    if anti:
        lines.append("必须避开：" + "；".join(anti))
    if b:
        u = b.get("usable_in_copy") or {}
        own = b.get("own_reference") or {}
        lines.append("自家对标来源：" + str(own.get("source_type", "")))
        if u.get("allowed"):
            lines.append("同城对标可用：" + "；".join(u["allowed"]))
        if u.get("forbidden"):
            lines.append("同城对标禁用：" + "；".join(u["forbidden"]))
    lines.append("证据边界：无视频逐字稿，以上是从卡片文本提炼的结构与我方转写，不是视频原文。")
    return "\n".join(lines)
