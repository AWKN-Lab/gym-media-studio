"""本地获客文案逻辑的回归 + 证伪门禁。

核心判据不是「配置里加了 local 字段」，而是「产物文案真的变了」：
1. 开启 local 后，模板与 LLM 两条生成入口都要带上本地锚点；
2. 删掉 local 段后，产物必须逐字回到改前快照 ab_before.json；
3. 假 provider 捕获请求体，证明 LLM 的 system / user 消息确实注入，
   而不是只改了 template 路径就宣称「已修」。

2026-11-08 二次纠偏后新增：
4. 禁止断言受众住在某个小区/公寓（旧版把「住浯家公寓」钉成了必须项，是错误口径）；
5. 多分支人群锚点按标题稳定轮换，且方法库 / 同城对标必须真正进入产物与 LLM 请求体。
"""
from __future__ import annotations

import copy
import json
import re
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import core.llm as llm  # noqa: E402
from core.compliance import check_and_fix, check_text  # noqa: E402
from core.playbook import (format_for, load_benchmark, load_method,  # noqa: E402
                           method_block, pick_anchor, topic_transfers)
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

# 住址断言：口播 / 字幕里一旦出现即视为事实越界。
RESIDENCE_CLAIM = re.compile(r"住(?:在)?[^\s，。、]{0,10}(?:公寓|小区|社区)")


def flatten(script: dict) -> str:
    return " ".join([
        str(script.get("title") or ""), str(script.get("cover_text") or ""),
        str(script.get("caption") or ""),
        " ".join(str(s.get("narration") or "") for s in script.get("scenes") or []),
        " ".join(str(s.get("sub") or "") for s in script.get("scenes") or []),
    ])


