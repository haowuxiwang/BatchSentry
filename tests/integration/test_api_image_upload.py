"""API 集成测试 — 图片上传（Phase 13）：jpg/png/webp/bmp/tif 转 PDF 全链路。

设计要点：
- 与 test_api_jobs_upload.py 相同的 client/test_db fixture 模式
- 真实 Pillow 生成图片（含 EXIF 方向、截断文件、伪装扩展名等对抗样本）
- Mock `api.jobs.launch_pipeline` 避免真实 OCR/LLM 执行
- 覆盖：格式白名单、magic bytes 独立校验、EXIF 方向修正、像素上限防 DoS、
  解码失败友好 400、去重 409/force、转换 PDF 页面预览、报告导出、删除清理
"""
import io

import pytest
import pytest_asyncio
from unittest.mock import patch
from httpx import AsyncClient, ASGITransport

from PIL import Image

IMAGES = {
    "jpg": ("image/jpeg", lambda im: im.save(io.BytesIO(), format="JPEG")),
    "png": ("image/png", lambda im: im.save(io.BytesIO(), format="PNG")),
    "webp": ("image/webp", lambda im: im.save(io.BytesIO(), format="WEBP")),
    "bmp": ("image/bmp", lambda im: im.save(io.BytesIO(), format="BMP")),
    "tif": ("image/tiff", lambda im: im.save(io.BytesIO(), format="TIFF")),
}


def _make_image_bytes(fmt: str, size=(300, 200), exif_orientation=None) -> bytes:
    """生成真实图片 bytes（可指定 EXIF 方向）。fmt: jpg/png/webp/bmp/tif。"""
    img = Image.new("RGB", size, "white")
    buf = io.BytesIO()
    kwargs = {}
    if exif_orientation is not None:
        exif = img.getexif()
        exif[0x0112] = exif_orientation
        kwargs["exif"] = exif
    pil_fmt = {"jpg": "JPEG", "tif": "TIFF"}.get(fmt, fmt.upper())
    img.save(buf, format=pil_fmt, **kwargs)
    return buf.getvalue()


@pytest_asyncio.fixture
async def client(test_db):
    from main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8000"
    ) as c:
        yield c


