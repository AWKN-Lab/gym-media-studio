"""用户自填密钥：本地持久化、脱敏展示、逐项连通性测试（默认全部关闭）。

设计对标 NarratoAI `webui/components/basic_settings.py` 与 ShortGPT
`gui/ui_tab_config.py`：用户在界面填 Key → 保存到本机 → 一键测试连通。

**零密钥默认值**：出厂状态不读取任何环境变量里的 API Key。会话里注入的
DEEPSEEK / DASHSCOPE / MINIMAX 等 Key 一律不自动生效，避免「我没填却用了我的密钥」。
脚本文案走 Magpie（喜鹊）等本地开源网关的免费/登录态模型，配音走 edge-tts，
都不需要 Key。只有用户在界面主动填写、或显式打开「允许读取环境变量」才启用。

优先级：控制台填写 > （仅当显式允许时）进程环境变量 > 空。
密钥只写 data/secrets.json，绝不写进 config.json、日志或成片元数据。
"""
from __future__ import annotations

import os

from .util import DATA, http_get, http_post_json, read_json, write_json

SECRETS_PATH = DATA / "secrets.json"

# kind 决定「测试连通」怎么打；provider 对应 config.json / llm.providers 的 name
PROVIDERS = [
    {"key": "DEEPSEEK_DIRECT_API_KEY", "name": "DeepSeek", "usage": "脚本文案生成", "kind": "llm_chat",
     "provider": "deepseek", "default_model": "deepseek-chat", "need_model": True,
     "url": "https://api.deepseek.com/chat/completions", "apply": "https://platform.deepseek.com/api_keys"},
    {"key": "DASHSCOPE_API_KEY", "name": "阿里百炼 / 通义千问", "usage": "脚本文案生成", "kind": "llm_chat",
     "provider": "dashscope", "default_model": "qwen-plus", "need_model": True,
     "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
     "apply": "https://bailian.console.aliyun.com/"},
    {"key": "MOONSHOT_API_KEY", "name": "Moonshot / Kimi", "usage": "脚本文案生成", "kind": "llm_chat",
     "provider": "moonshot", "default_model": "moonshot-v1-8k", "need_model": True,
     "url": "https://api.moonshot.cn/v1/chat/completions", "apply": "https://platform.moonshot.cn/"},
    {"key": "ZHIPUAI_API_KEY", "name": "智谱 GLM", "usage": "脚本文案生成", "kind": "llm_chat",
     "provider": "zhipu", "default_model": "glm-4-flash", "need_model": True,
     "url": "https://open.bigmodel.cn/api/paas/v4/chat/completions", "apply": "https://open.bigmodel.cn/"},
    {"key": "ARK_API_KEY", "name": "火山方舟 / 豆包", "usage": "脚本文案生成", "kind": "llm_chat",
     "provider": "ark", "default_model": "", "need_model": True,
     "url": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
     "apply": "https://console.volcengine.com/ark", "hint": "需填已开通的推理接入点 ID（ep-xxx 或模型 ID）"},
    {"key": "PEXELS_API_KEY", "name": "Pexels", "usage": "正版健身房视频素材检索（生产路径）", "kind": "pexels",
     "provider": "pexels", "default_model": "", "need_model": False,
     "url": "https://api.pexels.com/v1/videos/search", "apply": "https://www.pexels.com/api/"},
    {"key": "PIXABAY_API_KEY", "name": "Pixabay", "usage": "备用正版视频素材检索", "kind": "pixabay",
     "provider": "pixabay", "default_model": "", "need_model": False,
     "url": "https://pixabay.com/api/videos/", "apply": "https://pixabay.com/api/docs/"},
]

BY_KEY = {p["key"]: p for p in PROVIDERS}
BY_PROVIDER = {p["provider"]: p for p in PROVIDERS if p.get("provider")}


def load_secrets() -> dict:
    d = read_json(SECRETS_PATH, {}) or {}
    d.setdefault("keys", {})
    d.setdefault("models", {})
    # 出厂默认：不读取环境变量。旧字段 ignore_env 一律作废（不再作为开关）。
    d["allow_env"] = bool(d.get("allow_env", False))
    d.pop("ignore_env", None)
    return d


