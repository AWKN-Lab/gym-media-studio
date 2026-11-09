"""从已落盘的真实卡片抽取「可复用素材方法」与「同城对标打法」。

本脚本不联网、不抓浏览器、不读取任何 Cookie。
输入：
  - B 端获客方法论：data/browser/results/eval_1794140287379_a942e0.json（前 22 条为搜索结果）
  - 同城对标：data/baseline/candidates.json（「翔安 健身房」17 条，含自家 #16）
输出：
  - data/baseline/method_playbook.json    参考素材方法库
  - data/baseline/local_benchmark.json    同城对标：自家钩子 / 差异化 / 竞品钩子类型

证据纪律：
  只有卡片标题与简介文本，没有视频逐字稿。所有「C 端转写」都标注为
  我方创作，不是视频原文，也不代表视频内部确实这么讲。
  达人探店文案不是门店官方承诺，价格/无推销等不得当作事实写进生成文案。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from core.util import read_json, write_json  # noqa: E402
from local_copy import _parse_card  # noqa: E402

BASE = ROOT / "data" / "baseline"
B_SOURCE = ROOT / "data" / "browser" / "results" / "eval_1794140287379_a942e0.json"
L_SOURCE = BASE / "candidates.json"
SEARCH_COUNT = 22

# 引文一律由 source_no 从原始卡片自动取，不手写，避免编造。
SKELETON = [
    {"step": 1, "name": "3 秒点名「谁 + 在哪」", "evidence_nos": [1, 6],
     "c_transfer": "在翔安前浯附近找健身房的，先别急着问年卡多少钱。",
     "output_field": "scenes[0].narration / on_screen"},
    {"step": 2, "name": "痛点落到具体阻力", "evidence_nos": [6, 14, 20],
     "c_transfer": "怕一进门就被围着推卡，是很多人一直没进来的真实原因。",
     "output_field": "scenes[1].narration"},
    {"step": 3, "name": "给可数、可判断的方法", "evidence_nos": [4, 5, 19, 21, 22],
     "c_transfer": "进馆先看这 3 个不用问销售也能自己确认的地方。",
     "output_field": "scenes[2].narration / scenes[3].narration"},
    {"step": 4, "name": "用实拍当证据，不靠话术", "evidence_nos": [11],
     "c_transfer": "镜头从门口一路走到器械区，今天馆里什么样直接拍给你看。",
     "output_field": "visual_prompt"},
    {"step": 5, "name": "行动落到到店 + 同城标签", "evidence_nos": [1, 17],
     "c_transfer": "想直接来看看，4 号线前浯站出来就能到，先试一次单次体验。",
     "output_field": "scenes[4].narration / caption"},
]

HOOK_PATTERNS = [
    {"id": "H1", "pattern": "否定一个默认动作，再给替代项", "source_no": 1,
     "c_rewrite": "不用先办卡，先来看一眼再决定。", "use_when": "新手 / 怕被推卡 / 预算敏感"},
    {"id": "H2", "pattern": "把「大主题」收成「一共就 N 点」", "source_no": 6,
     "c_rewrite": "判断一家馆适不适合你，其实就看 3 个细节。", "use_when": "清单体 / 避坑 / 怎么选"},
    {"id": "H3", "pattern": "反直觉提问：常见做法为什么反而挡人", "source_no": 20,
     "c_rewrite": "只拍大重量，为什么反而让第一次来的人不敢进门？", "use_when": "小白 / 社恐 / 内容自筛"},
    {"id": "H4", "pattern": "「最头疼的 X，其实不用这么难」", "source_no": 14,
     "c_rewrite": "第一次进健身房最怕的环节，其实两分钟就能走完。", "use_when": "降低门槛"},
    {"id": "H5", "pattern": "「一条视频讲清楚 X」的确定性承诺", "source_no": 5,
     "c_rewrite": "一条视频讲清楚，第一次进馆到底该先看什么。", "use_when": "信息密度高的选题"},
    {"id": "H6", "pattern": "「记住 N 点 / N 个指标」的可数框架", "source_no": 21,
     "c_rewrite": "记住这 4 点，第一次进馆就不会手足无措。", "use_when": "教学 / 清单"},
]

TOPIC_TRANSFERS = [
    {"id": "T1", "from_no": 4, "kind": "method_transfer", "hook_mode": "self_contained",
     "title": "进馆前先看这 5 个地方，比先问价格靠谱",
     "cover": "进馆看 5 处",
     "angle": "把「5 个标准化」从老板管理视角转译为消费者可自查的进馆清单",
     "hook": "在翔安前浯附近找健身房的，进馆先别急着问年卡，看这 5 个地方就够。"},
    {"id": "T2", "from_no": 20, "kind": "method_transfer", "hook_mode": "self_contained",
     "title": "只拍大重量，会把第一次来的人挡在门外",
     "cover": "别只拍硬核",
     "angle": "反直觉开头，明确小白与社恐同样适合，避免内容自我筛掉客群",
     "hook": "在翔安找健身房时，刷到全是硬核大重量的视频，是不是反而觉得不敢进？"},
    {"id": "T3", "from_no": 11, "kind": "method_transfer", "hook_mode": "self_contained",
     "title": "不用背词，带你从前浯站走到器械区",
     "cover": "进门实拍",
     "angle": "第一人称沉浸式实拍还原完整动线，把「真实在场」当证据",
     "hook": "不用背词，镜头从 4 号线前浯站一路走到器械区，今天什么样直接看。"},
    {"id": "T4", "from_no": 22, "kind": "method_transfer", "hook_mode": "needs_anchor",
     "title": "不看广告看细节：6 个自己就能确认的地方",
     "cover": "6 个细节",
     "angle": "把「6 个数据指标」从经营诊断转译为消费者可观察的判断标准",
     "hook": "不想被话术带着走？这 6 个细节，进馆自己就能确认。"},
    {"id": "T5", "from_no": 5, "kind": "method_transfer", "hook_mode": "needs_anchor",
     "title": "一条视频讲清楚，第一次进馆该看什么",
     "cover": "一次讲清楚",
     "angle": "高信息密度清单体，给确定性预期",
     "hook": "一条视频讲清楚，第一次进健身房到底该先看什么。"},
    {"id": "T6", "from_no": 8, "kind": "local_compare", "hook_mode": "self_contained",
     "title": "翔安健身房怎么选：先避开 3 个办卡坑",
     "cover": "办卡先看坑",
     "angle": "吸收同城「避坑 / 别被低价套餐套路」钩子，只讲通用判断标准，不点名不贬低同行",
     "hook": "在翔安挑健身房，先别比谁更便宜，这 3 个坑更值得先避开。"},
    {"id": "T7", "from_no": 6, "kind": "local_compare", "hook_mode": "needs_anchor",
     "title": "一层一层走一遍：进馆动线实拍",
     "cover": "动线实拍",
     "angle": "吸收同城逐层探店的空间展示形式，改为我方第一人称动线实拍",
     "hook": "从门口到器械区到底要走几步？带你一层一层走一遍。"},
]

FORMATS = [
    {"id": "F1", "name": "第一人称沉浸式实拍 / 不背词", "source_kind": "method", "source_no": 11,
     "visual_prompt": "第一人称从地铁口推门进馆，沿前台到器械区一镜走完，不摆拍不解说", "usable": True},
    {"id": "F2", "name": "逐层 / 分区空间展示", "source_kind": "local", "source_no": 6,
     "visual_prompt": "按进门顺序依次实拍前台、有氧区、力量区与淋浴区，每处给一个可验证细节", "usable": True},
    {"id": "F3", "name": "多店对比 / 锐评", "source_kind": "local", "source_no": 4,
     "visual_prompt": "只拍我方场馆实拍，用可验证细节做对比说明，不出现同行店名与画面", "usable": True,
     "constraint": "只吸收形式，不点名、不贬低、不引用竞品评价"},
    {"id": "F4", "name": "新手教学特写", "source_kind": "local", "source_no": 17,
     "visual_prompt": "单个动作侧面机位特写，关节角度清晰，配 9 字以内画面主文案", "usable": True},
]

ANTI_PATTERNS = [
    {"id": "A1", "name": "只拍器械和大重量", "source_no": 20,
     "why": "会把第一次来的人和小白、社恐挡在门外，内容自己筛掉了客群"},
    {"id": "A2", "name": "追泛流量、内容飘在天上", "source_no": 6,
     "why": "不落在具体需求上，播放量不等于到店"},
    {"id": "A3", "name": "硬背词、表演式话术", "source_no": 11,
     "why": "距离感强，实拍证据比话术更能降低第一次进门的门槛"},
    {"id": "A4", "name": "只收评论、不给到店路径", "source_no": 1,
     "why": "线上互动没接到线下，本地标签和位置信息缺失"},
    {"id": "A5", "name": "断言受众住在某小区", "source_no": None,
     "why": "门店周边客群不等于某个公寓住户，属于无证据的住址断言（用户 11-08 指出）"},
]

# 同城对标：哪些原文可以进生成文案，哪些只能作为观察。
OWN_DIFFERENTIATORS = [
    {"id": "D1", "claim": "地铁 4 号线前浯站过来很近（到店路径）", "usable_in_copy": True,
     "note": "位置事实，可写成到店指引；不写成通勤时长承诺"},
    {"id": "D2", "claim": "健身小白、社恐朋友都能安心来体验（人群指向）", "usable_in_copy": True,
     "note": "人群邀请，可写；不写成「保证不被打扰」"},
    {"id": "D3", "claim": "先试一次单次体验（低门槛动作）", "usable_in_copy": True,
     "note": "动作可写，价格数字不写"},
    {"id": "D4", "claim": "场地宽敞、器械齐全、不用长时间排队", "usable_in_copy": False,
     "note": "达人主观描述，未经门店确认，不作为官方承诺"},
    {"id": "D5", "claim": "没有教练贴身推销、不会强行引导办卡", "usable_in_copy": False,
     "note": "达人探店话术；可作为「内容要回应这个顾虑」的依据，不写成门店承诺"},
    {"id": "D6", "claim": "单次 9.9、不限时长、练一整天", "usable_in_copy": False,
     "note": "开业期活动价格，须以门店当期公示为准，按 claim_policy 不写进生成文案"},
]

COMPETITOR_HOOKS = [
    {"no": 4, "type": "多店锐评 / 从夯到拉", "likes": "2868", "transfer": "只吸收「横向可比较」的结构，用我方实拍细节呈现，不点名同行"},
    {"no": 5, "type": "避坑 / 别被低价套餐套路", "likes": "5", "transfer": "改成通用判断标准清单，不评价任何具体商家"},
    {"no": 6, "type": "逐层分区探店", "likes": "297", "transfer": "吸收空间展示顺序，用于我方动线实拍"},
    {"no": 8, "type": "办卡避坑指南", "likes": "6", "transfer": "吸收「先讲坑再讲怎么选」的信息顺序"},
    {"no": 3, "type": "反价格战定位", "likes": "45", "transfer": "不跟价，改为讲可验证的场馆细节"},
    {"no": 10, "type": "低价体验 + 本地点名", "likes": "1100", "transfer": "只吸收「先试一次」的低门槛动作，不写价格"},
    {"no": 12, "type": "价格可验证：99 元能用多少设备", "likes": "16", "transfer": "吸收「用可数细节替代形容词」，不引用价格"},
    {"no": 17, "type": "新手小白必看的单点教学", "likes": "719", "transfer": "吸收单点教学形式，落到动作细节特写"},
    {"no": 7, "type": "日常 vlog / 长期主义", "likes": "4369", "transfer": "吸收陪伴感，不硬套口号"},
    {"no": 11, "type": "周年庆活动钩子", "likes": "1414", "transfer": "活动类不复用，避免价格承诺"},
]


def _cards(source: Path, limit: int | None = None) -> list[dict]:
    raw = read_json(source, {}) or {}
    items = (raw.get("value") or {}).get("items") or []
    if not items:
        raise SystemExit(f"原始卡片为空：{source}")
    out = []
    for i, it in enumerate(items, 1):
        if limit and i > limit:
            break
        p = _parse_card(it.get("card_text") or "")
        out.append({"no": i, "url": it.get("url") or "", **p})
    return out


def _quotes(cards: list[dict], nos: list[int]) -> list[dict]:
    res = []
    for n in nos:
        if n and 1 <= n <= len(cards):
            c = cards[n - 1]
            res.append({"no": n, "quote": c["card_text"][:190], "url": c["url"]})
    return res


def build_method() -> dict:
    cards = _cards(B_SOURCE, SEARCH_COUNT)
    if len(cards) < SEARCH_COUNT:
        raise SystemExit(f"B 端原始卡片不足：{len(cards)}")
    local_raw = read_json(L_SOURCE, {}) or {}
    local_by_no = {x["no"]: x for x in (local_raw.get("items") or [])}

    def quote(kind: str, no: int | None) -> dict:
        """按来源取引文：同一编号在 B 端卡片与同城清单里是不同视频，绝不能混用。"""
        if not no:
            return {"no": None, "quote": "", "url": "", "source": "用户 11-08 口头纠偏"}
        if kind == "local":
            c = local_by_no.get(no)
            if not c:
                raise SystemExit(f"同城清单缺 #{no}，拒绝生成无出处条目")
            return {"no": no, "quote": (c.get("card_text") or c.get("title") or "")[:190],
                    "url": c.get("url", ""), "source": "同城对标清单 · 翔安 健身房"}
        q = _quotes(cards, [no])
        if not q:
            raise SystemExit(f"B 端卡片缺 #{no}，拒绝生成无出处条目")
        return {**q[0], "source": "B 端获客卡片 · 健身房 获客"}

    skeleton = [{**s, "evidence": _quotes(cards, s["evidence_nos"])} for s in SKELETON]
    hooks = []
    for h in HOOK_PATTERNS:
        q = quote("method", h["source_no"])
        hooks.append({**h, "source_quote": q["quote"], "source_url": q["url"]})
    topics = []
    for t in TOPIC_TRANSFERS:
        is_local = t["kind"] == "local_compare"
        q = quote("local" if is_local else "method", t["from_no"])
        topics.append({**t, "source_kind": "local" if is_local else "method",
                       "source_label": q["source"], "source_quote": q["quote"],
                       "source_url": q["url"]})
    formats = [{**f, "evidence": [quote(f["source_kind"], f["source_no"])]} for f in FORMATS]
    anti = [{**a, "evidence": _quotes(cards, [a["source_no"]]) if a["source_no"] else []}
            for a in ANTI_PATTERNS]

    return {
        "version": "2.0",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "role": "参考素材方法库：把 B 端获客视频的可复用结构，转译为我方 C 端视频的素材方法",
        "source": {
            "file": str(B_SOURCE.relative_to(ROOT)).replace("\\", "/"),
            "keyword": "健身房 获客",
            "cards_used": SEARCH_COUNT,
            "cards_total_in_capture": 70,
            "capture": "用户本人登录的抖音搜索页；其余 48 条为混入的无关推荐流，已排除",
        },
        "role_clarification": {
            "reference_material": "B 端获客方法论：教我方如何把本地人变成到店客户",
            "output_audience": "厦门翔安前浯片区潜在会员（C 端）；不假定其住在哪个小区",
            "not": "不是把 B 端视频播给消费者看，也不是把参考素材受众当产出受众",
        },
        "script_skeleton": skeleton,
        "hook_patterns": hooks,
        "topic_transfers": topics,
        "formats": formats,
        "anti_patterns": anti,
        "claim_policy": "价格、活动、时长、无推销等具体承诺不写进生成文案；须以门店当期公示为准",
        "evidence_boundary": [
            "有：22 条真实卡片标题与简介原文、编号可交叉验证、时长/点赞/作者/链接",
            "无：视频逐字稿、视频内部画面与口播、投放转化数据",
            "所有 c_rewrite / c_transfer / topic_transfers 均为我方转写创作，不是视频原文，也不代表视频内部确实这么讲",
            "点赞数是卡片层公开读数，不同账号基数不同，不等于转化效果",
        ],
    }


def build_benchmark() -> dict:
    raw = read_json(L_SOURCE, {}) or {}
    items = raw.get("items") or []
    if not items:
        raise SystemExit(f"同城清单为空：{L_SOURCE}")
    by_no = {x["no"]: x for x in items}
    own = next((x for x in items if x.get("is_own") or "凌扬" in (x.get("card_text") or "")), None)
    if not own:
        raise SystemExit("同城清单里未找到自家视频，拒绝生成对标打法")

    hooks = []
    for h in COMPETITOR_HOOKS:
        c = by_no.get(h["no"])
        if not c:
            continue
        hooks.append({**h, "quote": (c.get("card_text") or c.get("title") or "")[:190],
                      "url": c.get("url", ""), "author": c.get("author", "")})

    return {
        "version": "2.0",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": {
            "file": str(L_SOURCE.relative_to(ROOT)).replace("\\", "/"),
            "keyword": raw.get("meta", {}).get("keyword", ""),
            "captured_at": raw.get("meta", {}).get("captured_at", ""),
            "total": len(items),
            "note": "用户本人登录浏览器会话的实时搜索结果，非模拟数据",
        },
        "own_reference": {
            "no": own["no"], "author": own.get("author", ""), "url": own.get("url", ""),
            "source_type": "第三方达人探店视频，非门店官方账号 / 非门店书面确认",
            "full_text": own.get("card_text") or own.get("title") or "",
            "hook_anatomy": [
                "新店 / 新去处（本地资讯钩子）",
                "单次体验的低门槛动作",
                "没有贴身推销、不强行引导办卡（回应社恐顾虑）",
                "地铁站点给出到店路径",
                "健身小白、社恐的人群点名",
            ],
            "differentiators": OWN_DIFFERENTIATORS,
        },
        "competitor_hooks": hooks,
        "hook_types_observed": [
            "多店横向锐评", "办卡避坑 / 别被低价套路", "逐层分区空间实拍",
            "可验证价格与设备清单", "新手单点教学", "日常 vlog 陪伴感", "反价格战定位",
        ],
        "usable_in_copy": {
            "allowed": ["到店路径（4 号线前浯站）", "人群邀请（小白 / 社恐）",
                        "先试一次的低门槛动作", "可验证的场馆细节清单", "避坑类通用判断标准"],
            "forbidden": ["任何价格数字与活动有效期", "无推销 / 不排队等承诺性表述",
                          "点名、影射或贬低同行", "把达人探店话术当门店官方承诺",
                          "断言受众住在某个小区"],
        },
        "evidence_boundary": [
            "有：17 条同城搜索结果卡片原文、自家视频达人全文、编号与链接",
            "无：竞品后台数据、真实到店转化、门店官方书面确认、达人授权",
            "「差异化打法」是从自家达人探店文案提炼的内容策略，不是已核实的服务承诺",
            "同城样本量小且多为本地内容，不能外推为行业规律",
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "verify"])
    a = ap.parse_args()
    if a.cmd == "verify":
        m = read_json(BASE / "method_playbook.json", {}) or {}
        b = read_json(BASE / "local_benchmark.json", {}) or {}
        assert len(m["script_skeleton"]) == 5, "骨架步数不对"
        assert all(x["evidence"] for x in m["script_skeleton"]), "骨架缺原始引文"
        assert all(x["source_quote"] for x in m["hook_patterns"]), "钩子缺原始引文"
        assert all(x["source_quote"] and x["source_label"] for x in m["topic_transfers"]), "转译缺来源"
        assert all(x["evidence"][0]["source"] for x in m["formats"]), "形式缺来源标注"
        assert b["own_reference"]["full_text"], "自家原文缺失"
        assert len(b["competitor_hooks"]) >= 8, "竞品钩子不足"
        print("PLAYBOOK_VERIFY_OK skeleton=5 hooks=%d transfers=%d formats=%d anti=%d competitor_hooks=%d"
              % (len(m["hook_patterns"]), len(m["topic_transfers"]), len(m["formats"]),
                 len(m["anti_patterns"]), len(b["competitor_hooks"])))
        return 0
    m, b = build_method(), build_benchmark()
    write_json(BASE / "method_playbook.json", m)
    write_json(BASE / "local_benchmark.json", b)
    print("BUILD_OK cards=%d skeleton=%d hooks=%d transfers=%d formats=%d anti=%d"
          % (m["source"]["cards_used"], len(m["script_skeleton"]), len(m["hook_patterns"]),
             len(m["topic_transfers"]), len(m["formats"]), len(m["anti_patterns"])))
    print("BENCHMARK_OK own=#%d competitor_hooks=%d"
          % (b["own_reference"]["no"], len(b["competitor_hooks"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
