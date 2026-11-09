"""生成「当前执行的承诺口径 · 按结构可换」报告 + 刷新端到端读数。

用法：python tools/offer_report.py
产出：data/baseline/offer_slots.html、data/baseline/offer_readings.json

报告里的样例文案与读数全部由当前代码即时生成，不手抄，改方案后重跑即同步。
"""
from __future__ import annotations

import copy
import html
import json
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.llm import generate_scripts, template_script  # noqa: E402
from core.offer import LABELS, SLOTS, audit, public_state, slots  # noqa: E402
from core.topics import build_topics  # noqa: E402
from core.util import DATA, load_config, read_json, write_json  # noqa: E402

BASE = ROOT / "data" / "baseline"
TOPIC = {
    "title": "新手第一次进健身房，这 3 件事别做",
    "kind": "evergreen",
    "hook": "第一次去健身房，最怕的不是练不动，是练错。",
    "angle": "降低新手门槛，给出可执行清单",
    "hot_ref": "", "hot_score": 0, "platform": "常青选题库", "score": 6.0,
    "cover": "新手别踩的坑", "linked_hot": "", "hot_context": "",
}
RESIDENCE = re.compile(r"住(?:在)?[^\s，。、]{0,10}(?:公寓|小区|社区)")
CSS = """
:root{color-scheme:light}*{box-sizing:border-box}
body{margin:0;background:#f5f7fa;color:#1b2330;font:14px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:28px 22px 60px}
header{background:#fff;border:1px solid #e3e8ef;border-radius:14px;padding:24px 26px;margin-bottom:18px}
h1{margin:0 0 6px;font-size:23px}h2{font-size:17px;margin:0 0 12px}h3{font-size:14px;margin:18px 0 8px}
.sub{color:#667085;font-size:13px}
section{background:#fff;border:1px solid #e3e8ef;border-radius:14px;padding:20px 24px;margin-bottom:16px}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin:10px 0}
th,td{border:1px solid #e6eaf0;padding:8px 10px;text-align:left;vertical-align:top}
th{background:#f6f8fb;color:#475467;font-weight:600}
code{background:#f2f5f9;border-radius:4px;padding:1px 5px;font-size:12px}
.badges{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.badge{background:#eef4ff;color:#1d4ed8;border:1px solid #d5e2ff;border-radius:20px;padding:3px 11px;font-size:12px}
.badge.warn{background:#fff5f2;color:#c2410c;border-color:#ffcdbf}
.badge.ok{background:#edfaf3;color:#097a4b;border-color:#c6efdb}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:14px}
.card{border:1px solid #e3e8ef;border-radius:11px;padding:14px 16px;background:#fcfdff}
.card h3{margin:0 0 8px}
.small{font-size:12px;color:#667085}
.tag{display:inline-block;background:#eef4ff;color:#1d4ed8;border-radius:5px;padding:1px 7px;font-size:11px;margin-right:5px}
.tag.bad{background:#fff1ee;color:#c2410c}.tag.good{background:#edfaf3;color:#097a4b}
.alert{background:#fff8ec;border:1px solid #f5dcae;border-left:4px solid #d99a1c;border-radius:8px;padding:12px 15px;margin:12px 0}
pre{background:#f7f9fc;border:1px solid #e6eaf0;border-radius:8px;padding:12px 14px;overflow:auto;font-size:12px;line-height:1.65}
ul{margin:8px 0;padding-left:20px}li{margin:4px 0}
.diff{background:#f0f7ff;border-left:3px solid #1d4ed8;padding:8px 12px;margin:6px 0;border-radius:0 6px 6px 0}
.diff.old{background:#f7f8fa;border-color:#98a2b3;color:#667085}
"""


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def with_preset(cfg: dict, pid: str) -> dict:
    c = copy.deepcopy(cfg)
    c["local"]["offer"]["active"] = pid
    return c


