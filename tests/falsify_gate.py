"""证伪工具：证明单测能抓住「合规 passed 恒 True」这一回归。

手起刀落的做法：临时把 compliance.py 的判定行改成恒 True，跑一次单测，
断言退出码非 0，然后**无条件恢复原文件并做字节级校验**。

判据（缺一即无效证伪）：
  1. 改动计数真的变化（目标串出现次数 == 1，替换前后字节不同）；
  2. 测试确实转红（unittest 退出码 != 0）。
本脚本不进 CI（会临时改源码），手动跑：python tests/falsify_gate.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "core" / "compliance.py"
OLD = b"passed = (not hits) and external_checked and (not ext_hits)"
NEW = b"passed = True  # TEMP FALSIFICATION"


def _run(cmd: list[str]) -> int:
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=180)
    if r.returncode != 0:
        tail = (r.stdout or "") + (r.stderr or "")
        print("  证伪轮测试输出尾部：", tail.strip()[-600:])
    return r.returncode


def main() -> int:
    orig = TARGET.read_bytes()
    count = orig.count(OLD)
    print(f"1) 目标串出现次数 = {count}")
    assert count == 1, "目标串计数异常，证伪无效（避免静默不匹配的假证伪）"
    mutated = orig.replace(OLD, NEW)
    assert mutated != orig, "替换未生效，证伪无效"

    rc = -1
    try:
        TARGET.write_bytes(mutated)
        print("2) 已临时改为恒 True，跑单测…")
        rc = _run([sys.executable, "-m", "unittest", "discover",
                   "-s", "tests", "-p", "test_*.py"])
        print(f"3) 证伪轮退出码 = {rc}（期望非 0）")
    finally:
        TARGET.write_bytes(orig)
        restored = TARGET.read_bytes()
        print(f"4) 源码已恢复且字节一致 = {restored == orig}")
        assert restored == orig, "源码恢复失败，请立即检查 compliance.py"

    assert rc != 0, "证伪失败：把闸门改回恒 True 后测试仍全绿，说明钉子无效"
    print("证伪有效：改动计数变化 ✓ + 测试转红 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