def save_secrets(obj: dict) -> dict:
    cur = load_secrets()
    keys, models = cur["keys"], cur["models"]
    for k, v in (obj.get("keys") or {}).items():
        if k not in BY_KEY:
            continue
        if v == "__delete__":
            keys.pop(k, None)
        elif isinstance(v, str) and v.strip():
            keys[k] = v.strip()
    for k, v in (obj.get("models") or {}).items():
        if isinstance(v, str):
            if v.strip():
                models[k] = v.strip()
            else:
                models.pop(k, None)
    for k in (obj.get("clear") or []):
        keys.pop(k, None)
    if "allow_env" in obj:
        cur["allow_env"] = bool(obj["allow_env"])
    cur.pop("ignore_env", None)
    cur["keys"], cur["models"] = keys, models
    write_json(SECRETS_PATH, cur)
    return cur


def get_secret(env_name: str) -> str:
    """统一取值入口：默认只认控制台填写的 Key，绝不自动读环境变量。"""
    if not env_name:
        return ""
    s = load_secrets()
    v = s["keys"].get(env_name, "")
    if v:
        return v
    if not s.get("allow_env"):
        return ""
    return os.environ.get(env_name, "")


def env_summary() -> dict:
    """界面提示用：检测到几个环境变量 Key、当前是否允许使用（不返回任何明文）。"""
    s = load_secrets()
    names = [p["key"] for p in PROVIDERS]
    return {"detected": sum(1 for n in names if os.environ.get(n)),
            "enabled": bool(s.get("allow_env"))}


def get_model(provider: str, default: str = "") -> str:
    return load_secrets()["models"].get(provider, "") or default


def resolve_provider(p: dict) -> dict:
    """把 config 里的 provider 与用户自填的 model 合并。"""
    q = dict(p)
    q["model"] = get_model(q["name"], q.get("model", ""))
    return q


def _mask(v: str) -> str:
    if len(v) <= 8:
        return v[:2] + "****"
    return v[:5] + "****" + v[-4:]


def public_state() -> list[dict]:
    s = load_secrets()
    allow = bool(s.get("allow_env"))
    out = []
    for p in PROVIDERS:
        user_key = s["keys"].get(p["key"], "")
        env_present = bool(os.environ.get(p["key"], ""))
        env_key = os.environ.get(p["key"], "") if allow else ""
        model = s["models"].get(p["provider"], "") or p["default_model"]
        out.append({
            "key": p["key"], "name": p["name"], "usage": p["usage"], "kind": p["kind"],
            "provider": p["provider"], "apply": p["apply"], "hint": p.get("hint", ""),
            "need_model": p["need_model"], "model": model,
            "configured": bool(user_key or env_key),
            "source": "控制台填写" if user_key else ("环境变量·已开启" if env_key else "未配置"),
            "env_present": env_present,
            "masked": _mask(user_key or env_key) if (user_key or env_key) else "",
        })
    return out


def test_connection(env_name: str, model: str = "") -> tuple[bool, str]:
    """对单个凭据打一次最小请求，返回 (是否可用,  human-readable 结论)。"""
    p = BY_KEY.get(env_name)
    if not p:
        return False, "未知凭据"
    key = get_secret(env_name)
    if not key:
        return False, "未填写 Key"
    kind = p["kind"]
    try:
        if kind == "llm_chat":
            m = (model or get_model(p["provider"], p["default_model"])).strip()
            if not m:
                return False, "请先填写模型 ID / 接入点"
            d = http_post_json(p["url"],
                               {"model": m, "messages": [{"role": "user", "content": "回 OK"}],
                                "max_tokens": 8},
                               {"Authorization": "Bearer " + key}, timeout=20)
            if d.get("choices"):
                return True, f"连通正常 · {m}"
            return False, f"返回异常：{str(d)[:110]}"

        if kind == "pexels":
            body = http_get(p["url"] + "?query=gym&per_page=1",
                            timeout=15, headers={"Authorization": key}).decode("utf-8", "ignore")
            return ("total_results" in body or "videos" in body), "素材检索可用（Pexels API 200）"

        if kind == "pixabay":
            body = http_get(f"{p['url']}?key={key}&q=gym&per_page=3", timeout=15)\
                .decode("utf-8", "ignore")
            return ("hits" in body or "total" in body), "素材检索可用（Pixabay API 200）"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:150]}"
    return False, "未实现的测试类型"
