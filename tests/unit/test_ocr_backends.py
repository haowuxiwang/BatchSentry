"""M7 T7.1/T7.2 —— OCR 后端统一接口 + 能力声明 + docling 可选后端。

覆盖：
- OcrCapabilities / describe_backend 单源能力表（含未知后端保守兜底）；
- supports_slicing / supports_page_subset / is_backend_available 包装；
- _get_ocr_backend 对 OCR_BACKEND=docling 的处理（未装 → OcrBackendUnavailable）；
- _get_ocr_chain 的"缺失即降级"（docling 未装 → 回退 PaddleOCR，主链不受影响）；
- docling_client.run_ocr 的签名/输出形状/进度/取消（用假 converter，无需真实依赖）；
- 跨文件漂移护栏：能力判定不得再散落字符串字面量（单一来源不变式）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from config import config
from core.pipeline import ocr_support


REPO = Path(__file__).resolve().parents[2]


# ── 能力表 ──────────────────────────────────────────────────────

class TestCapabilities:
    def test_known_backends_covered(self):
        for name in ocr_support._KNOWN_BACKENDS:
            assert name in ocr_support._CAPABILITIES
            assert ocr_support.describe_backend(name).name == name

    def test_capabilities_keys_match_known(self):
        """能力表键集合 == _KNOWN_BACKENDS（防新增后端漏登记）。"""
        assert set(ocr_support._CAPABILITIES) == set(ocr_support._KNOWN_BACKENDS)

    def test_remote_backends_subset_of_known(self):
        assert set(ocr_support._REMOTE_BACKENDS) <= set(ocr_support._KNOWN_BACKENDS)
        assert "docling" not in ocr_support._REMOTE_BACKENDS

    def test_paddle_supports_page_subset_not_slicing(self):
        c = ocr_support.describe_backend("paddle")
        assert c.local is False and c.requires_token is True
        assert c.supports_slicing is False
        assert c.supports_page_subset is True

    def test_mineru_supports_slicing_and_subset(self):
        c = ocr_support.describe_backend("mineru")
        assert c.supports_slicing is True
        assert c.supports_page_subset is True
        assert c.local is False

    def test_docling_is_local_no_token_no_slicing(self):
        c = ocr_support.describe_backend("docling")
        assert c.local is True
        assert c.requires_token is False
        assert c.supports_slicing is False
        assert c.supports_page_subset is False

    def test_unknown_backend_conservative_default(self):
        c = ocr_support.describe_backend("nope")
        assert c.name == "nope"
        assert c.supports_slicing is False
        assert c.supports_page_subset is False
        assert c.requires_token is True
        assert c.local is False

    def test_describe_backend_case_insensitive(self):
        assert ocr_support.describe_backend("MiNeRu").name == "mineru"

    def test_wrappers(self):
        assert ocr_support.supports_slicing("mineru") is True
        assert ocr_support.supports_slicing("paddle") is False
        assert ocr_support.supports_page_subset("paddle") is True
        assert ocr_support.supports_page_subset("docling") is False


class TestAvailability:
    def test_paddle_mineru_always_available(self):
        assert ocr_support.is_backend_available("paddle") is True
        assert ocr_support.is_backend_available("mineru") is True

    def test_unknown_not_available(self):
        assert ocr_support.is_backend_available("nope") is False

    def test_docling_delegates_to_client(self, monkeypatch):
        monkeypatch.setattr("core.docling_client.is_available", lambda: True)
        assert ocr_support.is_backend_available("docling") is True
        monkeypatch.setattr("core.docling_client.is_available", lambda: False)
        assert ocr_support.is_backend_available("docling") is False


class TestRemoteBackendConfigured:
    def test_paddle_needs_url_and_token(self):
        cfg = config["paddle_ocr"]
        old = (cfg.api_url, cfg.token)
        try:
            cfg.api_url, cfg.token = "http://x", "tok"
            assert ocr_support.remote_backend_configured("paddle") is True
            cfg.token = ""
            assert ocr_support.remote_backend_configured("paddle") is False
            cfg.api_url = ""
            assert ocr_support.remote_backend_configured("paddle") is False
        finally:
            cfg.api_url, cfg.token = old

    def test_mineru_needs_token(self):
        old = config["mineru"].token
        try:
            config["mineru"].token = "tok"
            assert ocr_support.remote_backend_configured("mineru") is True
            config["mineru"].token = ""
            assert ocr_support.remote_backend_configured("mineru") is False
        finally:
            config["mineru"].token = old

    def test_docling_not_remote(self):
        assert ocr_support.remote_backend_configured("docling") is False


# ── _get_ocr_backend / _get_ocr_chain ───────────────────────────

class TestBackendSelection:
    def test_docling_unavailable_raises(self, monkeypatch):
        old = config["app"].ocr_backend
        monkeypatch.setattr("core.docling_client.is_available", lambda: False)
        config["app"].ocr_backend = "docling"
        try:
            with pytest.raises(ocr_support.OcrBackendUnavailable):
                ocr_support._get_ocr_backend()
        finally:
            config["app"].ocr_backend = old

    def test_docling_available_returns_client_run_ocr(self, monkeypatch):
        from core import docling_client

        old = config["app"].ocr_backend
        monkeypatch.setattr("core.docling_client.is_available", lambda: True)
        config["app"].ocr_backend = "docling"
        try:
            fn = ocr_support._get_ocr_backend()
            assert fn is docling_client.run_ocr
        finally:
            config["app"].ocr_backend = old

    def test_chain_degrades_when_docling_missing(self, monkeypatch):
        """核心验收：docling 未装 + OCR_BACKEND=docling → 主链回退 PaddleOCR。"""
        old = config["app"].ocr_backend
        old_paddle = (config["paddle_ocr"].api_url, config["paddle_ocr"].token)
        monkeypatch.setattr("core.docling_client.is_available", lambda: False)
        config["app"].ocr_backend = "docling"
        config["paddle_ocr"].api_url, config["paddle_ocr"].token = "", ""
        try:
            chain = ocr_support._get_ocr_chain()
            assert chain, "降级后链不能为空"
            assert chain[0][1] == "paddle"
        finally:
            config["app"].ocr_backend = old
            config["paddle_ocr"].api_url, config["paddle_ocr"].token = old_paddle

    def test_chain_docling_primary_with_remote_failover(self, monkeypatch):
        old = config["app"].ocr_backend
        old_paddle = (config["paddle_ocr"].api_url, config["paddle_ocr"].token)
        old_mineru = config["mineru"].token
        monkeypatch.setattr("core.docling_client.is_available", lambda: True)
        config["app"].ocr_backend = "docling"
        # 只保留 mineru 作为远程兜底（清掉 paddle，避免候选顺序不确定）
        config["paddle_ocr"].api_url, config["paddle_ocr"].token = "", ""
        config["mineru"].token = "tok"
        try:
            names = [n for _, n in ocr_support._get_ocr_chain()]
            assert names[0] == "docling"
            assert names[1:] == ["mineru"]  # 远程兜底
        finally:
            config["app"].ocr_backend = old
            config["paddle_ocr"].api_url, config["paddle_ocr"].token = old_paddle
            config["mineru"].token = old_mineru

    def test_chain_paddle_primary_mineru_failover(self, monkeypatch):
        old = config["app"].ocr_backend
        old_mineru = config["mineru"].token
        config["app"].ocr_backend = "paddle"
        config["mineru"].token = "tok"
        try:
            names = [n for _, n in ocr_support._get_ocr_chain()]
            assert names == ["paddle", "mineru"]
        finally:
            config["app"].ocr_backend = old
            config["mineru"].token = old_mineru


# ── docling_client ──────────────────────────────────────────────

class _FakeDoc:
    def __init__(self, n):
        self._n = n

    def num_pages(self):
        return self._n

    def export_to_markdown(self, page_no=None):
        return "FULL" if page_no is None else f"page-{page_no}"


class _FakeResult:
    def __init__(self, n):
        self.document = _FakeDoc(n)


class _FakeConverter:
    def __init__(self, n):
        self._n = n

    def convert(self, path):
        return _FakeResult(self._n)


class TestDoclingClient:
    def test_unavailable_raises(self, monkeypatch):
        from core import docling_client

        monkeypatch.setattr(docling_client, "is_available", lambda: False)
        with pytest.raises(ocr_support.OcrBackendUnavailable):
            docling_client.run_ocr("/tmp/x.pdf")

    def test_run_ocr_shape_and_progress(self, monkeypatch):
        from core import docling_client

        monkeypatch.setattr(docling_client, "is_available", lambda: True)
        monkeypatch.setattr(docling_client, "_load_converter", lambda: _FakeConverter(3))
        seen: list[tuple[int, int]] = []
        pages = docling_client.run_ocr(
            "/tmp/x.pdf", progress_callback=lambda d, t: seen.append((d, t))
        )
        assert len(pages) == 3
        assert [p["page_count"] for p in pages] == [1, 2, 3]
        assert all(p["_source"] == "docling" for p in pages)
        assert pages[0]["markdown"]["text"] == "page-1"
        assert seen == [(1, 3), (2, 3), (3, 3)]

    def test_run_ocr_cancel_raises(self, monkeypatch):
        from core import docling_client
        from core.ocr_client import OCRCancelled

        monkeypatch.setattr(docling_client, "is_available", lambda: True)
        monkeypatch.setattr(docling_client, "_load_converter", lambda: _FakeConverter(5))
        with pytest.raises(OCRCancelled):
            docling_client.run_ocr("/tmp/x.pdf", cancel_check=lambda: True)

    def test_run_ocr_falls_back_to_split_when_no_page_count(self, monkeypatch):
        from core import docling_client

        class _NoPagesDoc:
            def num_pages(self):
                return 0

            def export_to_markdown(self, page_no=None):
                return "A<!-- page break -->B"

        class _Conv:
            def convert(self, path):
                class _R:
                    document = _NoPagesDoc()
                return _R()

        monkeypatch.setattr(docling_client, "is_available", lambda: True)
        monkeypatch.setattr(docling_client, "_load_converter", lambda: _Conv())
        pages = docling_client.run_ocr("/tmp/x.pdf")
        assert [p["markdown"]["text"] for p in pages] == ["A", "B"]

    def test_num_pages_from_pages_dict(self):
        from core import docling_client

        class _D:
            pages = {1: object(), 2: object(), 3: object()}

        assert docling_client._num_pages(_D()) == 3

    def test_num_pages_from_list(self):
        from core import docling_client

        class _D:
            pages = [object(), object()]

        assert docling_client._num_pages(_D()) == 2

    def test_num_pages_unknown(self):
        from core import docling_client

        class _D:
            pass

        assert docling_client._num_pages(_D()) == 0

    def test_is_available_never_raises(self):
        from core import docling_client

        # 真实环境下 docling 未装 → False；无论如何不得抛异常。
        assert docling_client.is_available() in (True, False)

    def test_split_by_page_sep_requires_matching_total(self):
        from core import docling_client

        assert docling_client._split_by_page_sep("A<!-- page break -->B", 2) == ["A", "B"]
        # 段数与 total 不匹配 → 返回 []（调用方改走单页兜底）
        assert docling_client._split_by_page_sep("A<!-- page break -->B", 5) == []


# ── 跨文件漂移护栏（单一来源不变式）──────────────────────────────

_CAP_COMPARE = re.compile(r"==\s*[\"'](paddle|mineru|docling)[\"']")
_CAP_MEMBERSHIP = re.compile(r"in\s*\(\s*[\"'](paddle|mineru|docling)[\"']")


@pytest.mark.parametrize("rel", ["core/pipeline/engine.py", "core/pipeline/stage1.py"])
def test_no_capability_literals_in_pipeline(rel):
    """能力判定必须走 ocr_support（能力表单一来源），不得散落字符串字面量。

    注释行忽略（历史说明保留）。若新增后端时又写回 `== "paddle"`，
    本测试即失败 → 强制改走 describe_backend/supports_*。
    """
    offenders = []
    for i, line in enumerate((REPO / rel).read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        if _CAP_COMPARE.search(line) or _CAP_MEMBERSHIP.search(line):
            offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, "能力字面量漂移（应改用 ocr_support 能力表）：\n" + "\n".join(offenders)
