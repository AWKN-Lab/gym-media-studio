"""最小可行实验框架（零依赖）：稳定分流 + 最小样本量 + 事件记录。

判定纪律（防止把「没跑够」说成「没效果」）：
- 样本不足一律写 "insufficient_sample"，不写「无效」；
- 未做显著性检验一律写 "not_significant"，不写「A 优于 B」；
- 本模块不产出因果结论，因果结论需要平台回执（第 3 层）与时间检验（第 5 层）。
"""
from __future__ import annotations

import hashlib
import math

from .metrics import track, read_events

# 双侧 alpha=0.05 / power=0.80 的常用正态分位近似，避免引入 scipy
Z_ALPHA = 1.96
Z_POWER = 0.84


def assign(experiment: str, unit: str, arms=("A", "B"),
           salt: str = "lynvos-v1") -> str:
    """同一 unit 在同一实验里永远落到同一组（sha256 稳定分流）。"""
    arms = list(arms)
    h = hashlib.sha256(f"{salt}|{experiment}|{unit}".encode("utf-8")).hexdigest()
    return arms[int(h[:8], 16) % len(arms)]


def min_sample_size(baseline_rate: float, mde: float = 0.20) -> int:
    """每组所需最小样本量（两比例检验，正态近似）。

    baseline_rate: 对照组基线转化率（0~1）
    mde: 期望检测的相对提升，如 0.20 表示想检出 +20%
    """
    p = min(max(float(baseline_rate), 0.0001), 0.9999)
    delta = p * float(mde)
    if delta <= 0:
        raise ValueError("mde 必须为正")
    n = 2 * (Z_ALPHA + Z_POWER) ** 2 * p * (1 - p) / (delta ** 2)
    return int(math.ceil(n))


def record_exposure(experiment: str, unit: str, variant: str, **kw) -> dict:
    return track("experiment_exposure", experiment=experiment,
                 unit=str(unit), variant=variant, **kw)


def record_outcome(experiment: str, unit: str, variant: str,
                   metric: str, value: float, **kw) -> dict:
    return track("experiment_outcome", experiment=experiment,
                 unit=str(unit), variant=variant, metric=metric,
                 value=value, **kw)


def analyze(experiment: str, baseline_rate: float = 0.05,
            mde: float = 0.20, metric: str = "lead") -> dict:
    ev = read_events()
    exposures = [e for e in ev if e.get("event") == "experiment_exposure"
                 and e.get("experiment") == experiment]
    outcomes = [e for e in ev if e.get("event") == "experiment_outcome"
                and e.get("experiment") == experiment
                and e.get("metric") == metric]

    groups: dict[str, dict] = {}
    for e in exposures:
        v = str(e.get("variant", ""))
        g = groups.setdefault(v, {"exposed": 0, "converted": 0})
        g["exposed"] += 1
    for e in outcomes:
        v = str(e.get("variant", ""))
        if v in groups and float(e.get("value", 0) or 0) > 0:
            groups[v]["converted"] += 1
    for g in groups.values():
        g["rate"] = round(g["converted"] / g["exposed"], 4) if g["exposed"] else None

    need = min_sample_size(baseline_rate, mde)
    enough = all(g["exposed"] >= need for g in groups.values()) and len(groups) >= 2
    if not enough:
        status = "insufficient_sample"
    else:
        status = "not_significant"  # 未实现统计检验前，绝不宣称显著

    return {
        "experiment": experiment,
        "metric": metric,
        "groups": groups,
        "min_sample_per_arm": need,
        "status": status,
        "conclusion": None,
        "note": "status 为样本量/检验状态，不是效果结论；不得据此宣称某方案更优",
        "evidence_level": 2,
    }
