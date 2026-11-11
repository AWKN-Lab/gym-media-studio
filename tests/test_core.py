"""可重复运行的门禁单测：零第三方依赖、不联网、不依赖 FFmpeg / 模型 / 真实账号。

运行：python -m unittest discover -s tests -p "test_*.py" -v
判定：只看退出码。测试中用 mock.patch 把落盘路径重定向到临时目录，
      因此不会产生污染数据，也不会改动用户的账号台账。
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import compliance, credentials, experiment, metrics, studio  # noqa: E402
from core import secrets as secret_store  # noqa: E402
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

    def test_production_flag_parsing(self):
        """GPT 第二次复核第 5 节第 1 项：LYNVOS_PRODUCTION 的取值语义。"""
        for value, expected in (("1", True), ("true", True), ("yes", True),
                                ("TRUE", True), ("0", False), ("", False),
                                ("no", False)):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"LYNVOS_PRODUCTION": value}):
                    self.assertEqual(credentials.is_production(), expected)
        with mock.patch.dict(os.environ, {}, clear=True):
            # 环境变量缺失 = 开发默认，不生产。
            if "LYNVOS_PRODUCTION" in os.environ:
                del os.environ["LYNVOS_PRODUCTION"]
            self.assertFalse(credentials.is_production())

    def test_seal_refuses_plain_fallback_when_production(self):
        """GPT 第二次复核第 5 节第 1 项：生产标记 + 非 Windows 时 seal 必须
        报错，拒绝 plain:v1: 降级；开发默认仍允许降级（本机测试要能跑）。"""
        with mock.patch.dict(os.environ, {"LYNVOS_PRODUCTION": "1"}):
            if os.name == "nt":
                self.skipTest("本测试只覆盖非 Windows 生产降级分支")
            with self.assertRaises(RuntimeError):
                credentials.seal("sk-must-not-be-plaintext-in-prod")
        with mock.patch.dict(os.environ, {"LYNVOS_PRODUCTION": "0"}):
            self.assertEqual(credentials.unseal(credentials.seal("dev-ok")),
                             "dev-ok")


class TestSecretsPersistence(unittest.TestCase):
    """H3：密钥落盘必须走 seal()，不准写明文；读回要能解回原值。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "secrets.json"
        self.p = mock.patch.object(secret_store, "SECRETS_PATH", self.path)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_save_secrets_never_writes_plaintext_and_roundtrips(self):
        secret_store.save_secrets(
            {"keys": {"DEEPSEEK_DIRECT_API_KEY": "sk-test-SECRET123"}})
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("sk-test-SECRET123", raw, "密钥明文落盘")
        stored = json.loads(raw)["keys"]["DEEPSEEK_DIRECT_API_KEY"]
        # GPT 复核 2026-11-10 第 5 项：plain:v1: 是 base64 降级标记，不是加密。
        # 本机（Linux）只能断言“不是明文 + 明确标记为非加密”；真加密须在 Windows 上验证。
        if os.name == "nt":
            self.assertTrue(stored.startswith("enc:v1:"))
            self.assertTrue(credentials.is_encrypted(stored))
        else:
            self.assertTrue(stored.startswith("plain:v1:"))
            self.assertFalse(credentials.is_encrypted(stored),
                             "plain:v1: 不是加密，不得当成加密通过")
        self.assertEqual(secret_store.get_secret("DEEPSEEK_DIRECT_API_KEY"),
                         "sk-test-SECRET123")
        if os.name != "nt":
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_legacy_plaintext_is_migrated_on_read(self):
        self.path.write_text(json.dumps(
            {"keys": {"DEEPSEEK_DIRECT_API_KEY": "sk-legacy-SECRET456"},
             "models": {}, "allow_env": False}, ensure_ascii=False),
            encoding="utf-8")
        self.assertEqual(secret_store.get_secret("DEEPSEEK_DIRECT_API_KEY"),
                         "sk-legacy-SECRET456")
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("sk-legacy-SECRET456", raw, "历史明文读后仍未密封")
        stored = json.loads(raw)["keys"]["DEEPSEEK_DIRECT_API_KEY"]
        if os.name == "nt":
            self.assertTrue(credentials.is_encrypted(stored))
        else:
            self.assertTrue(stored.startswith("plain:v1:"))
            self.assertFalse(credentials.is_encrypted(stored))

    def test_plain_v1_is_not_encryption(self):
        """钉死 plain:v1: 的语义：base64 降级标记，可逆，不是加密。"""
        sealed = credentials.seal("sk-semantic-CHECK")
        if os.name == "nt":
            self.assertTrue(credentials.is_encrypted(sealed))
        else:
            self.assertTrue(sealed.startswith("plain:v1:"))
            self.assertFalse(credentials.is_encrypted(sealed))
            self.assertEqual(credentials.protection_mode(), "plaintext-0600")
            # 须在 Windows 上验证 DPAPI 真加密；本机（Linux）无法验证，不得冒充。
        self.assertEqual(credentials.unseal(sealed), "sk-semantic-CHECK")

    def test_seal_fails_closed_on_windows_without_dpapi(self):
        """生产路径强制加密：Windows 上 DPAPI 不可用时 seal 必须报错，
        拒绝静默降级成明文（本机用 mock 模拟 Windows + DPAPI 缺失）。"""
        with mock.patch.object(os, "name", "nt"), \
             mock.patch.object(credentials, "_protect", return_value=None):
            with self.assertRaises(RuntimeError):
                credentials.seal("sk-must-not-be-plaintext")

    def test_migration_write_hardens_file(self):
        """GPT 第二次复核第 5 节第 2 项：历史明文密钥迁移写盘后，文件权限
        同样收紧到 0600，不只 save_secrets() 一条路收紧。"""
        if os.name == "nt":
            self.skipTest("文件权限收紧只在非 Windows 上断言")
        self.path.write_text(json.dumps(
            {"keys": {"DEEPSEEK_DIRECT_API_KEY": "sk-mig-SECRET789"},
             "models": {}, "allow_env": False}, ensure_ascii=False),
            encoding="utf-8")
        os.chmod(self.path, 0o644)
        self.assertEqual(secret_store.get_secret("DEEPSEEK_DIRECT_API_KEY"),
                         "sk-mig-SECRET789")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)


