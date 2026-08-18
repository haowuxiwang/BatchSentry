"""Security helpers unit tests — URL validation + local-request guard.

Covers:
- _is_blocked_host: IPv4 / IPv6 / hostname / empty
- validate_external_url: empty / bad scheme / require_https / blocked host
- is_local_request: localhost host / non-local host / Origin allow-list
"""
from unittest.mock import MagicMock

from core.security import (
    _is_blocked_host,
    validate_external_url,
    is_local_request,
    redact_urls,
)


class TestIsBlockedHost:
    """_is_blocked_host — IP literal + hostname classification."""

    def test_empty_host_is_blocked(self):
        assert _is_blocked_host("") is True
        assert _is_blocked_host(None) is True

    def test_ipv4_loopback_blocked(self):
        assert _is_blocked_host("127.0.0.1") is True
        assert _is_blocked_host("127.1.2.3") is True

    def test_ipv4_private_blocked(self):
        assert _is_blocked_host("10.0.0.1") is True
        assert _is_blocked_host("192.168.1.1") is True
        assert _is_blocked_host("172.16.0.1") is True

    def test_ipv4_link_local_blocked(self):
        assert _is_blocked_host("169.254.169.254") is True

    def test_ipv4_unspecified_blocked(self):
        assert _is_blocked_host("0.0.0.0") is True

    def test_ipv6_loopback_blocked(self):
        assert _is_blocked_host("::1") is True

    def test_ipv6_link_local_blocked(self):
        assert _is_blocked_host("fe80::1") is True

    def test_ipv6_unique_local_blocked(self):
        assert _is_blocked_host("fc00::1") is True
        assert _is_blocked_host("fd00::1") is True

    def test_public_hostname_allowed(self):
        assert _is_blocked_host("api.deepseek.com") is False
        assert _is_blocked_host("example.com") is False

    def test_localhost_literal_blocked(self):
        """对抗审查：localhost 字面量是 loopback 最常用别名，此前只拦 IP
        字面量漏掉它 — "http://localhost:8080/scan" 保存时被放行，后续
        OCR 客户端会携带 token 以服务端身份请求本机服务（SSRF 纵深）。"""
        assert _is_blocked_host("localhost") is True
        assert _is_blocked_host("LOCALHOST") is True
        # .localhost 是保留 TLD（RFC 6761），浏览器/解析器按 loopback 处理
        assert _is_blocked_host("foo.localhost") is True
        assert _is_blocked_host("localhost.localdomain") is False  # 字面量外不猜

    def test_public_ipv4_allowed(self):
        assert _is_blocked_host("8.8.8.8") is False
        assert _is_blocked_host("1.2.3.4") is False

    def test_ipv6_brackets_stripped(self):
        """IPv6 in bracket form [::1] should be normalized and blocked."""
        assert _is_blocked_host("[::1]") is True

    def test_non_dotted_ip_literals_blocked(self):
        """对抗审查(cr-2): 非点分 IP 字面量（十进制/十六进制/八进制）必须
        被识别为内网地址 — 底层 getaddrinfo 会把它们解析到 loopback/内网。"""
        assert _is_blocked_host("2130706433") is True   # 127.0.0.1
        assert _is_blocked_host("0x7f000001") is True   # 127.0.0.1 hex
        assert _is_blocked_host("017700000001") is True  # 127.0.0.1 octal
        assert _is_blocked_host("167772161") is True    # 10.0.0.1
        assert _is_blocked_host("0x0a000001") is True   # 10.0.0.1 hex
        assert _is_blocked_host("2851995649") is True   # 169.254.169.254
        assert _is_blocked_host("0") is True            # 0.0.0.0

    def test_non_dotted_public_ip_allowed(self):
        """非点分但指向公网的字面量应放行（如 8.8.8.8 = 134744072）。"""
        assert _is_blocked_host("134744072") is False   # 8.8.8.8
        assert _is_blocked_host("0x08080808") is False

    def test_numeric_hostname_not_parsed_as_ip(self):
        """纯数字字符串超出 IPv4 范围（> 2^32-1）→ 当 hostname 处理不阻断。"""
        assert _is_blocked_host("99999999999") is False
        assert _is_blocked_host("12345678901234567890") is False


class TestValidateExternalUrl:
    """validate_external_url — scheme + host validation."""

    def test_empty_url_rejected(self):
        ok, reason = validate_external_url("", kind="test")
        assert ok is False
        assert "不能为空" in reason

    def test_whitespace_only_url_rejected(self):
        ok, reason = validate_external_url("   ", kind="test")
        assert ok is False

    def test_no_scheme_rejected(self):
        ok, reason = validate_external_url("api.deepseek.com", kind="test")
        assert ok is False
        assert "http(s)" in reason

    def test_ftp_scheme_rejected(self):
        ok, reason = validate_external_url("ftp://example.com", kind="test")
        assert ok is False
        assert "http(s)" in reason

    def test_https_required_when_flag_set(self):
        ok, reason = validate_external_url(
            "http://api.deepseek.com", require_https=True, kind="test"
        )
        assert ok is False
        assert "https" in reason

    def test_https_passes_when_required(self):
        ok, _ = validate_external_url(
            "https://api.deepseek.com", require_https=True, kind="test"
        )
        assert ok is True

    def test_blocked_loopback_rejected(self):
        ok, reason = validate_external_url(
            "http://127.0.0.1:8000/api", kind="OCR URL"
        )
        assert ok is False
        assert "内部地址" in reason
        assert "127.0.0.1" in reason

    def test_localhost_literal_rejected(self):
        """对抗审查回归：localhost 字面量 URL 必须被拒（此前放行）。"""
        ok, _ = validate_external_url(
            "http://localhost:8080/scan", kind="OCR URL"
        )
        assert ok is False
        ok, _ = validate_external_url(
            "http://foo.localhost:8080/scan", kind="base_url"
        )
        assert ok is False

    def test_blocked_link_local_rejected(self):
        """SSRF protection: 169.254.169.254 (cloud metadata) blocked."""
        ok, reason = validate_external_url(
            "http://169.254.169.254/latest/meta-data", kind="base_url"
        )
        assert ok is False
        assert "169.254" in reason

    def test_public_https_url_accepted(self):
        ok, _ = validate_external_url(
            "https://api.deepseek.com/v1", kind="LLM base_url"
        )
        assert ok is True


