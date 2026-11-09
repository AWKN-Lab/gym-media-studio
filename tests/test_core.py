"""可重复运行的门禁单测：零第三方依赖、不联网、不依赖 FFmpeg / 模型 / 真实账号。

运行：python -m unittest discover -s tests -p "test_*.py" -v
判定：只看退出码。测试中用 mock.patch 把落盘路径重定向到临时目录，
      因此不会产生污染数据，也不会改动用户的账号台账。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import compliance, credentials, experiment, metrics, studio  # noqa: E402
from core.platforms import cookies  # noqa: E402

CFG = {"compliance": {
    "local_banned": ["保证", "包瘦", "最有效", "100%"],
    "soft_banned": ["必须"],
    "external_api": False,
}}


class TestComplianceGate(unittest.TestCase):
    """钉住「passed 恒 True」这个已被取证发现的缺陷。"""

    def test_clean_text_is_not_silently_passed(self):
        r = compliance.check_text("今天练腿，注意膝盖不要内扣", CFG, external=False)
        rep = r["report"]
        self.assertFalse(rep["passed"])
        self.assertEqual(rep["status"], "local_only_needs_review")
        self.assertEqual(rep["scope"], "local_regex_only")
        self.assertTrue(rep["review_required"])
        self.assertTrue(any("法务" in x or "授权" in x for x in rep["limitations"]))

    def test_banned_text_is_rewritten_and_flagged(self):
        r = compliance.check_text("保证一周瘦，最有效", CFG, external=False)
        self.assertNotIn("保证", r["text"])
        self.assertGreaterEqual(r["report"]["local_hits"], 1)
        self.assertFalse(r["report"]["passed"])
        self.assertEqual(r["report"]["status"], "rewritten_needs_review")

    def test_check_and_fix_never_unconditionally_passes(self):
        # 证伪点：旧实现无论命中多少都写 passed=True
        script = {"title": "保证瘦", "narration": "最有效"}
        out = compliance.check_and_fix(script, CFG)
        self.assertFalse(out["compliance"]["passed"])
        self.assertNotIn("保证", out["title"])
        self.assertGreater(out["compliance"]["local_hits"], 0)
        self.assertEqual(out["compliance"]["scope"], "local_regex_only")

    def test_external_hit_keeps_gate_closed(self):
        with mock.patch.object(compliance, "_external_check",
                               return_value={"sensitive_words": ["违规词"]}):
            r = compliance.check_text("今天练腿", CFG)
        self.assertEqual(r["report"]["external_hits"], 1)
        self.assertFalse(r["report"]["passed"])

    def test_only_publish_fields_are_scanned(self):
        script = {"title": "保证瘦", "internal_note": "保证瘦"}
        out = compliance.check_and_fix(script, CFG)
        self.assertNotIn("保证", out["title"])
        self.assertIn("保证", out["internal_note"])


class TestCredentials(unittest.TestCase):
    def test_seal_roundtrip(self):
        s = credentials.seal("SESSDATA=ABC; bili_jct=XYZ")
        self.assertTrue(s.startswith(("enc:v1:", "plain:v1:")))
        self.assertEqual(credentials.unseal(s), "SESSDATA=ABC; bili_jct=XYZ")

    def test_ciphertext_never_contains_plaintext(self):
        self.assertNotIn("SUPERSECRET_XYZ", credentials.seal("SUPERSECRET_XYZ"))

    def test_legacy_plaintext_compat_and_migration_flag(self):
        self.assertEqual(credentials.unseal("LEGACY"), "LEGACY")
        self.assertTrue(credentials.needs_migration("LEGACY"))
        self.assertFalse(credentials.needs_migration(credentials.seal("X")))
        self.assertFalse(credentials.needs_migration(""))

    def test_password_hash_and_legacy_compat(self):
        h = credentials.hash_password("p@ss")
        self.assertNotIn("p@ss", h)
        self.assertTrue(credentials.verify_password("p@ss", h))
        self.assertFalse(credentials.verify_password("wrong", h))
        self.assertTrue(credentials.verify_password("p@ss", "p@ss"))


class TestCookieStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(cookies, "DATA", Path(self.tmp.name))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_roundtrip_and_no_plaintext_on_disk(self):
        r = cookies.save_cookie("acc-t", "B站", "SESSDATA=SECRET99; bili_jct=CSRF", "CSRF")
        self.assertTrue(r["ok"])
        f = Path(self.tmp.name) / "publish" / "acc-t" / "cookies.json"
        self.assertNotIn("SECRET99", f.read_text(encoding="utf-8"))
        got = cookies.get_cookie("acc-t", "bilibili")
        self.assertEqual(got["cookie"], "SESSDATA=SECRET99; bili_jct=CSRF")
        self.assertEqual(got["csrf"], "CSRF")

    def test_legacy_plaintext_is_migrated(self):
        f = Path(self.tmp.name) / "publish" / "acc-t" / "cookies.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"bilibili": {"cookie": "LEGACY_PLAIN", "csrf": ""}}),
                     encoding="utf-8")
        self.assertEqual(cookies.get_cookie("acc-t", "B站")["cookie"], "LEGACY_PLAIN")
        self.assertNotIn("LEGACY_PLAIN", f.read_text(encoding="utf-8"))
        self.assertTrue(cookies.masked_state("acc-t")["bilibili"])


class TestReplyGate(unittest.TestCase):
    """面向公众的 AI 回复必须过闸门 —— 此前完全缺失。"""

    def _patch(self):
        return [
            mock.patch.object(studio, "load_config", return_value=CFG),
            mock.patch("core.llm.gateway_cfg", return_value={"enabled": False}),
        ]

    def test_reply_rewritten_and_cta_lead_recorded(self):
        events = []
        patches = self._patch() + [
            mock.patch("core.metrics.track",
                       side_effect=lambda e, **k: events.append((e, k)))]
        for p in patches:
            p.start()
        try:
            r = studio.draft_reply("acc-x", "保证瘦，我要计划")
        finally:
            for p in patches:
                p.stop()
        self.assertTrue(r["ok"])
        self.assertEqual(r["source"], "template")
        self.assertNotIn("保证", r["reply"])
        self.assertIn("compliance", r)
        self.assertFalse(r["compliance"]["passed"])
        self.assertTrue(any(e == "lead" and k.get("signal") == "cta_plan"
                            for e, k in events))

    def test_gate_failure_blocks_reply_instead_of_silent_passthrough(self):
        patches = self._patch() + [
            mock.patch("core.metrics.track", return_value={}),
            mock.patch("core.compliance.check_text", side_effect=RuntimeError("boom"))]
        for p in patches:
            p.start()
        try:
            r = studio.draft_reply("acc-x", "你好")
        finally:
            for p in patches:
                p.stop()
        self.assertFalse(r["ok"])
        self.assertNotIn("reply", r)
        self.assertIn("已拦截", r["error"])


class TestMetrics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p1 = mock.patch.object(metrics, "EVENTS", Path(self.tmp.name) / "events.jsonl")
        self.p2 = mock.patch.object(metrics, "BASELINE", Path(self.tmp.name) / "baseline.json")
        self.p1.start()
        self.p2.start()

    def tearDown(self):
        self.p1.stop()
        self.p2.stop()
        self.tmp.cleanup()

    def test_summary_never_fakes_roi_without_baseline(self):
        metrics.track("lead", signal="cta_plan")
        with metrics.timer("run", run_id="r1"):
            pass
        s = metrics.summary()
        self.assertEqual(s["leads_cta_plan"], 1)
        self.assertEqual(s["runs"], 1)
        self.assertEqual(s["comparison"], "unavailable")
        self.assertIsNone(s["human_time_saving_pct"])
        self.assertEqual(s["evidence_level"], 2)

    def test_baseline_measured_enables_comparison(self):
        with metrics.timer("run", run_id="r1"):
            pass
        metrics.set_baseline(human_minutes_per_video=10.0)
        s = metrics.summary()
        self.assertEqual(s["comparison"], "baseline_measured")
        self.assertIsNotNone(s["human_time_saving_pct"])


class TestExperiment(unittest.TestCase):
    def test_assignment_is_stable(self):
        a = experiment.assign("hook_v1", "video_001")
        self.assertIn(a, ("A", "B"))
        self.assertEqual(a, experiment.assign("hook_v1", "video_001"))

    def test_min_sample_size_positive(self):
        self.assertGreater(experiment.min_sample_size(0.05, 0.2), 0)

    def test_insufficient_sample_has_no_conclusion(self):
        with mock.patch.object(experiment, "read_events", return_value=[]):
            r = experiment.analyze("hook_v1")
        self.assertEqual(r["status"], "insufficient_sample")
        self.assertIsNone(r["conclusion"])


class TestSmoke(unittest.TestCase):
    def test_all_modules_import(self):
        import core.compliance, core.credentials, core.metrics  # noqa: F401
        import core.experiment, core.studio, core.util  # noqa: F401
        import core.platforms, web.server  # noqa: F401


if __name__ == "__main__":
    unittest.main(verbosity=2)
