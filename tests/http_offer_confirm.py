"""真实 HTTP 回归：门店书面确认的登记 / 拒绝 / 撤销闭环。

前置：服务以非回环地址启动（本环境 TCP 回环被拦，见 http_loopback_probe.json）：
  python run.py --serve --host <本机网卡IP> --port 8899 --allow-insecure
用法：python tests/http_offer_confirm.py [base_url]

纪律：会真实写 config.json 与 offer_confirmations.json，
      因此先备份字节、finally 无条件恢复并断言字节一致，绝不把测试确认留在台账里。
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

from http_transport import req

ROOT = Path(__file__).resolve().parents[1]
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://192.168.10.249:8899"
CFG = ROOT / "config.json"
LEDGER = ROOT / "data" / "baseline" / "offer_confirmations.json"
OUT = ROOT / "data" / "baseline" / "offer_confirm_http_readings.json"

PASSED, FAILED = 0, 0
READINGS = {"base": BASE, "steps": []}


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}" + (f" | {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  FAIL  {name} | {detail}")
    READINGS["steps"].append({"name": name, "pass": bool(cond), "detail": str(detail)})


def body(**kw):
    b = {
        "preset_id": "P2_store_offer", "action": "confirm",
        "confirmed_by": "前浯店店长 张三",
        "evidence": "门店盖章《单次体验活动说明》（HTTP 回归测试数据，用完即恢复）",
        "valid_until": (date.today() + timedelta(days=30)).isoformat(),
        "allows_specific_claims": True,
    }
    b.update(kw)
    return b


def main() -> int:
    cfg_bytes, ledger_bytes = CFG.read_bytes(), LEDGER.read_bytes()
    try:
        st, d = req(BASE, "/api/offers")
        check("GET /api/offers 200", st == 200, f"status={st}")
        check("目录暴露放行四项与台账",
              d.get("unlock_requirements") == ["confirmed_by", "evidence",
                                               "allows_specific_claims", "valid.until"]
              and isinstance(d.get("confirmations"), list),
              f"requirements={d.get('unlock_requirements')}")
        p2 = next((o for o in d.get("offers", []) if o["id"] == "P2_store_offer"), {})
        check("P2 初始缺口四项",
              (p2.get("unlock") or {}).get("status") == "none"
              and len((p2.get("unlock") or {}).get("missing") or []) == 4,
              f"unlock={p2.get('unlock')}")

        st, r = req(BASE, "/api/offers/set",
                    {"active": "P2_store_offer", "overrides": {}})
        check("切到 P2（未确认仍被拦）",
              st == 200 and r.get("ok") is True
              and r["audit"]["specific_allowed"] is False
              and "closing" in r["audit"]["blocked_slots"],
              f"blocked={r.get('audit', {}).get('blocked_slots')}")

        st, bad = req(BASE, "/api/offers/confirm", body(confirmed_by=""))
        check("缺确认人被 400 拒绝", st == 400 and bad.get("ok") is False,
              f"status={st} error={bad.get('error', '')[:40]}")
        st, bad2 = req(BASE, "/api/offers/confirm", body(valid_until="2020-01-01"))
        check("过期截止日被 400 拒绝", st == 400 and bad2.get("ok") is False,
              f"status={st} error={bad2.get('error', '')[:40]}")
        st, g = req(BASE, "/api/offers")
        check("被拒登记未写进台账", len(g.get("confirmations", [])) == 0,
              f"records={len(g.get('confirmations', []))}")

        st, ok = req(BASE, "/api/offers/confirm", body())
        check("四项齐全登记成功并即时放行",
              st == 200 and ok.get("ok") is True
              and ok["state"]["status"] == "confirmed"
              and ok["audit"]["specific_allowed"] is True
              and ok["audit"]["blocked_slots"] == [],
              f"status={st} blocked={ok.get('audit', {}).get('blocked_slots')}")
        check("台账写出 1 条且目录状态同步",
              len(ok.get("confirmations", [])) == 1
              and next(o for o in ok["offers"] if o["id"] == "P2_store_offer")["unlock"]["status"] == "confirmed",
              f"records={len(ok.get('confirmations', []))}")

        st, rv = req(BASE, "/api/offers/confirm",
                     {"preset_id": "P2_store_offer", "action": "revoke",
                      "reason": "HTTP 回归测试撤销，用完即恢复"})
        check("撤销后具体承诺立即回退",
              st == 200 and rv["state"]["status"] == "revoked"
              and rv["audit"]["specific_allowed"] is False
              and "closing" in rv["audit"]["blocked_slots"],
              f"blocked={rv.get('audit', {}).get('blocked_slots')}")
        check("撤销只追加不删历史",
              len(rv.get("confirmations", [])) == 2
              and rv["confirmations"][0]["action"] == "confirm"
              and rv["confirmations"][1]["action"] == "revoke",
              f"actions={[x['action'] for x in rv.get('confirmations', [])]}")
        st, g2 = req(BASE, "/api/offers")
        check("重读仍为撤销态（持久化正确）",
              g2["audit"]["confirmation"]["status"] == "revoked",
              f"status={g2['audit']['confirmation']['status']}")
    finally:
        CFG.write_bytes(cfg_bytes)
        LEDGER.write_bytes(ledger_bytes)
        restored = (CFG.read_bytes() == cfg_bytes and LEDGER.read_bytes() == ledger_bytes)
        check("真实 config / 台账已按字节恢复", restored,
              f"restored={restored}")
        READINGS["passed"], READINGS["failed"] = PASSED, FAILED
        OUT.write_text(json.dumps(READINGS, ensure_ascii=False, indent=2), encoding="utf-8")

    print("-" * 66)
    print(f"PASS={PASSED}  FAIL={FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
