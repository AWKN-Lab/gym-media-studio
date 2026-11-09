"""门店承诺口径：按「开头 / 钩子 / 内容 / 结尾」四结构位组织，整套可换、逐位可覆盖。

背景（2026-11-08 用户指令：「当前仍在执行的官方承诺需要能换，可以按结构来，
开头，钩子，结尾，内容」）：
改之前，正在执行的口径散落在三处，换一句要改代码，也说不清「当前执行的是哪一版」：
  1. config.local.claim_policy —— 一条写死的禁令；
  2. core/llm.py 硬编码 —— 「先试一次」「怕被推卡？」「可以先给你做一次体态评估」；
  3. config.local 的 cta_store / pain_local / audience_anchors —— 分散在配置里。

现在单一数据源是 data/baseline/offer_presets.json：
  每个 preset 含四结构位，config.local.offer.active 指向当前执行的那一版，
  config.local.offer.overrides 可按结构位单独覆盖。

两条硬约束（与之前几轮的证据纪律一致）：
  A. 严格回退：删掉 config.local 或 local.offer.enabled=false 时，本模块所有函数返回空，
     生成链路逐字回到 ab_before.json，不污染通用路径。
  B. 承诺闸门：未获门店书面确认的具体承诺（价格 / 活动 / 时长 / 无推销）不进文案，
     命中即降级到 P0_current 的同名结构位，并在 offer_audit 里留痕，不静默放过。
"""
from __future__ import annotations

import re
from datetime import date, datetime

from .util import ROOT, read_json, write_json

SLOTS = ("opening", "hook", "body", "closing")
LABELS = {"opening": "开头", "hook": "钩子", "body": "内容", "closing": "结尾"}
BASELINE_ID = "P0_current"
DEFAULT_SOURCE = "data/baseline/offer_presets.json"
CONFIRM_SOURCE = "data/baseline/offer_confirmations.json"
UNLOCK_FIELDS = ("confirmed_by", "evidence", "allows_specific_claims", "valid.until")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------- 加载与当前方案
def offer_cfg(cfg: dict) -> dict:
    """local 未启用 / offer 未启用时一律返回空 —— A/B 严格回退的开关。"""
    local = cfg.get("local") or {}
    if not local.get("enabled"):
        return {}
    oc = local.get("offer") or {}
    return oc if oc.get("enabled") else {}


def catalog(cfg: dict) -> dict:
    oc = offer_cfg(cfg)
    if not oc:
        return {}
    rel = str(oc.get("presets_source") or DEFAULT_SOURCE)
    return read_json(ROOT / rel, {}) or {}


def presets(cfg: dict) -> list[dict]:
    return [p for p in (catalog(cfg).get("presets") or []) if p.get("id")]


def baseline(cfg: dict) -> dict:
    ps = presets(cfg)
    for p in ps:
        if p.get("id") == BASELINE_ID:
            return p
    return ps[0] if ps else {}


def active_offer(cfg: dict) -> dict:
    oc = offer_cfg(cfg)
    if not oc:
        return {}
    wanted = str(oc.get("active") or BASELINE_ID)
    for p in presets(cfg):
        if p.get("id") == wanted:
            return p
    return baseline(cfg)


# ---------------------------------------------------------------- 闸门
def banned_claims(cfg: dict) -> list[str]:
    return [str(x) for x in (catalog(cfg).get("claim_gate") or {}).get("banned_claims") or []]


def hits(text: str, cfg: dict) -> list[str]:
    s = str(text or "")
    return [w for w in banned_claims(cfg) if w and w in s]


def specific_allowed(preset: dict, today: str = "") -> bool:
    """只有「门店已书面确认 + 明确允许具体承诺 + 有效期未过」才放行具体承诺。

    刻意要求 valid.until 必填：开业期活动若没有截止日，会长期挂在文案里变成虚假承诺。
    """
    today = today or date.today().isoformat()
    if not preset.get("confirmed") or not preset.get("allows_specific_claims"):
        return False
    if not str(preset.get("confirmed_by") or "").strip():
        return False
    until = str((preset.get("valid") or {}).get("until") or "").strip()
    return bool(until) and until >= today


# ---------------------------------------------------------------- 门店书面确认台账
def confirmations_source(cfg: dict):
    oc = offer_cfg(cfg)
    rel = str((oc or {}).get("confirmations_source") or CONFIRM_SOURCE)
    return ROOT / rel


def confirmation_log(cfg: dict) -> dict:
    """台账是运行时证据，与 presets 模板分开：方案定义可版本化，确认记录只追加。"""
    if not offer_cfg(cfg):
        return {}
    return read_json(confirmations_source(cfg), {}) or {}


