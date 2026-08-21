"""OCR 输入规范化 + 整表降级 stub 识别测试（对抗审查 2026-08-20）。

覆盖：
- _prepare_ocr_pdf：超大页面盒（72dpi 错误嵌入）→ 300dpi 工作副本，原件保留
- _block_to_markdown：simple_table 等无表格结构字面量 → 识别为表格缺失
  （计数 + 占位），合法 HTML/管道表格不被误杀
- _has_missing_markers：自愈/空页验收的缺失标记检测
"""
import pytest
from pathlib import Path

import fitz


class TestPrepareOcrPdf:
    """OCR 提交前输入规范化（Stage 0）。"""

    def _make_pdf(self, path, pages=((595, 842), (3000, 4000))):
        """构造 PDF：每页按 (w,h)pt 建页并各嵌入一张同尺寸像素图
        （模拟扫描件 1px:1pt，即 72dpi 错误嵌入）。"""
        doc = fitz.open()
        for w, h in pages:
            page = doc.new_page(width=w, height=h)
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, int(w), int(h)), False)
            pix.clear_with(210)
            page.insert_image(page.rect, pixmap=pix)
        doc.save(str(path))
        doc.close()
        return path

    def test_normal_pdf_returns_original(self, tmp_path):
        from core.pipeline.ocr_support import _prepare_ocr_pdf

        pdf = self._make_pdf(str(tmp_path / "normal.pdf"), ((595, 842),))
        out_path, normalized = _prepare_ocr_pdf(str(pdf), "job-x")
        assert out_path == str(pdf)
        assert normalized == []

    def test_abnormal_media_box_renders_normalized_copy(self, tmp_path):
        from core.pipeline.ocr_support import _prepare_ocr_pdf

        src = self._make_pdf(str(tmp_path / "bad.pdf"), ((595, 842), (3000, 4000)))
        out_path, normalized = _prepare_ocr_pdf(str(src), "job-x")
        assert normalized == [2]
        assert Path(out_path).exists()
        assert out_path != str(src)

        # 副本第 2 页应按 300dpi 摆放：3000px -> 720pt（等于 3000*72/300）
        with fitz.open(out_path) as doc:
            assert doc.page_count == 2
            # 正常页原样保留
            assert abs(doc[0].rect.width - 595) < 1
            # 畸形页重渲染：720x960pt（约 10x13.3 in @300dpi），不再是 3000x4000
            assert abs(doc[1].rect.width - 720) < 1
            assert abs(doc[1].rect.height - 960) < 1

        # 原件未被修改（尺寸/字节不变）
        with fitz.open(src) as doc:
            assert abs(doc[1].rect.width - 3000) < 1

    def test_abnormal_pdf_keeps_normal_pages_verbatim(self, tmp_path):
        """正常页原样插入，避免无关页被重采样损失保真度。"""
        from core.pipeline.ocr_support import _prepare_ocr_pdf

        src = self._make_pdf(str(tmp_path / "mixed.pdf"), ((595, 842), (3000, 4000), (842, 595)))
        out_path, normalized = _prepare_ocr_pdf(str(src), "job-x")
        assert normalized == [2]
        with fitz.open(out_path) as doc:
            assert doc.page_count == 3
            assert abs(doc[0].rect.width - 595) < 1
            assert abs(doc[2].rect.width - 842) < 1
            assert abs(doc[2].rect.height - 595) < 1

    def test_invalid_pdf_falls_back_to_original(self, tmp_path):
        from core.pipeline.ocr_support import _prepare_ocr_pdf

        bad = tmp_path / "notreally.pdf"
        bad.write_bytes(b"%PDF-1.4 not a real pdf")
        out_path, normalized = _prepare_ocr_pdf(str(bad), "job-x")
        assert out_path == str(bad)
        assert normalized == []


class TestTableStubDetection:
    """MinerU 整表降级 stub 识别（_block_to_markdown 表分支）。"""

    def test_simple_table_literal_recognized_as_missing(self):
        """服务端把整表降级为字面量 simple_table 时，不能当正文输出。"""
        from core.mineru_client import _block_to_markdown

        assert _block_to_markdown({"type": "table", "html": "simple_table"}) == ""

    def test_stub_with_whitespace_recognized(self):
        from core.mineru_client import _block_to_markdown

        assert _block_to_markdown({"type": "table", "content": {"html": "  simple_table  "}}) == ""

    def test_plain_html_table_kept(self):
        from core.mineru_client import _block_to_markdown

        block = {
            "type": "table",
            "content": {
                "html": '<table><tr><td>温度</td><td>25.0</td></tr></table>'
            },
        }
        out = _block_to_markdown(block)
        assert "<table>" in out
        assert "温度" in out

    def test_markdown_pipe_table_kept(self):
        from core.mineru_client import _block_to_markdown

        block = {"type": "table", "content": {"table_markdown": "| 温度 | 实测 |\n|---|---|\n| 25.0 | 25.1 |"}}
        out = _block_to_markdown(block)
        assert "|" in out

    def test_compose_page_marks_stub_as_discarded(self):
        """stub 页走占位 + 计数路径，触发 OCR 不完整警告。"""
        from core.mineru_client import _compose_page_markdown

        blocks = [
            {"type": "page_header", "text": "丝裂霉素精制岗位清洗记录"},
            {"type": "table", "html": "simple_table"},
        ]
        md, discarded = _compose_page_markdown(48, blocks)
        assert discarded == 1
        assert "[表格内容提取失败 — OCR 结构缺失]" in md
        # 页头仍保留（页面信息不丢）
        assert "丝裂霉素" in md


