"""GET /api/jobs/{id}/page/{n} 页面预览端点边界分支补测。

补全 test_api_image_upload.py 未触及的路径：
- 非本地 403、缺 job/缺 PDF 404、路径穿越 403、页码越界 404
- ETag 协商 304、stat 失败降级（仍返回图片）、打开失败 500、渲染失败 500
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client(test_db):
    from main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8000"
    ) as c:
        yield c


async def _insert_job(db, job_id: str, pdf_path: str) -> None:
    await db.execute(
        "INSERT INTO jobs (id, filename, status, pdf_path, total_pages) "
        "VALUES (?, ?, 'review', ?, 1)",
        (job_id, Path(pdf_path).name, pdf_path),
    )
    await db.commit()


def _make_pdf(path: Path) -> Path:
    import fitz
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 50), "batch record page")
    doc.save(str(path))
    doc.close()
    return path


class _FakeDoc:
    def __init__(self, page_count: int = 1, fail_load: bool = False):
        self.page_count = page_count
        self._fail_load = fail_load

    def load_page(self, idx):
        if self._fail_load:
            raise RuntimeError("render boom")
        raise AssertionError("unexpected load_page on fake doc")


class TestPageImageEndpointBranches:
    @pytest.mark.asyncio
    async def test_non_local_rejected(self, test_db):
        from main import app
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://evil.com:8000"
        ) as c:
            r = await c.get("/api/jobs/x/page/1")
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_unknown_job_returns_404(self, client):
        r = await client.get("/api/jobs/no-such-job/page/1")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_path_traversal_blocked(self, client, test_db, tmp_path):
        outside = _make_pdf(tmp_path / "outside.pdf")
        await _insert_job(test_db, "traversal", str(outside))
        r = await client.get("/api/jobs/traversal/page/1")
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_pdf_file_returns_404(self, client, test_db, tmp_path):
        ghost = tmp_path / "output" / "ghost.pdf"  # 不存在
        await _insert_job(test_db, "ghost", str(ghost))
        r = await client.get("/api/jobs/ghost/page/1")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_open_failure_returns_500(self, client, test_db, tmp_path, monkeypatch):
        pdf = _make_pdf(tmp_path / "output" / "openfail.pdf")
        await _insert_job(test_db, "openfail", str(pdf))

        def boom(job_id, path):
            raise RuntimeError("cannot open")

        monkeypatch.setattr("api.jobs._get_pdf_doc", boom)
        r = await client.get("/api/jobs/openfail/page/1")
        assert r.status_code == 500

    @pytest.mark.asyncio
    async def test_page_out_of_range_returns_404(self, client, test_db, tmp_path, monkeypatch):
        pdf = _make_pdf(tmp_path / "output" / "range.pdf")
        await _insert_job(test_db, "range", str(pdf))
        monkeypatch.setattr(
            "api.jobs._get_pdf_doc", lambda jid, p: _FakeDoc(page_count=2)
        )
        r = await client.get("/api/jobs/range/page/9")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_render_failure_returns_500(self, client, test_db, tmp_path, monkeypatch):
        pdf = _make_pdf(tmp_path / "output" / "renderfail.pdf")
        await _insert_job(test_db, "renderfail", str(pdf))
        monkeypatch.setattr(
            "api.jobs._get_pdf_doc", lambda jid, p: _FakeDoc(page_count=1, fail_load=True)
        )
        r = await client.get("/api/jobs/renderfail/page/1")
        assert r.status_code == 500

    @pytest.mark.asyncio
    async def test_etag_negotiation_returns_304(self, client, test_db, tmp_path):
        pdf = _make_pdf(tmp_path / "output" / "etag.pdf")
        await _insert_job(test_db, "etag", str(pdf))
        first = await client.get("/api/jobs/etag/page/1")
        assert first.status_code == 200
        etag = first.headers.get("etag")
        assert etag
        second = await client.get(
            "/api/jobs/etag/page/1", headers={"If-None-Match": etag}
        )
        assert second.status_code == 304

    @pytest.mark.asyncio
    async def test_stat_failure_still_renders_image(self, client, test_db, tmp_path, monkeypatch):
        """stat 抛 OSError → etag=None，仍返回图片（不走 304）。"""
        pdf = _make_pdf(tmp_path / "output" / "nostat.pdf")
        await _insert_job(test_db, "nostat", str(pdf))

        orig_stat = Path.stat

        def fake_stat(self, *a, **k):
            # 仅当调用方是 page_image 模块（ETag 计算处）才失败；resolve()
            # 与 exists() 内部的 stat 必须放行，否则 exists 会因 errno 过滤重抛。
            import inspect
            caller = inspect.currentframe().f_back
            if self.name == "nostat.pdf" and "page_image" in caller.f_code.co_filename:
                raise OSError(5, "stat denied")
            return orig_stat(self, *a, **k)

        monkeypatch.setattr(Path, "stat", fake_stat)
        r = await client.get("/api/jobs/nostat/page/1")
        assert r.status_code == 200
        assert r.headers.get("etag") is None
        assert r.headers["content-type"] == "image/jpeg"
