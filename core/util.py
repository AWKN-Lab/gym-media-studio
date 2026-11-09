"""基础设施：路径、配置、日志、HTTP、进程调用。

设计要点：
1. 平台内所有出站 HTTP 显式禁用环境代理。WorkBuddy 每会话注入的 HTTP_PROXY 是内部代理，
   不转发普通公网流量；实测清空代理后国内热搜/语音接口才可达。
2. 除 Pillow 外不依赖第三方包，保证无人值守环境可复现。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# 全局写锁：控制台是 ThreadingHTTPServer，多个 API 请求会并发写同一份 JSON
# （如 publish/drafts、transcripts、schedule_state）。统一在这里加锁，避免并发写损坏。
_WRITE_LOCK = threading.Lock()

# 桌面安装版：设 LYNVOS_HOME 后，ROOT 整体指向用户数据目录
# （%LOCALAPPDATA%\LynvosFit），程序目录保持只读，装进 Program Files 不会因
# 无写权限而崩。不设该变量时逐字保持开发期行为 —— 与 A/B 严格回退同一条纪律。
# 决策逻辑放在不依赖本模块的 core/paths.py，供 sidecar 在 import core 之前调用。
from .paths import bundle_root, default_user_home, is_packaged, runtime_root  # noqa: E402

ROOT = runtime_root()
BUNDLE_ROOT = bundle_root()
CONFIG_PATH = ROOT / "config.json"
DATA = ROOT / "data"
OUTPUT = ROOT / "output"
ASSETS = ROOT / "assets"
WEB = ROOT / "web"

_FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/msyhbd.ttc"),
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/simhei.ttf"),
    Path("/System/Library/Fonts/PingFang.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
]

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def font_path(bold: bool = True) -> str:
    # 部署到 Linux 时，把任意中文字体(.ttf/.ttc/.otf)放进 assets/fonts/ 即自动生效
    fd = ASSETS / "fonts"
    if fd.is_dir():
        for p in sorted(fd.iterdir()):
            if p.is_file() and p.suffix.lower() in (".ttf", ".ttc", ".otf"):
                return str(p)
    for p in _FONT_CANDIDATES:
        if p.exists():
            return str(p)
    return str(_FONT_CANDIDATES[0])


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path, obj) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    return p


def read_text(path, default="") -> str:
    p = Path(path)
    if not p.exists():
        return default
    return p.read_text(encoding="utf-8", errors="ignore")


def write_text(path, text: str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def net_opener() -> urllib.request.OpenerDirector:
    """显式不走代理的 opener。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---- 公网调用礼仪：每域名限速 + 429/5xx 指数退避 ----
# 此前平台读侧只有 timeout，没有限速/退避：既易触发风控，也违反平台调用规范。
# 本机网关（127.0.0.1 / localhost）跳过限速，避免拖慢本地 LLM 与预览。
_RATE_LOCK = threading.Lock()
_LAST_CALL: dict[str, float] = {}
_MIN_INTERVAL = 0.35
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _host_of(url: str) -> str:
    rest = str(url).split("://", 1)[-1]
    return rest.split("/", 1)[0].split("@")[-1].split(":")[0].lower()


def _polite_wait(url: str) -> str:
    host = _host_of(url)
    if host in _LOCAL_HOSTS:
        return host
    with _RATE_LOCK:
        now = time.monotonic()
        wait = _MIN_INTERVAL - (now - _LAST_CALL.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[host] = time.monotonic()
    return host


def _open_with_retry(req, timeout: int, retries: int = 2) -> bytes:
    url = req.full_url
    local = _polite_wait(url) in _LOCAL_HOSTS
    for attempt in range(retries + 1):
        try:
            with net_opener().open(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            retryable = e.code == 429 or 500 <= e.code < 600
            if retryable and attempt < retries and not local:
                time.sleep(0.6 * (2 ** attempt))
                _polite_wait(url)
                continue
            raise RuntimeError(f"HTTP {e.code}: {body[:220].decode('utf-8', 'ignore')}") from e
        except urllib.error.URLError as e:
            if attempt < retries and not local:
                time.sleep(0.6 * (2 ** attempt))
                continue
            raise RuntimeError(f"网络不可达: {e.reason}") from e
    raise RuntimeError("重试耗尽")


def http_get(url: str, timeout: int = 15, headers: dict | None = None) -> bytes:
    h = {"User-Agent": _UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    return _open_with_retry(req, timeout)


def http_get_json(url: str, timeout: int = 15, headers: dict | None = None):
    return json.loads(http_get(url, timeout, headers).decode("utf-8", "ignore"))


def http_post_json(url: str, payload: dict, headers: dict | None = None,
                   timeout: int = 30, raw: bool = False):
    h = {"Content-Type": "application/json", "User-Agent": _UA}
    if headers:
        h.update(headers)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    body = _open_with_retry(req, timeout).decode("utf-8", "ignore")
    return body if raw else json.loads(body)


def run_cmd(cmd: list[str], timeout: int = 300, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd or ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def ffmpeg(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    r = run_cmd(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + args, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败 (rc={r.returncode}): {r.stderr[-900:]}")
    return r


def ffprobe_duration(path) -> float:
    r = run_cmd(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)], timeout=60)
    try:
        return float(r.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def ffprobe_info(path) -> dict:
    r = run_cmd(["ffprobe", "-v", "error", "-show_entries",
                 "format=duration:stream=codec_type,width,height",
                 "-of", "json", str(path)], timeout=60)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Run:
    """一次流水线执行的产物目录与日志。"""

    def __init__(self, name: str | None = None):
        self.id = name or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = DATA / "runs" / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.dir / "run.log"
        # 两阶段工作流：render 复用 prepare 的 Run 目录时，继承已有步骤进度
        self.steps: list[dict] = (read_json(self.dir / "status.json", {}) or {}).get("steps", [])
        self.started = time.time()

    def log(self, msg: str, level: str = "INFO") -> None:
        line = f"[{timestamp()}] {level} {msg}"
        print(line, flush=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def step(self, name: str, status: str, detail: str = "") -> None:
        self.steps.append({"name": name, "status": status, "detail": detail, "at": timestamp()})
        write_json(self.dir / "status.json", {
            "run_id": self.id, "steps": self.steps,
            "elapsed": round(time.time() - self.started, 1),
            "finished": status in ("done", "failed") and name == "成片输出",
        })
        self.log(f"[{name}] {status} {detail}", "STEP" if status != "failed" else "ERROR")

    def save(self, name: str, obj) -> Path:
        return write_json(self.dir / name, obj)

    def path(self, name: str) -> Path:
        return self.dir / name
