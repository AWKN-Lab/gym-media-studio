#!/usr/bin/env python3
"""桌面安装版门禁的严格证伪。

判据沿用本项目既有纪律（2026-10-12 实证）：**改动计数变化 + 测试转红** 两个都要，
缺一即是无效证伪。CRLF 会静默吃掉 replace，所以这里一律按字节操作并同时尝试
LF / CRLF 两种变体，先确认目标串真的出现且只出现一次。

每组结束按字节恢复；全部恢复后再跑一遍该测试文件，确认环境回到全绿。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

CASES = [
    {
        "name": "白名单改成整包复制（缓存混进安装包）",
        "file": "desktop/sidecar/app_entry.py",
        "old": b'PROGRAM_GLOBS = ("web/*.html", "web/*.css", "web/*.js",',
        "new": b'PROGRAM_GLOBS = ("data/**/*", "web/*.html", "web/*.css", "web/*.js",',
        "test": "tests.test_desktop_paths.BootstrapContract"
                ".test_first_run_seeds_only_whitelisted_files",
    },
    {
        "name": "破坏严格回退（无环境变量也重定向）",
        "file": "core/paths.py",
        "old": b"    if v:\n        return Path(v).expanduser().resolve()\n"
               b"    return Path(__file__).resolve().parent.parent",
        "new": b"    if v:\n        return Path(v).expanduser().resolve()\n"
               b"    return default_user_home(env)",
        "test": "tests.test_desktop_paths.PathContract"
                ".test_no_env_is_strict_fallback_to_source_root",
    },
    {
        "name": "启动失败时不再回收子进程（留后台服务）",
        "file": "desktop/src-tauri/src/main.rs",
        "old": b"                        kill_child(app.handle());\n"
               b"                        return Err(e);",
        "new": b"                        return Err(e);",
        "test": "tests.test_desktop_paths.SourceContract"
                ".test_parent_child_lifecycle_is_double_sided",
    },
    {
        "name": "spec 改成整包 data（864MB 缓存进包）",
        "file": "desktop/sidecar/lynvos.spec",
        "old": b'    (str(PROJECT / "data" / "baseline"), "data/baseline"),',
        "new": b'    (str(PROJECT / "data"), "data"),',
        "test": "tests.test_desktop_paths.SourceContract"
                ".test_spec_datas_are_explicit_and_exclude_cache_tree",
    },
]


def variants(raw: bytes) -> list[bytes]:
    crlf = raw.replace(b"\n", b"\r\n")
    return [raw] if raw == crlf else [raw, crlf]


def run_test(test_id: str) -> int:
    r = subprocess.run([PY, "-m", "unittest", test_id], cwd=str(ROOT),
                       capture_output=True, text=True, timeout=180,
                       encoding="utf-8", errors="replace")
    return r.returncode


def main() -> int:
    targets = {c["file"]: (ROOT / c["file"]).read_bytes() for c in CASES}
    results = []
    try:
        for case in CASES:
            path = ROOT / case["file"]
            data = path.read_bytes()
            chosen = None
            for old in variants(case["old"]):
                if data.count(old) == 1:
                    chosen = old
                    break
            if chosen is None:
                counts = [data.count(o) for o in variants(case["old"])]
                results.append({"case": case["name"], "valid": False,
                                "reason": f"目标串出现次数={counts}，替换未生效"})
                print(f"[INVALID] {case['name']}: 目标串命中 {counts}")
                continue
            # 按选定变体的换行风格重建 new，避免 CRLF 文件里插进半截 LF
            if b"\r\n" in chosen:
                new_bytes = case["new"].replace(b"\n", b"\r\n")
            else:
                new_bytes = case["new"]
            changed = data.replace(chosen, new_bytes, 1)
            assert changed != data, f"{case['name']}: 字节未变化，证伪无效"
            path.write_bytes(changed)
            print(f"[APPLY] {case['name']}: 字节变化 {len(data)} -> {len(changed)}")

            rc_red = run_test(case["test"])
            path.write_bytes(targets[case["file"]])
            assert path.read_bytes() == targets[case["file"]], "恢复失败"
            rc_green = run_test(case["test"])
            ok = rc_red != 0 and rc_green == 0
            results.append({"case": case["name"], "valid": True,
                            "bytes_changed": True, "rc_when_broken": rc_red,
                            "rc_after_restore": rc_green, "pass": ok})
            print(f"[{('PASS' if ok else 'FAIL')}] {case['name']}: "
                  f"破坏后 rc={rc_red} / 恢复后 rc={rc_green}")
    finally:
        for rel, raw in targets.items():
            (ROOT / rel).write_bytes(raw)

    full = subprocess.run([PY, "-m", "unittest", "tests.test_desktop_paths", "-q"],
                          cwd=str(ROOT), capture_output=True, text=True,
                          timeout=300, encoding="utf-8", errors="replace")
    print(f"\n恢复后全组单测 rc={full.returncode} | {(full.stderr or '')[-300:]}")
    valid = [r for r in results if r.get("valid")]
    passed = [r for r in valid if r.get("pass")]
    print(f"有效证伪 {len(passed)}/{len(valid)} 组；"
          f"无效（替换未生效）{len(results) - len(valid)} 组")
    return 0 if (len(valid) == len(CASES) and len(passed) == len(valid)
                 and full.returncode == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
