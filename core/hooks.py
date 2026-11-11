"""钩子库与脚本结构：配置驱动，不写死在提示词里。

方法来源（只借思路，不抄原文）：
- social-account-doctor 的 7 种视频开头钩子与 10 个标题公式 → 转译成 5 种健身房口播结构。
- 太上老君“开头留白、爆了之后下一条怎么接” → 每种结构都留 `leave_blank`（留白句式）与 `series_next`（下一条承接）。
- viral-copywriting-skill 的四步漏斗与标题写法 → 结构里的标题公式（数字+人群、反认知、答案前置）。
- MoneyPrinterTurbo 的 build_script_prompt：结构参数拼进提示词，而不是改主提示词。

本文件所有句式与例子均为 GYM 自写（健身房口播口径），不引用任何来源的原文句子。
`config["hooks"]` 可覆盖：{"default": "<结构 id>", "enabled": ["<id>", ...]}，
或按 id 覆盖某结构的 hook_examples；不配则用内置默认。
"""
from __future__ import annotations

import copy
import hashlib

STRUCTURES = [
    {
        "id": "pain_question",
        "name": "痛点反问",
        "scene_roles": ["钩子（反问痛点）", "痛点共鸣", "干货点1", "干货点2", "行动号召"],
        "hook_instruction": (
            "第 1 镜必须是反问句，直接点出目标人群正在犯的具体错误或正在忍的具体不舒服，"
            "不要用“大家好”开场；反问里要带一个可验证的细节（数字、动作、部位），不要只问感受。"
        ),
        "hook_examples": [
            "深蹲膝盖总往里扣，你是不是也这样？",
            "练了三个月体重一动不动，问题可能不在练得少",
        ],
        "title_pattern": "痛点反问 + 数字：如“深蹲膝盖内扣？先查这 2 个地方”",
        "leave_blank": "先别急着改动作，看完第 2 个点再决定。",
        "series_next": "下一条讲这个动作的退阶版本，新手先从那条练起。",
    },
    {
        "id": "contrarian",
        "name": "反常识",
        "scene_roles": ["钩子（反常识）", "为什么常识错了", "干货点1", "干货点2", "行动号召"],
        "hook_instruction": (
            "第 1 镜必须先说一句反常识的判断（“以为……其实……”），再立刻给一个可验证的证据"
            "（数字、对比、判断标准），不要只抛观点不给证据；反常识不等于抬杠，不贬低同行。"
        ),
        "hook_examples": [
            "以为蛋白粉伤肾？先看你每天到底吃了多少克",
            "拉伸不是练前做的，练前这样拉反而发不上力",
        ],
        "title_pattern": "反认知钩子：如“原来热身不是拉伸”",
        "leave_blank": "证据在第 2 镜，先别划走。",
        "series_next": "下一条把今天这个判断做成自测：3 个动作就知道你中没中。",
    },
    {
        "id": "numbered_list",
        "name": "数字清单",
        "scene_roles": ["钩子（数字预告）", "清单第1项", "清单第2项", "清单第3项", "行动号召"],
        "hook_instruction": (
            "第 1 镜必须先报数字（“3 个动作”“2 个误区”），让观众知道这条视频有多少干货；"
            "每项只讲一个动作或一个判断标准，项与项之间不重复；数字与实际分镜数一致，不要报 3 项只讲 2 项。"
        ),
        "hook_examples": [
            "新手第一次进健身房，记住这 3 件事就够了",
            "圆肩自测只要 30 秒，2 个动作就能看出来",
        ],
        "title_pattern": "数字+人群+效果：如“3 个动作，新手 1 周练稳深蹲”",
        "leave_blank": "第 3 个最容易被忽略，先看完前两个。",
        "series_next": "下一条把这份清单做成打卡表，照着练 8 周。",
    },
    {
        "id": "scene",
        "name": "场景代入",
        "scene_roles": ["钩子（场景）", "场景里的阻力", "干货点1", "干货点2", "行动号召"],
        "hook_instruction": (
            "第 1 镜必须是一个具体场景（下班从地铁出来、第一次推开健身房门、久坐一下午站起来），"
            "用“你”开头、带一个感官细节（累、僵、怕），不要写通用焦虑；场景要落到本地人能对号入座的"
            "具体阻力（下班晚、怕被推卡、预算、社恐、通勤）。"
        ),
        "hook_examples": [
            "下班从 4 号线出来，肩膀僵得转不动，你也是这样吗",
            "第一次推开健身房的门，最怕的就是被人推卡",
        ],
        "title_pattern": "身份共鸣：如“下班晚的上班族，一周 3 次这样练”",
        "leave_blank": "这个场景的解法不在开头，在第 3 镜。",
        "series_next": "下一条讲同一个场景的另一个阻力：预算不够怎么练。",
    },
    {
        "id": "conclusion_first",
        "name": "先给结论",
        "scene_roles": ["钩子（结论前置）", "结论的依据", "干货点1", "干货点2", "行动号召"],
        "hook_instruction": (
            "第 1 镜直接给结论（“先说答案：……”），结论必须是一句可执行的判断（做/不做、先做哪个），"
            "不要给模糊的建议；第 2 镜立刻给依据（数字或判断标准），不要让观众等到最后才知道为什么。"
        ),
        "hook_examples": [
            "先说答案：新手第一周别追重量，先把动作走稳",
            "先说结论：减脂先记 3 天饮食，再谈怎么调",
        ],
        "title_pattern": "答案前置：如“膝盖疼能不能深蹲？答案是先停”",
        "leave_blank": "结论先给了，依据在后面，看到底再决定跟不跟。",
        "series_next": "下一条讲结论背后的判断标准，自己就能测。",
    },
]

