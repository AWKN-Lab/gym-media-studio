"""共性原始素材库：网上搜来的健身房共性素材，不是本店记录。

来源：公开的新手指南、厦门同行抖音主页与视频、团购价格报道。
用途：FAQ 选题进选题池；竞品定位进对标；市场价只做市场参考。
本店价以门店书面确认为准，没有确认就没有价，市场价绝不写进文案。

加载规则（GPT 复核 2026-11-10 第 2 项）：
  - 优先读真实文件 data/baseline/common_materials.json（gitignored，不入库）；
  - 真实文件缺失时读随仓示例 examples/baseline/common_materials.example.json，
    并在返回结果里打 is_example=True 标记 —— 示例素材绝不能冒充真实门店资料，
    生产生成流程拿到 is_example=True 时必须在界面/日志里显示“示例素材”；
  - 两份都没有时直接报 FileNotFoundError，不静默返回空数据。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "data" / "baseline" / "common_materials.json"
EXAMPLE_PATH = ROOT / "examples" / "baseline" / "common_materials.example.json"
_FALLBACK_NOTICED = False


def load_common_materials() -> dict:
    """优先真实文件，缺失时回退到示例文件并打 is_example 标记。"""
    global _FALLBACK_NOTICED
    for path, is_example in ((PATH, False), (EXAMPLE_PATH, True)):
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            data["is_example"] = is_example
            if is_example and not _FALLBACK_NOTICED:
                print("提示：未找到 data/baseline/common_materials.json，"
                      "已使用 examples/baseline/common_materials.example.json"
                      "（示例素材，不是门店记录）。")
                _FALLBACK_NOTICED = True
            return data
    raise FileNotFoundError(
        "共性素材缺失：data/baseline/common_materials.json 与 "
        "examples/baseline/common_materials.example.json 都不存在")


def materials_source(materials: dict | None = None) -> str:
    """返回素材来源：\"real\" 真实文件 / \"example\" 示例文件。"""
    m = materials if materials is not None else load_common_materials()
    return "example" if m.get("is_example") else "real"


def faq_topics(materials: dict | None = None) -> list[dict]:
    """FAQ 选题：每个问题一条常青选题，标题、钩子、角度、封面都取自素材库。
    本店不适用的（applicable 为 False，如没有课表的团课题）跳过。
    注意：materials 显式传空字典时不准偷偷读磁盘，只有 None 才触发加载。
    每条选题带 materials_source（"real" / "example"）：生成物元数据里
    必须能分清素材来源 —— 示例素材绝不能冒充门店记录，template_script
    把 topic 原样写进脚本的 topic 字段，标记随产物走（GPT 第二次复核
    第 1 项“生产界面是否展示示例状态未证实”的跟进；日志提示在
    load_common_materials 回退时已打）。"""
    m = materials if materials is not None else load_common_materials()
    if not m.get("faq"):
        return []
    src = "example" if m.get("is_example") else "real"
    return [{"title": f["topic"]["title"], "hook": f["topic"]["hook"],
             "angle": f["topic"]["angle"], "cover": f["topic"]["cover"],
             "answer": f["a"], "takeaway": f["topic"]["cover"],
             "pain": f.get("pain", ""), "materials_source": src}
            for f in m["faq"] if f.get("applicable") is not False]


def xiamen_competitors(materials: dict | None = None) -> list[dict]:
    m = materials if materials is not None else load_common_materials()
    return list(m.get("competitors_xiamen") or [])
