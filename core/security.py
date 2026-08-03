"""Security helpers — URL validation + Settings API guard.

Centralizes the small but critical security checks that are easy to get
wrong inline. Keeping them in one module makes audits easier.
"""
from __future__ import annotations

import ipaddress
import logging
import os
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Hosts we never want the server to fetch from. Even though PBC is a local
# single-user app, a malicious web page (CSRF) or compromised co-user could
# try to point OCR/LLM at internal services to exfiltrate cloud metadata or
# scan the intranet. We block:
#   - link-local 169.254.0.0/16 (AWS/Azure/GCP metadata endpoints)
#   - loopback 127.0.0.0/8 + ::1 (avoid self-triggering local services)
#   - private 10/8, 172.16/12, 192.168/16 (internal network scan)
#   - unspecified 0.0.0.0
#
# Allow IPv6 too (::1, fc00::/7, fe80::/10).
_BLOCKED_PREFIXES = (
    "169.254.",       # link-local
    "127.",            # loopback v4
    "10.",             # private 10/8
    "192.168.",        # private 192.168/16
    "0.0.0.0",         # unspecified
)


def _is_blocked_host(host: str) -> bool:
    """Return True if `host` is a blocked (internal/link-local) address."""
    if not host:
        return True
    host = host.lower().strip("[]")
    # Bare hostname check (fast path for IPv4 strings)
    for prefix in _BLOCKED_PREFIXES:
        if host.startswith(prefix):
            return True
    # IPv4 in ipaddress form: 172.16/12 is tricky to prefix-match, so parse.
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_unspecified:
            return True
    except ValueError:
        # Not an IP literal (it's a hostname like api.deepseek.com). Allow.
        # We deliberately do NOT do DNS resolution here — that would be slow
        # and the threat model is direct IP literal in base_url, not DNS
        # rebinding (which would require the attacker to control DNS for a
        # hostname they convinced the user to type).
        pass
    return False


def validate_external_url(url: str, *, require_https: bool = False,
                          kind: str = "url") -> tuple[bool, str]:
    """Validate that `url` points to a non-internal endpoint.

    Args:
        url: The URL to validate.
        require_https: If True, only https:// URLs are allowed.
        kind: Label for error messages (e.g. "OCR API URL", "LLM base_url").

    Returns:
        (True, "") if valid, else (False, reason).
    """
    if not url or not url.strip():
        return False, f"{kind} 不能为空"
    url = url.strip()
    try:
        parsed = urlparse(url)
    except ValueError as e:
        return False, f"{kind} 解析失败: {e}"
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"{kind} 必须是 http(s):// 开头（当前: {scheme or '无协议'}）"
    if require_https and scheme != "https":
        return False, f"{kind} 必须 https://（当前: {scheme}）"
    host = parsed.hostname or ""
    if _is_blocked_host(host):
        return False, (
            f"{kind} 指向内部地址 {host}，已被安全策略阻止"
            f"（不允许 link-local/loopback/private 网段）"
        )
    return True, ""


def is_local_request(request) -> bool:
    """Return True if `request` originated from the local machine.

    Used by Settings API to allow mutation ONLY from the local Electron
    renderer or localhost dev server, blocking CSRF from arbitrary origins.
    Checks (in order):
      1. Host header is localhost/127.0.0.1 (same-origin only)
      2. Origin header is in the allow-list (matches CORS origins)
    """
    host = (request.headers.get("host") or "").lower()
    if not host or not any(
        host.startswith(p) for p in ("localhost:", "127.0.0.1:", "[::1]:")
    ):
        return False
    origin = (request.headers.get("origin") or "").lower()
    if origin:
        allowed = (
            "http://localhost:8000", "http://127.0.0.1:8000",
            "http://localhost:58765", "http://127.0.0.1:58765",
        )
        if origin not in allowed:
            return False
    return True