class TestLocalCopyGate(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.off = copy.deepcopy(self.cfg)
        self.off.pop("local", None)
        self.local = self.cfg["local"]

    # ---------------------------------------------------------------- 配置
    def test_local_config_shape(self):
        self.assertTrue(self.local["enabled"])
        for key in ("transit", "district", "store", "audience_anchors",
                    "anchor_tokens", "no_residence_claim", "cta_store",
                    "tags_local", "local_topics", "playbook"):
            self.assertTrue(self.local.get(key), f"local.{key} 缺失")
        self.assertNotIn("hook_prefix", self.local,
                         "单一住址前缀已废止，不得回退")
        self.assertTrue(self.local["playbook"]["enabled"])

    # ---------------------------------------------------------------- 模板入口
    def test_template_injects_local_markers(self):
        s = llm.template_script(dict(TOPIC), self.cfg)
        text = flatten(s)
        self.assertEqual(len(s["scenes"]), 5)
        for marker in (self.local["transit"], self.local["district"],
                       self.local["cta_store"], self.local["tags_local"][0]):
            self.assertIn(marker, text, f"产物缺少 {marker}")
        hook = s["scenes"][0]["narration"]
        self.assertTrue(any(a.strip("，, ") in hook
                            for a in self.local["audience_anchors"]),
                        f"产物未注入任何人群锚点：{hook}")

    def test_off_equals_before_snapshot(self):
        before = read_json(BASE / "ab_before.json", {}) or {}
        self.assertTrue(before, "缺少改前快照 ab_before.json")
        off = llm.template_script(dict(TOPIC), self.off)
        self.assertEqual(flatten(off), flatten(before),
                         "删掉 local 段后没有逐字回退，说明改动污染了通用路径")
        for marker in (self.local["transit"], self.local["district"],
                       self.local["tags_local"][0]):
            self.assertNotIn(marker, flatten(off))

    def test_anchor_not_duplicated_in_local_topic(self):
        """自带锚点的本地选题不得再叠一层前缀（4 号线只出现一次）。"""
        src = self.local["local_topics"][2]
        t = dict(TOPIC, hook=src["hook"], title=src["title"],
                 hook_mode=src.get("hook_mode", "self_contained"))
        s = llm.template_script(t, self.cfg)
        self.assertIsNone(RESIDENCE_CLAIM.search(s["scenes"][0]["narration"]))
        self.assertLessEqual(s["scenes"][0]["narration"].count("4 号线"), 1)

    # ---------------------------------------------------------------- LLM 入口
    def test_llm_prompt_carries_local_rules(self):
        captured = {}

        def fake_post(url, body, headers, timeout=None, **kw):
            captured["body"] = body
            scenes = [{"narration": f"口播{i}", "on_screen": "画面", "sub": "补充",
                       "visual_prompt": "实拍"} for i in range(5)]
            return {"choices": [{"message": {"content": json.dumps({
                "title": "标题", "cover_text": "封面", "caption": "发布文案",
                "scenes": scenes}, ensure_ascii=False)}}]}

        provider = {"name": "gateway:magpie", "base_url": "http://fake/v1",
                    "model": "fake-model", "key_env": "", "api_key": ""}
        with patch.object(llm, "http_post_json", side_effect=fake_post):
            on = llm._llm_script(dict(TOPIC), self.cfg, [provider])
            body_on = copy.deepcopy(captured["body"])
            llm._llm_script(dict(TOPIC), self.off, [provider])
            body_off = copy.deepcopy(captured["body"])

        self.assertTrue(on and on["generator"].startswith("llm:"))
        self.assertIn("本地门店号补充规则", body_on["messages"][0]["content"])
        self.assertIn(self.local["transit"], body_on["messages"][1]["content"])
        self.assertIn(self.local["tags_local"][0], body_on["messages"][1]["content"])
        self.assertNotIn("本地门店号补充规则", body_off["messages"][0]["content"])
        self.assertNotIn(self.local["transit"], body_off["messages"][1]["content"])

    # ---------------------------------------------------------------- 选题入口
    def test_build_topics_local_and_rollback(self):
        topic_file = DATA / "scripts" / f"topics-{datetime.now():%Y-%m-%d}.json"
        existed = topic_file.exists()
        backup = read_json(topic_file, None) if existed else None
        hot = {"fitness_related": [], "bridge_candidates": []}
        try:
            topics = build_topics(hot, self.cfg, count=3)
            self.assertEqual(len(topics), 3)
            self.assertTrue(any(t["kind"] == "local" for t in topics))
            for key in ("place", "store", "transit", "local_tags", "cta_store",
                        "audience_anchor", "no_residence_claim"):
                self.assertIn(key, topics[0])
            off = build_topics(hot, self.off, count=3)
            self.assertFalse(any(t["kind"] == "local" for t in off))
            self.assertFalse(any("place" in t for t in off))
            self.assertFalse(any("audience_anchor" in t for t in off))
        finally:
            if existed:
                write_json(topic_file, backup)
            elif topic_file.exists():
                topic_file.unlink()

    # ---------------------------------------------------------------- 合规
    def test_new_copy_does_not_trip_compliance(self):
        s = llm.template_script(dict(TOPIC), self.cfg)
        checked = check_and_fix(copy.deepcopy(s), self.cfg)
        rep = checked["compliance"]
        self.assertEqual(rep["local_hits"], 0, f"新增文案引入违禁词：{rep['replacements']}")
        self.assertFalse(rep["passed"], "外部审核未启用时不得宣称 passed")
        self.assertIn("第一次", checked["scenes"][0]["narration"],
                      "SAFE_PHRASES 保护失效，正常表述被误改写")
        self.assertIn("第一周", checked["caption"])


class TestResidenceClaimGate(unittest.TestCase):
    """用户 11-08 纠偏：门店周边客 ≠ 某个公寓住户。反向钉死，防止回退。"""

    def setUp(self):
        self.cfg = load_config()
        self.off = copy.deepcopy(self.cfg)
        self.off.pop("local", None)
        self.local = self.cfg["local"]

    def test_no_residence_assertion_anywhere_in_output(self):
        titles = ["新手第一次进健身房，这 3 件事别做", "圆肩驼背一个动作救回来",
                  "减脂总失败，多半卡在这个热量误区", "深蹲膝盖疼检查 2 个细节",
                  "一周练几次才有效", "体脂降了体重没变", "蛋白质到底吃多少"]
        for title in titles:
            t = dict(TOPIC, title=title)
            s = llm.template_script(t, self.cfg)
            text = flatten(s)
            self.assertIsNone(RESIDENCE_CLAIM.search(text),
                              f"产物出现住址断言：{title} → {s['scenes'][0]['narration']}")
            self.assertNotIn("住浯家公寓", text)

    def test_anchors_are_multibranch_and_stable(self):
        seen = set()
        for i in range(24):
            t = dict(TOPIC, title=f"选题样本 {i}：第一次进馆")
            a = pick_anchor(t, self.local)
            self.assertTrue(a)
            self.assertIsNone(RESIDENCE_CLAIM.search(a), f"锚点含住址断言：{a}")
            seen.add(a)
            self.assertEqual(a, pick_anchor(t, self.local), "同一标题锚点必须稳定")
        self.assertGreaterEqual(len(seen), 2, "多分支锚点没有实际轮换")
        self.assertLessEqual(len(seen), len(self.local["audience_anchors"]))

    def test_off_has_no_anchor_machinery(self):
        self.assertEqual(pick_anchor(dict(TOPIC), {}), "")
        self.assertEqual(method_block(self.off), "")
        self.assertEqual(load_method(self.off), {})
        self.assertEqual(load_benchmark(self.off), {})
        self.assertEqual(topic_transfers(self.off), [])
        self.assertEqual(format_for(dict(TOPIC), self.off), "")


class TestPlaybookGate(unittest.TestCase):
    """素材方法库与同城对标必须真的进入产物 / LLM 请求，不是只写进文档。"""

    def setUp(self):
        self.cfg = load_config()
        self.off = copy.deepcopy(self.cfg)
        self.off.pop("local", None)

    def test_method_library_sources_are_traceable(self):
        m = load_method(self.cfg)
        self.assertEqual(len(m["script_skeleton"]), 5)
        self.assertTrue(all(x["evidence"] for x in m["script_skeleton"]))
        self.assertTrue(all(x["source_quote"] for x in m["hook_patterns"]))
        for t in m["topic_transfers"]:
            self.assertTrue(t["source_quote"], f"{t['id']} 缺引文")
            self.assertTrue(t["source_label"], f"{t['id']} 缺来源标注")
        for f in m["formats"]:
            self.assertTrue(f["evidence"][0]["source"])
        self.assertTrue(any("视频逐字稿" in x for x in m["evidence_boundary"]),
                        "方法库必须把「无逐字稿」写进证据边界")

    def test_benchmark_separates_fact_from_claim(self):
        b = load_benchmark(self.cfg)
        own = b["own_reference"]
        self.assertIn("非门店官方", own["source_type"])
        self.assertIn("凌扬", own["full_text"])
        usable = {d["id"]: d["usable_in_copy"] for d in own["differentiators"]}
        self.assertTrue(usable["D1"] and usable["D2"] and usable["D3"])
        self.assertFalse(usable["D5"] and usable["D6"],
                         "无推销承诺与开业期价格不得进入生成文案")
        forb = " ".join(b["usable_in_copy"]["forbidden"])
        self.assertIn("价格", forb)
        self.assertIn("同行", forb)

    def test_llm_request_carries_method_and_benchmark(self):
        captured = {}

        def fake_post(url, body, headers, timeout=None, **kw):
            captured["body"] = body
            scenes = [{"narration": f"口播{i}", "on_screen": "画面", "sub": "补充",
                       "visual_prompt": "实拍"} for i in range(5)]
            return {"choices": [{"message": {"content": json.dumps({
                "title": "标题", "cover_text": "封面", "caption": "发布文案",
                "scenes": scenes}, ensure_ascii=False)}}]}

        provider = {"name": "gateway:magpie", "base_url": "http://fake/v1",
                    "model": "fake-model", "key_env": "", "api_key": ""}
        with patch.object(llm, "http_post_json", side_effect=fake_post):
            llm._llm_script(dict(TOPIC), self.cfg, [provider])
            user_on = copy.deepcopy(captured["body"]["messages"][1]["content"])
            llm._llm_script(dict(TOPIC), self.off, [provider])
            user_off = copy.deepcopy(captured["body"]["messages"][1]["content"])

        for marker in ("脚本骨架", "可用钩子句式", "必须避开", "同城对标可用",
                       "同城对标禁用", "住址口径", "断言受众住在"):
            self.assertIn(marker, user_on, f"LLM user 消息缺 {marker}")
        self.assertNotIn("脚本骨架", user_off)
        self.assertNotIn("同城对标禁用", user_off)

    def test_transfer_topics_enter_selection_pool(self):
        topic_file = DATA / "scripts" / f"topics-{datetime.now():%Y-%m-%d}.json"
        existed = topic_file.exists()
        backup = read_json(topic_file, None) if existed else None
        try:
            transfers = topic_transfers(self.cfg)
            self.assertGreaterEqual(len(transfers), 7)
            self.assertTrue(any(t["kind"] == "method_transfer" for t in transfers))
            self.assertTrue(any(t["kind"] == "local_compare" for t in transfers))
            # 逐条单独生成，确认每条转译选题都能产出、且不含住址断言
            for t in transfers:
                topic = dict(TOPIC, title=t["title"], hook=t["hook"],
                             angle=t["angle"], hook_mode=t.get("hook_mode", "needs_anchor"))
                s = llm.template_script(topic, self.cfg)
                self.assertIsNone(RESIDENCE_CLAIM.search(flatten(s)), t["id"])
                self.assertIn(self.cfg["local"]["cta_store"], s["scenes"][-1]["narration"])
        finally:
            if existed:
                write_json(topic_file, backup)
            elif topic_file.exists():
                topic_file.unlink()

    def test_visual_prompt_uses_playbook_format(self):
        forms = [f["visual_prompt"] for f in load_method(self.cfg)["formats"]
                 if f.get("usable")]
        t = dict(TOPIC)
        s = llm.template_script(t, self.cfg)
        self.assertIn(s["scenes"][2]["visual_prompt"], forms)
        off = llm.template_script(t, self.off)
        self.assertNotIn(off["scenes"][2]["visual_prompt"], forms)


class TestSafePhraseGate(unittest.TestCase):
    """钉住本轮发现的既有缺陷：被保护词未改写却记一次违规命中。"""

    def test_protected_phrases_not_flagged_but_real_absolute_is(self):
        cfg = load_config()
        clean = "第一次进健身房，第一周别追重量，第一视角实拍"
        r = check_text(clean, cfg, external=False)
        self.assertEqual(r["text"], clean)
        self.assertEqual(r["report"]["local_hits"], 0,
                         "保护词文本未变，不应误报为违规命中")
        bad = check_text("这是第一名的说法", cfg, external=False)
        self.assertNotIn("第一名", bad["text"])
        self.assertGreaterEqual(bad["report"]["local_hits"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