class TestImageUpload:
    """POST /api/jobs — 图片上传转 PDF。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fmt", ["jpg", "png", "webp", "bmp", "tif"])
    async def test_upload_image_creates_job(self, client, test_db, fmt):
        """各格式图片上传应成功创建 job：pdf_path 指向转换 PDF、1 页、原图留档。"""
        ext = "jpeg" if fmt == "jpg" else fmt
        data = _make_image_bytes(fmt)
        with patch("api.jobs.launch_pipeline") as mock_pipe:
            r = await client.post(
                "/api/jobs",
                files={"file": (f"scan.{ext}", data, IMAGES[fmt][0])},
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "pending"
        assert body["filename"] == f"scan.{ext}"
        assert mock_pipe.call_count == 1
        job_id = mock_pipe.call_args.args[0]

        cursor = await test_db.execute(
            "SELECT filename, pdf_path, total_pages, status FROM jobs WHERE id = ?",
            (job_id,),
        )
        row = await cursor.fetchone()
        assert row["status"] == "pending"
        assert row["total_pages"] == 1
        # 转换 PDF 位于 job 目录，名为 {job_id}.pdf（避免同名源图冲突）
        assert row["pdf_path"].endswith(f"{job_id}.pdf")
        import os
        from config import config
        job_dir = os.path.join(config["app"].output_dir, job_id)
        assert os.path.exists(row["pdf_path"])
        assert os.path.exists(os.path.join(job_dir, f"scan.{ext}"))  # 原图留档

        # 转换 PDF 真实可读（1 页）
        import fitz
        with fitz.open(row["pdf_path"]) as doc:
            assert doc.page_count == 1

        # audit_log 记录 source=image
        cursor = await test_db.execute(
            "SELECT detail FROM audit_log WHERE job_id = ? AND action = 'pipeline_start'",
            (job_id,),
        )
        detail = (await cursor.fetchone())["detail"]
        assert "source=image" in detail

    @pytest.mark.asyncio
    async def test_upload_rejects_bad_magic_image(self, client):
        """伪装 .jpg 扩展名但文件头无效 → 400（magic bytes 独立于扩展名校验）。"""
        data = b"not an image at all, just text bytes padding"
        r = await client.post(
            "/api/jobs",
            files={"file": ("fake.jpg", data, "image/jpeg")},
        )
        assert r.status_code == 400
        assert "图片" in r.text

    @pytest.mark.asyncio
    async def test_upload_rejects_pdf_disguised_as_image(self, client):
        """PDF 内容伪装 .jpg 扩展名 → 400（图片 magic 校验拦截，不落入 PDF 路径）。"""
        import fitz
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((50, 50), "real pdf content")
        buf = io.BytesIO()
        doc.save(buf)
        doc.close()
        r = await client.post(
            "/api/jobs",
            files={"file": ("trick.jpg", buf.getvalue(), "image/jpeg")},
        )
        assert r.status_code == 400
        assert "图片" in r.text

    @pytest.mark.asyncio
    async def test_upload_exif_orientation_corrected(self, client, test_db):
        """EXIF 方向 6（竖拍）图片：转换 PDF 页面应为竖版（高 > 宽）。"""
        data = _make_image_bytes("jpg", size=(400, 200), exif_orientation=6)
        with patch("api.jobs.launch_pipeline") as mock_pipe:
            r = await client.post(
                "/api/jobs",
                files={"file": ("portrait.jpg", data, "image/jpeg")},
            )
        assert r.status_code == 200, r.text
        job_id = mock_pipe.call_args.args[0]
        cursor = await test_db.execute(
            "SELECT pdf_path FROM jobs WHERE id = ?", (job_id,)
        )
        pdf_path = (await cursor.fetchone())["pdf_path"]
        import fitz
        with fitz.open(pdf_path) as doc:
            assert doc.page_count == 1
            rect = doc[0].rect
        assert rect.height > rect.width, f"EXIF transpose failed: {rect}"

    @pytest.mark.asyncio
    async def test_upload_image_over_pixel_limit_rejected(self, client, test_db, monkeypatch):
        """像素超上限（防解码 DoS）→ 400 友好提示。"""
        from api.jobs import _MAX_IMAGE_PIXELS as orig
        monkeypatch.setattr("api.jobs._MAX_IMAGE_PIXELS", 40_000)  # 200x200 上限
        data = _make_image_bytes("png", size=(300, 300))
        try:
            r = await client.post(
                "/api/jobs",
                files={"file": ("big.png", data, "image/png")},
            )
        finally:
            monkeypatch.setattr("api.jobs._MAX_IMAGE_PIXELS", orig)
        assert r.status_code == 400
        assert "过大" in r.text

    @pytest.mark.asyncio
    async def test_upload_truncated_image_rejected(self, client):
        """截断图片（解码异常）→ 400 友好提示，不 500。"""
        full = _make_image_bytes("png", size=(100, 100))
        data = full[: len(full) // 2]  # 截半 → Pillow OSError
        r = await client.post(
            "/api/jobs",
            files={"file": ("broken.png", data, "image/png")},
        )
        assert r.status_code == 400
        assert "图片无法解析" in r.text

    @pytest.mark.asyncio
    async def test_upload_image_duplicate_409_and_force(self, client, test_db):
        """同图重复上传 → 409；force=1 → 创建新 job。"""
        data = _make_image_bytes("jpg")
        with patch("api.jobs.launch_pipeline"):
            r1 = await client.post(
                "/api/jobs", files={"file": ("dup.jpg", data, "image/jpeg")}
            )
            assert r1.status_code == 200
            r2 = await client.post(
                "/api/jobs", files={"file": ("dup.jpg", data, "image/jpeg")}
            )
            assert r2.status_code == 409
            r3 = await client.post(
                "/api/jobs",
                files={"file": ("dup.jpg", data, "image/jpeg")},
                params={"force": "1"},
            )
            assert r3.status_code == 200
            assert r3.json()["job_id"] != r1.json()["job_id"]

    @pytest.mark.asyncio
    async def test_image_job_page_preview(self, client, test_db):
        """图片 job 的转换 PDF 页面可预览渲染（PNG/JPEG 输出）。"""
        data = _make_image_bytes("jpg", size=(400, 300))
        with patch("api.jobs.launch_pipeline") as mock_pipe:
            r = await client.post(
                "/api/jobs", files={"file": ("prev.jpg", data, "image/jpeg")}
            )
            assert r.status_code == 200
        job_id = mock_pipe.call_args.args[0]
        r = await client.get(f"/api/jobs/{job_id}/page/1")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/")

    @pytest.mark.asyncio
    async def test_image_job_report_export(self, client, test_db):
        """图片 job 的报告导出端点可用。"""
        data = _make_image_bytes("jpg")
        with patch("api.jobs.launch_pipeline") as mock_pipe:
            r = await client.post(
                "/api/jobs", files={"file": ("rep.jpg", data, "image/jpeg")}
            )
            assert r.status_code == 200
        job_id = mock_pipe.call_args.args[0]
        r = await client.get(f"/api/jobs/{job_id}/report.md")
        assert r.status_code == 200
        assert "text/markdown" in r.headers["content-type"]

    @pytest.mark.asyncio
    async def test_upload_multipage_tiff_rejected(self, client):
        """多页 TIFF → 400（对抗性审查 H1：此前静默只保留首帧，扫描件
        多页被无声砍掉 — GMP 数据完整性风险）。"""
        img = Image.new("RGB", (100, 80), "white")
        buf = io.BytesIO()
        img.save(buf, format="TIFF", save_all=True, append_images=[img])
        data = buf.getvalue()
        r = await client.post(
            "/api/jobs",
            files={"file": ("multi.tif", data, "image/tiff")},
        )
        assert r.status_code == 400
        assert "多页" in r.text and "拆分" in r.text

    @pytest.mark.asyncio
    async def test_upload_animated_webp_rejected(self, client):
        """动画 WEBP → 400（对抗性审查 H3：与多页 TIFF 同源，只留首帧）。"""
        frames = []
        for color in ("red", "blue"):
            frames.append(Image.new("RGB", (100, 80), color))
        buf = io.BytesIO()
        frames[0].save(
            buf,
            format="WEBP",
            save_all=True,
            append_images=frames[1:],
            duration=100,
            loop=0,
        )
        data = buf.getvalue()
        r = await client.post(
            "/api/jobs",
            files={"file": ("anim.webp", data, "image/webp")},
        )
        assert r.status_code == 400
        assert "多页" in r.text and "拆分" in r.text

    @pytest.mark.asyncio
    async def test_upload_transparent_png_gets_white_background(self, client, test_db):
        """透明 PNG → 200 + 转换 PDF 白底合成（对抗性审查 H2：全透明黑像素
        之前直接变黑块，OCR 误认为内容）。"""
        img = Image.new("RGBA", (200, 150), (0, 0, 0, 0))  # 全透明黑
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        with patch("api.jobs.launch_pipeline") as mock_pipe:
            r = await client.post(
                "/api/jobs", files={"file": ("alpha.png", data, "image/png")}
            )
            assert r.status_code == 200, r.text
        job_id = mock_pipe.call_args.args[0]
        cursor = await test_db.execute(
            "SELECT pdf_path FROM jobs WHERE id = ?", (job_id,)
        )
        pdf_path = (await cursor.fetchone())["pdf_path"]
        import fitz
        with fitz.open(pdf_path) as doc:
            pix = doc[0].get_pixmap()
        sample = pix.pixel(pix.width // 2, pix.height // 2)
        assert sample == (255, 255, 255), f"expected white bg, got {sample}"

    @pytest.mark.asyncio
    async def test_upload_cmyk_jpeg_supported(self, client, test_db):
        """CMYK JPEG → 200 + convert("RGB") 路径（对抗性审查 M1 覆盖）。"""
        img = Image.new("CMYK", (120, 90), (0, 0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        data = buf.getvalue()
        with patch("api.jobs.launch_pipeline") as mock_pipe:
            r = await client.post(
                "/api/jobs", files={"file": ("cmyk.jpg", data, "image/jpeg")}
            )
            assert r.status_code == 200, r.text
        job_id = mock_pipe.call_args.args[0]
        import fitz
        cursor = await test_db.execute(
            "SELECT pdf_path FROM jobs WHERE id = ?", (job_id,)
        )
        pdf_path = (await cursor.fetchone())["pdf_path"]
        assert pdf_path.endswith(f"{job_id}.pdf")
        # 转换 PDF 真实可读（1 页）
        with fitz.open(pdf_path) as doc:
            assert doc.page_count == 1

    @pytest.mark.asyncio
    async def test_image_job_delete_cleans_converted_files(self, client, test_db):
        """删除图片 job：原图 + 转换 PDF + job 目录全部清理。"""
        import os
        from config import config
        data = _make_image_bytes("png")
        with patch("api.jobs.launch_pipeline") as mock_pipe:
            r = await client.post(
                "/api/jobs", files={"file": ("del.png", data, "image/png")}
            )
            assert r.status_code == 200
        job_id = mock_pipe.call_args.args[0]
        job_dir = os.path.join(config["app"].output_dir, job_id)
        assert os.path.isdir(job_dir)
        # 活跃 job 禁止删除（安全设计）— 先置终态再删
        await test_db.execute("UPDATE jobs SET status = 'review' WHERE id = ?", (job_id,))
        await test_db.commit()
        r = await client.delete(f"/api/jobs/{job_id}?keep_pdf=false")
        assert r.status_code == 200
        assert not os.path.exists(job_dir)
