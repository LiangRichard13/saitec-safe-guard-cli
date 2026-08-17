"""通用工具（纯函数，无 IO）"""
from __future__ import annotations

from datetime import datetime, timezone

_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "api-key",
        "cookie",
        "set-cookie",
        "x-auth-token",
        "proxy-authorization",
    }
)


def now_iso8601() -> str:
    """当前时间，ISO8601 格式（毫秒精度，UTC）

"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def redact_headers(h: dict) -> dict:
    """脱敏请求/响应 headers 中的敏感字段（Authorization / API Key 等）

    非敏感字段保留原值；不存在的 header 不会出现在返回中。
    """
    if not h:
        return {}
    return {
        k: ("***" if k.lower() in _SENSITIVE_HEADERS else v)
        for k, v in h.items()
    }