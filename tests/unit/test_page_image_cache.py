"""api/jobs/page_image.py 缓存与渲染缩放单测。

覆盖此前从未触发的：
- _get_pdf_doc：TTL 淘汰、容量淘汰（LRU）、缓存命中刷新时间戳
- _invalidate_pdf_doc：闭合句柄 + close 异常吞掉
- _pdf_render_zoom：超宽页缩放钳制
"""
from __future__ import annotations

import time

import pytest


class _FakeDoc:
    def __init__(self, fail_close: bool = False):
        self.closed = False
        self._fail_close = fail_close

    def close(self):
        if self._fail_close:
            raise RuntimeError("close boom")
        self.closed = True


@pytest.fixture(autouse=True)
def _clean_cache():
    import api.jobs.page_image as pi
    pi._pdf_doc_cache.clear()
    pi._doc_locks.clear()
    yield
    pi._pdf_doc_cache.clear()
    pi._doc_locks.clear()


class TestGetPdfDocCache:
    def test_open_and_cache_new_doc(self, tmp_path, monkeypatch):
        import api.jobs.page_image as pi
        fake = _FakeDoc()
        monkeypatch.setattr(pi.fitz, "open", lambda p: fake)
        doc = pi._get_pdf_doc("job-a", str(tmp_path / "a.pdf"))
        assert doc is fake
        assert "job-a" in pi._pdf_doc_cache

    def test_cache_hit_refreshes_timestamp(self, tmp_path):
        import api.jobs.page_image as pi
        fake = _FakeDoc()
        old_ts = time.time() - 100
        pi._pdf_doc_cache["job-b"] = (fake, old_ts)
        doc = pi._get_pdf_doc("job-b", str(tmp_path / "b.pdf"))
        assert doc is fake
        assert pi._pdf_doc_cache["job-b"][1] > old_ts

    def test_ttl_eviction_closes_stale(self, tmp_path, monkeypatch):
        import api.jobs.page_image as pi
        stale = _FakeDoc()
        pi._pdf_doc_cache["stale"] = (stale, time.time() - pi._PDF_CACHE_TTL - 60)
        fresh = _FakeDoc()
        monkeypatch.setattr(pi.fitz, "open", lambda p: fresh)
        pi._get_pdf_doc("job-c", str(tmp_path / "c.pdf"))
        assert stale.closed is True
        assert "stale" not in pi._pdf_doc_cache

    def test_capacity_eviction_removes_oldest(self, tmp_path, monkeypatch):
        import api.jobs.page_image as pi
        docs = {}
        now = time.time()
        for i in range(pi._PDF_CACHE_MAX):
            d = _FakeDoc()
            docs[f"j{i}"] = d
            pi._pdf_doc_cache[f"j{i}"] = (d, now - (pi._PDF_CACHE_MAX - i))
        newest = _FakeDoc()
        monkeypatch.setattr(pi.fitz, "open", lambda p: newest)
        pi._get_pdf_doc("j-new", str(tmp_path / "new.pdf"))
        # j0 最旧 → 被淘汰
        assert docs["j0"].closed is True
        assert "j0" not in pi._pdf_doc_cache
        assert "j-new" in pi._pdf_doc_cache

    def test_ttl_eviction_swallows_close_error(self, tmp_path, monkeypatch):
        import api.jobs.page_image as pi
        bad = _FakeDoc(fail_close=True)
        pi._pdf_doc_cache["bad"] = (bad, time.time() - pi._PDF_CACHE_TTL - 60)
        monkeypatch.setattr(pi.fitz, "open", lambda p: _FakeDoc())
        pi._get_pdf_doc("job-d", str(tmp_path / "d.pdf"))  # 不应抛异常
        assert "bad" not in pi._pdf_doc_cache


class TestInvalidatePdfDoc:
    def test_closes_and_drops(self):
        import api.jobs.page_image as pi
        doc = _FakeDoc()
        pi._pdf_doc_cache["jx"] = (doc, time.time())
        pi._invalidate_pdf_doc("jx")
        assert doc.closed is True
        assert "jx" not in pi._pdf_doc_cache

    def test_missing_key_is_noop(self):
        import api.jobs.page_image as pi
        pi._invalidate_pdf_doc("nope")  # 不应抛异常

    def test_close_error_is_swallowed(self):
        import api.jobs.page_image as pi
        pi._pdf_doc_cache["jy"] = (_FakeDoc(fail_close=True), time.time())
        pi._invalidate_pdf_doc("jy")  # close 抛异常应被吞掉
        assert "jy" not in pi._pdf_doc_cache


class TestPdfRenderZoom:
    def test_wide_page_zoom_clamped(self):
        import api.jobs.page_image as pi

        class _Page:
            rect = type("R", (), {"width": 4000.0})()

        # 4000pt 宽 → 2000/4000 = 0.5 < 1.5
        assert pi._pdf_render_zoom(_Page()) == 0.5

    def test_narrow_page_uses_default_zoom(self):
        import api.jobs.page_image as pi

        class _Page:
            rect = type("R", (), {"width": 500.0})()

        assert pi._pdf_render_zoom(_Page()) == pi._PDF_RENDER_ZOOM

    def test_zero_width_page_does_not_divide_by_zero(self):
        import api.jobs.page_image as pi

        class _Page:
            rect = type("R", (), {"width": 0.0})()

        assert pi._pdf_render_zoom(_Page()) == pi._PDF_RENDER_ZOOM
