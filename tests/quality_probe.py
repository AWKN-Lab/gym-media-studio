"""手动质量探针：词错率 / 字错率客观读数。

**不进 CI**（依赖 faster-whisper 模型与本机转写产物）。CI 只跑 test_*.py。
用途：为「逐字稿准不准」提供第 2 层客观测量，替代「肉眼看着还行」。

用法：
    python tests/quality_probe.py --tid <transcript_id> --ref "期望的原文"
输出 WER / CER 读数并追加到 data/metrics/quality.jsonl（观测事实，不是结论）。
中文无声调空格，CER 比 WER 更有解释力；两者都报，不挑选好看的那个。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.util import DATA  # noqa: E402


def _lev(a: list, b: list) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def error_rate(reference: str, hypothesis: str, char_level: bool = False) -> float:
    r = list(reference.strip()) if char_level else reference.split()
    h = list(hypothesis.strip()) if char_level else hypothesis.split()
    if not r:
        return 1.0 if h else 0.0
    return round(_lev(r, h) / len(r), 4)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tid", help="data/transcripts/<tid>/transcript.json")
    ap.add_argument("--ref", help="带标注的期望原文")
    args = ap.parse_args()
    if not args.tid or not args.ref:
        print("未取得读数：需要 --tid 与 --ref。本探针不进 CI，不产出自证式结论。")
        return 0

    src = DATA / "transcripts" / args.tid / "transcript.json"
    if not src.exists():
        print(f"未取得读数：{src} 不存在（不等于该转写从未产生）")
        return 0
    hyp = (json.loads(src.read_text(encoding="utf-8")) or {}).get("text", "")
    reading = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tid": args.tid,
        "ref_chars": len(args.ref.strip()),
        "hyp_chars": len(hyp.strip()),
        "wer": error_rate(args.ref, hyp, char_level=False),
        "cer": error_rate(args.ref, hyp, char_level=True),
        "evidence_level": 2,
        "note": "自采客观测量；标注集仅此一条，不足以代表总体准确率",
    }
    out = DATA / "metrics" / "quality.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a", encoding="utf-8") as f:
        f.write(json.dumps(reading, ensure_ascii=False) + "\n")
    print(json.dumps(reading, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
