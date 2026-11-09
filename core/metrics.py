"""零依赖指标埋点：把 brand.cta 接成「线索 → 转化」的可读取数。

证据层级（如实标注）：本模块产出的是第 2 层「自采客观测量」，
不等于平台后台回执（第 3 层），更不等于「获客有效」（需真实发布 + 对照组）。

人工基线未实测时，summary 明确返回 comparison="unavailable"，
绝不编造节省比例或 ROI —— 那是上一阶段被判定为「零效果指标」的根因。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from .util import DATA, read_json, write_json

METRICS_DIR = DATA / "metrics"
EVENTS: Path = METRICS_DIR / "events.jsonl"
BASELINE: Path = METRICS_DIR / "baseline.json"
_LOCK = threading.Lock()

DEFAULT_BASELINE = {
    "measured": False,
    "human_minutes_per_video": None,
    "human_cost_cny_per_video": None,
    "source": "unmeasured",
    "note": "人工基线须由运营实测填写；占位值不是观测数据，不得用于任何 ROI 结论",
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def track(event: str, **fields) -> dict:
    """追加一条事件。jsonl 追加写 + 全局锁，避免多用户并发写坏。"""
    rec = {"ts": _now(), "t": round(time.time(), 3), "event": str(event)}
    rec.update(fields)
    line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
    with _LOCK:
        EVENTS.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENTS, "a", encoding="utf-8") as f:
            f.write(line)
    return rec


class _Timer:
    def __init__(self, event: str, **fields):
        self.event = event
        self.fields = fields
        self.t0 = 0.0

    def __enter__(self):
        self.t0 = time.perf_counter()
        track(self.event + "_start", **self.fields)
        return self

    def __exit__(self, *exc):
        track(self.event + "_end",
              elapsed_sec=round(time.perf_counter() - self.t0, 3),
              ok=exc[0] is None, **self.fields)
        return False


def timer(event: str, **fields) -> _Timer:
    """用法：with timer("run", run_id=r): run_pipeline(...)"""
    return _Timer(event, **fields)


def read_events(limit: int = 20000) -> list[dict]:
    if not EVENTS.exists():
        return []
    out = []
    with _LOCK:
        with open(EVENTS, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue  # 损坏行不计入有效读数
    return out[-limit:]


def set_baseline(human_minutes_per_video: float,
                 human_cost_cny_per_video: float | None = None,
                 source: str = "operator-measured") -> dict:
    data = {
        "measured": True,
        "human_minutes_per_video": float(human_minutes_per_video),
        "human_cost_cny_per_video": human_cost_cny_per_video,
        "source": source,
        "updated": _now(),
    }
    write_json(BASELINE, data)
    return data


def summary() -> dict:
    ev = read_events()
    by: dict[str, int] = {}
    for e in ev:
        k = str(e.get("event", ""))
        by[k] = by.get(k, 0) + 1
    ends = [e for e in ev if e.get("event") == "run_end"
            and isinstance(e.get("elapsed_sec"), (int, float))]
    leads = [e for e in ev if e.get("event") == "lead"]
    base = read_json(BASELINE, None) or DEFAULT_BASELINE
    measured = bool(base.get("measured"))
    avg = round(sum(e["elapsed_sec"] for e in ends) / len(ends), 2) if ends else None

    comparison = "unavailable"
    saving = None
    if measured and avg is not None and base.get("human_minutes_per_video"):
        human_sec = float(base["human_minutes_per_video"]) * 60
        if human_sec > 0:
            saving = round((1 - avg / human_sec) * 100, 1)
            comparison = "baseline_measured"

    return {
        "events_total": len(ev),
        "by_event": by,
        "runs": len(ends),
        "avg_run_seconds": avg,
        "leads_cta_plan": len(leads),
        "baseline_measured": measured,
        "comparison": comparison,
        "human_time_saving_pct": saving,
        "evidence_level": 2,
        "evidence_note": "自采客观测量；非平台后台回执，非获客有效性证明",
        "limitations": [
            "耗时为本机流水线自测，未含人工审核与发布等待",
            "线索来自评论文本中「计划」关键词命中，非平台私信/到店回执",
            "无对照组，不能据此推断因果关系",
            "人工基线未实测时不输出任何节省比例",
        ],
        "generated_at": _now(),
    }