def _record_is_valid(rec: dict, today: str = "") -> bool:
    today = today or date.today().isoformat()
    if rec.get("action") != "confirm":
        return False
    if not str(rec.get("confirmed_by") or "").strip():
        return False
    if not str(rec.get("evidence") or "").strip():
        return False
    if rec.get("allows_specific_claims") is not True:
        return False
    until = str((rec.get("valid") or {}).get("until") or "").strip()
    return bool(until) and until >= today


def confirmation_for(preset_id: str, cfg: dict, today: str = "") -> dict:
    """某方案当前生效的确认状态 = 台账里针对它的最后一条记录。"""
    recs = [r for r in (confirmation_log(cfg).get("records") or [])
            if r.get("preset_id") == preset_id]
    if not recs:
        return {"status": "none", "record": None, "missing": list(UNLOCK_FIELDS)}
    last = recs[-1]
    if last.get("action") == "revoke":
        return {"status": "revoked", "record": last, "missing": list(UNLOCK_FIELDS)}
    if not _record_is_valid(last, today):
        until = str((last.get("valid") or {}).get("until") or "")
        today = today or date.today().isoformat()
        status = "expired" if until and until < today else "incomplete"
        return {"status": status, "record": last, "missing": list(UNLOCK_FIELDS)}
    return {"status": "confirmed", "record": last, "missing": []}


def effective_preset(cfg: dict, preset: dict, today: str = "") -> dict:
    """把台账合并进方案定义，得到真正生效的确认字段。

    模板 presets 描述「这一版写了什么」，台账描述「门店是否书面批准」，
    两者分开才不会出现「改一行 JSON 就放出未确认价格」的情况。
    """
    st = confirmation_for(str(preset.get("id") or ""), cfg, today)
    p = dict(preset)
    p["confirmation"] = st
    if st["status"] == "confirmed":
        r = st["record"]
        p.update(
            confirmed=True,
            confirmed_by=str(r.get("confirmed_by") or ""),
            allows_specific_claims=True,
            valid=dict(r.get("valid") or {}),
            source=f"门店书面确认：{r.get('evidence', '')}"
                   f"（确认人 {r.get('confirmed_by', '')}，有效期至 {(r.get('valid') or {}).get('until', '')}）",
        )
    elif p.get("allows_specific_claims"):
        # 台账未放行时，不允许模板自身把具体承诺打开。
        p["confirmed"] = False
    return p


def apply_confirmation(cfg: dict, body: dict) -> dict:
    """POST /api/offers/confirm：登记或撤销门店书面确认（append-only，不改 config）。"""
    oc = offer_cfg(cfg)
    if not oc:
        raise ValueError("承诺方案未启用（local.offer.enabled=false）")
    pid = str(body.get("preset_id") or "").strip()
    ids = [p.get("id", "") for p in presets(cfg)]
    if pid not in ids:
        raise ValueError(f"方案不存在：{pid or '(空)'}；可选 {', '.join(ids)}")
    action = str(body.get("action") or "").strip()
    if action not in ("confirm", "revoke"):
        raise ValueError("action 只接受 confirm / revoke")
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    rec: dict = {"preset_id": pid, "action": action, "recorded_at": now}
    if action == "confirm":
        who = str(body.get("confirmed_by") or "").strip()
        ev = str(body.get("evidence") or "").strip()
        until = str(body.get("valid_until") or "").strip()
        vfrom = str(body.get("valid_from") or date.today().isoformat()).strip()
        if not who:
            raise ValueError("确认人不能为空（书面确认须有门店责任人）")
        if not ev:
            raise ValueError("书面凭证说明不能为空（如：门店盖章活动说明 / 公示照片编号）")
        if not _ISO_DATE.match(until):
            raise ValueError("有效期截止日须为 YYYY-MM-DD")
        if until < date.today().isoformat():
            raise ValueError(f"截止日 {until} 已过期，不予放行")
        if _ISO_DATE.match(vfrom) and vfrom > until:
            raise ValueError("生效日不能晚于截止日")
        if body.get("allows_specific_claims") is not True:
            raise ValueError("须显式确认 allows_specific_claims=true 才放行具体承诺")
        rec.update(confirmed_by=who, evidence=ev, allows_specific_claims=True,
                   valid={"from": vfrom, "until": until},
                   note=str(body.get("note") or "").strip()[:200])
    else:
        rec["reason"] = str(body.get("reason") or "").strip()[:200] or "未填写撤销原因"
    path = confirmations_source(cfg)
    log = read_json(path, {}) or {}
    log.setdefault("schema", 1)
    log.setdefault("records", [])
    log["records"].append(rec)          # 只追加；撤销是另一条记录，不删历史
    log["updated_at"] = now
    log["note"] = "门店书面确认台账：仅追加，撤销另写一条记录，不删历史"
    write_json(path, log)
    return {"ok": True, "preset_id": pid, "action": action,
            "record": rec, "state": confirmation_for(pid, cfg)}


