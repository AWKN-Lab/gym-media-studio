"""HTTP 层端到端回归（需服务已启动；不进 CI）。

纪律：
- 硬断言只针对本机可控路径；真实公网接口失败记为 NETWORK_UNVERIFIED（未取得），
  不算「不存在」，也不计入通过数。
- 只看退出码判绿红；每轮读数逐条打印，便于事后核对。
- 测试写入的账号数据会在结束前清理。

用法：python tests/http_regression.py [base_url]
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787"
ROOT = Path(__file__).resolve().parents[1]
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PASSED, FAILED, UNVERIFIED = 0, 0, 0


def req(path: str, payload: dict | None = None):
    # 本环境 TCP 回环被拦（见 data/baseline/http_loopback_probe.json），
    # 设 GYMPULSE_HTTP_BACKEND=curl 可走本机网卡 IP 取真实 HTTP 读数。
    if os.environ.get("GYMPULSE_HTTP_BACKEND") == "curl":
        from http_transport import req as curl_req
        return curl_req(BASE, path, payload)
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data, method="POST" if data else "GET",
                               headers={"Content-Type": "application/json"} if data else {})
    def _decode(raw: str):
        try:
            return json.loads(raw)
        except Exception:
            return {"_text": raw}  # 首页是 HTML，不该按 JSON 解析

    try:
        with OPENER.open(r, timeout=45) as resp:
            return resp.status, _decode(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        return e.code, _decode(e.read().decode("utf-8", "ignore") or "{}")


def check(name: str, cond: bool, detail: str = ""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}" + (f" | {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  FAIL  {name} | {detail}")


def soft(name: str, fn):
    """公网读数：可达就硬断言，不可达只记未取得。"""
    global UNVERIFIED
    try:
        st, body = fn()
    except Exception as e:
        UNVERIFIED += 1
        print(f"  UNVERIFIED  {name} | 网络未取得：{type(e).__name__}: {str(e)[:90]}")
        return None
    if st != 200:
        UNVERIFIED += 1
        print(f"  UNVERIFIED  {name} | HTTP {st}")
        return None
    return body


def main() -> int:
    print("=" * 66)
    print("HTTP 端到端回归（P0-P2 改动后）")
    print("=" * 66)

    # 1 静态面
    st, _ = req("/")
    check("首页 200", st == 200, f"status={st}")
    st, v = req("/api/voices")
    engines = [e.get("id") for e in v.get("engines", [])]
    check("音色引擎无商业 MiniMax", st == 200 and "minimax" not in engines, str(engines))

    # 2 凭据不回传明文（此前修过，回归确认未被本轮改动破坏）
    st, accs = req("/api/publish/accounts")
    no_plain = isinstance(accs, list) and all("credential" not in a and "cookie" not in a
                                              for a in accs)
    check("账号响应无明文 credential / cookie", st == 200 and no_plain,
          f"accounts={len(accs) if isinstance(accs, list) else '?'}")

    # 3 新增指标端点
    st, s = req("/api/metrics/summary")
    check("指标 summary 可读取", st == 200 and s.get("evidence_level") == 2,
          f"events={s.get('events_total')} comparison={s.get('comparison')}")
    check("无人工基线时不编造 ROI",
          s.get("comparison") == "unavailable" or s.get("baseline_measured") is True,
          f"comparison={s.get('comparison')}")
    st, ev = req("/api/metrics/events?limit=50")
    check("指标 events 可读取", st == 200 and isinstance(ev.get("items"), list),
          f"items={len(ev.get('items', []))}")

    # 4 转写模型清单
    st, t = req("/api/transcripts")
    ready = [m.get("id") for m in t.get("models", []) if m.get("ready")]
    check("base 模型就绪", st == 200 and "base" in ready, f"ready={ready}")

    # 5 合规闸门真实生效（核心缺陷的 HTTP 层验证）
    st, r = req("/api/publish/reply", {"account_id": "__httptest__",
                                       "comment": "保证瘦，我要计划", "tone": ""})
    rep = r.get("compliance", {})
    check("AI 回复过合规闸门", st == 200 and r.get("ok") is True and "保证" not in r.get("reply", ""),
          f"source={r.get('source')} reply={(r.get('reply') or '')[:40]}")
    check("闸门不再恒真 passed", rep.get("passed") is False and rep.get("review_required") is True,
          f"status={rep.get('status')} scope={rep.get('scope')}")
    st, s2 = req("/api/metrics/summary")
    check("CTA「计划」记为线索事件",
          s2.get("leads_cta_plan", 0) >= s.get("leads_cta_plan", 0) + 1,
          f"before={s.get('leads_cta_plan')} after={s2.get('leads_cta_plan')}")

    # 6 Cookie 加密的 HTTP 层闭环
    acc = "__httptest__"
    st, c = req("/api/publish/cookies", {"account_id": acc, "platform": "B站",
                                          "cookie": "SESSDATA=HTTPSECRET77; bili_jct=CSRF",
                                          "csrf": "CSRF"})
    f = ROOT / "data" / "publish" / acc / "cookies.json"
    raw = f.read_text(encoding="utf-8") if f.exists() else ""
    check("Cookie 保存返回保护模式", st == 200 and bool(c.get("protection")),
          f"protection={c.get('protection')}")
    check("Cookie 明文不落盘", "HTTPSECRET77" not in raw, f"file_bytes={len(raw)}")
    st, accs2 = req("/api/publish/accounts")
    check("账号列表仍无 Cookie 明文", all("cookie" not in a for a in accs2), "")

    # 7 真实公网读侧（软断言）
    b = soft("B站热点", lambda: req("/api/platform/" + quote("B站") + "/hot?keyword=" + quote("健身")))
    if b is not None:
        check("B站热点真实出数", b.get("ok") is True and len(b.get("items", [])) > 0,
              f"items={len(b.get('items', []))}")
    d = soft("抖音热榜", lambda: req("/api/platform/" + quote("抖音") + "/hot"))
    if d is not None:
        check("抖音热榜真实出数", d.get("ok") is True and len(d.get("items", [])) > 0,
              f"items={len(d.get('items', []))}")

    # 清理本轮写入
    try:
        f.unlink(missing_ok=True)
        f.parent.rmdir()
    except Exception:
        pass

    print("-" * 66)
    print(f"PASS={PASSED}  FAIL={FAILED}  NETWORK_UNVERIFIED={UNVERIFIED}")
    print("（未取得的公网读数不计入 PASS/FAIL，不作「不可达」结论）")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