class TestMissingMarkers:
    """自愈/空页验收的缺失标记检测。"""

    def test_stub_marker_detected(self):
        from core.pipeline.self_heal import _has_missing_markers

        assert _has_missing_markers("## 第 48 页\n\nsimple_table")
        assert _has_missing_markers("x [表格内容提取失败 — OCR 结构缺失] y")
        assert _has_missing_markers("（此页无文本内容）")

    def test_header_only_markdown_is_not_accepted_as_recovered_content(self):
        from core.pipeline.self_heal import _has_missing_markers

        assert _has_missing_markers("## 第 48 页\n### HISUN 海正药业\n### 丝裂霉素")

    def test_healthy_text_not_marked(self):
        from core.pipeline.self_heal import _has_missing_markers

        assert not _has_missing_markers("正常表格 <table><tr><td>温度 25.0</td></tr></table>")
        assert not _has_missing_markers("## 第 1 页\n\n普通段落内容")


class TestOcrIntegrityDiagnostics:
    """页存在不等于正文存在：必须识别页眉/页脚孤岛。"""

    def test_header_footer_only_is_incomplete(self):
        from core.mineru_client import _page_ocr_diagnostics
        from core.pipeline.ocr_support import assess_ocr_page

        blocks = [
            {"type": "page_header", "text": "丝裂霉素提取批记录"},
            {"type": "page_footer", "text": "第 48 页"},
        ]
        diag = _page_ocr_diagnostics(blocks, 0)
        page = {
            "markdown": {"text": "## 第 48 页\n### 丝裂霉素提取批记录"},
            "_ocr_diagnostics": diag,
            "_source": "mineru",
        }
        persisted, reasons = assess_ocr_page(page)

        assert persisted["integrity"] == "incomplete"
        assert persisted["header_footer_only"] is True
        assert any("未识别正文" in reason for reason in reasons)

    def test_short_meaningful_form_is_not_rejected_by_length(self):
        from core.pipeline.ocr_support import assess_ocr_page

        page = {
            "markdown": {"text": "## 第 5 页\n放罐通知单\n批号：1127011N250101"},
            "_ocr_diagnostics": {"meaningful_blocks": 2},
            "_source": "mineru",
        }
        persisted, reasons = assess_ocr_page(page)

        assert persisted["integrity"] == "ok"
        assert reasons == []


