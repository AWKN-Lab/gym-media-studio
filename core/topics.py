"""选题决策：把全网热点转成健身房可拍的选题。

原则：宁可用常青选题，也不硬蹭与健身无真实连接的社会热点（硬蹭会伤害账号专业感）。
"""
from __future__ import annotations

import random
from datetime import datetime

from .util import write_json, DATA
from .playbook import pick_anchor, topic_transfers
from .offer import anchors as offer_anchors, offer_meta
from .llm import HOT_BRIDGES  # 切口触发词唯一来源：只有能桥接回训练的热点才配当开头

BRIDGE_WORDS = ["熬夜", "睡眠", "久坐", "饮食", "外卖", "控糖", "健康", "体检",
                "亚健康", "自律", "打卡", "降温", "上班", "加班", "中年", "走路", "步数"]

# 与健身无真实连接的语境：即使含「为什么」等知识式词，也绝不做成健身房视频
BAD_CONTEXT = ["孩子", "家长", "父母", "亲子", "学生", "老师", "学校", "高考", "考研",
               "考试", "婚姻", "恋爱", "分手", "工资", "老板", "职场", "房价", "股票",
               "明星", "演员", "综艺", "电影", "电视剧", "游戏", "手机", "汽车"]

CORE_WORDS = ["健身", "减肥", "减脂", "增肌", "训练", "肌肉", "健身房", "私教", "体态",
              "深蹲", "硬拉", "卧推", "瑜伽", "普拉提", "马拉松", "跑步", "体脂", "塑形",
              "蛋白", "热量", "拉伸", "撸铁", "八段锦"]

# 热榜里大量是情绪化 / 争议向 / 具体人物向的视频标题，不适合作为门店品牌号选题。
# 命中即降级为「选题灵感」，不直接做视频标题。
QUALITY_BAD = ["讨厌", "骂", "翻车", "争议", "塌房", "吐槽", "曝光", "打脸", "离谱",
               "震惊", "竟然", "怒", "怼", "撕", "网红", "博主", "up主", "甲亢哥",
               "后悔", "被喷", "我服", "笑死", "整活", "挑战", "vlog", "Vlog", "日常",
               "全网", "正式发出", "邀请", "讨厌了", "讨厌的"]


# 只有「问题式 / 知识式」热点才适合直接做视频标题；
# 纯新闻短语（如「XX马拉松500米冠军」）只作为发布引子，避免通用干货硬套导致文不对题。
KNOWLEDGE_STYLE = ["怎么", "为什么", "如何", "几个", "误区", "吗", "？", "?", "原理",
                   "教程", "正确", "科学", "方法", "注意", "真相", "区别", "到底",
                   "该不该", "要不要", "多久", "有用", "错误", "建议"]


def _usable(title: str) -> bool:
    if not 6 <= len(title) <= 28:
        return False
    if any(w in title for w in QUALITY_BAD + BAD_CONTEXT):
        return False
    return any(w in title for w in KNOWLEDGE_STYLE)


# 话术切口：热点不必是选题，也能当开头第一句。只排除风险/负面新闻，
# 不按 BAD_CONTEXT 拦——股票、A股这类中性热点本来就是切口。
HOOK_BAD = ["去世", "死亡", "遇难", "判刑", "被捕", "诈骗", "战争", "坠机", "地震",
            "塌房", "离婚", "股价", "涨停", "通报", "处分", "涉政", "遗体", "枪击",
            "藏尸", "宣判", "命案", "车祸", "自杀", "坠亡", "刑拘", "被查", "落马",
            "会谈", "表态", "停火", "制裁"]


