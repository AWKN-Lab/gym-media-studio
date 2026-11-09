"""脚本生成：本地开源网关的免费/登录态模型优先 → 用户主动填的 Key → 内置模板降级。

零密钥默认值（2026-11-06 实测）：
- 会话注入的 DEEPSEEK / DASHSCOPE / MINIMAX 等 8 个 API Key 一律不自动读取。
- Magpie（喜鹊，github.com/yetone/magpie）在本机 127.0.0.1:3425/v1 提供
  OpenAI 兼容网关。用户在 Magpie 客户端「供应商」里登录 WorkBuddy / OpenCode Zen /
  Copilot 等订阅即可用模型，本平台不填任何 Key。
  实测 openrouter/recursive/flint-v1.0:free 返回 cost=0 且能输出中文 JSON；
  auto/minimax-m3.1-flash（登录态订阅）同样可用。
- 网关离线或额度耗尽时，才尝试用户在控制台主动填写的 Key；全不可用则内置模板照常出片。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .util import http_get_json, http_post_json, read_json, write_json, DATA
from .secrets import get_secret, resolve_provider
from .playbook import format_for, method_block, pick_anchor
from .offer import audit as offer_audit, offer_block, slots as offer_slots

SYSTEM_PROMPT = """你是一家连锁健身房的短视频编导，负责写 45~60 秒竖屏口播脚本。
要求：
1. 口语化、短句、反鸡汤，不写空话套话；每个分镜给一个可验证的具体信息（数字/动作/判断标准）。
2. 绝对不使用绝对化、疗效承诺类词汇（最有效、保证、100%、根治、包瘦等）。
3. 不编造医学结论，涉及伤病一律提示「疼痛请找医生或线下评估」。
4. on_screen 必须是语义完整的短句（<=9 字），禁止截断半句话；sub 亦然（<=18 字）。
5. 输出严格 JSON，不要 markdown 代码块，不要解释文字。
JSON 结构：
{
 "title": "视频标题（不超过 24 字）",
 "cover_text": "封面大字（不超过 10 字，语义完整）",
 "caption": "发布文案，含 3~5 个话题标签",
 "scenes": [
   {"narration":"口播文案 30~50 字","on_screen":"画面主文案 <=9 字","sub":"画面小字补充 <=18 字",
    "visual_prompt":"健身房实拍画面描述，中文 20~40 字，用于后续 AI 生成或素材检索"}
 ]
}
scenes 固定 5 个，依次为：钩子、痛点共鸣、干货点1、干货点2、行动号召。"""

# 本地门店号补充规则：仅在 config.local.enabled 时拼到 SYSTEM_PROMPT 之后。
# 主提示词保持通用，删掉 local 段即严格回退 —— A/B 证伪依赖这一点。
# 依据见 data/baseline/method_logic.json（六条原则均引自真实搜索卡片）。
LOCAL_SYSTEM_ADDON = """
本地门店号补充规则（优先级高于以上通用要求）：
6. 前 3 秒必须点名「谁 + 在哪」，但禁止断言受众住在某个小区或公寓：用站点、片区、商圈或行为状态（在前浯附近找健身房 / 下班从 4 号线出来 / 第一次想进馆怕被推卡）；禁止「很多人」「大家」这类泛人群；若给定 hook 已含锚点，不要重复堆叠地名。
7. 痛点要落到本地人能对号入座的具体阻力（下班晚、怕被推卡、预算、社恐、通勤），不写通用焦虑。
8. 收尾除品牌引导语外，必须给可执行的到店动作；caption 必须带给定的同城话题标签与门店位置。
9. 画面描述用第一人称门店实拍；不写硬核门槛暗示，明确健身小白与社恐也适合。
10. 具体承诺以注入的「当前执行的承诺方案」为准：方案未放行具体承诺时，禁止写价格、活动有效期、营业时间等，须以门店当期公示为准；不得把达人探店话术当门店官方承诺；不得点名或贬低同行。
11. 按给定的脚本骨架与钩子句式写，画面对应给定的实拍形式，并主动避开给定的反例清单。
12. 同城对标只吸收形式与通用判断标准（避坑清单、动线实拍、可验证细节），不引用竞品评价、不写价格、不影射同行。
13. 按注入的「当前执行的承诺方案」四结构位（开头 / 钩子 / 内容 / 结尾）写；方案未确认的具体承诺一律不写，不得自行编造价格、活动有效期、时长或「无推销」。"""


def _local_cfg(cfg: dict) -> dict:
    local = cfg.get("local") or {}
    return local if local.get("enabled") else {}


# tag 为画面主文案（人工拟好，避免从长句硬截断导致语义断裂）
KNOWLEDGE = [
    {"keys": ["新手", "第一次", "入门", "进健身房"], "points": [
        {"tag": "别急着加片", "text": "第一周别追重量。每个动作 2 组、每组 12 次，先把动作轨迹走稳，比加片有用得多。"},
        {"tag": "先热身 8 分钟", "text": "进馆先花 8 分钟热身：关节活动加动态拉伸。冷启动直接上大重量，是新手最常见的受伤原因。"},
    ]},
    {"keys": ["圆肩", "驼背", "体态", "肩颈", "骨盆"], "points": [
        {"tag": "靠墙站 30 秒", "text": "先靠墙站 30 秒，看后脑勺和肩胛能不能同时贴墙。贴不上，说明胸小肌大概率是紧的。"},
        {"tag": "墙天使 2 组", "text": "每天做 2 组墙天使，每组 10 次，重点不是抬多高，而是肋骨别翻、腰别塌。"},
    ]},
    {"keys": ["减脂", "热量", "体重", "瘦", "胖", "体脂"], "points": [
        {"tag": "先记 3 天饮食", "text": "先记 3 天饮食再谈调整。多数人不是吃得不够少，是漏算了饮料、酱料和周末那两顿。"},
        {"tag": "缺口别拉太大", "text": "每天 300 到 500 千卡缺口就够。缺口拉太大，掉得快的多半是水和肌肉，也更容易反弹。"},
    ]},
    {"keys": ["深蹲", "膝盖", "硬拉", "卧推", "动作"], "points": [
        {"tag": "膝盖对齐脚尖", "text": "下蹲时膝盖方向和脚尖一致，别内扣。蹲到髋略低于膝就够了，不必一味追求深度。"},
        {"tag": "刺痛就先停", "text": "如果蹲下去膝前侧刺痛，先停，别靠忍。动作模式或活动度的问题，需要线下评估，不是加练能解决的。"},
    ]},
    {"keys": ["一周", "频次", "练几次", "恢复", "计划"], "points": [
        {"tag": "一周 3 次够用", "text": "上班族一周 3 次、每次 45 分钟，坚持 8 周，效果通常好过一周 6 次练两周就断。"},
        {"tag": "看第二天反应", "text": "判断强度合不合适，看第二天：轻微酸胀正常，影响到上下楼梯就该减量了。"},
    ]},
    {"keys": ["蛋白", "饮食", "营养", "吃"], "points": [
        {"tag": "每餐一掌心", "text": "按每公斤体重 1.2 到 1.6 克蛋白质估算就够用，不用精确到克，先保证每餐都有一掌心。"},
        {"tag": "别抢 30 分钟", "text": "训练后那顿不必抢 30 分钟窗口，把全天总量吃够，比掐点更重要。"},
    ]},
    {"keys": ["热身", "拉伸", "10 分钟", "十分钟"], "points": [
        {"tag": "热身不是拉酸", "text": "热身的目标是把关节活动度和体温提上来，不是把肌肉拉到酸。5 分钟动态活动加 3 组小重量递增就够。"},
        {"tag": "静态拉伸殿后", "text": "静态拉伸放在训练后。训练前长时间压腿，反而会短时间降低发力表现。"},
    ]},
]

GENERIC_POINTS = [
    {"tag": "先定 8 周计划", "text": "先定一个能连续做 8 周的最小计划，比一次性练到位更管用。记录重量和次数，看得见进步才留得下来。"},
    {"tag": "动作质量优先", "text": "动作质量优先于训练量。拍一段自己的侧面视频，比对关节轨迹，比凭感觉判断准得多。"},
]


def _knowledge_for(topic: dict) -> list[dict]:
    text = topic["title"] + topic.get("angle", "") + topic.get("hot_ref", "")
    for block in KNOWLEDGE:
        if any(k in text for k in block["keys"]):
            return block["points"]
    return GENERIC_POINTS


def _first_clause(text: str, limit: int) -> str:
    """取第一个完整句子，而不是粗暴截断字符。"""
    head = re.split(r"[。！？!?\n]", text, maxsplit=1)[0].strip("，,、 ")
    head = head.replace(" ", "")
    if len(head) <= limit:
        return head
    # 仍过长时按逗号等次级停顿断，保持语义完整
    part = re.split(r"[，,；;]", head, maxsplit=1)[0]
    return part[:limit] if len(part) > limit else part


def _short(text: str, n: int) -> str:
    text = re.sub(r"[\s，。！？、,.!?]", "", text)
    return text[:n]


def template_script(topic: dict, cfg: dict) -> dict:
    """零凭据降级：生成有信息量的结构化脚本，不是占位符。

    两条生成入口（LLM / 模板）都必须注入本地获客逻辑；只改 LLM 路径会让
    零凭据降级出的成片仍然是全网通用文案 —— 这正是「源码在位≠产物在位」。
    """
    brand, niche = cfg["brand"], cfg["niche"]
    local = _local_cfg(cfg)
    pts = _knowledge_for(topic)
    hook = topic["hook"]
    pain = (f"{niche['audience'].split('、')[0]}最容易卡在这一步："
            "不是不努力，是一开始方向就选偏了。")
    if topic["kind"] == "hot_direct":
        pain = f"「{topic['hot_ref'][:14]}」讨论得很热闹，但落到普通人身上，真正该问的是怎么开始。"
    cta = (f"{brand['cta']}。想要更稳的做法，{brand['name']}可以先给你做一次体态评估。")

    # 当前执行的承诺方案：四结构位（开头 / 钩子 / 内容 / 结尾）全部从方案库取，
    # 关掉 local.offer 时 of 为空，逐项回退到 config 里的现行值 —— 严格回退。
    of = offer_slots(cfg) if local else {}
    op, hk = of.get("opening") or {}, of.get("hook") or {}
    bd, cl = of.get("body") or {}, of.get("closing") or {}

    if local:
        # P1 点名「谁 + 在哪」；P2 痛点落到本地具体阻力；P5 收尾给到店动作。
        # 2026-11-08 用户纠偏：不再写「住浯家公寓」，改按标题稳定轮换的多分支
        # 行为/状态锚点；本地选题自带地理锚点时不再叠加，避免「4 号线…4 号线」。
        # 2026-11-08 二次改造：锚点池、开头模板、钩子口径、内容、结尾全部可换。
        anchor_pool = op.get("anchors") or local.get("audience_anchors") or []
        tokens = local.get("anchor_tokens") or []
        has_anchor = any(t and t in hook for t in tokens)
        # 钩子口径先接在主钩子前，再由「开头」锚点包在最前面 —— 结构顺序是
        # 开头 → 钩子 → 主钩子。选题已自带本地锚点（如「在翔安前浯附近找健身房的」）
        # 时不再叠加口径，否则会出现「先别问年卡…先别急着问年卡」的语义重复。
        hook_prefix = (hk.get("text") or "").strip()
        if hook_prefix and not has_anchor:
            hook = f"{hook_prefix}{hook}"
        if topic.get("hook_mode", "needs_anchor") == "needs_anchor" and not has_anchor:
            # 切换方案后 topic 里缓存的 audience_anchor 属于旧锚点池，必须按当前方案重选。
            if of and op.get("anchors"):
                anchor = pick_anchor(topic, {"audience_anchors": anchor_pool})
            else:
                anchor = topic.get("audience_anchor") or pick_anchor(topic, local)
            anchor = anchor.strip("，, ")
            if anchor:
                head = (op.get("text") or "{anchor}").replace("{anchor}", anchor).strip("，, ")
                hook = f"{head}，{hook}"
        local_pain = (bd.get("text") or local.get("pain_local") or "").strip()
        if local_pain:
            pain = (f"{pain}。{local_pain}" if topic["kind"] == "hot_direct"
                    else local_pain)
        store_cta = (cl.get("text") or local.get("cta_store") or "").strip("。 ")
        cta = f"{brand['cta']}。{store_cta}。"

    narrations = [hook, pain, pts[0]["text"], pts[1]["text"], cta]
    on_screens = [
        (hk.get("on_screen") or topic.get("hook_short") or _first_clause(hook, 9)), "问题不在意志力",
        pts[0]["tag"], pts[1]["tag"], "现在就开始",
    ]
    subs = [
        f"{brand['name']} · 60 秒讲清楚",
        "先别急着加练 / 加量 / 买装备",
        _first_clause(pts[0]["text"], 18),
        _first_clause(pts[1]["text"], 18),
        brand["slogan"],
    ]
    visual = [
        "健身房门口推门进入的第一视角，光线通透，干净利落",
        "久坐上班族揉肩的近景，办公室背景虚化",
        "教练示范动作的中景，器械区背景，侧面机位",
        "动作细节特写，关节角度清晰，稳定器缓慢推进",
        "教练面对镜头微笑收尾，门店 logo 虚化在背景",
    ]
    if local:
        on_screens[1] = bd.get("on_screen") or "怕被推卡？"
        on_screens[4] = cl.get("on_screen") or "先试一次"
        subs[0] = f"{local.get('district', '')} · 60 秒讲清楚"
        subs[4] = local.get("transit") or brand["slogan"]  # P5：画面小字给到店锚点
        visual[0] = (f"从{local.get('transit', '')}出来走到"
                     f"{local.get('store') or brand['name']}门口，推门进入的第一人称实拍，光线通透")
        # 方法库实拍形式（按标题稳定选择）落到画面描述，而不是只写在文档里。
        shot = format_for(topic, cfg)
        if shot:
            visual[2] = shot
    scenes = [{
        "index": i + 1, "narration": narrations[i], "on_screen": on_screens[i],
        "sub": subs[i], "visual_prompt": visual[i],
    } for i in range(5)]

    cover = topic.get("cover") or _first_clause(topic["title"], 9)
    linked = topic.get("linked_hot", "")
    hot_line = f"\n今日相关热点：{linked[:20]}" if linked else ""
    tags = "#健身 #健身房 #科学训练 #新手健身 #体态管理"
    cta_lines = [brand["cta"], brand["handle"]]
    if local:
        # P5：同城标签与门店位置接住本地分发；具体价格/活动不写进文案。
        tags += " " + " ".join(local.get("tags_local") or [])
        cta_lines = [brand["cta"], cl.get("text") or local.get("cta_store", ""),
                     brand["handle"],
                     f"门店：{local.get('store', '')} · {local.get('transit', '')}"]
    caption = (f"{topic['title']}\n\n{hook}\n\n{pts[0]['text'][:44]}……{hot_line}\n\n"
               + "\n".join(cta_lines) + f"\n\n{tags}")
    return {
        "title": topic["title"][:24],
        "cover_text": cover[:10],
        "caption": caption,
        "scenes": scenes, "generator": "template", "topic": topic,
        "offer_audit": offer_audit(cfg),
    }


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


# ---------------------------------------------------------------- 本地开源网关（喜鹊 / Ollama / gpt4free ...）
GATEWAY_CACHE = DATA / "gateway_status.json"
GATEWAY_TTL = 600  # 秒；避免每次刷新界面都去打模型
_GW_LOCK = threading.Lock()  # 防止界面轮询与「重新检测」并发把本机网关打爆


def gateway_cfg(cfg: dict) -> dict:
    g = dict((cfg.get("llm") or {}).get("gateway") or {})
    g.setdefault("enabled", True)
    g.setdefault("name", "Magpie 喜鹊")
    g.setdefault("base_url", "http://127.0.0.1:3425/v1")
    g.setdefault("model", "")
    g.setdefault("api_key", "")
    g.setdefault("prefer_free", True)
    g["base_url"] = str(g["base_url"]).rstrip("/")
    return g


def _gw_headers(cfg: dict) -> dict:
    """本机网关默认无需鉴权；若用户填了网关自身的 token 才带 Authorization。"""
    k = gateway_cfg(cfg)["api_key"]
    return {"Authorization": "Bearer " + k} if k else {}


def _gw_url(cfg: dict, tail: str) -> str:
    return gateway_cfg(cfg)["base_url"] + tail


def gateway_models(cfg: dict, timeout: int = 20) -> list[dict]:
    """读模型目录；本机网关偶发被并发推理请求堵住，给一次重试。"""
    last = None
    for attempt in range(2):
        try:
            d = http_get_json(_gw_url(cfg, "/models"), timeout=timeout,
                              headers=_gw_headers(cfg)) or {}
            return d.get("data") or []
        except Exception as e:
            last = e
            if attempt == 0:
                time.sleep(0.5)
    raise last


def _is_free(mid: str) -> bool:
    s = mid.lower()
    return s.endswith(":free") or s.endswith("-free") or "-free-" in s


def _gw_probe(cfg: dict, model: str, timeout: int = 25) -> tuple[bool, str]:
    # 不设 max_tokens：网关里多为推理模型，实测设 1800 会把预算全花在 reasoning 上，
    # 返回 502 empty reply。探测也用与生产同形的 JSON 请求，避免「短请求绿、长请求红」。
    try:
        d = http_post_json(_gw_url(cfg, "/chat/completions"),
                           {"model": model,
                            "messages": [
                                {"role": "system", "content": "只输出严格 JSON，不要解释。"},
                                {"role": "user", "content": '输出 {"ok":1}'}]},
                           _gw_headers(cfg), timeout=timeout)
        return bool(d.get("choices")), "可用"
    except Exception as e:
        return False, str(e)[:110]


def _gw_candidates(cfg: dict, ids: list[str]) -> list[str]:
    g = gateway_cfg(cfg)
    free = [i for i in ids if _is_free(i)]
    # 实测 OpenRouter 的 :free 比 OpenCode Zen 的 *-free 更稳（后者有每日额度，会 429）
    free.sort(key=lambda i: (0 if ":free" in i else 1))
    subs = [i for i in ids if i.startswith("auto/")][:4] if not g["prefer_free"] else []
    picks = ([g["model"]] if g["model"] else []) + free[:8] + subs
    return list(dict.fromkeys(i for i in picks if i))


def gateway_status(cfg: dict, force: bool = False, run=None) -> dict:
    """并发保护：强制检测串行；普通读取遇检测进行中直接返回上次结论，不卡住界面轮询。"""
    if force:
        with _GW_LOCK:
            return _gateway_status(cfg, True, run)
    if not _GW_LOCK.acquire(blocking=False):
        return read_json(GATEWAY_CACHE, {}) or {
            "online": False, "models": [], "free": [], "selected": "",
            "error": "正在检测，沿用上次结论"}
    try:
        return _gateway_status(cfg, False, run)
    finally:
        _GW_LOCK.release()


def _gateway_status(cfg: dict, force: bool, run=None) -> dict:
    """网关在线状态、模型目录、自动选中的实测可用模型（无需任何 Key）。"""
    g = gateway_cfg(cfg)
    cached = read_json(GATEWAY_CACHE, {})
    fresh = (cached.get("base_url") == g["base_url"]
             and cached.get("model_cfg") == g["model"]
             and cached.get("enabled") == bool(g["enabled"])
             and time.time() - float(cached.get("ts", 0)) < GATEWAY_TTL)
    if not force and fresh:
        return cached
    st = {"ts": time.time(), "base_url": g["base_url"], "model_cfg": g["model"],
          "name": g["name"], "enabled": bool(g["enabled"]),
          "api_key_set": bool(g["api_key"]),   # 不把任何凭据回显给前端
          "prefer_free": bool(g["prefer_free"]), "online": False,
          "models": [], "free": [], "selected": "", "probed": [], "error": ""}
    if not g["enabled"]:
        st["error"] = "已在平台设置中关闭"
        write_json(GATEWAY_CACHE, st)
        return st
    try:
        ms = gateway_models(cfg)
        ids = [m.get("id", "") for m in ms if m.get("id")]
        st["models"] = [{"id": m.get("id", ""),
                         "name": m.get("display_name") or m.get("id", ""),
                         "owned_by": m.get("owned_by", "")} for m in ms]
        st["free"] = [i for i in ids if _is_free(i)]
        st["online"] = True
        cand = _gw_candidates(cfg, ids)
        if cand:
            with ThreadPoolExecutor(max_workers=3) as ex:
                results = list(ex.map(lambda m: (m,) + _gw_probe(cfg, m), cand[:8]))
            st["probed"] = [{"model": m, "ok": ok, "detail": msg} for m, ok, msg in results]
            for m, ok, _ in results:
                if ok:
                    st["selected"] = m
                    break
        if not st["selected"]:
            st["error"] = "模型目录可读，但没有探测到可用模型（免费额度或登录态可能已过期）"
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:150]}"
        # 单次抖动不能把「已验证可用」的结论清成离线，否则整条流水线会莫名退回模板。
        if (cached.get("online") and cached.get("selected")
                and cached.get("base_url") == g["base_url"]):
            stale = dict(cached)
            stale["stale"] = True
            stale["last_error"] = "本次检测失败，沿用上次可用结论：" + err
            stale["error"] = ""
            return stale
        st["error"] = err
    if run:
        run.log(f"网关 {g['name']}: {'在线' if st['online'] else '离线'} / "
                f"{len(st['free'])} 个免费模型 / 选用 {st['selected'] or '无'}")
    write_json(GATEWAY_CACHE, st)
    return st


def gateway_test(cfg: dict, model: str = "") -> dict:
    """控制台「测试当前模型」：只做一次最小请求，不消耗成片额度。"""
    m = (model or gateway_cfg(cfg)["model"] or "").strip()
    if not m:
        m = gateway_status(cfg).get("selected", "")
    if not m:
        return {"ok": False, "model": "", "detail": "没有可用模型，请先重新检测"}
    ok, detail = _gw_probe(cfg, m, timeout=20)
    return {"ok": ok, "model": m, "detail": detail}


def gateway_provider(cfg: dict, model: str) -> dict:
    g = gateway_cfg(cfg)
    return {"name": "gateway:magpie", "base_url": _gw_url(cfg, "/chat/completions"),
            "key_env": "", "api_key": g["api_key"], "model": model}


def _probe_provider(p: dict, timeout: int = 12) -> tuple[bool, str]:
    if not p.get("model"):
        return False, "未配置 model"
    key = get_secret(p["key_env"]) if p.get("key_env") else p.get("api_key", "")
    if not key and p.get("key_env"):
        return False, "未配置 Key（环境变量默认禁用）"
    headers = {"Authorization": "Bearer " + key} if key else {}
    try:
        d = http_post_json(p["base_url"],
                           {"model": p["model"], "messages": [{"role": "user", "content": "回 OK"}],
                            "max_tokens": 8},
                           headers, timeout=timeout)
        return bool(d.get("choices")), "可用"
    except Exception as e:
        return False, str(e)[:90]


def available_providers(cfg: dict, run=None, force: bool = False) -> list[dict]:
    """网关免费/登录态模型永远排最前；付费 provider 只在用户主动填 Key 后才可能可用。"""
    cache_path = DATA / "llm_status.json"
    cache = read_json(cache_path, {})
    today = datetime.now().strftime("%Y-%m-%d")
    resolved = [resolve_provider(p) for p in cfg["llm"]["providers"]]
    if not force and cache.get("date") == today and "providers" in cache:
        ok_names = cache["providers"]
    else:
        ok_names, report = [], {}
        for p in resolved:
            good, msg = _probe_provider(p)
            report[p["name"]] = {"ok": good, "detail": msg}
            if good:
                ok_names.append(p["name"])
            if run:
                run.log(f"模型探测 {p['name']}: {'可用' if good else msg}")
        write_json(cache_path, {"date": today, "providers": ok_names, "report": report})
    out = []
    gst = gateway_status(cfg, force=force, run=run)
    if gst.get("selected"):
        out.append(gateway_provider(cfg, gst["selected"]))
    out.extend(p for p in resolved if p["name"] in ok_names)
    return out


def _llm_script(topic: dict, cfg: dict, providers: list[dict], run=None) -> dict | None:
    local = _local_cfg(cfg)
    # 「到店动作 / 承诺口径」也必须跟当前方案走，否则切换后 LLM 会同时收到
    # 旧 cta_store 与新结尾两套互相矛盾的口径。
    of = offer_slots(cfg) if local else {}
    oa = offer_audit(cfg) if local else {}
    closing = (of.get("closing") or {}).get("text") or local.get("cta_store", "")
    if oa.get("specific_allowed"):
        claim_line = ("门店已书面确认当期活动，只按当前承诺方案给定的价格与有效期写，"
                      "不得自行扩展、不得把达人探店话术当官方承诺")
    else:
        claim_line = local.get("claim_policy", "")
    local_block = ""
    if local:
        local_block = (
            f"\n门店：{local.get('store', '')}（{local.get('city', '')}{local.get('district', '')}）"
            f"\n位置锚点：{local.get('transit', '')} / {local.get('community', '')}"
            f"\n本地客群：{'、'.join(local.get('audience_segments') or [])}"
            f"\n到店动作：{closing}"
            f"\n同城话题标签：{' '.join(local.get('tags_local') or [])}"
            f"\n承诺口径：{claim_line}"
            f"\n当前执行的承诺方案（按结构）：\n{offer_block(cfg)}"
            f"\n住址口径：{local.get('no_residence_claim', '')}"
        )
        pb = method_block(cfg)
        if pb:
            local_block += "\n" + pb
    user = (f"品牌：{cfg['brand']['name']}（{cfg['brand']['slogan']}）\n"
            f"目标人群：{cfg['niche']['audience']}\n"
            f"主推：{'、'.join(cfg['niche']['products'])}\n"
            f"语气：{cfg['niche']['tone']}\n"
            f"选题：{topic['title']}\n"
            f"切入点：{topic['angle']}\n"
            f"热点原文（若为空则为常青选题）：{topic.get('hot_ref','')}\n"
            f"今日健身垂类热点（可作开头引子，内容主体仍需给可执行的专业建议）：{topic.get('hot_context','无')}\n"
            f"收尾引导：{cfg['brand']['cta']}\n"
            + local_block +
            "请输出 5 个分镜的严格 JSON。")
    system = SYSTEM_PROMPT + (LOCAL_SYSTEM_ADDON if local else "")
    for p in providers:
        key = get_secret(p["key_env"]) if p.get("key_env") else p.get("api_key", "")
        headers = {"Authorization": "Bearer " + key} if key else {}
        try:
            d = http_post_json(p["base_url"],
                               {"model": p["model"], "temperature": 0.8,
                                "messages": [{"role": "system", "content": system},
                                             {"role": "user", "content": user}]},
                               headers,
                               # 网关多为推理模型，实测长任务 22~52s；不要设 max_tokens
                               timeout=90 if p["name"].startswith("gateway") else cfg["llm"]["timeout"])
            content = d["choices"][0]["message"]["content"]
            script = _extract_json(content)
            if not script.get("scenes"):
                raise ValueError("返回缺少 scenes")
            scenes = []
            for i, s in enumerate(script["scenes"][:cfg["video"]["max_scenes"]]):
                scenes.append({
                    "index": i + 1,
                    "narration": str(s.get("narration", "")).strip(),
                    "on_screen": str(s.get("on_screen", ""))[:12],
                    "sub": str(s.get("sub", ""))[:22],
                    "visual_prompt": str(s.get("visual_prompt", "")),
                })
            script["scenes"] = scenes
            script["cover_text"] = str(script.get("cover_text", ""))[:10]
            script["generator"] = "llm:" + (p["model"] if p["name"].startswith("gateway") else p["name"])
            script["topic"] = topic
            script["offer_audit"] = offer_audit(cfg)
            if run:
                run.log(f"脚本由 {p['name']}/{p['model']} 生成"
                        + ("（零密钥网关）" if p["name"].startswith("gateway") else ""))
            return script
        except Exception as e:
            if run:
                run.log(f"{p['name']} 生成失败，切换下一个：{str(e)[:110]}", "WARN")
    return None


def generate_scripts(topics: list[dict], cfg: dict, run=None) -> list[dict]:
    providers = available_providers(cfg, run)
    if run:
        run.log(f"可用文本模型：{[p['model'] for p in providers] or '无，使用内置模板引擎'}"
                + ("（首选本地网关，零密钥）" if providers and providers[0]["name"].startswith("gateway") else ""))
    scripts = []
    for t in topics:
        script = _llm_script(t, cfg, providers, run) if providers else None
        if script is None:
            script = template_script(t, cfg)
        from .compliance import check_and_fix
        script = check_and_fix(script, cfg, run)
        scripts.append(script)
    if run:
        run.save("scripts.json", scripts)
        run.step("脚本生成", "done",
                 f"{len(scripts)} 份 | 生成器 {[s['generator'] for s in scripts]}")
    return scripts