class TestPdfPageDiagnostics:
    """PDF 结构诊断（门禁 1d，_pdf_page_diagnostics）。"""

    def _make_pdf(self, path, pages=((595, 842), (3000, 4000))):
        """构造 PDF：每页嵌入同尺寸图像（模拟扫描件 72dpi）。"""
        doc = fitz.open()
        for w, h in pages:
            page = doc.new_page(width=w, height=h)
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, int(w), int(h)), False)
            pix.clear_with(180)
            page.insert_image(page.rect, pixmap=pix)
        doc.save(str(path))
        doc.close()
        return path

    def test_normal_pdf_diagnostic_fields(self, tmp_path):
        from core.pipeline.ocr_support import _pdf_page_diagnostics

        # 构造 300dpi 图像（像素 2479×3509 = 595pt×842pt @300dpi），页面盒 595×842pt
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 2479, 3509), False)
        pix.clear_with(180)
        page.insert_image(page.rect, pixmap=pix)
        doc.save(str(tmp_path / "normal.pdf"))
        doc.close()

        diags = _pdf_page_diagnostics(str(tmp_path / "normal.pdf"))
        assert len(diags) == 1
        d = diags[1]
        assert d["media_box_pt"] == [595.0, 842.0]
        assert d["rotation"] == 0
        assert abs(d["aspect_ratio"] - 595.0 / 842.0) < 0.001
        assert d["image_pixels"] == [2479, 3509]
        assert d["effective_dpi"] is not None
        assert d["effective_dpi"] >= 290  # ~300dpi
        assert d["low_dpi"] is False

    def test_large_box_abnormal_detected(self, tmp_path):
        """3000x4000pt 页面盒（异常嵌入 72dpi）→ 有效 DPI 很低。"""
        from core.pipeline.ocr_support import _pdf_page_diagnostics

        pdf = self._make_pdf(str(tmp_path / "bad.pdf"), ((3000, 4000),))
        diags = _pdf_page_diagnostics(str(pdf))
        d = diags[1]
        assert d["media_box_pt"] == [3000.0, 4000.0]
        # 图像像素 3000x4000，显示尺寸 3000x4000pt → DPI = 3000/(3000/72) = 72
        assert d["effective_dpi"] is not None
        assert d["effective_dpi"] < 150
        assert d["low_dpi"] is True

    def test_rotated_page_detected(self, tmp_path):
        from core.pipeline.ocr_support import _pdf_page_diagnostics

        doc = fitz.open()
        page = doc.new_page(width=842, height=595)
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 842, 595), False)
        pix.clear_with(180)
        page.insert_image(page.rect, pixmap=pix)
        page.set_rotation(90)
        doc.save(str(tmp_path / "rot.pdf"))
        doc.close()
        diags = _pdf_page_diagnostics(str(tmp_path / "rot.pdf"))
        d = diags[1]
        # page.rect 受 set_rotation 影响：90° 旋转后 rect 宽高互换
        # (842×595 → 595×842)
        assert d["rotation"] == 90
        assert abs(d["aspect_ratio"] - 595.0 / 842.0) < 0.001

    def test_text_only_page_no_dpi(self, tmp_path):
        """纯文本页（无图像）→ effective_dpi=None，不误判。"""
        from core.pipeline.ocr_support import _pdf_page_diagnostics

        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        # 不插入图像
        doc.save(str(tmp_path / "text.pdf"))
        doc.close()
        diags = _pdf_page_diagnostics(str(tmp_path / "text.pdf"))
        d = diags[1]
        assert d["effective_dpi"] is None
        assert d["low_dpi"] is False
        assert d["image_pixels"] == [0, 0]

    def test_invalid_pdf_returns_empty(self, tmp_path):
        from core.pipeline.ocr_support import _pdf_page_diagnostics

        bad = tmp_path / "not.pdf"
        bad.write_bytes(b"%PDF-1.4 not a real pdf")
        assert _pdf_page_diagnostics(str(bad)) == {}

    def test_assess_ocr_page_with_pdf_diag(self, tmp_path):
        """assess_ocr_page 合并 PDF 诊断并触发低 DPI reasons。"""
        from core.pipeline.ocr_support import assess_ocr_page

        page = {
            "markdown": {"text": "## 第 1 页\n温度 25.0"},
            "_ocr_diagnostics": {"source": "mineru"},
            "_source": "mineru",
        }
        pdf_diag = {
            "media_box_pt": [3000.0, 4000.0],
            "rotation": 0,
            "aspect_ratio": 0.75,
            "image_pixels": [3000, 4000],
            "effective_dpi": 72.0,
            "low_dpi": True,
            "low_dpi_reason": "有效 DPI 72 低于 150，识别质量可能不足",
        }
        diag, reasons = assess_ocr_page(page, pdf_diag)

        assert diag["effective_dpi"] == 72.0
        assert diag["low_dpi"] is True
        assert diag["media_box_pt"] == [3000.0, 4000.0]
        # 低 DPI 触发 incomplete
        assert diag["integrity"] == "incomplete"
        assert any("72" in r for r in reasons)

    def test_assess_ocr_page_box_abnormal_reason(self):
        """超常页面盒在 PDF 诊断中时生成原因。"""
        from core.pipeline.ocr_support import assess_ocr_page

        page = {"markdown": {"text": "正常内容"}, "_ocr_diagnostics": {}}
        pdf_diag = {
            "media_box_pt": [3000.0, 4000.0],
            "rotation": 0,
            "effective_dpi": 300.0,
            "low_dpi": False,
        }
        diag, reasons = assess_ocr_page(page, pdf_diag)
        assert any("异常" in r for r in reasons)

    def test_assess_ocr_page_footer_drop_threshold(self):
        """≥3 个纯数字页脚被过滤 → 完整性警告（误分类数据行可观测化）。"""
        from core.pipeline.ocr_support import assess_ocr_page

        page = {
            "markdown": {"text": "正常内容"},
            "_ocr_diagnostics": {"footer_dropped": 3},
        }
        diag, reasons = assess_ocr_page(page)
        assert diag["integrity"] == "incomplete"
        assert any("页脚" in r for r in reasons)
        # 1-2 个属正常页脚，不触发
        page2 = {
            "markdown": {"text": "正常内容"},
            "_ocr_diagnostics": {"footer_dropped": 2},
        }
        diag2, reasons2 = assess_ocr_page(page2)
        assert diag2["integrity"] == "ok"
        assert reasons2 == []


class TestSelfHealDiag:
    """自愈恢复页诊断保留 prior_diagnostics。"""

    def test_preserves_prior(self):
        from core.pipeline.self_heal import _self_heal_diag

        prior = {"source": "mineru", "integrity": "incomplete", "text_chars": 50}
        result = _self_heal_diag(prior)
        assert result is not None
        import json
        diag = json.loads(result)
        assert diag["self_healed"] is True
        assert diag["source"] == "mineru"
        assert diag["prior_diagnostics"]["integrity"] == "incomplete"
        assert diag["prior_diagnostics"]["text_chars"] == 50

    def test_no_prior_returns_none(self):
        from core.pipeline.self_heal import _self_heal_diag

        assert _self_heal_diag(None) is None
        assert _self_heal_diag({}) is None