class TestSaveConfigAtomic(unittest.TestCase):
    """GPT 第二次复核第 5 节第 3 项的回滚跟进：保存中途失败不留半截
    config.json；并发保存不损坏文件。"""

    def setUp(self):
        from core import util
        self.util = util
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self.tmp.name) / "config.json"
        self.cfg_path.write_text(json.dumps({"brand": {"name": "原配置"}},
                                            ensure_ascii=False),
                                 encoding="utf-8")
        self.p = mock.patch.object(util, "CONFIG_PATH", self.cfg_path)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_replace_failure_keeps_original(self):
        """os.replace 失败时原 config.json 原样不动，临时文件删掉。"""
        with mock.patch.object(os, "replace",
                               side_effect=OSError("磁盘写满")):
            with self.assertRaises(OSError):
                self.util.save_config({"brand": {"name": "新配置"}})
        self.assertEqual(json.loads(self.cfg_path.read_text(encoding="utf-8")),
                         {"brand": {"name": "原配置"}})
        self.assertFalse(
            Path(str(self.cfg_path) + ".tmp").exists(), "半截临时文件未清理")

    def test_concurrent_saves_leave_valid_json(self):
        """ThreadingHTTPServer 下多个 API 请求并发保存：最终文件必须是
        某一版完整配置，不能是两版拼起来的损坏 JSON。"""
        import threading
        payloads = [{"brand": {"name": f"版本{i}"}} for i in range(4)]
        threads = [threading.Thread(target=self.util.save_config, args=(p,))
                   for p in payloads]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        final = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertIn(final, payloads)

    def test_corrupt_config_falls_back_to_example(self):
        """GPT 第二次复核第 3 节：config.json 损坏时回退到示例配置，不硬崩。"""
        example = Path(self.tmp.name) / "config.example.json"
        example.write_text(json.dumps({"brand": {"name": "示例配置"}},
                                      ensure_ascii=False), encoding="utf-8")
        self.cfg_path.write_text("{不是合法 JSON", encoding="utf-8")
        with mock.patch.object(self.util, "EXAMPLE_CONFIG_PATH", example):
            cfg = self.util.load_config()
        self.assertEqual(cfg["brand"]["name"], "示例配置")

    def test_both_configs_corrupt_falls_back_to_minimal_default(self):
        """config.json 与示例都损坏时，用内置最小默认（品牌名是中性占位）。"""
        example = Path(self.tmp.name) / "config.example.json"
        example.write_text("{坏", encoding="utf-8")
        self.cfg_path.write_text("{坏", encoding="utf-8")
        with mock.patch.object(self.util, "EXAMPLE_CONFIG_PATH", example):
            cfg = self.util.load_config()
        self.assertEqual(cfg["brand"]["name"], "示例健身房")
        # 最小默认的 local 是空字典：无本地模式，不是 enabled=false。
        self.assertEqual(cfg["local"], {})


