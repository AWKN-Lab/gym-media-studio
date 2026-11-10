"""本机门禁：一条命令跑完，全绿才算可交付。

用法（必须用工程自带运行时跑，它决定在线配音能不能出声）：
    .venv\\Scripts\\python.exe tools\\gate.py
    .venv\\Scripts\\python.exe tools\\gate.py --with-auth   # 加测抖音发布通道登录态

四道关：
  1. 字节编译：core/web/tools/desktop/tests/run.py 全部 compileall（原云端 ci.yml 那步的
     本地替代，且覆盖面更宽）
  2. 单元测试全绿
  3. 环境自检：11 项不许有 WARN（尤其「免费在线配音」必须实测成功，不是「装了没」）
  4. 真出片：完整跑一条，超时即红
  5. 成片体检：文件存在、>1MB、时长 >10s

`--with-auth` 追加一关：真起一次浏览器、访问创作者中心、拿到账号标识才算过
（判据见 tools/auth_probe.py；看文件在不在不算证明）。

本机门禁是唯一门禁：仓库里不允许出现 .github/workflows/*，云端 runner 一律不启用。
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUTPUT = ROOT / "output"
PY = sys.executable

results: list[tuple[str, bool, str, float]] = []


def _tail(text: str, lines: int = 6) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:])


def run_step(name: str, args: list[str], timeout: int) -> tuple[bool, str]:
    t0 = time.time()
    try:
        proc = subprocess.run([PY, *args], cwd=str(ROOT), capture_output=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        results.append((name, False, f"超时（上限 {timeout}s），已终止", timeout))
        return False, ""
    out = (proc.stdout or b"").decode("utf-8", "ignore")
    err = (proc.stderr or b"").decode("utf-8", "ignore")
    detail = "退出码 0" if proc.returncode == 0 else f"退出码 {proc.returncode}：{_tail(err or out, 3)}"
    results.append((name, proc.returncode == 0, detail, time.time() - t0))
    return proc.returncode == 0, out


def step_compile() -> None:
    """字节编译：把原来只在云端 workflow 里跑的那一步挪到本机。

    覆盖范围比原 ci.yml 更宽（它漏了 tools/desktop/tests），因为本地门禁是唯一门禁，
    不能只测 core 和 web。
    """
    t0 = time.time()
    targets = ["core", "web", "tools", "desktop", "tests", "run.py"]
    try:
        proc = subprocess.run([PY, "-m", "compileall", "-q", *targets],
                              cwd=str(ROOT), capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        results.append(("字节编译", False, "超时（上限 180s），已终止", 180.0))
        return
    out = (proc.stdout or b"").decode("utf-8", "ignore")
    err = (proc.stderr or b"").decode("utf-8", "ignore")
    detail = "全部通过" if proc.returncode == 0 else _tail(err or out, 4)
    results.append(("字节编译", proc.returncode == 0, detail, time.time() - t0))


def step_tests() -> None:
    run_step("单元测试", ["-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"], 180)


def step_probe() -> None:
    ok, out = run_step("环境自检", ["run.py", "--probe"], 300)
    if not ok:
        return
    warns = re.findall(r"\[WARN\]", out)
    if warns:
        bad = _tail("\n".join(l for l in out.splitlines() if "[WARN]" in l), 4)
        results.append(("自检无告警", False, f"有 {len(warns)} 项告警：{bad}", 0.0))
    else:
        results.append(("自检无告警", True, "11 项全 OK", 0.0))
    line = next((l for l in out.splitlines() if "免费在线配音" in l), "")
    if "[OK ]" not in line:
        results.append(("在线配音实测", False, line.strip() or "自检里没有这一项", 0.0))


def step_render() -> None:
    started = time.time()
    ok, out = run_step("真出片（1 条）", ["run.py", "--count", "1"], 900)
    if not ok:
        return
    fresh = [p for p in OUTPUT.glob("*.mp4")
             if p.stat().st_mtime >= started - 2] if OUTPUT.exists() else []
    if not fresh:
        results.append(("成片体检", False, "本次没有写出 mp4（可能同名覆盖也未被识别）", 0.0))
        return
    video = max(fresh, key=lambda p: p.stat().st_mtime)
    from core.util import ffprobe_duration
    size_mb = video.stat().st_size / 1_048_576
    dur = ffprobe_duration(video)
    ok_media = size_mb > 1 and dur > 10
    detail = f"{video.name} | {dur:.2f}s | {size_mb:.2f}MB"
    if not ok_media:
        detail += " ← 文件过小或时长过短"
    results.append(("成片体检", ok_media, detail, 0.0))


def step_auth() -> None:
    """可选关：抖音发布通道登录态。会弹一次 Chrome 窗口，所以默认不跑。"""
    t0 = time.time()
    try:
        proc = subprocess.run([PY, "tools/auth_probe.py", "--quiet"], cwd=str(ROOT),
                              capture_output=True, timeout=240)
    except subprocess.TimeoutExpired:
        results.append(("发布通道登录", False, "超时（上限 240s），已终止", 240.0))
        return
    out = (proc.stdout or b"").decode("utf-8", "ignore")
    err = (proc.stderr or b"").decode("utf-8", "ignore")
    results.append(("发布通道登录", proc.returncode == 0,
                    _tail(out or err, 2) or f"退出码 {proc.returncode}", time.time() - t0))


def main() -> int:
    with_auth = "--with-auth" in sys.argv[1:]
    print(f"门禁解释器：{PY}")
    import platform
    print(f"版本：{platform.python_version()}\n")
    step_compile()
    step_tests()
    step_probe()
    step_render()
    if with_auth:
        step_auth()

    print("\n=== 门禁结果 ===")
    failed = 0
    for name, ok, detail, secs in results:
        flag = "OK  " if ok else "RED "
        print(f"  [{flag}] {name:<12} {detail}" + (f"  （{secs:.1f}s）" if secs else ""))
        if not ok:
            failed += 1
    print()
    if failed:
        print(f"门禁不通过：{failed} 项红。")
        return 1
    print("门禁通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
