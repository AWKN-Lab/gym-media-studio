"""桌面安装版门禁：路径隔离、白名单引导、进程清理、产物清单。

这一组钉子针对的是「源码好、装完坏」：
  - Program Files 无写权限 → 必须由 LYNVOS_HOME 整体重定向
  - PyInstaller 的 _MEIPASS 布局 → 静态骨架必须能找到
  - 关掉窗口留后台服务 → 父子双保险必须同时存在
  - 864MB 抖音缓存混进安装包 → spec 与 bootstrap 都必须是白名单
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import paths, util  # noqa: E402


def _load_app_entry():
    spec = importlib.util.spec_from_file_location(
        "lynvos_app_entry", ROOT / "desktop" / "sidecar" / "app_entry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


APP = _load_app_entry()


def _write(p: Path, data: str | bytes) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        p.write_bytes(data)
    else:
        p.write_text(data, encoding="utf-8")
    return p


class PathContract(unittest.TestCase):
    def test_no_env_is_strict_fallback_to_source_root(self):
        """不设 LYNVOS_HOME 时逐字保持开发期行为 —— 所有既有测试的前提。"""
        self.assertEqual(paths.runtime_root({"LYNVOS_HOME": ""}), ROOT)
        self.assertEqual(paths.runtime_root({}), ROOT)
        self.assertFalse(paths.user_home_enabled({"LYNVOS_HOME": ""}))

    def test_env_redirects_root_and_every_writable_constant(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "LynvosFit"
            self.assertEqual(paths.runtime_root({"LYNVOS_HOME": str(home)}),
                             home.resolve())
            self.assertTrue(paths.user_home_enabled({"LYNVOS_HOME": str(home)}))
            # core.util 的六个常量全部由 runtime_root 派生，不允许某个漏网留在源码目录
            with patch.object(paths, "runtime_root", return_value=home.resolve()), \
                 patch.object(paths, "bundle_root", return_value=ROOT):
                code = (ROOT / "core" / "util.py").read_text(encoding="utf-8")
                for name in ("CONFIG_PATH", "DATA", "OUTPUT", "ASSETS", "WEB"):
                    self.assertIn(f"{name} = ROOT /", code, f"{name} 必须跟随 ROOT")

    def test_util_root_matches_runtime_root_at_import_time(self):
        self.assertEqual(util.ROOT, paths.runtime_root())
        self.assertEqual(util.CONFIG_PATH, util.ROOT / "config.json")
        self.assertEqual(util.DATA, util.ROOT / "data")
        self.assertEqual(util.WEB, util.ROOT / "web")

    def test_default_home_matches_rust_hardcoded_name(self):
        """Rust 壳与 Python 内核必须用同一个目录名，否则数据会裂成两份。"""
        with patch.dict(os.environ, {"LOCALAPPDATA": "C:/Users/Test/AppData/Local"},
                        clear=False):
            home = paths.default_user_home()
        if sys.platform == "win32":
            self.assertEqual(home.name, "LynvosFit")
        rust = (ROOT / "desktop" / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
        self.assertIn('base.join("LynvosFit")', rust)

    def test_bundle_root_falls_back_when_datas_sit_beside_exe(self):
        """PyInstaller 6 onedir 的 _MEIPASS 指向 _internal；兼容 datas 在 exe 同级的布局。"""
        with tempfile.TemporaryDirectory() as td:
            internal = Path(td) / "_internal"
            _write(Path(td) / "web" / "index.html", "<html>ok</html>")
            with patch.object(sys, "_MEIPASS", str(internal), create=True):
                self.assertEqual(paths.bundle_root(), Path(td))
                self.assertTrue(paths.is_packaged())
            with patch.object(sys, "_MEIPASS", "", create=True):
                self.assertEqual(paths.bundle_root(), ROOT)
                self.assertFalse(paths.is_packaged())

    def test_playbook_uses_shared_root_not_its_own(self):
        """playbook 若再自定义 ROOT，用户改过的方法库/方案库就会读不到。"""
        src = (ROOT / "core" / "playbook.py").read_text(encoding="utf-8")
        self.assertIn("from .util import ROOT, read_json", src)
        self.assertNotIn("parents[1]", src)


class BootstrapContract(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.bundle = Path(self.td.name) / "bundle"
        self.home = Path(self.td.name) / "home"
        _write(self.bundle / "web" / "index.html", "<html>v1</html>")
        _write(self.bundle / "config.json", '{"brand":{"name":"orig"}}')
        # GPT 第二次复核第 5 节第 8 项：播种的是随包示例方法库，不是本机
        # data/baseline；bundle 里留一份 data/baseline 文件当陷阱，
        # 断言它绝不被播种。
        _write(self.bundle / "examples" / "baseline" / "method_playbook.example.json",
               '{"version":1}')
        _write(self.bundle / "examples" / "baseline" / "offer_presets.example.json",
               '{"presets":[]}')
        _write(self.bundle / "examples" / "baseline" / "common_materials.example.json",
               '{"faq":[]}')
        _write(self.bundle / "data" / "baseline" / "offer_presets.json", '{"presets":[]}')
        _write(self.bundle / "assets" / "fonts" / "a.ttf", b"font")
        _write(self.bundle / "assets" / "bgm" / "b.wav", b"wave")
        _write(self.bundle / "assets" / "gym" / "demo.mp4", b"video")
        _write(self.bundle / "assets" / "logo.png", b"png")
        # 故意放项目里真实存在的大缓存，验证它们绝不会被复制
        _write(self.bundle / "data" / "browser-profile-v2" / "Cookies", b"secret")
        _write(self.bundle / "data" / "_cdp_probe" / "x.json", b"x")
        _write(self.bundle / "data" / "runs" / "old.json", b"x")

    def tearDown(self):
        self.td.cleanup()

    def test_first_run_seeds_only_whitelisted_files(self):
        rep = APP.bootstrap(self.bundle, self.home)
        self.assertEqual(rep["missing"], [])
        for rel in ("config.json", "web/index.html",
                    "examples/baseline/method_playbook.example.json",
                    "examples/baseline/offer_presets.example.json",
                    "examples/baseline/common_materials.example.json",
                    "assets/fonts/a.ttf",
                    "assets/bgm/b.wav", "assets/gym/demo.mp4", "assets/logo.png"):
            self.assertTrue((self.home / rel).is_file(), f"缺少 {rel}")
        # GPT 第二次复核第 5 节第 8 项：data/baseline 缺失时不报错，
        # bundle 里即便有真实文件也不播种 —— 首次启动只播种示例文件。
        self.assertFalse((self.home / "data" / "baseline" / "offer_presets.json").exists(),
                         "data/baseline 真实文件被播种进用户目录")
        for rel in ("output", "data/transcripts", "data/runs", "assets/gym"):
            self.assertTrue((self.home / rel).is_dir(), f"缺少目录 {rel}")
        # 缓存目录：一个文件都不能进来
        self.assertFalse((self.home / "data" / "browser-profile-v2").exists())
        self.assertFalse((self.home / "data" / "_cdp_probe").exists())
        self.assertFalse(list((self.home / "data" / "runs").iterdir()))
        touched = rep["synced"] + rep["seeded"] + rep["kept"]
        for prefix in ("data/browser", "data/_cdp", "data/_pipe",
                       "data/runs/", "data/tmp_", "voice_previews"):
            self.assertFalse(any(prefix in rel for rel in touched), prefix)

    def test_upgrade_syncs_program_files_but_never_overwrites_user_data(self):
        APP.bootstrap(self.bundle, self.home)
        _write(self.home / "config.json", '{"brand":{"name":"USER_EDITED"}}')
        _write(self.home / "data" / "baseline" / "offer_presets.json", '{"user":1}')
        _write(self.bundle / "web" / "index.html", "<html>v2</html>")
        rep2 = APP.bootstrap(self.bundle, self.home)
        self.assertIn("USER_EDITED", (self.home / "config.json").read_text(encoding="utf-8"))
        self.assertEqual((self.home / "data" / "baseline" / "offer_presets.json")
                         .read_text(encoding="utf-8"), '{"user":1}')
        self.assertIn("v2", (self.home / "web" / "index.html").read_text(encoding="utf-8"))
        self.assertIn("config.json", rep2["kept"])
        self.assertIn("web/index.html", rep2["synced"])

    def test_never_bundle_marker_covers_real_cache_names(self):
        for name in ("data/browser", "data/_cdp_probe", "data/_pipe_probe",
                     "data/tmp_", "data/voice_previews"):
            self.assertTrue(any(name.startswith(x.rstrip("/")) or x.startswith(name)
                                for x in APP.NEVER_BUNDLE), name)

    def test_program_globs_cover_bgm_used_by_compose(self):
        """compose.py 用 ROOT/assets/bgm；桌面态 ROOT 是用户目录，必须随包同步。"""
        compose = (ROOT / "core" / "compose.py").read_text(encoding="utf-8")
        self.assertIn('"bgm"', compose)
        self.assertIn("assets/bgm/*", APP.PROGRAM_GLOBS)


class SourceContract(unittest.TestCase):
    def test_parent_child_lifecycle_is_double_sided(self):
        py = (ROOT / "desktop" / "sidecar" / "app_entry.py").read_text(encoding="utf-8")
        for marker in ("def watch_parent", "os._exit(0)", "unlink(missing_ok=True)",
                       "LYNVOS_PARENT_PID", "HOME = _setup_home()"):
            self.assertIn(marker, py, marker)
        rust = (ROOT / "desktop" / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
        for marker in ("LYNVOS_PARENT_PID", "RunEvent::Exit", "kill_child(app.handle())",
                       "fn kill_child", "child.kill()"):
            self.assertIn(marker, rust, marker)

    def test_import_has_no_side_effect(self):
        """门禁在 import 时不许创建用户目录或改环境变量。"""
        before = dict(os.environ)
        self.assertIsNone(APP.HOME)
        self.assertEqual(dict(os.environ), before)

    def test_tauri_config_is_per_user_and_bundles_only_whitelisted_resources(self):
        conf = json.loads((ROOT / "desktop" / "src-tauri" / "tauri.conf.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(conf["app"]["windows"], [])
        self.assertEqual(conf["bundle"]["targets"], ["nsis"])
        self.assertEqual(conf["bundle"]["windows"]["nsis"]["installMode"], "currentUser")
        self.assertEqual(conf["bundle"]["resources"], {"../dist/sidecar/": "resources/lynvos/"})
        self.assertEqual(conf["build"]["frontendDist"], "../ui")
        self.assertIsNone(conf["app"]["security"]["csp"])

    def test_spec_datas_are_explicit_and_exclude_cache_tree(self):
        spec = (ROOT / "desktop" / "sidecar" / "lynvos.spec").read_text(encoding="utf-8")
        # GPT 复核 2026-11-10 第 3 项：data/baseline 整个目录永不打包，
        # 方法库只打包 examples/baseline 下的白名单示例文件。
        self.assertNotIn('"data" / "baseline"', spec,
                         "整包 data/baseline 会把本机真实素材打进安装包")
        self.assertIn('"examples" / "baseline"', spec)
        self.assertIn("common_materials.example.json", spec)
        self.assertIn('"web" / "index.html"', spec)
        self.assertNotIn('str(PROJECT / "data"), "data"', spec,
                         "整包 data/ 会把 864MB 浏览器缓存打进安装包")
        self.assertIn("faster_whisper", spec, "精简包必须显式排除重依赖")
        self.assertIn('name="sidecar"', spec, "COLLECT 目录须与 tauri resources 对齐")

    def test_build_script_has_forbidden_bundle_gate(self):
        build = (ROOT / "desktop" / "build.py").read_text(encoding="utf-8")
        for marker in ("forbidden_in_bundle", "browser-profile", "_cdp_probe",
                       "BUILD_REPORT.json"):
            self.assertIn(marker, build, marker)


if __name__ == "__main__":
    unittest.main()