class TestGatewayKeyStorage(unittest.TestCase):
    """GPT 复核 2026-11-10 第 4 项：/api/gateway/set 的 key 只进密封存储，
    config.json 只留非秘密字段；读回走 get_secret()。"""

    def setUp(self):
        from core import util  # noqa: F401  (patch 目标)
        from web import server as srv
        self.srv = srv
        self.util = util
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self.tmp.name) / "config.json"
        src = Path(__file__).resolve().parents[1] / "config.example.json"
        self.cfg_path.write_bytes(src.read_bytes())
        self.sec_path = Path(self.tmp.name) / "secrets.json"
        self.patches = [
            mock.patch.object(util, "CONFIG_PATH", self.cfg_path),
            mock.patch.object(secret_store, "SECRETS_PATH", self.sec_path),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _call(self, path, payload):
        h = object.__new__(self.srv.Handler)
        h.path = path
        raw = json.dumps(payload, ensure_ascii=False).encode()
        h.headers = {"Content-Length": str(len(raw))}
        h.rfile, h.wfile = io.BytesIO(raw), io.BytesIO()
        h._user, h._role = "admin", "admin"
        codes = []
        with mock.patch.object(self.srv.Handler, "send_response",
                               lambda self, code, *a, **k: codes.append(code)), \
             mock.patch.object(self.srv.Handler, "send_header",
                               lambda *a, **k: None), \
             mock.patch.object(self.srv.Handler, "end_headers",
                               lambda *a, **k: None):
            h.do_POST()
        return codes[0] if codes else 0, json.loads(h.wfile.getvalue().decode())

    def test_set_stores_key_in_secrets_not_config(self):
        import core.llm as llm_mod
        code, body = self._call("/api/gateway/set", {
            "api_key": "gw-test-key-123", "base_url": "http://127.0.0.1:3425/v1",
            "model": "test-model"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        raw_cfg = self.cfg_path.read_text(encoding="utf-8")
        self.assertNotIn("gw-test-key-123", raw_cfg, "网关 key 明文进了 config.json")
        self.assertNotIn('"api_key"', raw_cfg, "config.json 不得再有 api_key 字段")
        self.assertEqual(secret_store.get_secret("GATEWAY_MAGPIE_API_KEY"),
                         "gw-test-key-123")
        cfg = self.util.load_config()
        self.assertEqual(llm_mod.gateway_cfg(cfg)["api_key"], "gw-test-key-123")
        self.assertEqual(llm_mod.gateway_cfg(cfg)["base_url"],
                         "http://127.0.0.1:3425/v1")

    def test_legacy_plaintext_key_is_migrated_on_set(self):
        import core.llm as llm_mod
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        cfg.setdefault("llm", {}).setdefault("gateway", {})["api_key"] = "legacy-key-456"
        self.cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        code, body = self._call("/api/gateway/set", {})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        raw_cfg = self.cfg_path.read_text(encoding="utf-8")
        self.assertNotIn("legacy-key-456", raw_cfg, "旧明文 key 仍留在 config.json")
        self.assertNotIn('"api_key"', raw_cfg)
        self.assertEqual(secret_store.get_secret("GATEWAY_MAGPIE_API_KEY"),
                         "legacy-key-456")
        self.assertEqual(
            llm_mod.gateway_cfg(self.util.load_config())["api_key"], "legacy-key-456")

    def test_clear_removes_key(self):
        import core.llm as llm_mod
        secret_store.save_secrets({"keys": {"GATEWAY_MAGPIE_API_KEY": "gw-to-clear"}})
        self.assertEqual(secret_store.get_secret("GATEWAY_MAGPIE_API_KEY"), "gw-to-clear")
        code, body = self._call("/api/gateway/set", {"clear_api_key": True})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(secret_store.get_secret("GATEWAY_MAGPIE_API_KEY"), "")
        self.assertEqual(
            llm_mod.gateway_cfg(self.util.load_config())["api_key"], "")
        self.assertNotIn('"api_key"', self.cfg_path.read_text(encoding="utf-8"))


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
