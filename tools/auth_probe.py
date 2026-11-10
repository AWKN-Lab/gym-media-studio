"""登录态确定性探测：证明抖音发布通道「真的还登着」，而不是看文件在不在。

判据（全部满足才算过）：
  1. 用 data/browser-profile-v2 启动的 Chrome 能被 CDP 接上
  2. 访问创作者中心后没被踢到登录页（URL 与页面文案都不含登录入口）
  3. 同源接口返回 status_code=0 且带账号标识（昵称 / uid）
  4. 可选：账号 uid 与 --expect-uid 一致

退出码：
  0 登录有效    1 登录失效（需人工重新扫码）    2 环境问题（bridge 起不来等）

用法：
    .venv\\Scripts\\python.exe tools\\auth_probe.py
    .venv\\Scripts\\python.exe tools\\auth_probe.py --expect-uid 94434200326
    .venv\\Scripts\\python.exe tools\\auth_probe.py --no-start   # 复用已在跑的 bridge
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BR = ROOT / "data" / "browser"
CMD = BR / "cmd.json"
RESULTS = BR / "results"
STATUS = BR / "status.json"
BRIDGE = ROOT / "tools" / "douyin_bridge.mjs"
STATE = BR / "auth_probe.json"
CREATOR = "https://creator.douyin.com/creator-micro/home"


def bridge_call(op: str, timeout: float = 60, **kwargs) -> dict:
    cid = f"{op}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    CMD.write_text(json.dumps({"id": cid, "op": op, **kwargs}, ensure_ascii=False),
                   encoding="utf-8")
    out = RESULTS / f"{cid}.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if out.exists():
            data = json.loads(out.read_text(encoding="utf-8"))
            if not data.get("ok"):
                raise RuntimeError(str(data.get("error")))
            return data.get("value") or {}
        time.sleep(0.3)
    raise TimeoutError(f"bridge 未在 {timeout:.0f}s 内应答")


AUTH_JS = r"""(() => {
  const t = (document.body && document.body.innerText) || '';
  const head = t.slice(0, 1500);
  const out = {
    url: location.href,
    title: document.title,
    body_len: t.length,
    login_words: /扫码登录|手机号登录|登录后|请先登录|立即登录|登录创作者/.test(head),
    url_login: /passport|login/i.test(location.href)
  };
  const nick = document.querySelector('[class*="nickname"], .nickname, .user-name, .account-name');
  if (nick) out.dom_nick = (nick.innerText || '').trim().slice(0, 40);
  const c = ['/web/api/media/user/info/', '/api/media/user/info/', '/creator-micro/api/user/info/'];
  out.probed = [];
  return (async () => {
    for (const p of c) {
      try {
        const r = await fetch(p, { credentials: 'include', headers: { accept: 'application/json' } });
        out.probed.push({ p: p, status: r.status });
        if (r.ok) {
          let j = null;
          try { j = await r.json(); } catch (e) { continue; }
          const code = (j && (j.status_code !== undefined ? j.status_code : j.code));
          if (code === 0 && (j.user || j.data)) {
            const u = j.user || j.data;
            out.account = {
              nickname: (u.nickname || u.name || '').slice(0, 40),
              uid: String(u.uid || u.user_id || ''),
              has_sec_uid: !!(u.sec_uid || u.sec_user_id)
            };
            break;
          }
        }
      } catch (e) {
        out.probed.push({ p: p, err: String(e).slice(0, 60) });
      }
    }
    return out;
  })();
})()"""


def _profile_chrome_pids() -> list[int]:
    """只找用本工程配置档的 chrome —— 绝不动用户自己的浏览器。"""
    try:
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
             "Where-Object { $_.CommandLine -like '*browser-profile-v2*' } | "
             "ForEach-Object { $_.ProcessId }"],
            capture_output=True, text=True, timeout=30)
    except Exception:
        return []
    return [int(x) for x in ps.stdout.split() if x.strip().isdigit()]


def _kill_profile_chrome() -> None:
    for pid in _profile_chrome_pids():
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_bridge() -> subprocess.Popen | None:
    """bridge 没在跑就起一个；返回自己起的进程（复用别人的则返回 None）。"""
    alive = STATUS.exists() and (time.time() - STATUS.stat().st_mtime) < 8
    if alive:
        return None
    BR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["node", str(BRIDGE)], cwd=str(ROOT),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    while time.time() - t0 < 45:
        if STATUS.exists() and (time.time() - STATUS.stat().st_mtime) < 8:
            return proc
        time.sleep(0.5)
    try:
        proc.kill()
    except Exception:
        pass
    return "FAILED"  # type: ignore[return-value]


def stop_bridge(proc) -> None:
    try:
        bridge_call("stop", timeout=15)
    except Exception:
        pass  # bridge 收到 stop 会先抛 UNKNOWN_OP 再照常退出，属预期
    time.sleep(2)
    if proc not in (None, "FAILED"):
        try:
            proc.kill()
        except Exception:
            pass
    _kill_profile_chrome()


def probe(expect_uid: str = "", start: bool = True) -> tuple[int, dict]:
    if start:
        proc = start_bridge()
        if proc == "FAILED":
            return 2, {"error": "bridge 45s 内没写出 status.json（node/Chrome 或端口环境问题）"}
    else:
        proc = None

    payload: dict = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "profile": str(Path("data") / "browser-profile-v2")}
    try:
        payload["goto"] = bridge_call("goto", url=CREATOR, wait=9000, timeout=70)
        auth = bridge_call("eval", expr=AUTH_JS, timeout=70)
    except Exception as e:
        payload["error"] = str(e)
        STATE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        stop_bridge(proc)
        return 2, payload

    acc = auth.get("account") or {}
    nick = acc.get("nickname") or auth.get("dom_nick") or ""
    uid = acc.get("uid") or ""
    authed = bool(nick or uid) and not auth.get("login_words") and not auth.get("url_login")

    payload.update({
        "authenticated": authed,
        "final_url": auth.get("url"),
        "nickname": nick,
        "uid": uid,
        "login_words": auth.get("login_words"),
        "url_login": auth.get("url_login"),
        "probed": auth.get("probed"),
        "body_len": auth.get("body_len"),
    })
    if expect_uid:
        payload["expect_uid"] = expect_uid
        payload["uid_matched"] = (uid == expect_uid)
        if authed and uid != expect_uid:
            authed = False
            payload["reason"] = f"登录的账号 uid={uid} 与预期 {expect_uid} 不符"

    STATE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    stop_bridge(proc)
    return (0 if authed else 1), payload


def main() -> int:
    ap = argparse.ArgumentParser(description="抖音发布通道登录态探测")
    ap.add_argument("--expect-uid", default="", help="预期账号 uid，不符即判红")
    ap.add_argument("--no-start", action="store_true", help="复用已在运行的 bridge")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    code, data = probe(expect_uid=args.expect_uid, start=not args.no_start)
    if not args.quiet:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    if code == 0:
        print(f"[OK  ] 登录有效：{data.get('nickname')}（uid {data.get('uid')}）")
    elif code == 1:
        print("[RED ] 登录失效或账号不符：" + str(data.get("reason") or "页面被踢到登录入口"))
        print("       处理：跑 start_douyin_bridge.bat，人工扫码登录一次后重跑本脚本")
    else:
        print("[ERR ] 环境问题：" + str(data.get("error")))
    return code


if __name__ == "__main__":
    sys.exit(main())
