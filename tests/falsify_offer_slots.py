"""证伪：证明「承诺口径按结构可换」的门禁不是摆设。

两组独立的破坏，每组都要求：
  1. 改动计数真的变化（目标串出现次数 == 1，替换前后字节不同）；
  2. 测试确实转红（unittest 退出码 != 0）；
  3. 源码无条件恢复且字节一致，恢复后全量复跑为绿。
缺一即判为无效证伪 —— 不做「改了但没生效却显示全绿」的假证伪。

手动跑：python tests/falsify_offer_slots.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CASES = [
    {
        "name": "把四结构位从模板生成链路上摘掉（配置还在，产物不换）",
        "file": ROOT / "core" / "llm.py",
        "old": (b'    of = offer_slots(cfg) if local else {}\n'
                b'    op, hk = of.get("opening") or {}, of.get("hook") or {}'),
        "new": (b'    of = {}\n'
                b'    op, hk = of.get("opening") or {}, of.get("hook") or {}'),
    },
    {
        "name": "让承诺闸门恒不拦截（未确认价格可流进文案）",
        "file": ROOT / "core" / "offer.py",
        "old": b"        blocked = bool(found) and not allowed\n",
        "new": b"        blocked = False\n",
    },
]


def run_tests() -> int:
    r = subprocess.run([sys.executable, "-m", "unittest", "discover",
                        "-s", "tests", "-p", "test_*.py"],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    tail = ((r.stdout or "") + (r.stderr or "")).strip()[-700:]
    return r.returncode, tail


def main() -> int:
    base_rc, base_tail = run_tests()
    print(f"0) 基线全量单测 rc={base_rc}（期望 0）")
    if base_rc != 0:
        print(base_tail)
        return 1

    for i, case in enumerate(CASES, 1):
        path = case["file"]
        orig = path.read_bytes()
        count = orig.count(case["old"])
        print(f"\n{i}) {case['name']}")
        print(f"   目标串出现次数 = {count}（期望 1）")
        if count != 1:
            print("   证伪无效：目标串计数异常，停止")
            return 1
        mutated = orig.replace(case["old"], case["new"])
        if mutated == orig:
            print("   证伪无效：替换未生效，停止")
            return 1
        rc, tail = -1, ""
        try:
            path.write_bytes(mutated)
            rc, tail = run_tests()
            print(f"   证伪轮 rc={rc}（期望非 0）")
            if rc == 0:
                print(tail)
        finally:
            path.write_bytes(orig)
            ok = path.read_bytes() == orig
            print(f"   源码已恢复且字节一致 = {ok}")
            if not ok:
                return 1
        if rc == 0:
            print("   证伪失败：破坏后测试仍全绿，钉子无效")
            return 1
        print("   ✓ 改动生效 + 测试转红")

    final_rc, final_tail = run_tests()
    print(f"\n3) 全部恢复后复跑 rc={final_rc}（期望 0）")
    if final_rc != 0:
        print(final_tail)
        return 1
    print("证伪有效：两组破坏均被门禁抓住，恢复后全绿")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
