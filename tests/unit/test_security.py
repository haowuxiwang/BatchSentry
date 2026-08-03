"""Security helpers unit tests — URL validation + local-request guard.

Covers:
- _is_blocked_host: IPv4 / IPv6 / hostname / empty
- validate_external_url: empty / bad scheme / require_https / blocked host
- is_local_request: localhost host / non-local host / Origin allow-list
"""
import pytest
from unittest.mock import MagicMock

from core.security import (
    _is_blocked_host,
    validate_external_url,
    is_local_request,
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

    def test_public_ipv4_allowed(self):
        assert _is_blocked_host("8.8.8.8") is False
        assert _is_blocked_host("1.2.3.4") is False

    def test_ipv6_brackets_stripped(self):
        """IPv6 in bracket form [::1] should be normalized and blocked."""
        assert _is_blocked_host("[::1]") is True


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
    """is_local_request — Host + Origin check for Settings CSRF guard."""

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

    def test_localhost_host_accepted(self):
        req = self._make_request(host="localhost:8000")
        assert is_local_request(req) is True

    def test_127_host_accepted(self):
        req = self._make_request(host="127.0.0.1:8000")
        assert is_local_request(req) is True

    def test_ipv6_localhost_accepted(self):
        req = self._make_request(host="[::1]:8000")
        assert is_local_request(req) is True

    def test_non_local_host_rejected(self):
        req = self._make_request(host="evil.com:8000")
        assert is_local_request(req) is False

    def test_empty_host_rejected(self):
        req = self._make_request(host="")
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

    def test_no_origin_header_passes_when_host_is_local(self):
        """Non-browser clients (curl) don't send Origin — allowed if Host is local."""
        req = self._make_request(host="localhost:8000", origin="")
        assert is_local_request(req) is True
