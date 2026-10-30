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

#: 企业内网部署的显式逃生开关（默认关）。
#:
#: 默认拒绝私网/环回是 SSRF 纵深：`validate_external_url` 把关的是**用户填写的**
#: base_url，放行私网等于允许"让服务端带着 token 请求本机/内网服务"。
#: 但私有化部署的诉求恰恰相反 —— 模型与 OCR 都在企业内网（如
#: `http://10.0.0.8:8000/v1` 的 vLLM、`http://127.0.0.1:11434` 的 Ollama），
#: 数据不出企业。二者只能靠**显式 opt-in** 区分：默认保持拒绝，管理员
#: 设置 `PBC_ALLOW_PRIVATE_ENDPOINTS=1` 后才放行私网/环回。
#: link-local（169.254/16、fe80::/10）是云元数据端点，**任何模式下都拒**。
_ALLOW_PRIVATE_ENV = "PBC_ALLOW_PRIVATE_ENDPOINTS"


def allow_private_endpoints() -> bool:
    """是否允许把外部端点指向私网/环回（企业内网部署开关，默认 False）。"""
    return os.getenv(_ALLOW_PRIVATE_ENV, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _parse_host_as_ip(host: str):
    """Parse `host` as an IP address, including non-dotted literals.

    urllib/yyparse 会原样通过 "2130706433"（十进制 127.0.0.1）、
    "0x7f000001"（十六进制）、"017700000001"（八进制）等非点分 IP 字面量，
    而底层 httpx/socket.getaddrinfo 会解析到 127.0.0.1 或内网地址——
    仅用 ipaddress.ip_address() 会把它们当 hostname 放行（对抗审查 cr-2）。
    Returns: ipaddress.IPv4Address/IPv6Address or None.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    # Non-dotted numeric literals; base 0 handles 0x / 0o prefixes
    try:
        n = int(host, 0)
    except ValueError:
        # Python 3 int(,0) rejects legacy octal (leading 0, no 0o prefix)
        # e.g. 017700000001 = 127.0.0.1 — DNS resolvers accept this form.
        if len(host) > 1 and host[0] == "0" and host.isdigit():
            try:
                n = int(host, 8)
            except ValueError:
                return None
        else:
            return None
    # int() with base 0 also accepts "+5"/"-5"; reject signed forms
    if host and host[0] in "+-":
        return None
    if 0 <= n <= 0xFFFFFFFF:
        return ipaddress.ip_address(n)
    return None


def _is_blocked_host(host: str) -> bool:
    """Return True if `host` is a blocked (internal/link-local) address."""
    if not host:
        return True
    host = host.lower().strip("[]")
    allow_private = allow_private_endpoints()
    # localhost 字面量是 loopback 最常用别名，IP 字面量拦截之外的常见绕过
    # （对抗审查：此前仅拦 IP 字面量，http://localhost:8080 保存时放行，
    # 后续 ocr_client 携带 token 以服务端身份请求本机服务）。
    # 只拦字面量不做 DNS 解析 — 保持既有威胁模型（防 IP 字面量/别名，
    # 不防 DNS rebinding）。
    if host == "localhost" or host.endswith(".localhost"):
        # 企业内网部署时本机模型（Ollama/vLLM）就在 localhost，须放行。
        return not allow_private
    # Bare hostname check (fast path for IPv4 strings)
    if not allow_private:
        for prefix in _BLOCKED_PREFIXES:
            if host.startswith(prefix):
                return True
    # IPv4 in ipaddress form: 172.16/12 is tricky to prefix-match, so parse.
    ip = _parse_host_as_ip(host)
    if ip is not None:
        # link-local = 云元数据端点（169.254.169.254 等），**任何模式都拒**：
        # 它与企业内网模型无关，放行只会扩大 SSRF 面。
        if ip.is_link_local:
            return True
        if ip.is_loopback or ip.is_private or ip.is_unspecified:
            return not allow_private
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

    Used by Settings API and /api/shutdown to allow mutation ONLY from the
    local Electron renderer or localhost dev server, blocking CSRF from
    arbitrary origins.

    威胁模型：本地单用户应用。Host=localhost 已足以确认同源。
    旧版硬编码端口白名单 (8000/58765) 是过度设计——端口变更或
    Electron 动态端口时会导致 403，前端收到 403 后显示通用"失败"
    而非真实错误，排查极困难。

    Checks (in order):
      1. Host header is localhost/127.0.0.1/[::1] (任意端口)
      2. Origin header 如果存在，必须是 localhost/127.0.0.1 (任意端口)
         Origin 为空时（Electron 内部请求或非浏览器）允许通过，
         因为 Host 已验证是本机
    """
    host = (request.headers.get("host") or "").lower()
    if not host or not any(
        host.startswith(p) for p in ("localhost:", "127.0.0.1:", "[::1]:")
    ):
        return False
    origin = (request.headers.get("origin") or "").lower()
    if not origin:
        # 非浏览器请求（如 Electron 内部 fetch、curl）— Host 已验证本机
        return True
    # Origin 存在时，必须是 localhost/127.0.0.1 (任意端口)
    try:
        from urllib.parse import urlparse
        parsed = urlparse(origin)
        origin_host = (parsed.hostname or "").lower()
        return origin_host in ("localhost", "127.0.0.1", "::1")
    except ValueError:
        return False


# ── 签名 URL 反刍脱敏（P1-7） ────────────────────────────────────────────
# OCR 服务的 result URL 是签名 CDN 地址（query 携带签名 token）。requests
# 的 MaxRetryError 等异常消息会回显完整 URL（含 query），这些消息一路
# 进入 jobs.error_message → 报告/飞书通知 → 反刍泄露。所有进入异常消息
# /日志的 URL 必须经 redact_urls() 过滤后再传递。
_URL_RE = None


def _build_url_re():
    global _URL_RE
    if _URL_RE is None:
        import re as _re
        # 两类形态：
        # 1) 完整 URL：https://host/path?query
        # 2) relative path + query（requests 的 MaxRetryError str 回显
        #    "with url: /zip/out.zip?X-Amz-Signature=..." — 无 scheme）
        _URL_RE = _re.compile(
            r"""\b(?:https?://[^\s"'<>]+|/[^\s"'<>?]*\?[^\s"'<>]+)""", _re.I
        )
    return _URL_RE


def redact_urls(text: str) -> str:
    """Strip query strings (signed tokens) from URLs embedded in arbitrary text.

    规则：
    - 匹配完整 URL（https?://...）与 requests 异常回显的相对路径带 query
      两种形态（`with url: /zip/out.zip?X-Amz-Signature=...`）
    - 有 query → 保留 scheme://netloc+path（或无 scheme 的相对 path），
      query 整体替换为 ?<redacted>
    - path 超长（签名也可能藏在 path 尾部）→ 截前 120 字符 + ...
    - 无 query 的普通 URL/路径原样保留（配置类 URL 泄露面为 0，保留排障）

    用于：OCR 客户端 raise 前（源头）、pipeline error_message 入库前（兜底）。
    """
    if not text or ("http" not in text.lower() and "?" not in text):
        return text
    regex = _build_url_re()

    def _redact(m):
        url = m.group(0)
        try:
            from urllib.parse import urlsplit
            p = urlsplit(url)
        except ValueError:
            return "<redacted-url>"
        if p.query:
            return f"{p.path}?<redacted>" if not p.scheme else f"{p.scheme}://{p.netloc}{p.path}?<redacted>"
        path = p.path[:120]
        if len(p.path) > 120:
            path += "..."
        return f"{p.path}" if not p.scheme else f"{p.scheme}://{p.netloc}{path}"

    return regex.sub(_redact, text)
