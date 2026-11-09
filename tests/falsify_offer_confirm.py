"""证伪：证明「门店书面确认台账」的门禁不是摆设。

三组独立破坏，每组都要求：
  1. 目标串出现次数 == 1，且替换后字节确实变化（防 CRLF / 静默不匹配造成的假绿）；
  2. 全量单测确实转红（退出码 != 0）；
  3. 源码无条件字节恢复，全部恢复后复跑为绿。

手动跑：python tests/falsify_offer_confirm.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CASES = [
    {
        "name": "台账为空时谎称已确认（未确认价格可流进文案）",
        "file": ROOT / "core" / "offer.py",
        "old": b'        return {"status": "none", "record": None, "missing": list(UNLOCK_FIELDS)}\n',
        "new": b'        return {"status": "confirmed", "record": {"valid": {"until": "2099-12-31"}}, "missing": []}\n',
    },
    {
        "name": "让方案定义忽略台账（登记了确认也不放行）",
        "file": ROOT / "core" / "offer.py",
        "old": b'    st = confirmation_for(str(preset.get("id") or ""), cfg, today)\n',
        "new": b'    st = {"status": "none", "record": None, "missing": list(UNLOCK_FIELDS)}\n',
    },
    {
        "name": "撤销改成覆盖历史（台账不再只追加）",
        "file": ROOT / "core" / "offer.py",
        "old": b'    log["records"].append(rec)',
        "new": b'    log["records"] = [rec]       ',
    },
]


def run_tests() -> tuple[int, str]:
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
    print(f"\n{len(CASES) + 1}) 全部恢复后复跑 rc={final_rc}（期望 0）")
    if final_rc != 0:
        print(final_tail)
        return 1
    print("证伪有效：三组破坏均被门禁抓住，恢复后全绿")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