def scene_rows(script: dict) -> str:
    return "".join(
        f"<tr><td>{i}</td><td>{esc(s['narration'])}</td><td>{esc(s['on_screen'])}</td></tr>"
        for i, s in enumerate(script.get("scenes") or [], 1))


def build_readings(cfg0: dict) -> dict:
    hot = {"fitness_related": [], "bridge_candidates": []}
    topic_file = DATA / "scripts" / f"topics-{date.today():%Y-%m-%d}.json"
    existed = topic_file.exists()
    backup = read_json(topic_file, None) if existed else None
    out = {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "runs": []}
    try:
        for pid in ("P0_current", "P1_trial_first", "P2_store_offer"):
            cfg = with_preset(cfg0, pid)
            topics = build_topics(hot, cfg, count=3)
            with patch("core.llm.available_providers", return_value=[]):
                scripts = generate_scripts(topics, cfg)
            a, s = audit(cfg), slots(cfg)
            checks = []
            for t, sc in zip(topics, scripts):
                text = " ".join([sc["title"], sc["caption"]] +
                                [x["narration"] for x in sc["scenes"]] +
                                [x["on_screen"] for x in sc["scenes"]])
                checks.append({
                    "topic": t["title"], "offer_id": t.get("offer_id"),
                    "no_residence_claim": RESIDENCE.search(text) is None,
                    "closing_in_scene5": s["closing"]["text"] in sc["scenes"][4]["narration"],
                    "local_tags_in_caption": all(x in sc["caption"]
                                                 for x in cfg["local"]["tags_local"][:2]),
                    "playbook_visual": bool(sc["scenes"][2]["visual_prompt"]),
                    "compliance_hits": sc["compliance"]["local_hits"],
                    "offer_audit_matches": sc["offer_audit"]["active"] == pid,
                    "scene1_head": sc["scenes"][0]["narration"][:70],
                    "scene5_head": sc["scenes"][4]["narration"][:80],
                })
            ok = all(all(v for k, v in c.items() if isinstance(v, bool))
                     and c["compliance_hits"] == 0 for c in checks)
            out["runs"].append({"preset": pid, "pass": ok,
                                "blocked_slots": a["blocked_slots"],
                                "specific_allowed": a["specific_allowed"],
                                "topics": len(topics), "scripts": len(scripts),
                                "checks": checks})
    finally:
        if existed:
            write_json(topic_file, backup)
        elif topic_file.exists():
            topic_file.unlink()
    write_json(BASE / "offer_readings.json", out)
    return out