# 翔安绿线：地名表用来判断标题是否点了某个具体地方。
# 本地模式只放行两种标题——点了本地名的（厦门/翔安/福建），或没点任何地名的
# （全国性话题，如 A股、健康中国，在翔安也成立）。
# 点了外地名的不开口；地名表认不出的小地名仍可能漏网，表要随实测补。
PLACE_NAMES = [
    "北京", "上海", "天津", "重庆", "广东", "江苏", "浙江", "山东", "河南", "河北",
    "湖南", "湖北", "四川", "陕西", "辽宁", "吉林", "黑龙江", "安徽", "江西", "山西",
    "云南", "贵州", "甘肃", "青海", "海南", "台湾", "香港", "澳门", "内蒙古", "宁夏",
    "新疆", "西藏", "广西",
    "广州", "深圳", "成都", "武汉", "西安", "南京", "苏州", "杭州", "长沙", "郑州",
    "济南", "青岛", "大连", "宁波", "合肥", "福州", "泉州", "漳州", "莆田", "三明",
    "南平", "龙岩", "宁德", "盐城", "扬州", "徐州", "常州", "无锡", "南通", "镇江",
    "泰州", "淮安", "连云港", "宿迁", "嘉兴", "湖州", "绍兴", "台州", "温州", "金华",
    "衢州", "丽水", "舟山", "芜湖", "安庆", "蚌埠", "沈阳", "长春", "哈尔滨", "石家庄",
    "唐山", "太原", "呼和浩特", "昆明", "贵阳", "兰州", "西宁", "海口", "南宁", "南昌",
    "安溪", "德化", "永春", "南安", "惠安", "晋江", "石狮", "永泰", "闽清", "闽侯",
    "连江", "罗源", "平潭", "仙游", "尤溪", "沙县", "大田", "建瓯", "邵武", "武夷山",
    "建阳", "松溪", "政和", "浦城", "光泽", "顺昌", "将乐", "泰宁", "建宁", "宁化",
    "清流", "明溪", "长汀", "上杭", "武平", "连城", "永定", "漳平", "云霄", "漳浦",
    "东山", "南靖", "平和", "华安", "长泰", "龙海",
    "韩国", "日本", "美国", "英国", "法国", "韩网", "日网",
]


def _is_local_hook(title: str, local_names: list[str]) -> bool:
    """绿线：标题点了本地名，或没点任何地名，才算本地可用。点了外地名的不开口。"""
    if any(n and n in title for n in local_names):
        return True
    return not any(w in title for w in PLACE_NAMES)


def pick_hook_hots(hot: dict, count: int, local_names: list[str] | None = None) -> list[dict]:
    """从热榜取可当开头切口的热点：按分数排，排除风险/负面/情绪化/人物向标题，
    且必须命中一条桥接（股票/跑步/降温/健康/吃）——没有自然桥接的热点（如手机发布、
    陨石）不开口，不硬蹭；一个都配不上时返回空，选题用自己的钩子。
    本地模式（local_names 非空）再加一道绿线：只放行点了本地名或没点任何地名的标题。"""
    seen, out = set(), []
    for it in hot.get("top", []):
        title = it.get("title", "")
        if not title or title in seen:
            continue
        if len(title) < 5 or len(title) > 28:
            continue
        if it.get("risk") or any(w in title for w in HOOK_BAD + QUALITY_BAD + BAD_CONTEXT):
            continue
        if local_names is not None and not _is_local_hook(title, local_names):
            continue
        if not any(k in title for keys, _, _ in HOT_BRIDGES for k in keys):
            continue
        seen.add(title)
        out.append({"title": title, "platform": it.get("platform", ""),
                    "hot": it.get("hot", 0), "rank": it.get("rank", 0)})
        if len(out) >= count:
            break
    return out


def _bridge_angle(title: str, hits: list[str]) -> str:
    for w in ["熬夜", "睡眠"]:
        if w in title:
            return "睡眠不足会直接压低训练恢复和食欲控制，从热点切到训练安排"
    for w in ["久坐", "上班", "加班", "办公室"]:
        if w in title:
            return "久坐带来的髋屈肌紧张和胸椎活动度下降，给出工位可做的动作"
    for w in ["外卖", "饮食", "糖", "控糖"]:
        if w in title:
            return "不推翻饮食习惯，只给一个可替换的蛋白质选择"
    for w in ["压力", "情绪", "焦虑"]:
        if w in title:
            return "把训练当作压力出口，但提醒高压力日要降强度"
    for w in ["降温", "天冷", "天气"]:
        if w in title:
            return "低温下热身时间要翻倍，讲清楚为什么容易拉伤"
    return f"用「{hits[0] if hits else title[:8]}」这个大众话题做切口，落回可执行的训练建议"


