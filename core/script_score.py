"""脚本打分：一次生成多版，按四项打分选优，分低自动重写。

打分维度借用 social-account-doctor 的拆解思路（7 种视频开头钩子、10 个标题公式、
3 种正文骨架），判定规则为 GYM 自写；合规项只读 core/compliance 的禁词表（REPLACE），
不改合规闸本身。分数只用于选优，不代替合规闸：合规闸的结论以 compliance.check_and_fix 为准。
"""
from __future__ import annotations

import re

from .compliance import REPLACE, SAFE_PHRASES

# AI 味套话：命中即扣口语分。只列高频空话，不列正常词，避免误伤。
_AI_CLICHES = ("不是不努力", "在快节奏的时代", "让我们一起", "总之",
               "首先", "其次", "值得一提的是", "只要坚持", "相信我",
               "开启你的", "在当今社会", "众所周知")
# 钩子公式关键词（social-account-doctor 七种开头钩子的转译：提问、反差、数字、承诺、悬念、身份、利他）
_HOOK_MARKERS = ("你是不是", "以为", "其实", "原来", "先说答案", "先说结论", "记住这",
                 "别急着", "别", "如果你是", "只要", "?", "？")
# 具体信息：数字 + 单位（信息密度的硬指标）
_UNIT_RE = re.compile(r"\d+\s*(?:次|组|分钟|秒|克|斤|天|周|个|招|条|度|%)")
_DIGIT_RE = re.compile(r"\d+")


def _scenes(script: dict) -> list[dict]:
    return [s for s in (script.get("scenes") or []) if isinstance(s, dict)]


def _narrations(script: dict) -> list[str]:
    return [str(s.get("narration", "")) for s in _scenes(script)]


def score_hook(script: dict) -> tuple[int, list[str]]:
    """钩子 25 分：首镜有没有钩子公式、有没有具体信息、画面主文案是否完整。"""
    issues: list[str] = []
    score = 0
    scenes = _scenes(script)
    if not scenes:
        return 0, ["没有分镜"]
    first = str(scenes[0].get("narration", ""))
    if any(m in first for m in _HOOK_MARKERS):
        score += 10
    else:
        issues.append("首镜未命中钩子公式（反问/反常识/数字/结论前置）")
    if _DIGIT_RE.search(first) or _UNIT_RE.search(first):
        score += 10
    else:
        issues.append("首镜无数字或具体信息")
    on_screen = str(scenes[0].get("on_screen", "")).strip()
    if on_screen and len(on_screen) <= 9:
        score += 5
    else:
        issues.append("首镜画面主文案缺失或超过 9 字")
    return min(score, 25), issues


def score_density(script: dict) -> tuple[int, list[str]]:
    """信息密度 25 分：每镜至少一个可验证的具体信息（数字/动作/判断标准）。

    数字只从 narration 取：visual_prompt 是画面描述词，里面的数字
    （如“3 个机位”）不是口播信息，算进去就是抬分（GPT 第二次复核第 5 节第 6 项）。"""
    issues: list[str] = []
    scenes = _scenes(script)
    if not scenes:
        return 0, ["没有分镜"]
    per = 25 // max(1, len(scenes))
    score = 0
    for i, s in enumerate(scenes, 1):
        text = str(s.get("narration", ""))
        if _UNIT_RE.search(text) or _DIGIT_RE.search(text):
            score += per
        else:
            issues.append(f"第 {i} 镜无数字或具体信息")
    return min(score, 25), issues


def score_oral(script: dict) -> tuple[int, list[str]]:
    """口语度 25 分：短句为主，单镜口播 30~50 字，超长句扣分。"""
    issues: list[str] = []
    narrs = _narrations(script)
    if not narrs:
        return 0, ["没有口播"]
    score = 25
    for i, text in enumerate(narrs, 1):
        n = len(text)
        if n > 60:
            score -= 5
            issues.append(f"第 {i} 镜口播 {n} 字过长")
        elif n < 15:
            score -= 5
            issues.append(f"第 {i} 镜口播 {n} 字过短，信息不足")
        # 单句超过 25 字无停顿 → 念起来喘不上气
        for clause in re.split(r"[，。！？、；]", text):
            if len(clause) > 25:
                score -= 3
                issues.append(f"第 {i} 镜有超 25 字的无停顿长句")
                break
    # AI 味套话：每命中一句扣 3 分
    for text in narrs:
        hit = [c for c in _AI_CLICHES if c in text]
        for c in hit:
            score -= 3
            issues.append(f"套话：{c}")
    return max(score, 0), issues


def _hit(word: str, texts: list[str]) -> bool:
    """禁词命中判定。「第一」要按合规闸的 SAFE_PHRASES 保护词走：
    第一次 / 第一周 / 第一视角等不算命中，和 check_and_fix 的口径一致。"""
    if word == "第一":
        suffixes = "|".join(re.escape(p[2:]) for p in SAFE_PHRASES)
        pat = re.compile("第一(?!(" + suffixes + "))")
        return any(pat.search(t) for t in texts)
    return any(word in t for t in texts)


def score_compliance(script: dict) -> tuple[int, list[str]]:
    """合规 25 分：公开字段命中禁词表（只读 REPLACE）即扣分；0 命中满分。

    扫描字段：标题、封面、文案、口播、画面大字、画面小字 —— 禁词藏在
    on_screen / sub 里也必须被扫到（GPT 第二次复核第 5 节第 6 项）。"""
    issues: list[str] = []
    texts = [str(script.get("title", "")), str(script.get("cover_text", "")),
             str(script.get("caption", ""))]
    texts += _narrations(script)
    for s in _scenes(script):
        texts.append(str(s.get("on_screen", "")))
        texts.append(str(s.get("sub", "")))
    hits = sorted({w for w in REPLACE if _hit(w, texts)})
    if hits:
        issues.append("命中禁词：" + "、".join(hits[:8]))
    return max(0, 25 - 5 * len(hits)), issues


def score_script(script: dict) -> dict:
    """四项打分汇总。返回 {"total", "hook", "density", "oral", "compliance", "issues"}。"""
    hook, i1 = score_hook(script)
    density, i2 = score_density(script)
    oral, i3 = score_oral(script)
    comp, i4 = score_compliance(script)
    return {
        "total": hook + density + oral + comp,
        "hook": hook, "density": density, "oral": oral, "compliance": comp,
        "issues": i1 + i2 + i3 + i4,
    }
