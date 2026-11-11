"""零依赖凭据保护：Windows DPAPI（绑定当前用户）+ PBKDF2 口令哈希。

实测基线：Python 3.13.14 / Windows，CryptProtectData + CryptUnprotectData
往返一致（2026-11-08）。非 Windows 平台没有 DPAPI，此时**明确降级**为
带标记的明文并收紧文件权限，绝不伪装成「已加密」。

前缀语义（GPT 复核 2026-11-10 第 5 项，在此写死）：
  - enc:v1:  —— DPAPI 真加密（仅 Windows）。is_encrypted() 为真。
  - plain:v1: —— 非 Windows 开发降级标记：base64，可逆，**不是加密**。
    只允许在没有 DPAPI 的开发机上落盘，is_encrypted() 为假；任何测试
    不得把它当成“加密通过”。生产跑在 Windows 上，seal() 在 DPAPI
    不可用时直接报错，拒绝静默降级；Linux 上 DPAPI 无法验证的部分，
    须在 Windows 上验证，不得冒充已验证。

生产标记（GPT 第二次复核 2026-11-11 第 5 节第 1 项）：
  环境变量 LYNVOS_PRODUCTION=1/true/yes 置位时，seal() 在任何平台上
  都不允许降级成 plain:v1:：Windows 上 DPAPI 不可用报错，非 Windows
  直接报错。开发机默认不置位，仍可用 plain:v1: 降级标记。
密文带版本前缀，便于鉴别；历史明文数据会在读取时自动迁移。
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import os
import secrets
from pathlib import Path

_ENC = "enc:v1:"
_PLAIN = "plain:v1:"
_ITER = 200_000


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.c_void_p)]


def _crypt32():
    if os.name != "nt":
        return None
    try:
        c = ctypes.windll.crypt32
        c.CryptProtectData.argtypes = [
            ctypes.POINTER(_BLOB), ctypes.c_wchar_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(_BLOB)]
        c.CryptProtectData.restype = ctypes.c_int
        c.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_BLOB), ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(_BLOB)]
        c.CryptUnprotectData.restype = ctypes.c_int
        return c
    except Exception:
        return None


def _local_free(ptr):
    try:
        ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(ptr))
    except Exception:
        pass


def _protect(raw: bytes) -> bytes | None:
    c = _crypt32()
    if c is None or not raw:
        return None
    try:
        buf = ctypes.create_string_buffer(raw, len(raw))
        src = _BLOB(len(raw), ctypes.cast(buf, ctypes.c_void_p).value)
        out = _BLOB()
        if not c.CryptProtectData(ctypes.byref(src), None, None, None, None, 0,
                                  ctypes.byref(out)):
            return None
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _local_free(out.pbData)
    except Exception:
        return None


def _unprotect(blob: bytes) -> bytes | None:
    c = _crypt32()
    if c is None or not blob:
        return None
    try:
        buf = ctypes.create_string_buffer(blob, len(blob))
        src = _BLOB(len(blob), ctypes.cast(buf, ctypes.c_void_p).value)
        out = _BLOB()
        if not c.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0,
                                    ctypes.byref(out)):
            return None
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _local_free(out.pbData)
    except Exception:
        return None


def protection_mode() -> str:
    return "dpapi-current-user" if _crypt32() is not None else "plaintext-0600"


def is_encrypted(value: str) -> bool:
    """只有 DPAPI 真加密才算加密。plain:v1: 是 base64 降级标记，不是加密。"""
    return str(value or "").startswith(_ENC)


def is_production() -> bool:
    """生产标记：LYNVOS_PRODUCTION=1/true/yes 置位即生产环境。"""
    return str(os.environ.get("LYNVOS_PRODUCTION", "")).strip().lower() in (
        "1", "true", "yes")


def seal(text: str) -> str:
    """把凭据转成可安全落盘的字符串。空串原样返回。

    Windows 上 DPAPI 不可用时直接报错，拒绝静默降级成明文；
    非 Windows 上只有开发态才回退到 plain:v1: 开发降级标记，
    生产标记置位时同样直接报错（GPT 第二次复核第 5 节第 1 项）。
    """
    raw = str(text or "").encode("utf-8")
    if not raw:
        return ""
    blob = _protect(raw)
    if blob is None:
        if os.name == "nt":
            raise RuntimeError(
                "DPAPI 不可用：Windows 生产环境拒绝把凭据存成明文，"
                "请检查当前 Windows 用户凭据或重新配置")
        if is_production():
            raise RuntimeError(
                "生产环境拒绝 plain:v1: 降级：非 Windows 生产机没有 DPAPI，"
                "凭据不得以 base64 明文标记落盘，请改用 Windows 生产机")
        # 明确标记降级，避免以后被误读成「已加密」
        return _PLAIN + base64.b64encode(raw).decode("ascii")
    return _ENC + base64.b64encode(blob).decode("ascii")


def unseal(value: str) -> str:
    """读取凭据。兼容 enc:v1 / plain:v1 / 历史裸明文三种格式。"""
    s = str(value or "")
    if not s:
        return ""
    if s.startswith(_ENC):
        raw = _unprotect(base64.b64decode(s[len(_ENC):]))
        if raw is None:
            raise RuntimeError("DPAPI 解密失败：凭据可能由其他 Windows 用户加密，请重新配置登录态")
        return raw.decode("utf-8")
    if s.startswith(_PLAIN):
        return base64.b64decode(s[len(_PLAIN):]).decode("utf-8")
    return s  # 历史明文；调用方负责迁移


def needs_migration(value: str) -> bool:
    s = str(value or "")
    return bool(s) and not s.startswith((_ENC, _PLAIN))


def harden(path: Path) -> None:
    """非 Windows 平台尽量收紧权限（Windows 由用户 ACL 负责）。"""
    if os.name == "nt":
        return
    try:
        os.chmod(str(path), 0o600)
    except Exception:
        pass


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"),
                             bytes.fromhex(salt), _ITER)
    return f"pbkdf2_sha256${_ITER}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """验证口令。历史明文口令仍可登录（便于平滑迁移），但不再鼓励。"""
    s = str(stored or "")
    if not s.startswith("pbkdf2_sha256$"):
        return hmac.compare_digest(s, str(password))
    try:
        _, iterations, salt, expect = s.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"),
                                 bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(dk.hex(), expect)
    except Exception:
        return False
