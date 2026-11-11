"""高光挑选：把转写分段（SRT 式时间戳）挑出最值得剪的一段连续片段。

做法借用 FunClip 的 demo_prompt（SRT 进 → 最有意义的连续片段出），
但本机不调 LLM：按关键词密度 + 提问/数字信号给每段打分，再滑窗挑出
时长在 [min_dur, max_dur] 内、总分最高的连续窗口。输入分段格式与
core/transcribe.py 的 segments 一致：[{"start", "end", "text"}]。
"""
from __future__ import annotations

import re

# 连续窗口内相邻两段的最大间隔：隔得更远就是两段话，不拼成一个窗口。
_MAX_GAP = 10.0


def _clean(segments: list[dict] | None) -> list[dict]:
    """清洗输入分段：非数字时间戳、负数起点、end 不大于 start 的一律丢弃，
    剩下按 start 排序（GPT 第二次复核第 5 节第 5 项）。"""
    out = []
    for s in (segments or []):
        if not isinstance(s, dict):
            continue
        try:
            start, end = float(s.get("start")), float(s.get("end"))
        except (TypeError, ValueError):
            continue
        if end <= start or start < 0:
            continue
        out.append({"start": start, "end": end, "text": str(s.get("text", ""))})
    out.sort(key=lambda x: x["start"])
    return out
_HOOK_WORDS = ("为什么", "怎么", "别", "注意", "记住", "先", "其实", "原来",
               "千万", "如果", "?", "？", "吗")
_DEFAULT_KEYWORDS = ("深蹲", "膝盖", "热身", "拉伸", "减脂", "蛋白", "动作",
                     "呼吸", "发力", "组", "次")


def _segment_score(seg: dict, keywords: list[str]) -> float:
    text = str(seg.get("text", ""))
    score = 0.0
    for kw in keywords:
        if kw and kw in text:
            score += 2.0
    for w in _HOOK_WORDS:
        if w in text:
            score += 1.0
    if re.search(r"\d+", text):
        score += 2.0
    # 过短的片段（半句话）不值得单独成段
    if len(text) < 4:
        score -= 3.0
    return score


def pick_highlight(segments: list[dict], keywords: list[str] | None = None,
                   min_dur: float = 8.0, max_dur: float = 30.0) -> dict | None:
    """返回 {"start", "end", "text", "score", "segment_indexes"}，无可用分段返回 None。

    segment_indexes 是清洗排序后列表的下标。窗口绝不跨超过 _MAX_GAP 的间隔；
    单段回退的 end 也夹到 max_dur 内（GPT 第二次复核第 5 节第 5 项）。"""
    segs = _clean(segments)
    if not segs:
        return None
    kws = list(keywords or []) + list(_DEFAULT_KEYWORDS)
    scores = [_segment_score(s, kws) for s in segs]
    best: dict | None = None
    for i in range(len(segs)):
        total, dur = 0.0, 0.0
        for j in range(i, len(segs)):
            if j > i and segs[j]["start"] - segs[j - 1]["end"] > _MAX_GAP:
                break
            dur = float(segs[j]["end"]) - float(segs[i]["start"])
            if dur > max_dur:
                break
            total += scores[j]
            if dur >= min_dur and (best is None or total > best["score"]):
                best = {
                    "start": round(float(segs[i]["start"]), 2),
                    "end": round(float(segs[j]["end"]), 2),
                    "text": "".join(str(s.get("text", "")) for s in segs[i:j + 1]),
                    "score": round(total, 1),
                    "segment_indexes": list(range(i, j + 1)),
                }
    if best is None:
        # 全片不足 min_dur：返回得分最高的单段，不硬凑；单段本身超长时截到 max_dur。
        i = max(range(len(segs)), key=lambda k: scores[k])
        end = min(float(segs[i]["end"]), float(segs[i]["start"]) + max_dur)
        best = {
            "start": round(float(segs[i]["start"]), 2),
            "end": round(end, 2),
            "text": str(segs[i].get("text", "")),
            "score": round(scores[i], 1),
            "segment_indexes": [i],
        }
    return best