# ---------------------------------------------------------------- 四结构位
def _raw(name: str, preset: dict, local: dict) -> dict:
    """取方案里某个结构位的原始值；缺失字段回退到 config 里的现行值（兼容旧配置）。"""
    d = dict((preset.get("slots") or {}).get(name) or {})
    d.setdefault("text", "")
    d.setdefault("on_screen", "")
    d.setdefault("anchors", [])
    d.setdefault("note", "")
    if name == "opening":
        if not d["anchors"]:
            d["anchors"] = list(local.get("audience_anchors") or [])
        if not d["text"]:
            d["text"] = "{anchor}"
    if name == "body":
        if not d["text"]:
            d["text"] = local.get("pain_local") or ""
        if not d["on_screen"]:
            d["on_screen"] = "怕被推卡？"
    if name == "closing":
        if not d["text"]:
            d["text"] = local.get("cta_store") or ""
        if not d["on_screen"]:
            d["on_screen"] = "先试一次"
    return d


def slots(cfg: dict) -> dict:
    """返回四个结构位最终生效值；关掉 offer 时返回 {}。

    每个结构位附带 origin（preset / override / fallback）与 blocked 标记，
    便于把「当前到底在执行哪一句」显示到界面、写进产物审计。
    """
    oc = offer_cfg(cfg)
    if not oc:
        return {}
    local = cfg.get("local") or {}
    preset = effective_preset(cfg, active_offer(cfg))
    base = baseline(cfg)
    allowed = specific_allowed(preset)
    overrides = oc.get("overrides") or {}
    out: dict[str, dict] = {}
    for name in SLOTS:
        raw = _raw(name, preset, local)
        origin = "preset"
        ov = overrides.get(name)
        if isinstance(ov, str) and ov.strip():
            raw = dict(raw, text=ov.strip())
            origin = "override"
        found = hits(raw.get("text"), cfg) + hits(raw.get("on_screen"), cfg)
        blocked = bool(found) and not allowed
        if blocked:
            raw = _raw(name, base, local)
            origin = "fallback:" + BASELINE_ID
        out[name] = {
            "text": str(raw.get("text") or ""),
            "on_screen": str(raw.get("on_screen") or ""),
            "anchors": list(raw.get("anchors") or []),
            "note": str(raw.get("note") or ""),
            "origin": origin,
            "blocked": blocked,
            "blocked_claims": sorted(set(found)),
        }
    return out


def slot(cfg: dict, name: str) -> dict:
    return slots(cfg).get(name, {})


def anchors(cfg: dict) -> list[str]:
    """当前方案的开头锚点池；为空时调用方回退到 config.local.audience_anchors。"""
    return list(slot(cfg, "opening").get("anchors") or [])


# ---------------------------------------------------------------- 审计 / 注入
def audit(cfg: dict) -> dict:
    oc = offer_cfg(cfg)
    if not oc:
        return {"enabled": False, "active": "", "slots": {}, "blocked_slots": [],
                "specific_allowed": False}
    p, s = effective_preset(cfg, active_offer(cfg)), slots(cfg)
    return {
        "enabled": True,
        "confirmation": p.get("confirmation") or {"status": "none", "record": None, "missing": []},
        "active": p.get("id", ""),
        "name": p.get("name", ""),
        "summary": p.get("summary", ""),
        "confirmed": bool(p.get("confirmed")),
        "confirmed_by": str(p.get("confirmed_by") or ""),
        "allows_specific_claims": bool(p.get("allows_specific_claims")),
        "source": str(p.get("source") or ""),
        "valid": dict(p.get("valid") or {}),
        "specific_allowed": specific_allowed(p),
        "slots": {n: {"text": v["text"], "on_screen": v["on_screen"],
                      "origin": v["origin"], "blocked": v["blocked"],
                      "blocked_claims": v["blocked_claims"]} for n, v in s.items()},
        "blocked_slots": [n for n, v in s.items() if v["blocked"]],
    }


