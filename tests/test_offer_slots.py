"""承诺口径「按结构可换」的回归门禁（2026-11-08）。

判据不是 config 里多了几个字段，而是：
  1. 默认 P0 与现行产物逐字一致（能力升级不得悄悄改掉正在跑的文案）；
  2. 换方案后，开头 / 钩子 / 内容 / 结尾四结构位真的都变，且 LLM 入口同步变；
  3. 可以只换一个结构位，其余结构位逐字不动；
  4. 未获门店书面确认的具体承诺（价格 / 活动 / 时长 / 无推销）被闸门拦下并回退；
  5. 删掉 local 段，方案库全部失效，产物回到通用快照；
  6. 切换接口只改 active/overrides，不冲掉 playbook 等既有配置。
"""
from __future__ import annotations

import copy
import json
import re
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import core.llm as llm  # noqa: E402
from core.compliance import check_and_fix  # noqa: E402
from core.offer import (LABELS, SLOTS, active_offer, apply_confirmation,  # noqa: E402
                        apply_selection, audit, hits, offer_block, offer_cfg,
                        offer_meta, presets, public_state, slots, specific_allowed)
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
RESIDENCE_CLAIM = re.compile(r"住(?:在)?[^\s，。、]{0,10}(?:公寓|小区|社区)")


def cfg_with(cfg: dict, active: str = "", **overrides) -> dict:
    c = copy.deepcopy(cfg)
    if active:
        c["local"]["offer"]["active"] = active
    if overrides:
        c["local"]["offer"]["overrides"] = dict(overrides)
    return c


def flat(script: dict) -> str:
    return " ".join([
        str(script.get("title") or ""), str(script.get("cover_text") or ""),
        str(script.get("caption") or ""),
        " ".join(str(s.get("narration") or "") for s in script.get("scenes") or []),
        " ".join(str(s.get("on_screen") or "") for s in script.get("scenes") or []),
        " ".join(str(s.get("sub") or "") for s in script.get("scenes") or []),
    ])


