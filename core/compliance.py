"""发布前合规：健身行业绝对化用语 / 疗效承诺的本地拦截与自动改写。

外部审核（multi-wordcheck）需要 REDFOX_API_KEY，当前环境未配置，故默认走本地词库；
配置开关 compliance.external_api 打开且 Key 存在时才调用外部接口。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPLACE = {
    "最有效": "更稳妥", "第一": "优先", "唯一": "少见", "100%": "大概率",
    "百分百": "大概率", "保证": "尽量", "包瘦": "更容易坚持", "包治": "有助于",
    "根治": "改善", "治愈": "改善", "永不反弹": "更好维持", "躺瘦": "低门槛",
    "月瘦30斤": "稳步下降", "一周暴瘦": "逐步减重", "绝对": "通常",
    "国家级": "专业", "顶级": "专业", "特效": "针对性", "无副作用": "相对温和",
    "彻底解决": "明显改善", "医学证明": "实践观察", "祖传": "经验", "神器": "工具",
    "药到": "逐步", "必须": "建议", "一定": "多半", "千万别": "尽量别",
    "后悔一辈子": "容易走弯路", "智商税": "不太划算",
}

EXTERNAL_SCRIPT = Path("C:/Users/10919/.workbuddy/project-resources/"
                       "p_690992e4b77549fba56fca9ec9151e4f/"
                       "skill_2084102609373351936/multi-wordcheck/scripts/check_sensitive_words.py")


def _walk_strings(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                yield obj, k, v
            else:
                yield from _walk_strings(v)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str):
                yield obj, i, v
            else:
                yield from _walk_strings(v)


def _external_check(text: str, cfg: dict) -> dict | None:
    if not cfg["compliance"].get("external_api") or not os.environ.get("REDFOX_API_KEY"):
        return None
    if not EXTERNAL_SCRIPT.exists():
        return None
    try:
        r = subprocess.run([sys.executable, str(EXTERNAL_SCRIPT), "--content", text[:2800],
                            "--platform", "抖音"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=40)
        import json
        return json.loads(r.stdout)
    except Exception:
        return None


# 仅审核会公开发布的文字。画面生成提示词、内部元数据不参与，
# 否则「第一视角」「第一次进健身房」会被误当成绝对化用语改写。
PUBLISH_FIELDS = {"title", "cover_text", "caption", "narration", "on_screen", "sub"}
SAFE_PHRASES = ["第一视角", "第一次", "第一周", "第一天", "第一步", "第一时间",
                "第一组", "第一件", "第一轮", "第一年", "第一口", "第一顿"]


def _safe_replace(text: str, word: str) -> str:
    if word == "第一":
        for i, phrase in enumerate(SAFE_PHRASES):
            text = text.replace(phrase, f"\x00{i}\x00")
        text = text.replace(word, REPLACE.get(word, "优先"))
        for i, phrase in enumerate(SAFE_PHRASES):
            text = text.replace(f"\x00{i}\x00", phrase)
        return text
    return text.replace(word, REPLACE.get(word, "较为"))


def _scan(text: str, cfg: dict) -> tuple[str, list[dict]]:
    """返回（改写后文本, 命中记录）。仅本地词表，语义变体不保证覆盖。"""
    banned = cfg["compliance"]["local_banned"] + cfg["compliance"]["soft_banned"]
    hits: list[dict] = []
    new = text
    for w in banned:
        if w not in new:
            continue
        replaced = _safe_replace(new, w)
        # 只把「确实发生了改写」记为命中。SAFE_PHRASES（第一次 / 第一周 / 第一视角…）
        # 被保护时文本逐字未变，旧实现仍记一次命中，导致正常文案被误标待人工复核。
        if replaced != new:
            hits.append({"word": w})
            new = replaced
    return new, hits


def _report(hits: list, external_checked: bool, ext_hits: list, cfg: dict,
            extra: dict | None = None) -> dict:
    """审查报告。

    刻意不做「改写后即算通过」：出现命中说明原文有风险，必须由人复核；
    外部审核未启用 / 未取得平台授权与法务意见时，也一律不宣称 passed。
    此前本函数无条件写 passed=True，等于把闸门写成恒真，已由取证发现并修正。
    """
    limitations = [
        "仅本地词表字符串匹配，未覆盖语义变体与上下文",
        "未取得平台授权 / 法务书面意见（第 4 层证据缺失）",
    ]
    if not external_checked:
        limitations.append("外部语义审核未启用（compliance.external_api=false 或缺 Key）")
    passed = (not hits) and external_checked and (not ext_hits)
    if passed:
        status = "passed"
    elif hits or ext_hits:
        status = "rewritten_needs_review"
    else:
        status = "local_only_needs_review"
    rep = {
        "passed": passed,
        "status": status,
        "local_hits": len(hits),
        "external_hits": len(ext_hits),
        "external_checked": bool(external_checked),
        "scope": "local+external" if external_checked else "local_regex_only",
        "review_required": bool(hits or ext_hits or not external_checked),
        "limitations": limitations,
        "checked_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }
    if extra:
        rep.update(extra)
    return rep


def check_text(text: str, cfg: dict, external: bool = True) -> dict:
    """通用文本闸门：AI 回复、评论草稿、发布文案一律走这里。

    返回 {"text": 改写后文本, "report": 审查报告}。
    report.passed=False 时调用方须标记待人工复核，不得静默当作已通过。
    """
    cleaned, hits = _scan(str(text or ""), cfg)
    ext = _external_check(cleaned, cfg) if external else None
    ext_hits = [str(w) for w in (ext or {}).get("sensitive_words", [])][:20]
    rep = _report(hits, bool(ext), ext_hits, cfg,
                  {"replacements": [h["word"] for h in hits][:30]})
    return {"text": cleaned, "report": rep}


def check_and_fix(script: dict, cfg: dict, run=None) -> dict:
    """发布前扫描脚本的公开字段。返回带审查报告的 script。"""
    found: list[dict] = []
    publish_texts = []
    for container, key, value in _walk_strings(script):
        if str(key) not in PUBLISH_FIELDS:
            continue
        publish_texts.append(value)
        cleaned, hits = _scan(value, cfg)
        for h in hits:
            found.append({"field": str(key), "word": h["word"]})
        if cleaned != value:
            container[key] = cleaned

    all_text = " ".join(publish_texts)
    ext = _external_check(all_text, cfg)
    ext_hits = [str(w) for w in (ext or {}).get("sensitive_words", [])][:20]
    rep = _report(found, bool(ext), ext_hits, cfg,
                  {"replacements": found[:30], "fields_scanned": len(publish_texts)})
    script["compliance"] = rep
    if run and (found or ext_hits):
        run.log(f"合规改写 {len(found)} 处 / 外部命中 {len(ext_hits)} 处；状态={rep['status']}")
    return script
