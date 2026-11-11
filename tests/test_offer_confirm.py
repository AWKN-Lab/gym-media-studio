"""门店书面确认台账的回归门禁（2026-11-08 续）。

上一轮做完了「承诺按四结构位可换」，但留了一个闭环缺口：
闸门要求「门店书面确认 + 确认人 + 有效期」才放行具体承诺，
却没有任何界面/接口能登记这些信息 —— P2 永远换不上去，只能手改 JSON。

本文件钉死：确认必须走 append-only 台账，四项缺一不放行，
撤销/到期立即回退，台账不误伤其它方案，关掉 local 时台账完全失效，
以及通用回退路径里那句冻结硬编码不会漏进 local 开启时的产物。
"""
from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import core.llm as llm  # noqa: E402
from core import util  # noqa: E402
from core.offer import (UNLOCK_FIELDS, apply_confirmation, audit,  # noqa: E402
                        confirmation_for, confirmation_log, effective_preset,
                        offer_meta, public_state)
from core.topics import build_topics  # noqa: E402
from core.util import load_config, read_json, write_json  # noqa: E402

BASE = ROOT / "data" / "baseline"
TOPIC = {
    "title": "新手第一次进健身房，这 3 件事别做",
    "kind": "evergreen",
    "hook": "第一次去健身房，最怕的不是练不动，是练错。",
    "angle": "降低新手门槛，给出可执行清单",
    "hot_ref": "", "hot_score": 0, "platform": "常青选题库", "score": 6.0,
    "cover": "新手别踩的坑", "linked_hot": "", "hot_context": "",
}
FROZEN_LINE = "可以先给你做一次体态评估"


def flat(script: dict) -> str:
    return " ".join([
        str(script.get("title") or ""), str(script.get("cover_text") or ""),
        str(script.get("caption") or ""),
        " ".join(str(s.get("narration") or "") for s in script.get("scenes") or []),
        " ".join(str(s.get("on_screen") or "") for s in script.get("scenes") or []),
    ])


def snapshot_source() -> str:
    """快照来源：作者真实冻结快照（gitignored，不入库）还是随仓示例快照。
    两者生成口径不同，测试失败时先看来源再定性（GPT 第二次复核第 3 节）。"""
    return "author" if (BASE / "ab_before.json").exists() else "example"


def snapshot() -> dict:
    """改前快照：按 snapshot_source() 的来源取（作者真实文件优先，
    不在仓库里时用随仓的示例快照，由示例配置生成）。"""
    path = (BASE / "ab_before.json" if snapshot_source() == "author"
            else ROOT / "tests" / "fixtures" / "ab_before.json")
    return read_json(path, {}) or {}


def p2cfg(cfg: dict) -> dict:
    c = copy.deepcopy(cfg)
    c["local"]["offer"]["active"] = "P2_store_offer"
    return c