def build_topics(hot: dict, cfg: dict, run=None, count: int = 3) -> list[dict]:
    brand = cfg["brand"]
    niche = cfg["niche"]
    # 本地锚点只在显式开启时生效；删掉 local 段即严格回退到全平台通用选题。
    local = (cfg.get("local") or {}) if (cfg.get("local") or {}).get("enabled") else {}
    # 本地模式的地名集合：切口与引子都按它过滤外地地名。
    local_names = [n for n in (local.get("city", ""), local.get("district", ""), "福建") if n] if local else []
    picked: list[dict] = []

    # 垂类直接命中 + 可桥接的泛生活热点；两者都为空时由常青库兜底
    candidates = (hot.get("fitness_related", [])[:12] +
                  hot.get("bridge_candidates", [])[:8])
    # 全部垂类热点作为「发布引子」参与，但先滤掉情绪化 / 无关语境标题
    raw_insp = ([x["title"] for x in hot.get("fitness_related", [])] +
                [x["title"] for x in hot.get("bridge_candidates", [])])
    inspirations: list[str] = [t for t in raw_insp
                               if not any(w in t for w in QUALITY_BAD + BAD_CONTEXT)
                               and (not local_names or _is_local_hook(t, local_names))][:6]
    for item in candidates:
        title = item["title"]
        core = [w for w in CORE_WORDS if w in title]
        bridge = [w for w in BRIDGE_WORDS if w in title]
        if not _usable(title):
            continue
        # 只有真正命中健身核心词的知识式热点才直接做视频标题；
        # 泛生活热点一律降级为发布引子，避免硬蹭伤害门店号的专业一致性。
        if not core:
            continue
        kind = "hot_direct"
        angle = f"借「{title}」的热度，用教练视角讲清楚普通人该怎么落地"
        hook = "这个话题最近挺火，但落到训练上，先说清楚三件事。"
        hook_short = "热点别急着跟风"
        picked.append({
            "title": title[:26], "kind": kind, "hook": hook, "angle": angle,
            "hook_short": hook_short, "cover": "热点拆解",
            "hot_ref": title, "hot_score": item["score"],
            "platform": item["platform"], "score": round(item["score"] + (3 if core else 0), 2),
        })

    # 常青选题补足：保证任何一天都有可拍内容
    evergreen = list(niche["evergreen_topics"])
    random.Random(datetime.now().strftime("%Y-%m-%d")).shuffle(evergreen)
    used = {p["title"] for p in picked}
    for e in evergreen:
        if len(picked) >= count:
            break
        if e["title"] in used:
            continue
        picked.append({
            "title": e["title"], "kind": "evergreen", "hook": e["hook"],
            "angle": e["angle"], "hot_ref": "", "hot_score": 0,
            "platform": "常青选题库", "score": 6.0, "cover": e.get("cover", ""),
            "linked_hot": inspirations[len(picked)] if len(picked) < len(inspirations) else "",
            "hot_context": "、".join(inspirations[:3]),
        })

    # 本地选题保底：全网热榜不带地理维度，这是本店「获本地周边客」的真实缺口。
    # 池子 = 本地锚点选题 + 从 B 端卡片 / 同城对标转译来的可拍选题；按日期稳定轮换，
    # 保证「参考素材方法」真的进入产物，而不只是躺在 JSON 文档里。
    transfer_pool = [
        {"title": t["title"], "cover": t.get("cover", ""), "angle": t.get("angle", ""),
         "hook": t.get("hook", ""), "hook_mode": t.get("hook_mode", "needs_anchor"),
         "transfer_id": t.get("id", ""), "transfer_kind": t.get("kind", ""),
         "source_kind": t.get("source_kind", "")}
        for t in (topic_transfers(cfg) if local else [])
        if t.get("title") and t.get("hook")
    ]
    pool = list(local.get("local_topics") or []) + transfer_pool
    if pool:
        chosen = random.Random(datetime.now().strftime("%Y-%m-%d") + "-local").choice(pool)
        if chosen["title"] not in used:
            ceiling = max([p["score"] for p in picked], default=6.0)
            picked.append({
                "title": chosen["title"][:26], "kind": "local", "hook": chosen["hook"],
                "angle": chosen["angle"], "hot_ref": "", "hot_score": 0,
                "platform": ("素材方法转译选题库" if chosen.get("transfer_id") else "本地锚点选题库"),
                "score": round(ceiling + 0.5, 2),
                "cover": chosen.get("cover", ""), "linked_hot": "", "hot_context": "",
                "hook_mode": chosen.get("hook_mode", "self_contained"),
                "transfer_id": chosen.get("transfer_id", ""),
                "transfer_kind": chosen.get("transfer_kind", ""),
            })

    # 话术切口：每个非热点直出选题挂一个当日热点当开头第一句，
    # 热点直出选题本身已是热点，不再重复挂。
    # 同一个切口一天只用一次：切口不够时剩下的选题用自己的钩子，不重复蹭同一个热点。
    hooks = pick_hook_hots(hot, count, local_names or None)
    used_hooks: set[str] = set()
    for p in picked:
        if p["kind"] == "hot_direct":
            continue
        hh = next((h for h in hooks if h["title"] not in used_hooks), None)
        if hh is None:
            continue
        used_hooks.add(hh["title"])
        p["hook_hot"] = hh
        p["linked_hot"] = hh["title"]

    picked.sort(key=lambda x: -x["score"])
    picked = picked[:count]
    for p in picked:
        p["brand"] = brand["name"]
        p["audience"] = niche["audience"]
        p["cta"] = brand["cta"]
        if local:
            # 同城打法：标题前带城市+区域+片区（前浯站一带）。只给常青选题加——热点直出选题
            # 的标题本身就是热点新闻，加城市会让人误以为是本地事件；本地选题自带锚点，不重复加。
            if p["kind"] == "evergreen":
                names = [n for n in (local.get("city", ""), local.get("district", ""),
                                     local.get("micro_area", "")) if n]
                prefix = "".join(names)
                if prefix and not p["title"].startswith(tuple(names)):
                    p["title"] = (prefix + p["title"])[:26]
            p["place"] = (f"{local.get('city', '')}{local.get('district', '')}"
                          f"{local.get('micro_area', '')}")
            p["store"] = local.get("store", "")
            p["transit"] = local.get("transit", "")
            p["audience_local"] = list(local.get("audience_segments") or [])
            p["local_tags"] = list(local.get("tags_local") or [])
            p["cta_store"] = local.get("cta_store", "")
            p["claim_policy"] = local.get("claim_policy", "")
            # 开头锚点池属于「当前执行的承诺方案」，切换方案后选题缓存也要跟着换。
            pool = offer_anchors(cfg)
            p["audience_anchor"] = pick_anchor(p, {"audience_anchors": pool} if pool else local)
            p.update(offer_meta(cfg))
            p.setdefault("hook_mode", "needs_anchor")
            p["no_residence_claim"] = local.get("no_residence_claim", "")
            p["method_ref"] = local.get("method_ref", "")
            p["benchmark_ref"] = local.get("benchmark_ref", "")
            p["playbook_enabled"] = bool((local.get("playbook") or {}).get("enabled"))

    write_json(DATA / "scripts" / f"topics-{datetime.now():%Y-%m-%d}.json", picked)
    if run:
        run.save("topics.json", picked)
        kinds = {}
        for p in picked:
            kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
        run.step("选题决策", "done", f"{len(picked)} 个选题 {kinds} | 首选：{picked[0]['title']}")
    return picked
