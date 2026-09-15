"""上传页数限额的端点级验证（POST /api/jobs）。

与 tests/unit/test_upload_limits.py 的分工：那里锁的是**判定语义**（纯函数，
不构造 PDF），这里锁的是**接线**——上限真的进入了拒绝/放行路径，且拒绝后
不留孤儿目录。两者缺一：只有纯函数测试，会出现"判定写好了但端点没调用"；
只有端点测试，则边界语义（等于上限/读取失败）要靠造多份 PDF 才能覆盖。

限额通过 ``patch.object(api.jobs.upload, "UPLOAD_LIMITS", ...)`` 注入小值 ——
真实上限是 200 页，造 201 页 PDF 既慢又无意义；判定接受显式 limits 参数正是
为此（见 config.check_upload_page_limits 的签名）。
"""
import pytest
import pytest_asyncio
from unittest.mock import patch
from httpx import AsyncClient, ASGITransport

from pathlib import Path

from config import config as _cfg


def _make_pdf(pages: int) -> bytes:
    """生成指定页数的真实 PDF bytes（每页带文本，避免被当空页）。"""
    import fitz

    doc = fitz.open()
    for i in range(pages):
        doc.new_page().insert_text((50, 50), f"Page {i + 1} content")
    data = doc.tobytes()
    doc.close()
    return data


def _limits(max_pages: int, warn_pages: int) -> dict:
    return {
        "max_bytes": 200 * 1024 * 1024,
        "max_pages": max_pages,
        "warn_pages": warn_pages,
        "sec_per_page_total": 32.7,
    }


@pytest_asyncio.fixture
async def client(test_db):
    from main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8000"
    ) as c:
        yield c


class TestPageLimitEnforcement:
    @pytest.mark.asyncio
    async def test_over_limit_is_rejected_with_actionable_message(self, client, test_db):
        """超页数上限 → 400，消息含真实页数/上限/下一步动作，且不落库、不留目录。

        对标成熟产品做法（amaise: "Too many pages (max 2,000) — split the PDF"）：
        拒绝必须可执行。反面教材是 Azure F0 只回前 2 页且不解释原因。
        """
        with patch.object(
            __import__("api.jobs.upload", fromlist=["x"]),
            "UPLOAD_LIMITS",
            _limits(max_pages=3, warn_pages=2),
        ), patch("api.jobs.launch_pipeline") as mock_pipe:
            r = await client.post(
                "/api/jobs",
                files={"file": ("big.pdf", _make_pdf(5), "application/pdf")},
            )
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "5" in detail, f"消息须含真实页数：{detail!r}"
        assert "3" in detail, f"消息须含上限：{detail!r}"
        assert "拆分" in detail, f"消息须给出可执行动作：{detail!r}"
        # 拒绝必须发生在启动流水线之前 —— 否则等于跑了才说不支持
        assert mock_pipe.call_count == 0
        # 不落库
        cur = await test_db.execute("SELECT COUNT(*) FROM jobs")
        assert (await cur.fetchone())[0] == 0
        # 不留孤儿目录（上传已写盘，拒绝路径必须清理）
        output_dir = Path(_cfg["app"].output_dir)
        assert list(output_dir.iterdir()) == [], "拒绝后残留了 job 目录"

    @pytest.mark.asyncio
    async def test_at_limit_is_accepted(self, client, test_db):
        """恰好等于上限必须放行 —— 上限是闭区间（边界语义由纯函数锁，这里证接线）。"""
        with patch.object(
            __import__("api.jobs.upload", fromlist=["x"]),
            "UPLOAD_LIMITS",
            _limits(max_pages=5, warn_pages=4),
        ), patch("api.jobs.launch_pipeline"):
            r = await client.post(
                "/api/jobs",
                files={"file": ("exact.pdf", _make_pdf(5), "application/pdf")},
            )
        assert r.status_code == 200, r.text
        assert r.json()["page_warning"], "等于上限必然超过软阈值 → 应带告警"

    @pytest.mark.asyncio
    async def test_large_document_returns_estimate_warning(self, client, test_db):
        """超软阈值 → 接受，但响应带预估耗时（放行不等于沉默）。"""
        with patch.object(
            __import__("api.jobs.upload", fromlist=["x"]),
            "UPLOAD_LIMITS",
            _limits(max_pages=50, warn_pages=2),
        ), patch("api.jobs.launch_pipeline"):
            r = await client.post(
                "/api/jobs",
                files={"file": ("many.pdf", _make_pdf(4), "application/pdf")},
            )
        assert r.status_code == 200, r.text
        warn = r.json()["page_warning"]
        assert "4" in warn and "预估" in warn and "分钟" in warn
        # 4 页 × 32.7s ≈ 2 分钟 —— 预估须由 sec_per_page_total 派生
        assert "2 分钟" in warn, f"预估未按先验计算：{warn!r}"

    @pytest.mark.asyncio
    async def test_small_document_has_no_warning(self, client, test_db):
        """常态文件不得带告警 —— 对常态刷告警等于没有告警。"""
        with patch.object(
            __import__("api.jobs.upload", fromlist=["x"]),
            "UPLOAD_LIMITS",
            _limits(max_pages=50, warn_pages=10),
        ), patch("api.jobs.launch_pipeline"):
            r = await client.post(
                "/api/jobs",
                files={"file": ("small.pdf", _make_pdf(3), "application/pdf")},
            )
        assert r.status_code == 200, r.text
        assert r.json()["page_warning"] is None

    @pytest.mark.asyncio
    async def test_page_count_read_failure_is_not_blocked(self, client, test_db):
        """页数读不出来时必须放行 —— 既有契约，新增上限不得把它变严。

        构造：PDF 文件头合法（通过 magic bytes 校验），但 fitz 打开失败。
        此时 pdf_page_count=0，云端 OCR 各有容错、可能仍能处理，拦掉等于把
        "可能成功"变成"必然失败"。
        """
        # ⚠️ PDF 必须在进入 patch 之前构造 —— 否则 _make_pdf 自己的 fitz.open()
        # 也会被下面的 patch 拦下（第一版就踩了这个坑）。
        payload = _make_pdf(3)
        with patch.object(
            __import__("api.jobs.upload", fromlist=["x"]),
            "UPLOAD_LIMITS",
            _limits(max_pages=1, warn_pages=1),  # 故意设成 1 页：若误按 0 判定就会拒
        ), patch("api.jobs.upload.fitz.open", side_effect=RuntimeError("xref broken")), \
                patch("api.jobs.launch_pipeline") as mock_pipe:
            r = await client.post(
                "/api/jobs",
                files={"file": ("damaged.pdf", payload, "application/pdf")},
            )
        assert r.status_code == 200, f"页数未知不应被拦截：{r.text}"
        assert mock_pipe.call_count == 1
        # 页数未知 → 不得凭空产生告警
        assert r.json()["page_warning"] is None
        cur = await test_db.execute("SELECT total_pages FROM jobs")
        assert (await cur.fetchone())[0] is None
