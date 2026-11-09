"""HTTP 回归的 curl 传输后端（不依赖 Python socket 回环）。

背景：2026-11-08 受控对照读数（data/baseline/http_loopback_probe.json）：
  极简 HTTP 服务分别绑定 127.0.0.1 / localhost / ::1 / 0.0.0.0，
  前三者同进程 urllib 与独立 curl 全部 rc=28 超时；
  只有「本机网卡 IP 192.168.10.249 + 独立 curl 进程」返回 OK|200。
  即被拦的是 TCP 回环通道，不是服务代码，也不是 HTTP 协议栈。

因此本模块用 curl 子进程发请求，服务以 --host <本机网卡IP> 启动，
可在被拦环境里取得真实 HTTP 读数，不拿进程内直调冒充。

用法：
  GYMPULSE_HTTP_BACKEND=curl python tests/http_regression.py http://<网卡IP>:8899
"""
from __future__ import annotations

import json
import subprocess

MARKER = "\n__GYM_HTTP_STATUS__"


def _decode(raw: str):
    try:
        return json.loads(raw)
    except Exception:
        return {"_text": raw}


def req(base: str, path: str, payload: dict | None = None, timeout: int = 45):
    """返回 (status, body)，语义与 tests/http_regression.py:req 一致。

    curl 对 4xx/5xx 默认仍返回 body，正好对应原 req() 里 HTTPError 的分支。
    """
    url = base.rstrip("/") + path
    cmd = [
        "curl", "-sS", "--noproxy", "*", "--max-time", str(timeout),
        "-w", MARKER + "%{http_code}",
        "-X", "POST" if payload is not None else "GET",
    ]
    if payload is not None:
        cmd += ["-H", "Content-Type: application/json",
                "--data-binary", json.dumps(payload, ensure_ascii=False)]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
    if r.returncode != 0:
        raise RuntimeError(f"curl rc={r.returncode} {(r.stdout + r.stderr).strip()[:200]}")
    if MARKER not in r.stdout:
        raise RuntimeError(f"curl 未返回状态码：{r.stdout[:160]!r} {r.stderr[:160]!r}")
    body, code = r.stdout.rsplit(MARKER, 1)
    return int(code.strip()), _decode(body)