class TestOfferConfirmation(unittest.TestCase):
    """每个用例独立临时台账，绝不写真实 offer_confirmations.json。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name) / "confirmations.json"
        self.patcher = patch("core.offer.confirmations_source", return_value=self.tmp)
        self.patcher.start()
        self.cfg = load_config()

    def tearDown(self):
        self.patcher.stop()
        self.td.cleanup()

    def body(self, **kw) -> dict:
        b = {
            "preset_id": "P2_store_offer", "action": "confirm",
            "confirmed_by": "前浯店店长 张三",
            "evidence": "门店盖章《单次体验活动说明》2026-11-08",
            "valid_until": (date.today() + timedelta(days=30)).isoformat(),
            "allows_specific_claims": True,
        }
        b.update(kw)
        return b

    # ------------------------------------------------------------ 默认状态
    def test_empty_ledger_keeps_p2_blocked(self):
        self.assertEqual(confirmation_log(self.cfg).get("records", []), [])
        st = confirmation_for("P2_store_offer", self.cfg)
        self.assertEqual(st["status"], "none")
        self.assertEqual(st["missing"], list(UNLOCK_FIELDS))
        a = audit(p2cfg(self.cfg))
        self.assertFalse(a["specific_allowed"])
        for word in ("9.9", "不限时长", "不推卡"):
            self.assertNotIn(word, flat(llm.template_script(dict(TOPIC), p2cfg(self.cfg))))

    def test_public_state_exposes_unlock_gaps_and_ledger(self):
        st = public_state(self.cfg)
        self.assertEqual(st["unlock_requirements"], list(UNLOCK_FIELDS))
        self.assertEqual(st["confirmations"], [])
        p2 = next(o for o in st["offers"] if o["id"] == "P2_store_offer")
        self.assertEqual(p2["unlock"]["status"], "none")
        self.assertEqual(len(p2["unlock"]["missing"]), 4)

    # ------------------------------------------------------------ 正路径
    def test_confirm_unlocks_all_four_slots_and_is_traceable(self):
        cfg = p2cfg(self.cfg)
        out = apply_confirmation(cfg, self.body())
        self.assertTrue(out["ok"])
        self.assertEqual(out["state"]["status"], "confirmed")
        a = audit(cfg)
        self.assertTrue(a["specific_allowed"])
        self.assertEqual(a["blocked_slots"], [])
        self.assertEqual(a["confirmation"]["status"], "confirmed")
        s = llm.template_script(dict(TOPIC), cfg)
        for word in ("9.9", "不限时长", "不推卡"):
            self.assertIn(word, flat(s), f"已确认承诺未进产物：{word}")
        self.assertEqual(s["offer_audit"]["specific_allowed"], True)
        self.assertEqual(s["offer_audit"]["confirmation"]["status"], "confirmed")
        self.assertEqual(offer_meta(cfg)["offer_confirmation_status"], "confirmed")
        topics = build_topics({"fitness_related": [], "bridge_candidates": []}, cfg, count=2)
        self.assertTrue(all(t.get("offer_confirmation_status") == "confirmed" for t in topics))

    def test_confirm_does_not_touch_config_and_appends_only(self):
        cfg = p2cfg(self.cfg)
        before = json.dumps(cfg, ensure_ascii=False, sort_keys=True)
        apply_confirmation(cfg, self.body())
        self.assertEqual(json.dumps(cfg, ensure_ascii=False, sort_keys=True), before,
                         "确认登记不得改写 config")
        apply_confirmation(cfg, self.body(confirmed_by="另一位店长"))
        recs = confirmation_log(cfg)["records"]
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["confirmed_by"], "前浯店店长 张三")
        self.assertEqual(confirmation_for("P2_store_offer", cfg)["record"]["confirmed_by"],
                         "另一位店长")

    # ------------------------------------------------------------ 四项缺一不可
    def test_each_missing_field_is_rejected(self):
        cfg = p2cfg(self.cfg)
        cases = [
            ("无确认人", {"confirmed_by": ""}),
            ("无书面凭证", {"evidence": ""}),
            ("无截止日", {"valid_until": ""}),
            ("截止日格式错", {"valid_until": "下个月"}),
            ("截止日已过期", {"valid_until": (date.today() - timedelta(days=1)).isoformat()}),
            ("未明确允许具体承诺", {"allows_specific_claims": False}),
            ("生效日晚于截止日", {"valid_from": "2099-01-01"}),
            ("未知方案", {"preset_id": "NOPE"}),
            ("未知动作", {"action": "magic"}),
        ]
        for label, kw in cases:
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    apply_confirmation(cfg, self.body(**kw))
        self.assertEqual(confirmation_log(cfg).get("records", []), [],
                         "被拒绝的登记不得写进台账")
        self.assertFalse(audit(cfg)["specific_allowed"])

    def test_expired_record_fails_without_manual_revoke(self):
        cfg = p2cfg(self.cfg)
        apply_confirmation(cfg, self.body())
        log = read_json(self.tmp, {})
        log["records"][-1]["valid"]["until"] = (date.today() - timedelta(days=1)).isoformat()
        write_json(self.tmp, log)
        st = confirmation_for("P2_store_offer", cfg)
        self.assertEqual(st["status"], "expired")
        self.assertFalse(audit(cfg)["specific_allowed"])
        self.assertNotIn("9.9", flat(llm.template_script(dict(TOPIC), cfg)))

    # ------------------------------------------------------------ 撤销与隔离
    def test_revoke_falls_back_immediately_and_keeps_history(self):
        cfg = p2cfg(self.cfg)
        apply_confirmation(cfg, self.body())
        self.assertIn("9.9", flat(llm.template_script(dict(TOPIC), cfg)))
        apply_confirmation(cfg, {"preset_id": "P2_store_offer", "action": "revoke",
                                 "reason": "活动已结束，门店通知下架"})
        a = audit(cfg)
        self.assertEqual(a["confirmation"]["status"], "revoked")
        self.assertFalse(a["specific_allowed"])
        self.assertIn("closing", a["blocked_slots"])
        self.assertNotIn("9.9", flat(llm.template_script(dict(TOPIC), cfg)))
        recs = confirmation_log(cfg)["records"]
        self.assertEqual(len(recs), 2, "撤销必须只追加，不能删历史")
        self.assertEqual(recs[-1]["action"], "revoke")

    def test_ledger_is_scoped_to_one_preset(self):
        cfg = p2cfg(self.cfg)
        apply_confirmation(cfg, self.body())
        for pid in ("P0_current", "P1_trial_first"):
            self.assertEqual(confirmation_for(pid, cfg)["status"], "none")
            c = copy.deepcopy(cfg)
            c["local"]["offer"]["active"] = pid
            self.assertFalse(audit(c)["specific_allowed"],
                             "其它方案不得被 P2 的确认连带放行")
            self.assertNotIn("9.9", flat(llm.template_script(dict(TOPIC), c)))

    def test_disabling_local_makes_ledger_inert(self):
        """严格回退：关掉 offer 后台账再有效也不得影响通用产物。"""
        cfg = p2cfg(self.cfg)
        apply_confirmation(cfg, self.body())
        off = copy.deepcopy(cfg)
        off["local"]["offer"]["enabled"] = False
        a = audit(off)
        self.assertFalse(a["enabled"])
        self.assertFalse(a["specific_allowed"])
        self.assertEqual(offer_meta(off), {})
        self.assertEqual(confirmation_log(off), {})
        s = llm.template_script(dict(TOPIC), off)
        self.assertNotIn("9.9", flat(s))
        # 只关 offer 时 local 其余部分（playbook / 锚点 / 到店动作）仍生效，
        # 所以对照的是「同样关掉 offer、但从未登记确认」的产物，而不是冻结快照。
        plain = copy.deepcopy(self.cfg)
        plain["local"]["offer"]["enabled"] = False
        self.assertEqual(flat(s), flat(llm.template_script(dict(TOPIC), plain)),
                         "关掉 offer 后台账不得留下任何文字痕迹")
        # 删掉整个 local 才是严格回退，逐字回到 ab_before。
        strict = copy.deepcopy(cfg)
        del strict["local"]
        before = snapshot()
        self.assertEqual(flat(llm.template_script(dict(TOPIC), strict)), flat(before),
                         "删掉 local 后须逐字回到冻结快照")

    # ------------------------------------------------------------ 服务端路由
    def test_http_route_register_reject_and_state(self):
        from web import server as srv
        tmp_cfg = Path(self.td.name) / "config.json"
        src_cfg = ROOT / "config.json"
        if not src_cfg.exists():
            # 新 clone 还没有 config.json 时，用随仓的示例配置。
            src_cfg = ROOT / "config.example.json"
        tmp_cfg.write_bytes(src_cfg.read_bytes())
        # 模拟用户已在界面把当前方案切到 P2；确认接口本身不改 config。
        c = json.loads(tmp_cfg.read_text(encoding="utf-8"))
        c["local"]["offer"]["active"] = "P2_store_offer"
        tmp_cfg.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
        snapshot = tmp_cfg.read_bytes()

        def call(path, payload):
            h = object.__new__(srv.Handler)
            h.path = path
            raw = json.dumps(payload, ensure_ascii=False).encode()
            h.headers = {"Content-Length": str(len(raw))}
            h.rfile, h.wfile = io.BytesIO(raw), io.BytesIO()
            h._user, h._role = "admin", "admin"
            codes = []
            with patch.object(srv.Handler, "send_response",
                              lambda self, code, *a, **k: codes.append(code)), \
                 patch.object(srv.Handler, "send_header", lambda *a, **k: None), \
                 patch.object(srv.Handler, "end_headers", lambda *a, **k: None):
                h.do_POST()
            return codes[0] if codes else 0, json.loads(h.wfile.getvalue().decode())

        with patch.object(util, "CONFIG_PATH", tmp_cfg):
            code, bad = call("/api/offers/confirm", self.body(confirmed_by=""))
            self.assertEqual(code, 400)
            self.assertFalse(bad["ok"])
            code, ok = call("/api/offers/confirm", self.body())
            self.assertEqual(code, 200)
            self.assertTrue(ok["ok"])
            self.assertEqual(ok["state"]["status"], "confirmed")
            self.assertTrue(ok["audit"]["specific_allowed"])
            self.assertEqual(len(ok["confirmations"]), 1)
            self.assertEqual(tmp_cfg.read_bytes(), snapshot,
                             "确认接口不得改写 config")


class TestFrozenFallbackBoundary(unittest.TestCase):
    """通用回退路径里那句硬编码是 ab_before 冻结快照的一部分，不能改，
    但必须钉死它不会漏进 local 开启时的真实产物。"""

    def setUp(self):
        self.cfg = load_config()

    def test_frozen_snapshot_really_contains_the_line(self):
        before = snapshot()
        self.assertIn(FROZEN_LINE, flat(before),
                      "冻结快照若不再含该句，说明回退契约被悄悄改过，须重新核对")

    def test_snapshot_source_matches_file_existence(self):
        """GPT 第二次复核第 3 节：快照来源必须与文件存在性一致 ——
        本机无作者真实快照时来源是 example，测试失败先看来源再定性，
        不把示例快照当成作者冻结快照。"""
        expected = "author" if (BASE / "ab_before.json").exists() else "example"
        self.assertEqual(snapshot_source(), expected)
        self.assertTrue(snapshot(), "快照文件缺失")

    def test_frozen_line_never_leaks_into_local_output(self):
        for pid in ("P0_current", "P1_trial_first", "P2_store_offer"):
            cfg = copy.deepcopy(self.cfg)
            cfg["local"]["offer"]["active"] = pid
            with patch("core.offer.confirmations_source",
                       return_value=Path(tempfile.gettempdir()) / "_empty_ledger_never_written.json"):
                s = llm.template_script(dict(TOPIC), cfg)
            self.assertNotIn(FROZEN_LINE, flat(s),
                             f"{pid} 漏出了通用路径的冻结承诺句")

    def test_off_path_still_reproduces_snapshot_verbatim(self):
        off = copy.deepcopy(self.cfg)
        del off["local"]
        s = llm.template_script(dict(TOPIC), off)
        before = snapshot()
        self.assertEqual(flat(s), flat(before))
        self.assertIn(FROZEN_LINE, flat(s))


if __name__ == "__main__":
    unittest.main()