class TestOfferStructure(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.off = copy.deepcopy(self.cfg)
        self.off.pop("local", None)

    # ---------------------------------------------------------------- 配置形状
    def test_catalog_has_four_slots_and_three_presets(self):
        self.assertEqual(tuple(SLOTS), ("opening", "hook", "body", "closing"))
        self.assertEqual([LABELS[n] for n in SLOTS], ["开头", "钩子", "内容", "结尾"])
        ps = presets(self.cfg)
        self.assertEqual({p["id"] for p in ps},
                         {"P0_current", "P1_trial_first", "P2_store_offer"})
        for p in ps:
            self.assertEqual(set(p["slots"]), set(SLOTS), f"{p['id']} 结构位不齐")
        st = public_state(self.cfg)
        self.assertEqual(st["slot_order"], list(SLOTS))
        self.assertEqual(set(st["slots"]), set(SLOTS))
        self.assertTrue(st["claim_gate"]["banned_claims"])
        self.assertTrue(any("书面确认" in x for x in st["evidence_boundary"]))

    def test_default_is_the_current_running_copy(self):
        """P0 必须与改造前产物逐字一致，否则就是偷偷改了正在执行的口径。"""
        s = llm.template_script(dict(TOPIC), self.cfg)
        local = self.cfg["local"]
        self.assertEqual(s["offer_audit"]["active"], "P0_current")
        self.assertIn(local["cta_store"], s["scenes"][4]["narration"])
        self.assertIn(local["pain_local"], s["scenes"][1]["narration"])
        self.assertEqual(s["scenes"][1]["on_screen"], "怕被推卡？")
        self.assertEqual(s["scenes"][4]["on_screen"], "先试一次")
        self.assertTrue(any(a.strip("，, ") in s["scenes"][0]["narration"]
                            for a in local["audience_anchors"]))
        before = read_json(BASE / "ab_after.json", {}) or {}
        if before:
            self.assertEqual(s["scenes"][0]["narration"], before["scenes"][0]["narration"])
            self.assertEqual(s["caption"], before["caption"])

    # ---------------------------------------------------------------- 整套可换
    def test_switching_preset_changes_all_four_slots(self):
        p0 = llm.template_script(dict(TOPIC), self.cfg)
        p1 = llm.template_script(dict(TOPIC), cfg_with(self.cfg, "P1_trial_first"))
        p1offer = active_offer(cfg_with(self.cfg, "P1_trial_first"))
        s1 = p1offer["slots"]

        # 开头：锚点来自新方案，不再来自 config 的旧池
        self.assertTrue(any(a.strip("，, ") in p1["scenes"][0]["narration"]
                            for a in s1["opening"]["anchors"]),
                        p1["scenes"][0]["narration"])
        # 钩子：方案附加口径 + 画面大字
        self.assertIn(s1["hook"]["text"], p1["scenes"][0]["narration"])
        self.assertEqual(p1["scenes"][0]["on_screen"], s1["hook"]["on_screen"])
        # 内容 / 结尾：口播、画面大字、caption CTA 三处都换
        self.assertEqual(p1["scenes"][1]["narration"], s1["body"]["text"])
        self.assertEqual(p1["scenes"][1]["on_screen"], s1["body"]["on_screen"])
        self.assertIn(s1["closing"]["text"], p1["scenes"][4]["narration"])
        self.assertEqual(p1["scenes"][4]["on_screen"], s1["closing"]["on_screen"])
        self.assertIn(s1["closing"]["text"], p1["caption"])
        self.assertNotIn(self.cfg["local"]["cta_store"], p1["caption"])
        self.assertEqual(p1["offer_audit"]["active"], "P1_trial_first")
        self.assertNotEqual(flat(p0), flat(p1))

    def test_opening_comes_before_hook(self):
        """结构顺序：开头点名在最前，钩子口径在其后，主钩子最后。"""
        p1 = llm.template_script(dict(TOPIC), cfg_with(self.cfg, "P1_trial_first"))
        head = p1["scenes"][0]["narration"]
        s1 = active_offer(cfg_with(self.cfg, "P1_trial_first"))["slots"]
        positions = [head.find(a.strip("，, ")) for a in s1["opening"]["anchors"]
                     if a.strip("，, ") in head]
        self.assertTrue(positions, f"开头未使用当前方案锚点：{head}")
        anchor_pos = min(positions)
        self.assertLess(anchor_pos, head.find(s1["hook"]["text"]), head)
        self.assertLess(head.find(s1["hook"]["text"]), head.find("第一次去健身房"))

    def test_hook_prefix_skipped_for_self_contained_local_topic(self):
        """选题自带本地锚点时，方案钩子口径要主动避让，不叠出两个「别问年卡」。"""
        src = self.cfg["local"]["local_topics"][0]
        t = dict(TOPIC, title=src["title"], hook=src["hook"],
                 hook_mode=src.get("hook_mode", "self_contained"))
        p1 = llm.template_script(t, cfg_with(self.cfg, "P1_trial_first"))
        p0 = llm.template_script(t, self.cfg)
        self.assertNotIn("先别问年卡多少钱", p1["scenes"][0]["narration"])
        self.assertEqual(p1["scenes"][0]["narration"], p0["scenes"][0]["narration"])
        # 内容 / 结尾仍然要跟着方案换，避让不等于整套失效
        self.assertNotEqual(p1["scenes"][1]["narration"], p0["scenes"][1]["narration"])
        self.assertNotEqual(p1["scenes"][4]["narration"], p0["scenes"][4]["narration"])

    # ---------------------------------------------------------------- 逐位可换
    def test_single_slot_override_changes_only_that_slot(self):
        custom = "到店先领一张动线图，自己走一遍再决定。"
        override = cfg_with(self.cfg, closing=custom)
        p0 = llm.template_script(dict(TOPIC), self.cfg)
        ov = llm.template_script(dict(TOPIC), override)
        self.assertEqual(slots(override)["closing"]["origin"], "override")
        self.assertIn(custom, ov["scenes"][4]["narration"])
        self.assertIn(custom, ov["caption"])
        self.assertNotIn(self.cfg["local"]["cta_store"], ov["caption"])
        # 其余三个结构位逐字不动
        for i in (0, 1, 2, 3):
            self.assertEqual(ov["scenes"][i], p0["scenes"][i], f"scene{i+1} 被误改")
        self.assertEqual(ov["scenes"][4]["on_screen"], p0["scenes"][4]["on_screen"])

    # ---------------------------------------------------------------- 承诺闸门
    def test_unconfirmed_specific_claims_are_blocked_and_fallback(self):
        p2 = cfg_with(self.cfg, "P2_store_offer")
        a = audit(p2)
        self.assertFalse(a["specific_allowed"])
        self.assertIn("hook", a["blocked_slots"])
        self.assertIn("body", a["blocked_slots"])
        self.assertIn("closing", a["blocked_slots"])
        s = llm.template_script(dict(TOPIC), p2)
        text = flat(s)
        for word in ("9.9", "练一整天", "不限时长", "不推卡", "随时来"):
            self.assertNotIn(word, text, f"未确认承诺漏进产物：{word}")
        # 被拦结构位逐字回退到 P0
        p0 = llm.template_script(dict(TOPIC), self.cfg)
        self.assertEqual(s["scenes"][0]["narration"], p0["scenes"][0]["narration"])
        self.assertEqual(s["scenes"][1]["narration"], p0["scenes"][1]["narration"])
        self.assertEqual(s["scenes"][4]["narration"], p0["scenes"][4]["narration"])
        self.assertEqual(s["offer_audit"]["blocked_slots"], a["blocked_slots"])

    def test_written_confirmation_with_expiry_unlocks(self):
        """书面确认必须走 append-only 台账；只改模板 confirmed 字段不算确认。"""
        import tempfile
        p2 = cfg_with(self.cfg, "P2_store_offer")
        self.assertFalse(audit(p2)["specific_allowed"])
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "confirmations.json"
            with patch("core.offer.confirmations_source", return_value=tmp):
                from core.offer import catalog
                cat_obj = catalog(p2)
                target = next(p for p in cat_obj["presets"] if p["id"] == "P2_store_offer")
                target["confirmed"] = True
                target["confirmed_by"] = "店长签字（只改模板，未登记台账）"
                target["valid"]["until"] = (date.today() + timedelta(days=30)).isoformat()
                with patch("core.offer.catalog", return_value=cat_obj):
                    self.assertFalse(audit(p2)["specific_allowed"],
                                     "模板自述 confirmed=true 不得绕过书面确认台账")
                    self.assertNotIn("9.9", flat(llm.template_script(dict(TOPIC), p2)))
                # 正规路径：登记四项齐全的书面确认
                apply_confirmation(p2, {
                    "preset_id": "P2_store_offer", "action": "confirm",
                    "confirmed_by": "前浯店店长 张三",
                    "evidence": "门店盖章《单次体验活动说明》2026-11-08",
                    "valid_until": (date.today() + timedelta(days=30)).isoformat(),
                    "allows_specific_claims": True,
                })
                self.assertTrue(audit(p2)["specific_allowed"])
                unlocked = llm.template_script(dict(TOPIC), p2)
                self.assertIn("9.9", flat(unlocked))
                self.assertEqual(audit(p2)["blocked_slots"], [])
                # 有效期一过立即失效，无需人工撤销
                log = read_json(tmp, {})
                log["records"][-1]["valid"]["until"] = (date.today() - timedelta(days=1)).isoformat()
                write_json(tmp, log)
                self.assertFalse(audit(p2)["specific_allowed"])
                self.assertNotIn("9.9", flat(llm.template_script(dict(TOPIC), p2)))

    def test_override_cannot_smuggle_unconfirmed_claim(self):
        """即使当前方案是已确认的 P0，逐位覆盖也不得带进未确认的具体承诺。"""
        cfg = cfg_with(self.cfg, closing="开业期 9.9 单次，不限时长")
        self.assertFalse(specific_allowed(active_offer(cfg)))
        a = audit(cfg)
        self.assertIn("closing", a["blocked_slots"])
        s = llm.template_script(dict(TOPIC), cfg)
        self.assertNotIn("9.9", flat(s))
        self.assertEqual(s["scenes"][4]["narration"],
                         llm.template_script(dict(TOPIC), self.cfg)["scenes"][4]["narration"])

    def test_p1_keeps_compliance_clean(self):
        p1 = llm.template_script(dict(TOPIC), cfg_with(self.cfg, "P1_trial_first"))
        rep = check_and_fix(copy.deepcopy(p1), self.cfg)["compliance"]
        self.assertEqual(rep["local_hits"], 0, rep["replacements"])
        self.assertIsNone(RESIDENCE_CLAIM.search(flat(p1)))

    # ---------------------------------------------------------------- LLM 入口
    def test_llm_prompt_carries_four_slots(self):
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
        p1 = cfg_with(self.cfg, "P1_trial_first")
        with patch.object(llm, "http_post_json", side_effect=fake_post):
            llm._llm_script(dict(TOPIC), p1, [provider])
            user_on = copy.deepcopy(captured["body"]["messages"][1]["content"])
            sys_on = copy.deepcopy(captured["body"]["messages"][0]["content"])
            llm._llm_script(dict(TOPIC), self.off, [provider])
            user_off = copy.deepcopy(captured["body"]["messages"][1]["content"])
        for marker in ("当前执行的承诺方案", "P1_trial_first", "开头（opening）",
                       "钩子（hook）", "内容（body）", "结尾（closing）",
                       "先别问年卡多少钱", "先做一次体态评估"):
            self.assertIn(marker, user_on, f"LLM user 缺少 {marker}")
        self.assertIn("四结构位", sys_on)
        self.assertNotIn(self.cfg["local"]["cta_store"], user_on,
                         "切换方案后 LLM 仍收到旧的到店硬编码，出现双口径")
        self.assertNotIn("当前执行的承诺方案", user_off)

    # ---------------------------------------------------------------- 回退 / 选题
    def test_off_strictly_disables_offer_machinery(self):
        self.assertEqual(offer_cfg(self.off), {})
        self.assertEqual(slots(self.off), {})
        self.assertEqual(audit(self.off)["enabled"], False)
        self.assertEqual(offer_block(self.off), "")
        self.assertEqual(offer_meta(self.off), {})
        s = llm.template_script(dict(TOPIC), self.off)
        before = read_json(BASE / "ab_before.json", {}) or {}
        self.assertTrue(before)
        self.assertEqual(s["caption"], before["caption"])
        self.assertEqual(s["scenes"][0]["narration"], before["scenes"][0]["narration"])
        self.assertEqual(s["scenes"][4]["narration"], before["scenes"][4]["narration"])
        self.assertFalse(s["offer_audit"]["enabled"])

    def test_disabling_offer_only_falls_back_to_pre_change_copy(self):
        """只删 local.offer（不删整个 local）也必须逐字回到改造前口径。"""
        c = copy.deepcopy(self.cfg)
        c["local"].pop("offer", None)
        s = llm.template_script(dict(TOPIC), c)
        p0 = llm.template_script(dict(TOPIC), self.cfg)
        self.assertEqual(s["caption"], p0["caption"])
        self.assertEqual([x["narration"] for x in s["scenes"]],
                         [x["narration"] for x in p0["scenes"]])
        self.assertEqual([x["on_screen"] for x in s["scenes"]],
                         [x["on_screen"] for x in p0["scenes"]])
        self.assertFalse(s["offer_audit"]["enabled"])

    def test_topics_carry_current_offer_and_anchor_pool(self):
        topic_file = DATA / "scripts" / f"topics-{date.today():%Y-%m-%d}.json"
        existed = topic_file.exists()
        backup = read_json(topic_file, None) if existed else None
        hot = {"fitness_related": [], "bridge_candidates": []}
        try:
            p1 = cfg_with(self.cfg, "P1_trial_first")
            topics = build_topics(hot, p1, count=3)
            self.assertEqual(len(topics), 3)
            for t in topics:
                self.assertEqual(t["offer_id"], "P1_trial_first")
                self.assertEqual(set(t["offer_slots"]), set(SLOTS))
            pool = active_offer(p1)["slots"]["opening"]["anchors"]
            self.assertTrue(any(t["audience_anchor"] in pool for t in topics),
                            "选题缓存的锚点没有跟着方案切换")
            off = build_topics(hot, self.off, count=3)
            self.assertFalse(any("offer_id" in t for t in off))
        finally:
            if existed:
                write_json(topic_file, backup)
            elif topic_file.exists():
                topic_file.unlink()

    # ---------------------------------------------------------------- 切换接口
    def test_apply_selection_is_narrow_and_validated(self):
        c = copy.deepcopy(self.cfg)
        before_playbook = copy.deepcopy(c["local"]["playbook"])
        before_anchors = copy.deepcopy(c["local"]["audience_anchors"])
        out = apply_selection(c, {"active": "P1_trial_first",
                                  "overrides": {"closing": "先做一次体态评估再决定。"}})
        self.assertEqual(out["local"]["offer"]["active"], "P1_trial_first")
        self.assertEqual(out["local"]["offer"]["overrides"]["closing"],
                         "先做一次体态评估再决定。")
        self.assertEqual(out["local"]["playbook"], before_playbook,
                         "切换方案冲掉了 playbook 配置")
        self.assertEqual(out["local"]["audience_anchors"], before_anchors)
        for bad in ({"active": "NOPE"}, {"active": "P0_current", "overrides": {"title": "x"}},
                    {"active": "P0_current", "overrides": {"closing": "字" * 161}}):
            with self.assertRaises(ValueError):
                apply_selection(copy.deepcopy(self.cfg), bad)

    def test_claim_gate_detects_specific_claims(self):
        self.assertTrue(hits("单次 9.9 不限时长", self.cfg))
        self.assertTrue(hits("不强行引导办卡", self.cfg))
        self.assertTrue(hits("器械也不用排队", self.cfg))
        self.assertEqual(hits("先试一次单次体验", self.cfg), [],
                         "低门槛动作不应被当成具体承诺误拦")


if __name__ == "__main__":
    unittest.main(verbosity=2)
