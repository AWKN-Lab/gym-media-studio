"""本地获客文案逻辑：素材恢复 / 规范生成 / A-B 取证 / 回退证伪。

背景（2026-11-08 复盘）：
把「健身房 获客」那批 B 端方法论视频按「受众不对」剔除，是错误的口径 ——
它们是给我方学习「如何把本地人变成到店客户」的**参考素材**，
不是要播给消费者看的**产出内容**。产出内容的受众是门店周边潜在会员。

本脚本不联网、不抓浏览器。素材来自本人已登录会话留下的原始抓取结果文件。
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.util import load_config, read_json, write_json  # noqa: E402
from core.llm import template_script  # noqa: E402

BASE = ROOT / "data" / "baseline"
SOURCE = ROOT / "data" / "browser" / "results" / "eval_1794140287379_a942e0.json"
SEARCH_RESULT_COUNT = 22          # 该次抓取中位于搜索结果区的卡片数（其余为混入的推荐流）
USER_NAMED = [1, 3, 4, 5, 11, 14]  # 用户在对话中点名引用的编号

# 同一份选题用于改前 / 改后 / 关闭本地配置三次生成，保证可比。
TOPIC = {
    "title": "新手第一次进健身房，这 3 件事别做",
    "kind": "evergreen",
    "hook": "第一次去健身房，最怕的不是练不动，是练错。",
    "angle": "降低新手门槛，给出可执行清单",
    "hot_ref": "",
    "hot_score": 0,
    "platform": "常青选题库",
    "score": 6.0,
    "cover": "新手别踩的坑",
    "linked_hot": "",
    "hot_context": "",
}

# 六条获客文案逻辑：evidence 里的编号自动从真实卡片文本取原文，不手写引文。
PRINCIPLES = [
    {
        "id": "P1", "name": "点名「谁 + 在哪」，不写泛人群",
        "evidence": [1, 6],
        "rule": "前 3 秒必须出现可识别的本地身份或地点（片区 / 地铁线 / 商圈 / 行为状态），让算法和人都知道这条是给谁看的；但不得断言受众住在某个公寓或小区（用户 11-08 纠偏）。",
        "bad": "很多人第一次去健身房都会紧张。",
        "good": "在翔安前浯附近找健身房的，第一次进健身房最容易卡在这一步。",
        "code_hook": "hook 按标题稳定轮换多分支人群锚点；caption 带同城标签",
    },
    {
        "id": "P2", "name": "痛点落到具体场景，不写通用焦虑",
        "evidence": [14, 20],
        "rule": "痛点要能被本地人对号入座：下班晚、怕被推销、预算、社恐、通勤，而不是「想变好却没时间」。",
        "bad": "想变好，但总是坚持不下来。",
        "good": "怕一进门就被围着推卡，是周边不少人迟迟没来的真实原因。",
        "code_hook": "pain 改用本地客群的具体阻力",
    },
    {
        "id": "P3", "name": "方法可数、可执行、有判断标准",
        "evidence": [4, 5, 21, 22],
        "rule": "给数字、清单或判断标准（3 件事 / 8 分钟 / 2 组 12 次 / 第二天上下楼梯的反应），不写「科学训练」这类空词。",
        "bad": "建议您循序渐进、科学安排训练。",
        "good": "第一周每个动作 2 组、每组 12 次，先把轨迹走稳。",
        "code_hook": "沿用 KNOWLEDGE 可验证要点，不新增空泛建议",
    },
    {
        "id": "P4", "name": "低门槛实拍证据，不靠表演和话术",
        "evidence": [11],
        "rule": "第一人称 / 门店实拍 / 不背词，把「真实在场」当证据，降低距离感。",
        "bad": "专业教练团队，为您量身定制。",
        "good": "镜头从门口推进去，直接拍今天馆里真实的样子。",
        "code_hook": "visual_prompt 改成本地门店第一人称实拍",
    },
    {
        "id": "P5", "name": "行动落到「到店」，不只收评论",
        "evidence": [1, 17],
        "rule": "收尾必须给可执行的到店路径（地铁出口 / 单次体验 / 体态评估），并让发布文案带同城标签，把线上互动接到线下。",
        "bad": "评论区打「计划」，领一份新手训练表。",
        "good": "评论区打「计划」领训练表；想直接来看看，4 号线前浯站出来就能到，先试一次单次。",
        "code_hook": "cta 追加本地到店动作；caption 追加同城标签",
    },
    {
        "id": "P6", "name": "反直觉开头，且别把目标人群挡在门外",
        "evidence": [20, 1],
        "rule": "先推翻一个默认动作（只拍器械大重量 / 追泛流量），并明确「小白、社恐也适合」，避免内容自我筛掉客群。",
        "bad": "硬核器械、大重量，才是真训练。",
        "good": "只拍大重量，恰恰会把第一次来的人挡在门外。",
        "code_hook": "hook 保留反转结构；不写硬核门槛暗示",
    },
]

PRINCIPLE_BY_NO = {
    1: ["P1", "P5", "P6"], 3: ["P6"], 4: ["P3"], 5: ["P3"], 6: ["P1"],
    9: ["P2"], 11: ["P4"], 12: ["P3"], 14: ["P2"], 15: ["P6"],
    17: ["P5"], 19: ["P3"], 20: ["P2", "P6"], 21: ["P3"], 22: ["P3"],
}

LOCAL_ANCHOR = {
    "store": "凌扬运动前浯健身工场",
    "city": "厦门", "district": "翔安",
    "transit": "地铁 4 号线前浯站",
    "community": "浯家社区 / 浯家公寓（金海街道竹浦西里 14 号）",
    "catchment": "南部新城 TOD、新浦嘉园、保利国贸沁园、金海路、蓬莱路、厦大翔安校区",
    "segments": [
        "在前浯 / 南部新城一带上班或活动的人（不假定居住小区）",
        "坐 4 号线到前浯站、想就近练的通勤人群",
        "健身小白、社恐、怕被贴身推销的人",
        "预算敏感、想先单次体验再决定的人",
    ],
    "no_residence_claim": "用户 11-08 指出「用户不一定住这个公寓」；旧版写死「住浯家公寓、每天坐 4 号线通勤的」属无证据的住址断言，已废止，改为按标题稳定轮换的 4 条行为/状态锚点",
    "observed_offers": "自家达人探店视频出现「单次 9.9」「不贴身推销、不强行引导办卡」（开业期活动 + 达人主观描述，未经门店确认，不写进生成文案）",
    "evidence_note": "位置来自用户口述 + 本店达人探店视频文案；社区户数与区划为公开资料。均未取得门店书面确认，且该达人非门店官方账号。",
    "playbook_ref": "data/baseline/method_playbook.json",
    "benchmark_ref": "data/baseline/local_benchmark.json",
}


def _parse_card(text: str) -> dict:
    t = re.sub(r"\s+", " ", text or "").strip()
    m = re.match(r"^(\d{1,2}:\d{2})\s+(\d+(?:\.\d+)?\s*万?)?\s*", t)
    duration = (m.group(1) if m else "")
    likes = (m.group(2) or "").strip() if m else ""
    rest = t[m.end():] if m else t
    am = re.search(r"@([^\s·#]+)", rest)
    author = ("@" + am.group(1)) if am else ""
    dm = re.search(r"·\s*(\d{1,2}月\d{1,2}日|\d{4}年\d{1,2}月\d{1,2}日|[\d-]+前)", rest)
    date = dm.group(1) if dm else ""
    title = re.split(r"#|@", rest, maxsplit=1)[0].strip()
    return {"title": title[:120], "duration": duration, "likes": likes,
            "author": author, "date": date, "card_text": t}


def build_library() -> dict:
    raw = read_json(SOURCE, {}) or {}
    value = raw.get("value") or {}
    items = value.get("items") or []
    if len(items) < SEARCH_RESULT_COUNT:
        raise SystemExit(f"原始结果不足：{len(items)} 条，拒绝生成不完整素材库")
    out = []
    for i, it in enumerate(items, 1):
        parsed = _parse_card(it.get("card_text") or "")
        out.append({
            "no": i,
            "url": it.get("url") or "",
            "cover": it.get("cover") or "",
            "source_kind": "search_result" if i <= SEARCH_RESULT_COUNT else "recommend",
            "user_named": i in USER_NAMED,
            "principle_ids": PRINCIPLE_BY_NO.get(i, []),
            **parsed,
        })
    lib = {
        "meta": {
            "keyword": "健身房 获客",
            "source_file": str(SOURCE.relative_to(ROOT)).replace("\\", "/"),
            "page_url": value.get("url"),
            "captured_from": "用户本人登录的抖音搜索页（独立 Chrome 会话），无模拟登录 / 无绕过风控",
            "recovered_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "total": len(out),
            "search_results": SEARCH_RESULT_COUNT,
            "recommend": len(out) - SEARCH_RESULT_COUNT,
            "role": "参考素材：学习获客文案逻辑，不是产出内容的受众",
            "cross_check": "用户点名的 1/3/4/5/11/14 号文本与恢复顺序逐条一致",
            "limitation": "仅有卡片标题与简介文本，无逐字稿；下列原则为结构层提炼，非视频内容逐句分析",
            "prior_error": "上一轮按受众把这些 B 端方法论标红剔除，是把参考素材受众误当作产出内容受众",
        },
        "local_anchor": LOCAL_ANCHOR,
        "items": out,
    }
    write_json(BASE / "method_samples.json", lib)

    principles = []
    for p in PRINCIPLES:
        q = dict(p)
        q["evidence"] = [
            {"no": n, "quote": out[n - 1]["card_text"][:150], "url": out[n - 1]["url"]}
            for n in p["evidence"]
        ]
        principles.append(q)
    logic = {
        "version": "1.0",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "role_clarification": {
            "reference_material": "健身房获客方法论视频：给我方学习如何把本地人变成到店客户",
            "output_audience": "厦门翔安前浯 / 浯家周边潜在会员（C 端）",
            "wrong_prior": "把参考素材的受众当成产出内容的受众来筛，导致方法论样本被剔除",
        },
        "principles": principles,
        "cta_ladder": [
            {"level": "L0", "action": "评论区打「计划」领新手训练表", "status": "原有"},
            {"level": "L1", "action": "给出到店路径：4 号线前浯站 / 单次体验 / 到店体态评估", "status": "本轮新增"},
            {"level": "L2", "action": "发布文案带同城标签与位置，接住同城分发", "status": "本轮新增"},
        ],
        "claim_policy": "价格、活动、时长等具体承诺不写进生成文案，须由门店按当期公示确认；避免绝对化与疗效承诺",
        "evidence_boundary": [
            "有：真实搜索卡片原文、本人登录会话、编号交叉验证、改前冻结快照与改后产物对比、单测与证伪读数",
            "无：视频逐字稿、平台开放平台回执、法务意见、真人盲评、投放转化数据",
            "本轮未取得：HTTP 回归读数 —— 8787 与极简对照服务 8899 均端口监听但 TCP 超时（curl rc=28 / Node ETIMEDOUT），属环境回环拦截；历史 15/15 不冒充本轮",
        ],
    }
    write_json(BASE / "method_logic.json", logic)
    return {"library": lib, "logic": logic}


def _flatten(script: dict) -> str:
    return " ".join([
        str(script.get("title") or ""),
        str(script.get("cover_text") or ""),
        str(script.get("caption") or ""),
        " ".join(str(s.get("narration") or "") for s in script.get("scenes") or []),
        " ".join(str(s.get("sub") or "") for s in script.get("scenes") or []),
    ])


def render_script(cfg: dict) -> dict:
    topic = dict(TOPIC)
    local = cfg.get("local") or {}
    if local.get("enabled"):
        topic["audience_local"] = "、".join(local.get("audience_segments") or [])
        topic["place"] = local.get("district", "")
    return template_script(topic, cfg)


def cmd_before(_args):
    """改代码前调用一次，冻结旧产物。"""
    cfg = load_config()
    if "local" in cfg:
        raise SystemExit("配置已含 local 段，改前快照必须在修改前取得")
    s = render_script(cfg)
    write_json(BASE / "ab_before.json", s)
    print("BEFORE_FROZEN scenes=", len(s["scenes"]), "chars=", len(_flatten(s)))
    print("hook=", s["scenes"][0]["narration"][:70])
    print("cta =", s["scenes"][-1]["narration"][:70])


def cmd_ab(_args):
    """改后：新配置 / 关闭本地配置，两次生成并与改前快照比对。"""
    cfg = load_config()
    if not (cfg.get("local") or {}).get("enabled"):
        raise SystemExit("local.enabled 未开启，无法生成改后产物")
    before = read_json(BASE / "ab_before.json", {}) or {}
    if not before:
        raise SystemExit("缺少改前快照 ab_before.json")
    cfg_off = json.loads(json.dumps(cfg))
    cfg_off.pop("local", None)

    after, off = render_script(cfg), render_script(cfg_off)
    write_json(BASE / "ab_after.json", after)
    write_json(BASE / "ab_off.json", off)

    local = cfg["local"]
    markers = [local["transit"], local["district"], (local.get("tags_local") or [""])[0]]
    text_after, text_before, text_off = _flatten(after), _flatten(before), _flatten(off)
    hit_after = {m: (m in text_after) for m in markers if m}
    hit_before = {m: (m in text_before) for m in markers if m}
    hit_off = {m: (m in text_off) for m in markers if m}
    off_identical = (text_off == text_before)
    changed = (text_after != text_before)
    # 用户 11-08 二次纠偏：产物里不得再出现「住 XX 公寓」这类住址断言。
    residence = re.compile(r"住(?:在)?[^\s，。、]{0,10}(?:公寓|小区|社区)")
    claim_hit = bool(residence.search(text_after))
    # 与「上一版 local 产物」比，证明本轮确实改了文案，而不是只动了配置。
    prev = read_json(BASE / "ab_prev_local.json", {}) or {}
    prev_differs = bool(prev) and (_flatten(prev) != text_after)

    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "markers": markers,
        "hit_after": hit_after, "hit_before": hit_before, "hit_off": hit_off,
        "off_equals_before": off_identical,
        "after_differs_from_before": changed,
        "differs_from_prev_local": prev_differs,
        "residence_claim_in_after": claim_hit,
        "scene_count_after": len(after["scenes"]),
        "scene_count_before": len(before["scenes"]),
        "verdict": "PASS" if (off_identical and changed and all(hit_after.values())
                              and not any(hit_before.values()) and not claim_hit
                              and prev_differs) else "FAIL",
    }
    write_json(BASE / "ab_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("--- BEFORE hook ---", before["scenes"][0]["narration"])
    print("--- AFTER  hook ---", after["scenes"][0]["narration"])
    print("--- BEFORE cta  ---", before["scenes"][-1]["narration"])
    print("--- AFTER  cta  ---", after["scenes"][-1]["narration"])
    print("--- BEFORE caption ---", (before.get("caption") or "")[:200].replace("\n", " / "))
    print("--- AFTER  caption ---", (after.get("caption") or "")[:240].replace("\n", " / "))
    return 0 if report["verdict"] == "PASS" else 2


def cmd_html(_args):
    logic = read_json(BASE / "method_logic.json", {}) or {}
    lib = read_json(BASE / "method_samples.json", {}) or {}
    report = read_json(BASE / "ab_report.json", {}) or {}
    before = read_json(BASE / "ab_before.json", {}) or {}
    after = read_json(BASE / "ab_after.json", {}) or {}
    method = read_json(BASE / "method_playbook.json", {}) or {}
    bench = read_json(BASE / "local_benchmark.json", {}) or {}
    prev = read_json(BASE / "ab_prev_local.json", {}) or {}
    cfg_local = load_config().get("local") or {}
    readings = read_json(BASE / "v2_readings.json", {}) or {}
    if not logic or not report:
        raise SystemExit("请先执行 build 与 ab")

    def esc(s):
        return html.escape(str(s or ""))

    pcards = []
    for p in logic["principles"]:
        ev = "".join(
            f'<div class="ev"><span class="no">#{e["no"]}</span>{esc(e["quote"][:110])}…</div>'
            for e in p["evidence"])
        pcards.append(f'''<article class="p">
  <div class="pid">{esc(p['id'])}</div><h3>{esc(p['name'])}</h3>
  <p class="rule">{esc(p['rule'])}</p>{ev}
  <div class="ba"><div><span class="lbl bad">改前</span>{esc(p['bad'])}</div>
  <div><span class="lbl good">改后</span>{esc(p['good'])}</div></div>
  <div class="hook">落点：{esc(p['code_hook'])}</div></article>''')

    def scene_rows(script):
        return "".join(f'<tr><td>{i}</td><td>{esc(s["narration"])}</td></tr>'
                       for i, s in enumerate(script.get("scenes") or [], 1))

    a = dict(LOCAL_ANCHOR)
    a.update({
        "store": cfg_local.get("store", a["store"]),
        "city": cfg_local.get("city", a["city"]),
        "district": cfg_local.get("district", a["district"]),
        "transit": cfg_local.get("transit", a["transit"]),
        "community": cfg_local.get("community", a["community"]),
        "catchment": cfg_local.get("catchment", a["catchment"]),
        "segments": cfg_local.get("audience_segments") or a["segments"],
        "evidence_note": cfg_local.get("evidence_note", a["evidence_note"]),
    })
    seg = "".join(f"<li>{esc(x)}</li>" for x in a["segments"])
    ladder = "".join(
        f'<tr><td>{esc(x["level"])}</td><td>{esc(x["action"])}</td><td>{esc(x["status"])}</td></tr>'
        for x in logic["cta_ladder"])
    boundary = "".join(f"<li>{esc(x)}</li>" for x in logic["evidence_boundary"])
    boundary += "".join(f"<li>{esc(x)}</li>" for x in [
        "本轮新增但仍缺：B 端视频逐字稿、达人授权、门店书面确认、真实到店转化与投放回执",
        "方法库全部条目只有卡片标题/简介文本；转写与骨架是我方创作，不是视频内容逐句分析",
        "同城对标只吸收形式与通用判断标准；不点名同行、不引用竞品价格、不把达人话术当官方承诺",
        "住址断言已由正则反向钉死，但「锚点是否真的命中本地人群」仍需投放后的地域数据验证",
    ])
    verdict = esc(report.get("verdict"))
    cls = "ok" if verdict == "PASS" else "bad"

    anchor_list = "".join(f"<li>{esc(x)}</li>"
                          for x in (cfg_local.get("audience_anchors") or []))
    prev_hook = esc((prev.get("scenes") or [{}])[0].get("narration", ""))
    after_hook = esc((after.get("scenes") or [{}])[0].get("narration", ""))

    sk_rows = "".join(
        f'<tr><td>{s["step"]}</td><td>{esc(s["name"])}</td><td>{esc(s["c_transfer"])}</td>'
        f'<td class="small">{esc(s["output_field"])}</td></tr>'
        for s in method.get("script_skeleton", []))
    hook_rows = "".join(
        f'<tr><td>{esc(h["id"])}</td><td>{esc(h["pattern"])}</td><td>{esc(h["c_rewrite"])}</td>'
        f'<td class="small">#{h["source_no"]} {esc(h["source_quote"][:44])}…</td></tr>'
        for h in method.get("hook_patterns", []))
    transfer_rows = "".join(
        f'<tr><td>{esc(t["id"])}</td><td>{esc(t["title"])}</td>'
        f'<td>{esc(t["source_label"])} #{t["from_no"]}</td>'
        f'<td class="small">{esc(t["source_quote"][:50])}…</td></tr>'
        for t in method.get("topic_transfers", []))
    format_list = "".join(
        f'<li><b>{esc(f["id"])} {esc(f["name"])}</b>：{esc(f["visual_prompt"])}'
        f'<span class="small">（来源：{esc(f["evidence"][0]["source"])} #{f["source_no"]}）</span></li>'
        for f in method.get("formats", []))
    anti_list = "".join(
        f'<li><b>{esc(x["id"])} {esc(x["name"])}</b>：{esc(x["why"])}</li>'
        for x in method.get("anti_patterns", []))

    own = bench.get("own_reference") or {}
    diff_rows = []
    for d in own.get("differentiators", []):
        badge = "tagok" if d["usable_in_copy"] else "tagbad"
        label = "可进文案" if d["usable_in_copy"] else "禁入文案"
        diff_rows.append(
            f'<tr><td>{esc(d["id"])}</td><td>{esc(d["claim"])}</td>'
            f'<td><span class="{badge}">{label}</span></td>'
            f'<td class="small">{esc(d["note"])}</td></tr>')
    diff_rows = "".join(diff_rows)
    comp_rows = "".join(
        f'<tr><td>#{h["no"]}</td><td>{esc(h["type"])}</td><td>{esc(h["likes"])}</td>'
        f'<td>{esc(h["transfer"])}</td><td class="small">{esc(h["quote"][:56])}…</td></tr>'
        for h in bench.get("competitor_hooks", []))
    u = bench.get("usable_in_copy") or {}
    allowed_list = "".join(f"<li>{esc(x)}</li>" for x in u.get("allowed", []))
    forbidden_list = "".join(f"<li>{esc(x)}</li>" for x in u.get("forbidden", []))
    hook_type_list = "".join(f"<li>{esc(x)}</li>"
                             for x in bench.get("hook_types_observed", []))
    own_text = esc((own.get("full_text") or "")[:420])

    sec_residence = f'''<section><h2>五、撤销住址断言（用户 11-08 二次纠偏）</h2>
<div class="alert"><b>「用户不一定住这个公寓」。</b>上一版把 <code>hook_prefix</code> 写死成「住浯家公寓、每天坐 4 号线通勤的」，
等于把门店辐射范围误当成每一位观众的住址，是没有证据的事实断言。已整体废止，并加反向测试钉死。</div>
<div class="cmp" style="margin-top:14px"><div><h4>上一版 local 产物（已废止）</h4>
<div style="padding:12px 14px;font-size:13px">{prev_hook}</div></div>
<div class="after"><h4>本轮产物</h4><div style="padding:12px 14px;font-size:13px">{after_hook}</div></div></div>
<p class="small" style="margin-top:12px"><b>改成什么：</b>不假定住址，改按标题稳定轮换 4 条「片区 / 站点 / 行为状态」锚点；
同一选题每次生成同一句，不同选题可覆盖不同分支。</p><ul>{anchor_list}</ul>
<p class="small"><code>config.local.no_residence_claim</code> 与 LLM 补充规则第 6 条同步写明；
<code>#浯家公寓</code> 只保留为同城分发的地理标签，不描述观众住址。
A/B 报告新增判据：<code>residence_claim_in_after={esc(report.get('residence_claim_in_after'))}</code>、
<code>differs_from_prev_local={esc(report.get('differs_from_prev_local'))}</code>。</p></section>'''

    sec_method = f'''<section><h2>六、B 端获客视频 → 我方素材方法库</h2>
<p class="sub">从 22 条真实 B 端卡片文本抽取结构（其余 48 条为混入的无关推荐流，已排除）。
<b>c_rewrite / c_transfer 全部标注为我方转写，不是视频原文</b>；没有视频逐字稿，不宣称视频内部确实这么讲。</p>
<h3 style="font-size:14px;margin:18px 0 8px">脚本骨架（5 步，对应 5 个分镜）</h3>
<table><tr><th>步</th><th>结构</th><th>我方 C 端转写</th><th>产物落点</th></tr>{sk_rows}</table>
<h3 style="font-size:14px;margin:18px 0 8px">可复用钩子句式</h3>
<table><tr><th>ID</th><th>句式</th><th>我方转写示例</th><th>原始卡片引文</th></tr>{hook_rows}</table>
<h3 style="font-size:14px;margin:18px 0 8px">转译选题（已进平台选题池，按日期轮换）</h3>
<table><tr><th>ID</th><th>C 端选题</th><th>来源</th><th>原始引文</th></tr>{transfer_rows}</table>
<h3 style="font-size:14px;margin:18px 0 8px">实拍形式（已进入 visual_prompt）</h3><ul>{format_list}</ul>
<h3 style="font-size:14px;margin:18px 0 8px">必须避开的反例</h3><ul>{anti_list}</ul>
<p class="small">落点：<code>core/playbook.py</code> 单一加载入口 → <code>core/topics.py</code> 转译选题进池、
<code>core/llm.py</code> 模板与 LLM 双入口注入（system 第 11 条 + user 方法说明 + visual_prompt）。关掉 <code>local</code> 段全部为空。</p></section>'''

    sec_bench = f'''<section><h2>七、同城对标吸收（含自家钩子与差异化打法）</h2>
<p class="sub">用户 11-08：「同城竞品对标可以吸收，包括我们自己的钩子和差异化打法」。上一版把这条路径整体废止，本轮恢复并划清可用边界。</p>
<div class="kv" style="margin:12px 0"><dt>自家参照</dt><dd>#{esc(own.get('no'))} @{esc(own.get('author'))} · {esc(own.get('source_type'))}</dd>
<dt>原文</dt><dd class="small">{own_text}…</dd></div>
<h3 style="font-size:14px;margin:18px 0 8px">差异化打法：哪些能进生成文案</h3>
<table><tr><th>ID</th><th>打法</th><th>可用</th><th>理由</th></tr>{diff_rows}</table>
<h3 style="font-size:14px;margin:18px 0 8px">同城竞品钩子（只吸收形式与通用判断标准）</h3>
<table><tr><th>编号</th><th>钩子类型</th><th>赞</th><th>我方吸收方式</th><th>卡片引文</th></tr>{comp_rows}</table>
<div class="cmp" style="margin-top:16px"><div><h4>可进文案</h4><ul>{allowed_list}</ul></div>
<div><h4>禁入文案</h4><ul>{forbidden_list}</ul></div></div>
<p class="small" style="margin-top:12px"><b>观察到的钩子类型：</b></p><ul>{hook_type_list}</ul>
<p class="small">边界：#16 是<b>第三方达人探店</b>，不是门店官方账号；「不贴身推销」「场地宽敞」「9.9」是达人的主观描述或开业期活动，
只能作为「内容要回应这个顾虑」的依据，不能写成门店承诺。同城样本量小，不外推为行业规律；赞数不等于转化。</p></section>'''

    doc = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>本地获客文案逻辑 · 凌扬运动前浯店</title>
<style>
:root{{color-scheme:light}}*{{box-sizing:border-box}}
body{{margin:0;background:#f5f7fa;color:#1b2330;font:14px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1080px;margin:0 auto;padding:28px 22px 60px}}
header{{background:#fff;border:1px solid #e3e8ef;border-radius:14px;padding:24px 26px;margin-bottom:18px}}
h1{{margin:0 0 6px;font-size:23px}}h2{{font-size:17px;margin:30px 0 12px}}
.sub{{color:#667085;font-size:13px}}
.alert{{margin-top:16px;background:#fff5f2;border:1px solid #ffcdbf;border-left:4px solid #e2601a;border-radius:8px;padding:13px 16px;color:#7a3418}}
.alert b{{color:#c2410c}}
section{{background:#fff;border:1px solid #e3e8ef;border-radius:14px;padding:20px 24px;margin-bottom:16px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}}
.p{{border:1px solid #e3e8ef;border-radius:11px;padding:16px;position:relative;background:#fcfdff}}
.pid{{position:absolute;right:14px;top:14px;background:#eef4ff;color:#1d4ed8;font-weight:700;border-radius:6px;padding:2px 9px;font-size:12px}}
.p h3{{margin:0 0 8px;font-size:15px;padding-right:46px}}.rule{{margin:0 0 10px;color:#344054}}
.ev{{font-size:12px;color:#667085;background:#f6f8fb;border-radius:6px;padding:6px 9px;margin:5px 0}}
.no{{display:inline-block;min-width:28px;color:#1d4ed8;font-weight:700}}
.ba{{display:grid;gap:6px;margin-top:10px;font-size:12.5px}}
.ba>div{{padding:7px 10px;border-radius:6px;background:#f8f9fb}}
.lbl{{display:inline-block;margin-right:8px;border-radius:4px;padding:1px 6px;color:#fff;font-size:11px}}
.lbl.bad{{background:#98a2b3}}.lbl.good{{background:#15803d}}
.hook{{margin-top:9px;color:#475467;font-size:12px;border-top:1px dashed #e3e8ef;padding-top:8px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{border-bottom:1px solid #edf0f4;padding:9px 10px;text-align:left;vertical-align:top}}
th{{background:#f8fafc;color:#475467;font-weight:600}}
.kv{{display:grid;grid-template-columns:130px 1fr;gap:6px 14px;font-size:13.5px}}
.kv dt{{color:#667085}}.kv dd{{margin:0}}
.cmp{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
.cmp>div{{border:1px solid #e3e8ef;border-radius:10px;overflow:hidden}}
.cmp h4{{margin:0;padding:9px 13px;background:#f8fafc;font-size:13px;border-bottom:1px solid #e3e8ef}}
.cmp.after h4{{background:#ecfdf3;color:#166534}}
.cmp td:first-child{{width:28px;color:#98a2b3}}
.verdict{{display:inline-block;padding:5px 12px;border-radius:7px;font-weight:700;background:#ecfdf3;color:#166534}}
.verdict.bad{{background:#fef2f2;color:#b91c1c}}
code{{background:#f2f4f7;padding:1px 5px;border-radius:4px;font-size:12px}}
ul{{margin:6px 0;padding-left:20px;color:#475467}}
.small{{color:#667085;font-size:12.5px}}
.tagok{{color:#166534;font-weight:600}}.tagbad{{color:#b91c1c;font-weight:600}}
h3{{font-weight:600}}
</style></head><body><div class="wrap">
<header><h1>本地获客文案逻辑 · 凌扬运动前浯店</h1>
<div class="sub">素材恢复 + 逻辑提炼 + 平台文字链路改造 · 生成于 {esc(logic['generated_at'])}</div>
<div class="alert"><b>口径纠偏：</b>{esc(logic['role_clarification']['wrong_prior'])}。<br>
<b>参考素材</b>＝「健身房 获客」那批 B 端方法论视频，用于学习如何把本地人变成到店客户，<b>不面向消费者播放</b>；
<b>产出受众</b>＝{esc(logic['role_clarification']['output_audience'])}。上一轮把两者混为一谈，写受众分类器把方法论标红剔除，又转去找本地竞品对标 —— 方向反了。<br>
<b>本轮二次纠偏：</b>B 端视频的内容要提取成素材方法、同城对标（含自家钩子）要吸收；同时「住浯家公寓」是无证据的住址断言，已废止并反向钉死。详见第五～七节。</div></header>

<section><h2>一、素材与编号交叉验证</h2>
<div class="kv">
<dt>来源</dt><dd>用户本人登录的抖音搜索「健身房 获客」，原始抓取结果文件 <code>{esc(lib['meta']['source_file'])}</code></dd>
<dt>恢复条数</dt><dd>{lib['meta']['total']} 条（搜索结果 {lib['meta']['search_results']} 条 / 混入推荐流 {lib['meta']['recommend']} 条）</dd>
<dt>交叉验证</dt><dd>{esc(lib['meta']['cross_check'])}</dd>
<dt>证据边界</dt><dd>{esc(lib['meta']['limitation'])}</dd>
</div>
<p class="small">用户点名的 6 条：#1 同城流量怎么拍 · #3 每天拆解一个行业 · #4 五个标准化 · #5 一条视频讲清楚 · #11 第一人称拍摄私教馆 · #14 邪修获客 2，全部在恢复的前 22 条里，编号与文本一致。</p></section>

<section><h2>二、六条获客文案逻辑（引文来自真实卡片，非自拟）</h2>
<div class="grid">{''.join(pcards)}</div></section>

<section><h2>三、精准位置 × 客群映射</h2>
<div class="kv">
<dt>门店</dt><dd>{esc(a['store'])} · {esc(a['city'])} {esc(a['district'])}</dd>
<dt>交通锚点</dt><dd>{esc(a['transit'])}</dd>
<dt>社区</dt><dd>{esc(a['community'])}</dd>
<dt>辐射范围</dt><dd>{esc(a['catchment'])}</dd>
<dt>价格观察</dt><dd>{esc(a['observed_offers'])}</dd>
</div>
<p class="small" style="margin-top:10px"><b>客群推导（未验证，且为行为/状态维度，不是住址）</b></p><ul>{seg}</ul>
<p class="small"><b>住址口径：</b>{esc(a['no_residence_claim'])}</p>
<p class="small">{esc(a['evidence_note'])}</p>
<h2 style="margin-top:22px">行动阶梯</h2>
<table><tr><th>层级</th><th>动作</th><th>状态</th></tr>{ladder}</table>
<p class="small" style="margin-top:10px"><b>承诺口径：</b>{esc(logic['claim_policy'])}</p></section>

<section><h2>四、A/B 产物对比（同一选题，可直接复核）</h2>
<p class="sub">选题固定为「{esc(TOPIC['title'])}」；改前快照在改代码之前冻结，改后由新配置生成，第三组把 <code>local</code> 段删掉后生成。</p>
<p style="margin:12px 0">证伪结果：<span class="verdict {cls}">{verdict}</span>
<span class="small"> · 关掉本地配置后输出与改前逐字一致：{esc(report['off_equals_before'])} · 改后确有变化：{esc(report['after_differs_from_before'])} · 分镜数 {esc(report['scene_count_before'])} → {esc(report['scene_count_after'])}</span></p>
<div class="cmp"><div><h4>改前</h4><table>{scene_rows(before)}</table></div>
<div class="after"><h4>改后</h4><table>{scene_rows(after)}</table></div></div>
<p class="small" style="margin-top:12px"><b>改前发布文案：</b>{esc((before.get('caption') or '')[:180]).replace(chr(10),' / ')}</p>
<p class="small"><b>改后发布文案：</b>{esc((after.get('caption') or '')[:220]).replace(chr(10),' / ')}</p></section>

{sec_residence}
{sec_method}
{sec_bench}

<section><h2>八、证据边界（不伪造）</h2><ul>{boundary}</ul>
<p class="small">本页只证明「平台生成的文字变了、且可严格回退」。它不证明转化提升、不证明门店信息准确、不证明平台算法会因此多给同城流量 —— 这些需要投放回执与时间检验。</p>
<p class="small"><b>本轮 HTTP 回归未取得读数：</b>8787 工作台与最简对照服务 8899 均「端口 LISTENING 但 TCP 连接超时」（curl rc=28 / Node ETIMEDOUT / PowerShell 超时），判定为环境回环拦截，<b>不是</b>服务或代码缺陷；未据此下任何「服务不可用」结论，也不拿上一轮的 15/15 冒充本轮读数。运行时配置读取方式为 <code>load_config()</code> 每次请求读文件，新配置无需重建。</p></section>

<section><h2>九、本轮改动与可复核命令</h2>
<table><tr><th style="width:210px">文件</th><th>改动</th></tr>
<tr><td><code>config.json</code></td><td>local 段：删除单一 <code>hook_prefix</code> 住址断言；新增 4 条 <code>audience_anchors</code>、<code>no_residence_claim</code>、<code>playbook</code> 双数据源；客群改行为/状态维度</td></tr>
<tr><td><code>tools/playbook.py</code></td><td>新增：从 22 条 B 端卡片 + 17 条同城卡片抽取方法库与对标打法；引文按来源分别取值，杜绝同编号跨源错配</td></tr>
<tr><td><code>core/playbook.py</code></td><td>新增：单一加载入口、稳定锚点轮换、实拍形式选择、LLM 方法说明段；关掉 local 一律返回空</td></tr>
<tr><td><code>core/topics.py</code></td><td>选题池合并 7 条转译选题（5 条方法论 + 2 条同城对标），注入锚点与方法库元数据</td></tr>
<tr><td><code>core/llm.py</code></td><td>模板与 LLM <b>双入口</b>注入：多分支人群锚点、方法骨架/钩子句式/反例/同城可用范围、visual_prompt 实拍形式；system 补充规则 6/10/11/12 同步</td></tr>
<tr><td><code>core/compliance.py</code></td><td>修复 SAFE_PHRASES 未改写仍记违规命中的既有误报计数（上一轮）</td></tr>
<tr><td><code>tools/baseline.py</code></td><td>口径注释纠正：受众分类器只标注同城对标卡片，<b>不得</b>剔除获客方法论素材（上一轮）</td></tr>
<tr><td><code>tests/test_local_copy.py</code></td><td>反向钉死住址断言、多分支锚点稳定性、方法库出处可追溯、LLM 请求体注入、转译选题产物；含假 provider 捕获</td></tr>
</table>
<p class="small" style="margin-top:12px"><b>可复核命令：</b><code>python tools/playbook.py build &amp;&amp; python tools/playbook.py verify</code> · <code>python -m unittest discover -s tests -p "test_*.py"</code> · <code>python tools/local_copy.py ab</code> · <code>python tools/local_copy.py html</code></p>
<p class="small"><b>本轮实测读数：</b>全量单测 {esc(readings.get('unittest','待填'))}；A/B {verdict}
（关掉 local 后与改前逐字一致 = {esc(report['off_equals_before'])}；相对上一版 local 产物确有变化 = {esc(report.get('differs_from_prev_local'))}；住址断言命中 = {esc(report.get('residence_claim_in_after'))}）；
端到端 <code>build_topics → generate_scripts</code> {esc(readings.get('pipeline','待填'))}；
严格证伪：{esc(readings.get('falsify','未取得'))}；
配置端点直调：{esc(readings.get('config_endpoint','未取得'))}；
HTTP 回归：{esc(readings.get('http','未取得'))}。改前快照在动代码之前冻结，不是事后补写。</p></section>
</div></body></html>'''
    (BASE / "copy_logic.html").write_text(doc, encoding="utf-8")
    print("HTML_OK bytes=", len(doc))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="恢复素材库并生成逻辑规范").set_defaults(fn=lambda a: build_library())
    sub.add_parser("before", help="改代码前冻结旧产物快照").set_defaults(fn=cmd_before)
    sub.add_parser("ab", help="改后 A/B 对比与回退证伪").set_defaults(fn=cmd_ab)
    sub.add_parser("html", help="渲染说明页").set_defaults(fn=cmd_html)
    args = ap.parse_args()
    rc = args.fn(args)
    if isinstance(rc, int):
        return rc
    if isinstance(rc, dict):
        print("BUILD_OK total=", rc["library"]["meta"]["total"],
              "principles=", len(rc["logic"]["principles"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