def offer_block(cfg: dict) -> str:
    """给 LLM 的 user 消息：按结构注入当前执行的口径，而不是只给一条抽象禁令。"""
    a = audit(cfg)
    if not a["enabled"]:
        return ""
    lines = [
        f"当前执行的承诺方案：{a['active']}｜{a['name']}",
        f"确认状态：{'已确认' if a['confirmed'] else '未确认'}；具体承诺放行：{'是' if a['specific_allowed'] else '否'}；"
        f"口径来源：{a['source']}",
        "按结构使用以下口径（四结构位都不得自行添加价格、活动有效期、时长、无推销等具体承诺）：",
    ]
    for n in SLOTS:
        v = a["slots"][n]
        text = v["text"] or "沿用选题默认，不叠加口径"
        if n == "opening":
            text = text.replace("{anchor}", "按给定的人群锚点")
        line = f"- {LABELS[n]}（{n}）：{text}"
        if v["on_screen"]:
            line += f"；画面主文案：{v['on_screen']}"
        lines.append(line)
    if a["blocked_slots"]:
        lines.append("以下结构位含未确认具体承诺，已被闸门拦下并回退到保守口径："
                     + "、".join(LABELS[n] for n in a["blocked_slots"]))
    if a["specific_allowed"]:
        lines.append("本方案已登记门店书面确认且在有效期内，具体承诺可写，但不得超出确认范围；切换方案只改文字，不证明转化。")
    else:
        lines.append("证据边界：未取得门店书面确认的具体承诺不得写入；切换方案只改文字，不证明转化。")
    return "\n".join(lines)


def offer_meta(cfg: dict) -> dict:
    """写进选题元数据，让产物可追溯「这一版用的是哪个承诺方案」。"""
    a = audit(cfg)
    if not a["enabled"]:
        return {}
    return {
        "offer_id": a["active"], "offer_name": a["name"],
        "offer_confirmed": a["confirmed"],
        "offer_confirmation_status": (a.get("confirmation") or {}).get("status", ""),
        "offer_specific_allowed": a["specific_allowed"],
        "offer_blocked_slots": list(a["blocked_slots"]),
        "offer_slots": {n: v["text"] for n, v in a["slots"].items()},
    }


def public_state(cfg: dict) -> dict:
    """GET /api/offers：方案目录 + 当前执行 + 四结构位预览 + 闸门状态。"""
    c = catalog(cfg)
    a = audit(cfg)
    s = slots(cfg)

    def row(p: dict) -> dict:
        e = effective_preset(cfg, p)   # 界面显示「生效状态」，不是模板自述状态
        return {
            "id": p.get("id", ""), "name": p.get("name", ""),
            "summary": p.get("summary", ""), "confirmed": bool(e.get("confirmed")),
            "confirmed_by": e.get("confirmed_by", ""),
            "allows_specific_claims": bool(e.get("allows_specific_claims")),
            "source": e.get("source", ""), "valid": dict(e.get("valid") or {}),
            "unlock": e.get("confirmation"),
            "slots": {n: (p.get("slots") or {}).get(n, {}).get("text", "")
                      for n in SLOTS},
        }

    return {
        "enabled": bool(offer_cfg(cfg)),
        "active": a.get("active", ""),
        "slot_order": list(SLOTS),
        "slot_labels": dict(LABELS),
        "slot_products": dict(c.get("slot_products") or {}),
        "unlock_requirements": list(UNLOCK_FIELDS),
        "unlock_note": "四项齐全才放行含具体承诺的方案；截止日过期自动失效；撤销只追加一条 revoke 记录，不删历史。",
        "confirmations": list(confirmation_log(cfg).get("records") or []),
        "offers": [row(p) for p in presets(cfg)],
        "overrides": dict((offer_cfg(cfg) or {}).get("overrides") or {}),
        "slots": {n: {"label": LABELS[n], **v} for n, v in s.items()},
        "audit": a,
        "claim_gate": c.get("claim_gate") or {},
        "evidence_boundary": list(c.get("evidence_boundary") or []),
    }


def apply_selection(cfg: dict, body: dict) -> dict:
    """POST /api/offers/set：只改 active / overrides，不碰 local 其它字段。

    前端「平台设置」保存的是 brand/niche/voice 的浅 patch，这里单独走一个接口，
    避免整段覆盖把 playbook、anchors 等设置冲掉。
    """
    oc = offer_cfg(cfg)
    if not oc:
        raise ValueError("承诺方案未启用（local.offer.enabled=false）")
    aid = str(body.get("active") or "").strip()
    ids = [p.get("id", "") for p in presets(cfg)]
    if aid not in ids:
        raise ValueError(f"方案不存在：{aid or '(空)'}；可选 {', '.join(ids)}")
    if "overrides" in body:
        ov = body.get("overrides")
        if not isinstance(ov, dict):
            raise ValueError("overrides 必须是对象")
        clean = {}
        for k, v in ov.items():
            if k not in SLOTS:
                raise ValueError(f"未知结构位：{k}；只接受 {', '.join(SLOTS)}")
            val = str(v or "").strip()
            if len(val) > 160:
                raise ValueError(f"{LABELS[k]}最多 160 字")
            if val:
                clean[k] = val
        oc["overrides"] = clean
    oc["active"] = aid
    return cfg