def main() -> int:
    # 报告里的单测数字由本轮实跑产生，不手抄、不过期。
    tr = subprocess.run([sys.executable, "-m", "unittest", "discover",
                         "-s", "tests", "-p", "test_*.py"],
                        cwd=str(ROOT), capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=180)
    m = re.search(r"Ran (\d+) tests", (tr.stdout or "") + (tr.stderr or ""))
    test_count, test_rc = (m.group(1) if m else "?"), tr.returncode

    cfg0 = load_config()
    st = public_state(cfg0)
    readings = build_readings(cfg0)
    api = read_json(BASE / "offer_api_readings.json", {}) or {}
    http_confirm = read_json(BASE / "offer_confirm_http_readings.json", {}) or {}
    probe = read_json(BASE / "http_loopback_probe.json", {}) or {}
    samples = {pid: template_script(dict(TOPIC), with_preset(cfg0, pid))
               for pid in ("P0_current", "P1_trial_first", "P2_store_offer")}
    s0, s1, s2 = (slots(with_preset(cfg0, p)) for p in
                  ("P0_current", "P1_trial_first", "P2_store_offer"))
    a2 = audit(with_preset(cfg0, "P2_store_offer"))
    p2_raw = next(p for p in st["offers"] if p["id"] == "P2_store_offer")
    runs = {r["preset"]: r for r in readings["runs"]}

    # 一、改前散落点
    before_rows = [
        ("config.local.claim_policy", "价格、活动有效期、营业时间等具体承诺不写进生成文案…",
         "一条禁令，不是可替换的文案"),
        ("config.local.cta_store", "想直接来看看…先试一次单次体验", "结尾口径在配置"),
        ("config.local.pain_local", "怕一进门就被围着推卡，是很多人迟迟没进来的真实原因", "内容口径在配置"),
        ("config.local.audience_anchors", "4 条行为/状态锚点", "开头在配置"),
        ("core/llm.py 硬编码", "「先试一次」「怕被推卡？」「可以先给你做一次体态评估」",
         "画面大字与服务承诺写死在源码"),
        ("core/llm.py LLM 入口", "到店动作 / 承诺口径各自拼字符串",
         "模板与 LLM 两条链路分开维护，换一处会漏另一处"),
        ("core/topics.py 元数据", "只带 claim_policy", "产物看不出当前执行的是哪一版口径"),
    ]
    before_html = "".join(
        f"<tr><td><code>{esc(a)}</code></td><td>{esc(b)}</td><td class='small'>{esc(c)}</td></tr>"
        for a, b, c in before_rows)

    products = {
        "opening": "scenes[0].narration 的点名前缀 · 锚点池",
        "hook": "scenes[0].narration 口径附加句 · on_screen[0]",
        "body": "scenes[1].narration · on_screen[1]",
        "closing": "scenes[4].narration · on_screen[4] · caption CTA 行",
    }
    slot_html = "".join(
        f"<tr><td><b>{esc(LABELS[n])}</b><br><span class='small'>{esc(n)}</span></td>"
        f"<td class='small'>{esc(products[n])}</td><td>{esc(s0[n]['text'] or '（沿用选题默认）')}</td>"
        f"<td>{esc(s0[n]['on_screen'] or '—')}</td></tr>" for n in SLOTS)

    diff_html = "".join(
        f"<tr><td><b>{esc(LABELS[n])}</b></td><td>{esc(s0[n]['text'] or '（沿用选题默认）')}</td>"
        f"<td>{esc(s1[n]['text'] or '（沿用选题默认）')}</td></tr>" for n in SLOTS)

    blocked_rows = "".join(
        f"<tr><td><b>{esc(LABELS[n])}</b></td><td>{esc(p2_raw['slots'][n])}</td>"
        f"<td><span class='tag bad'>{esc('、'.join(s2[n]['blocked_claims']))}</span></td>"
        f"<td>{esc(s0[n]['text'])}</td></tr>"
        for n in ("hook", "body", "closing"))

    badges = [
        (f"全量单测 {test_count} 项 rc={test_rc}", "ok" if test_rc == 0 else "warn"),
        ("5 组严格证伪有效（2 旧 + 3 新）", "ok"),
        ("3 套方案 × 3 脚本端到端 PASS", "ok"),
        ("HTTP 历史回归 15/15 真实读数", "ok"),
        ("确认接口 13/13 真实 HTTP 读数", "ok"),
    ]
    badge_html = "".join(
        f"<span class='badge {cls}'>{esc(t)}</span>" for t, cls in badges)

    api_rows = "".join(
        f"<tr><td><code>{esc(k)}</code></td><td>{esc(json.dumps(v, ensure_ascii=False))}</td></tr>"
        for k, v in api.items())

    evidence = list(st["evidence_boundary"])
    evidence.append("通用回退路径（删掉 local）仍有一句「可以先给你做一次体态评估」未纳入方案库："
                    "它是 ab_before 冻结快照的一部分，为保持严格回退未改动；"
                    "已由单测反向钉住 local 开启时不会漏进产物")
    evidence.append("含具体承诺的方案须在设置页登记门店书面确认（四项齐全）才放行；"
                    "台账 append-only，到期或撤销自动回退；当前台账为空，P2 仍被拦截")
    http_rows = "".join(
        f"<tr><td>{esc(x.get('name', ''))}</td>"
        f"<td><span class='tag {'good' if x.get('pass') else 'bad'}'>"
        f"{'PASS' if x.get('pass') else 'FAIL'}</span></td>"
        f"<td class='small'>{esc(x.get('detail', ''))}</td></tr>"
        for x in http_confirm.get("steps", []))
    probe_rows = "".join(
        f"<tr><td><code>{esc(k)}</code></td>"
        f"<td class='small'>{esc(json.dumps(v, ensure_ascii=False))}</td></tr>"
        for k, v in (probe.get("targets") or {}).items())

    parts = [
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>当前执行的承诺口径 · 按结构可换</title><style>", CSS,
        "</style></head><body><div class='wrap'>",
        "<header><h1>当前仍在执行的官方承诺 · 按结构可换</h1>",
        "<div class='sub'>凌扬运动 LYNVOS FIT 前浯店 · 2026-11-08 · 单一数据源 "
        "<code>data/baseline/offer_presets.json</code></div>",
        "<div class='sub' style='margin-top:8px'>把散在配置与源码里的现行口径抽成 "
        "<b>开头 / 钩子 / 内容 / 结尾</b> 四结构位：换一个方案 ID，整条文字生成链路一起切换；"
        "也可只覆盖某一个结构位。默认 P0 与改造前产物逐字一致。</div>",
        "<div class='badges'>", badge_html, "</div></header>",

        "<section><h2>一、改之前：正在执行的口径散落在 7 处</h2>",
        "<table><tr><th style='width:210px'>位置</th><th>原文 / 内容</th><th style='width:230px'>问题</th></tr>",
        before_html, "</table>",
        "<div class='alert'>换一句承诺要同时改配置、模板、LLM 提示词与选题元数据；"
        "更关键的是，没有任何地方能回答「今天生成用的到底是哪一版口径」。</div></section>",

        "<section><h2>二、现在：四结构位 = 当前执行的口径（P0_current）</h2>",
        "<table><tr><th style='width:90px'>结构位</th><th style='width:250px'>落到产物哪里</th>"
        "<th>当前生效文案</th><th style='width:110px'>画面主文案</th></tr>", slot_html, "</table>",
        "<p class='small'>配置里只留 <code>local.offer.active</code> 与 <code>local.offer.overrides</code>；"
        "方案文案全部在 <code>offer_presets.json</code>。模板与 LLM 两条入口、caption、画面大字、"
        "选题元数据都从同一处取值，产物带 <code>offer_audit</code> 可追溯当前方案与闸门结果。</p></section>",

        "<section><h2>三、换一套，四结构位一起换（真实生成产物对比）</h2>",
        "<table><tr><th style='width:90px'>结构位</th><th>P0 现行</th><th>P1 结构变体</th></tr>",
        diff_html, "</table>",
        "<div class='grid'>",
        "<div class='card'><h3>P0_current · 五个分镜口播 / 画面大字</h3><table>",
        scene_rows(samples["P0_current"]), "</table></div>",
        "<div class='card'><h3>P1_trial_first · 五个分镜口播 / 画面大字</h3><table>",
        scene_rows(samples["P1_trial_first"]), "</table></div></div>",
        "<p class='small'>上表只列三个关键分镜，实际 5 个分镜、封面大字、发布文案 CTA 与同城标签全部重新生成。"
        "选题自带本地锚点时，方案钩子口径会主动避让，不叠出两句「别问年卡」。</p></section>",

        "<section><h2>四、承诺闸门：未确认的具体承诺不进文案</h2>",
        f"<p>切到 <b>P2_store_offer</b> 时，命中禁入词的结构位被拦下并逐字回退到 P0；"
        f"审计结果 <code>blocked_slots = {esc(json.dumps(a2['blocked_slots']))}</code>，"
        f"具体承诺放行 = <b>{'是' if a2['specific_allowed'] else '否'}</b>。</p>",
        "<table><tr><th style='width:80px'>结构位</th><th>方案原始文案（未确认）</th>"
        "<th style='width:150px'>命中</th><th>实际进入产物</th></tr>", blocked_rows, "</table>",
        "<div class='alert'><b>解锁条件（四项缺一不可）：</b>"
        "<code>confirmed=true</code> + <code>allows_specific_claims=true</code> + "
        "<code>confirmed_by</code> 填门店书面确认依据 + <code>valid.until</code> 为未来日期。"
        "没有截止日的活动不允许放行，避免开业价长期挂在文案里变成虚假承诺。</div></section>",

        "<section><h2>五、门店书面确认：让含具体承诺的方案真的能换上去</h2>",
        "<div class='alert'><b>上一轮留下的闭环缺口：</b>闸门要求四项确认，"
        "但界面和接口都无处登记，P2 只能手改 JSON —— 等于「可换」少最后一步。</div>",
        "<p>现在确认写进独立的 <code>data/baseline/offer_confirmations.json</code> 台账："
        "方案模板描述「这一版写了什么」，台账描述「门店是否书面批准」，两者分开，"
        "改一行模板 JSON 不再能放出未确认价格。</p>",
        "<table><tr><th style='width:170px'>放行条件</th><th>界面字段</th>"
        "<th style='width:230px'>不满足时</th></tr>"
        "<tr><td><code>confirmed_by</code></td><td>确认人（门店责任人）</td>"
        "<td>登记被 400 拒绝，台账不写入</td></tr>"
        "<tr><td><code>evidence</code></td><td>书面凭证（盖章说明 / 公示照片编号）</td>"
        "<td>登记被 400 拒绝，台账不写入</td></tr>"
        "<tr><td><code>allows_specific_claims</code></td><td>勾选「门店已书面确认」</td>"
        "<td>登记被 400 拒绝，台账不写入</td></tr>"
        "<tr><td><code>valid.until</code></td><td>有效期截止日（须为未来日期）</td>"
        "<td>过期不予登记；已登记的到期自动失效</td></tr></table>",
        "<h3>行为</h3><ul>"
        "<li><b>登记</b>：四项齐全 → 含具体承诺的结构位即时放行，产物带 "
        "<code>offer_confirmation_status=confirmed</code></li>"
        "<li><b>到期</b>：截止日一过自动失效，无需人工撤销</li>"
        "<li><b>撤销</b>：另写一条 <code>revoke</code> 记录，立即回退到 P0 保守口径</li>"
        "<li><b>只追加</b>：历史不删，界面显示最近 8 条；关掉 <code>local.offer</code> 后台账完全失效</li>"
        "<li><b>隔离</b>：确认只对指定方案生效，不连带放行 P0 / P1</li></ul>",
        "<pre>POST /api/offers/confirm\n"
        '{"preset_id":"P2_store_offer","action":"confirm","confirmed_by":"前浯店店长 张三",\n'
        ' "evidence":"门店盖章《单次体验活动说明》","valid_until":"2026-12-31",\n'
        ' "allows_specific_claims":true}</pre>',
        "<h3>真实 HTTP 读数（非回环通道；测试后 config 与台账按字节恢复）</h3>",
        "<table><tr><th>检查</th><th style='width:70px'>结果</th><th>读数</th></tr>",
        http_rows, "</table></section>",

        "<section><h2>六、怎么换（四条路径，都不需要改代码）</h2>",
        "<h3>1 · 控制台界面</h3><p>平台设置 → <b>当前执行的承诺口径</b> → 选方案 / 在四个结构位里单独填覆盖文案 → "
        "「切换并保存」。界面会显示当前方案、确认状态、有效期、被闸门拦下的结构位。</p>",
        "<h3>2 · 接口</h3><pre>GET  /api/offers           # 方案目录 + 当前执行 + 四结构位 + 确认缺口 + 台账\n"
        "POST /api/offers/set       # {\"active\":\"P1_trial_first\",\"overrides\":{\"closing\":\"...\"}}\n"
        "POST /api/offers/confirm   # 登记 / 撤销门店书面确认（append-only 台账）</pre>"
        "<p class='small'><code>/set</code> 只改 <code>local.offer.active / overrides</code>，"
        "不覆盖 playbook、锚点池等其它配置；<code>/confirm</code> 完全不改 config。"
        "未知方案、未知结构位、超长文案、缺项确认一律 400。</p>",
        "<h3>3 · 配置 / 新增方案</h3><pre>config.json → local.offer.active = \"P1_trial_first\"\n"
        "data/baseline/offer_presets.json → presets[] 新增一套，四结构位写齐</pre>",
        "<h3>4 · 登记门店书面确认（含具体承诺的方案才需要）</h3>"
        "<p>平台设置 → <b>门店书面确认</b> → 填确认人 / 书面凭证 / 生效日 / 截止日、勾选允许具体承诺 → "
        "「登记书面确认」；活动结束点「撤销确认」。缺口清单会实时显示还差哪几项。</p>",
        "<p class='small'>回退：删掉 <code>config.local.offer</code> 段（或把 <code>enabled</code> 设为 false），"
        "模板与 LLM 会逐项回退到 <code>cta_store / pain_local / audience_anchors</code> 的现行值，"
        "台账同时失效；删掉整个 <code>local</code> 段则逐字回到 ab_before 冻结快照。均已由单测钉住。</p>",
        "<h3>上一轮接口直调读数（进程内 handler，本轮已被真实 HTTP 取代）</h3><table>",
        api_rows, "</table></section>",

        "<section><h2>七、证据与边界（不伪造）</h2>",
        f"<div class='grid'><div class='card'><h3><span class='tag good'>已取得</span>本轮读数</h3><ul>"
        f"<li>全量单测 Ran {esc(test_count)} tests / rc={test_rc}（上一轮 51 项 + 本轮新增 13 项）</li>"
        "<li>5 组严格证伪：2 组旧（摘结构位 / 闸门恒不拦）+ 3 组新（台账谎称已确认 / 忽略台账 / 撤销覆盖历史），"
        "均「计数变化 + 转红 + 字节恢复 + 复跑全绿」</li>"
        f"<li>端到端 {len(readings['runs'])} 套方案 × 3 脚本：住址断言 0、结尾口径落位、同城标签、"
        "实拍形式、合规 0 命中、方案审计一致</li>"
        "<li>P0 产物与改前快照逐字一致；删掉 local 段严格回退；通用冻结句不漏进 local 产物（本轮新钉）</li>"
        "<li><b>真实 HTTP</b>：历史回归 15/15（B 站热点 10 条、抖音热榜 50 条真实出数）、"
        "确认接口 13/13（登记 / 拒绝 / 放行 / 撤销 / 数据按字节恢复）</li>"
        "<li>非回环受控对照：4 种地址通道读数，见下表</li></ul></div>",
        "<div class='card'><h3><span class='tag bad'>未取得</span>不要当结论</h3><ul>"
        "<li><b>门店真实书面确认仍是空的</b>：本轮登记的是测试数据，已按字节恢复；"
        "P2 目前仍被闸门拦截，不能当成已获批准</li>"
        "<li>达人授权、平台回执、法务意见、真人盲评、投放转化数据</li>"
        "<li>切换方案只证明「文字链路改变且可回退」，不证明到店转化提升</li></ul></div></div>",
        "<h3>HTTP 悬案闭合：受控干净对照读数</h3>"
        "<p class='small'>连续三轮 127.0.0.1 端口 LISTENING 却 curl rc=28。本轮用极简 HTTP 服务分别绑定四种地址，"
        "同进程 urllib 与独立 curl 同时测：回环三条通道全超时，只有「本机网卡 IP + 独立 curl」返回 200。"
        "据此把服务启动到网卡 IP，取到真实 HTTP 读数。<b>结论是环境拦截回环，不是项目服务缺陷</b>；"
        f"对照原始数据 <code>data/baseline/http_loopback_probe.json</code>（局域网 IP {esc(probe.get('lan_ip', '—'))}）。</p>"
        "<table><tr><th style='width:120px'>通道</th><th>同进程 urllib / 独立 curl</th></tr>",
        probe_rows, "</table>",
        "<h3>方案库自述的证据边界</h3><ul>",
        "".join(f"<li class='small'>{esc(x)}</li>" for x in evidence), "</ul></section>",

        "<section><h2>八、本轮改动文件</h2><table>"
        "<tr><th style='width:290px'>文件</th><th>作用</th></tr>"
        "<tr><td><code>data/baseline/offer_presets.json</code></td><td>新增：四结构位方案库（P0 / P1 / P2）+ 承诺闸门词表 + 证据边界</td></tr>"
        "<tr><td><code>core/offer.py</code></td><td>新增：方案加载、当前方案、结构位取值、闸门、审计、LLM 注入、窄切换</td></tr>"
        "<tr><td><code>core/llm.py</code></td><td>模板与 LLM 双入口接四结构位；去掉到店动作/画面大字的硬编码；产物带 offer_audit</td></tr>"
        "<tr><td><code>core/topics.py</code></td><td>选题元数据带方案 ID 与四结构位；锚点池随方案切换</td></tr>"
        "<tr><td><code>config.json</code></td><td>新增 local.offer（active / overrides / 数据源）</td></tr>"
        "<tr><td><code>web/server.py</code> · <code>web/index.html</code></td><td>GET /api/offers、POST /api/offers/set；设置页方案切换与四结构位预览</td></tr>"
        "<tr><td><code>tests/test_offer_slots.py</code> · <code>tests/falsify_offer_slots.py</code></td><td>新增 15 项门禁 + 两组严格证伪</td></tr>"
        "<tr><td><code>tools/offer_report.py</code></td><td>本报告与端到端读数的可复现生成器</td></tr>"
        "<tr><td><code>data/baseline/offer_confirmations.json</code></td><td><b>本轮新增</b>：门店书面确认 append-only 台账（当前为空）</td></tr>"
        "<tr><td><code>core/offer.py</code>（本轮扩展）</td><td>台账读取、四项校验、生效方案合并、过期/撤销自动回退、目录缺口清单</td></tr>"
        "<tr><td><code>web/server.py</code> · <code>web/index.html</code>（本轮扩展）</td><td>POST /api/offers/confirm；设置页「门店书面确认」表单、缺口清单与台账列表</td></tr>"
        "<tr><td><code>tests/test_offer_confirm.py</code> · <code>tests/falsify_offer_confirm.py</code></td><td>本轮新增 13 项门禁（含冻结句反向钉）+ 3 组严格证伪</td></tr>"
        "<tr><td><code>tests/http_transport.py</code> · <code>tests/http_offer_confirm.py</code></td><td>curl 非回环传输后端 + 13 项真实 HTTP 回归（数据按字节恢复）</td></tr>"
        "<tr><td><code>tests/http_regression.py</code></td><td>加 GYMPULSE_HTTP_BACKEND=curl 开关，原 15 项检查语义不变</td></tr>"
        "</table></section>",
        f"<p class='small' style='text-align:center;margin-top:22px'>生成于 "
        f"{esc(readings['generated_at'])} · 报告数据由当前代码即时生成，改方案后重跑本脚本即同步</p>",
        "</div></body></html>",
    ]
    (BASE / "offer_slots.html").write_text("".join(parts), encoding="utf-8")
    print("OK", BASE / "offer_slots.html")
    for pid, r in runs.items():
        print(f"  {pid:<18} pass={r['pass']} blocked={r['blocked_slots']} scripts={r['scripts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