class TestIsLocalRequest:
    """is_local_request — Host + Origin check for Settings CSRF guard.

    Phase 8: Origin header is now REQUIRED (blocks curl/non-browser CSRF).
    Previously empty Origin would pass; now it's rejected.
    """

    def _make_request(self, host="", origin=""):
        req = MagicMock()
        headers = {}
        if host:
            headers["host"] = host
        if origin:
            headers["origin"] = origin
        # Support .get() with default
        req.headers = MagicMock()
        req.headers.get = lambda key, default="": headers.get(key.lower(), default)
        return req

    def test_localhost_host_with_origin_accepted(self):
        """Local host + local Origin → accepted."""
        req = self._make_request(
            host="localhost:8000", origin="http://localhost:8000"
        )
        assert is_local_request(req) is True

    def test_127_host_with_origin_accepted(self):
        """127.0.0.1 host + 127.0.0.1 Origin → accepted."""
        req = self._make_request(
            host="127.0.0.1:8000", origin="http://127.0.0.1:8000"
        )
        assert is_local_request(req) is True

    def test_ipv6_localhost_with_origin_accepted(self):
        """IPv6 loopback host + Origin → accepted."""
        req = self._make_request(
            host="[::1]:8000", origin="http://127.0.0.1:8000"
        )
        assert is_local_request(req) is True

    def test_non_local_host_rejected(self):
        req = self._make_request(
            host="evil.com:8000", origin="http://evil.com:8000"
        )
        assert is_local_request(req) is False

    def test_empty_host_rejected(self):
        req = self._make_request(host="", origin="http://127.0.0.1:8000")
        assert is_local_request(req) is False

    def test_allowed_origin_accepted(self):
        req = self._make_request(
            host="localhost:8000", origin="http://localhost:8000"
        )
        assert is_local_request(req) is True

    def test_disallowed_origin_rejected(self):
        """Local host but evil Origin — CSRF attempt."""
        req = self._make_request(
            host="localhost:8000", origin="http://evil.com"
        )
        assert is_local_request(req) is False

    def test_port_58765_origin_accepted(self):
        """Electron dev port also in allow-list."""
        req = self._make_request(
            host="127.0.0.1:58765", origin="http://127.0.0.1:58765"
        )
        assert is_local_request(req) is True

    def test_no_origin_header_allowed_for_localhost(self):
        """无 Origin header（Electron 内部请求/curl）+ Host=localhost 应允许。

        新逻辑：Host=localhost 已验证本机，Origin 为空时不阻断。
        本地单用户应用，无需 Origin 白名单防 CSRF。
        """
        req = self._make_request(host="localhost:8000", origin="")
        assert is_local_request(req) is True

    def test_no_origin_header_allowed_for_127(self):
        """无 Origin header + Host=127.0.0.1 应允许。"""
        req = self._make_request(host="127.0.0.1:8000", origin="")
        assert is_local_request(req) is True


# ── redact_urls（P1-7）：签名 URL 反刍脱敏 ──────────────────────────────


class TestRedactUrls:
    """redact_urls — 异常消息/日志中的签名 URL 反刍脱敏。"""

    def test_query_string_redacted(self):
        out = redact_urls(
            "ConnectionError: GET https://cdn.example.com/down/abc123.zip"
            "?sign=xyz&expires=9999"
        )
        assert "?sign=xyz" not in out
        assert "sign=xyz" not in out
        assert "abc123.zip?<redacted>" in out

    def test_plain_url_kept(self):
        out = redact_urls("https://api.example.com/v1/extract")
        assert out == "https://api.example.com/v1/extract"

    def test_long_path_truncated(self):
        url = "https://cdn.example.com/" + "x" * 200
        out = redact_urls(url)
        assert "x" * 200 not in out
        assert "..." in out

    def test_json_text_redacted(self):
        payload = (
            '{"data": {"resultUrl": {"jsonUrl": '
            '"https://res.example.com/out?token=SECRET"}}}'
        )
        out = redact_urls(payload)
        assert "token=SECRET" not in out
        assert "<redacted>" in out

    def test_no_url_passthrough(self):
        assert redact_urls("") == ""
        assert redact_urls("纯中文无 URL 消息") == "纯中文无 URL 消息"

    def test_urls_in_pipeline_error_message(self):
        msg = (
            "MinerU poll failed: 5 consecutive network errors: "
            "HTTPSConnectionPool(host=res.example.com): Max retries exceeded "
            "with url: /zip/out.zip?X-Amz-Signature=deadbeef "
            "(Caused by ConnectionError)"
        )
        out = redact_urls(msg)
        assert "X-Amz-Signature=deadbeef" not in out
        assert "Max retries exceeded" in out