_BY_ID = {s["id"]: s for s in STRUCTURES}


def list_structures(cfg: dict | None = None) -> list[dict]:
    """返回启用的结构（id + name）。cfg["hooks"]["enabled"] 可缩小范围。

    enabled 未配（None）时全启用；显式空列表表示一个都不启用 ——
    空列表不再含糊地当成“全启用”（GPT 第二次复核第 3 节）。"""
    enabled = None
    if cfg:
        enabled = (cfg.get("hooks") or {}).get("enabled")
    if enabled is None:
        return [{"id": s["id"], "name": s["name"]} for s in STRUCTURES]
    return [{"id": s["id"], "name": s["name"]} for s in STRUCTURES
            if s["id"] in enabled]


def get_structure(structure_id: str, cfg: dict | None = None) -> dict:
    """取某结构；cfg["hooks"]["structures"] 可按 id 覆盖 hook_examples 等字段。

    返回深拷贝：调用方改 hook_examples 等嵌套列表也不会污染全局 STRUCTURES
    （GPT 第二次复核第 3 节：浅拷贝污染全局结构）。"""
    base = _BY_ID.get(structure_id) or STRUCTURES[0]
    out = copy.deepcopy(base)
    if cfg:
        overrides = (cfg.get("hooks") or {}).get("structures") or {}
        if isinstance(overrides, dict) and structure_id in overrides:
            for k, v in (overrides[structure_id] or {}).items():
                if k in out:
                    out[k] = v
    return out


def pick_structure(topic: dict, cfg: dict | None = None) -> str:
    """按选题稳定选结构：同一标题每次生成同一结构，不同标题轮换。

    cfg["hooks"]["default"] 可钉死一个结构；topic["structure"] 可单条指定。
    """
    if topic.get("structure") in _BY_ID:
        return topic["structure"]
    if cfg:
        default = (cfg.get("hooks") or {}).get("default")
        if default in _BY_ID:
            return default
    pool = list_structures(cfg) or [{"id": STRUCTURES[0]["id"]}]
    digest = hashlib.sha256(str(topic.get("title", "")).encode("utf-8")).hexdigest()
    return pool[int(digest, 16) % len(pool)]["id"]


def structure_block(structure_id: str, cfg: dict | None = None) -> str:
    """拼进 system prompt 的结构块。主提示词（SYSTEM_PROMPT）本身不动。"""
    s = get_structure(structure_id, cfg)
    roles = "、".join(f"{i + 1}.{r}" for i, r in enumerate(s["scene_roles"]))
    return (
        f"本次选用脚本结构「{s['name']}」：分镜依次为 {roles}。\n"
        f"{s['hook_instruction']}\n"
        f"标题套用公式：{s['title_pattern']}。\n"
        f"留白：{s['leave_blank']}"
    )
