"""M3 OCR 尺寸/形态鲁棒性回归（O1/O2/O3/O6）。

覆盖：
- `_box_geometry_reason` / `_normalize_zoom` / `_normalization_decision`
  的几何分类、缩放决策与边界；
- `_prepare_ocr_pdf` 对 O1 微型盒 / O2 极端长宽比 / O3 混合尺寸的
  工作副本归一化，且正常幅面与矢量微型页**不被重采样**；
- `_pdf_page_diagnostics` 新增 image_coverage / min_font_pt / small_font；
- `assess_ocr_page` 的 O6 小字号+低 DPI 联合标记与 O2 极端长宽比标记
  （"该页不得静默标记成功"）。

合成样本由 `scripts/gen_ocr_samples.py` 确定性构造（非包 → importlib）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import fitz
import pytest

_REPO = Path(__file__).resolve().parents[2]
_GEN = _REPO / "scripts" / "gen_ocr_samples.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


gen = _load("gen_ocr_samples", _GEN)


# ── 纯函数：几何分类与缩放决策 ────────────────────────────────────────
class TestBoxGeometryReason:
    def test_normal_band_returns_none(self):
        from core.pipeline.ocr_support import _box_geometry_reason

        assert _box_geometry_reason(595, 842) is None      # A4
        assert _box_geometry_reason(842, 595) is None      # A4 横向
        assert _box_geometry_reason(842, 1191) is None     # A3（正常印刷幅面）
        assert _box_geometry_reason(300, 842) is None      # 长边在带内

    def test_small_box_below_lower_bound(self):
        from core.pipeline.ocr_support import _box_geometry_reason

        assert _box_geometry_reason(200, 120) == "small_box"
        assert _box_geometry_reason(299, 120) == "small_box"

    def test_large_box_above_upper_bound(self):
        from core.pipeline.ocr_support import _box_geometry_reason

        assert _box_geometry_reason(3000, 4000) == "large_box"
        assert _box_geometry_reason(1601, 842) == "large_box"

    def test_extreme_aspect_takes_precedence(self):
        from core.pipeline.ocr_support import _box_geometry_reason

        # 8:1 且长边 >1600 → 先判极端长宽比
        assert _box_geometry_reason(250, 2000) == "extreme_aspect"
        assert _box_geometry_reason(2000, 250) == "extreme_aspect"
        # 4:1 边界
        assert _box_geometry_reason(250, 1000) == "extreme_aspect"

    def test_degenerate_dims_return_none(self):
        from core.pipeline.ocr_support import _box_geometry_reason

        assert _box_geometry_reason(0, 0) is None
        assert _box_geometry_reason(-5, 100) is None


class TestNormalizeZoom:
    def test_small_box_upscales_to_target_dpi(self):
        from core.pipeline.ocr_support import _normalize_zoom

        z = _normalize_zoom(200, 120, "small_box")
        assert 4.1 <= z <= 4.2            # 300/72 ≈ 4.1667

    def test_large_box_never_upscales(self):
        from core.pipeline.ocr_support import _normalize_zoom

        assert _normalize_zoom(3000, 4000, "large_box") == 1.0
        # 长边超 4096px 上限时按 cap 缩小
        assert abs(_normalize_zoom(5000, 6000, "large_box") - 4096 / 6000) < 1e-6

    def test_extreme_aspect_raises_short_side_within_cap(self):
        from core.pipeline.ocr_support import _normalize_zoom

        z = _normalize_zoom(250, 2000, "extreme_aspect")
        assert abs(z - 8192 / 2000) < 1e-6   # 受 8192px 长边上限约束
        # 超细条（80:1）：短边无法达 1024px，退化为长边上限
        z2 = _normalize_zoom(100, 8000, "extreme_aspect")
        assert abs(z2 - 8192 / 8000) < 1e-6

    def test_degenerate_returns_one(self):
        from core.pipeline.ocr_support import _normalize_zoom

        assert _normalize_zoom(0, 0, "small_box") == 1.0


class TestIsExtremeAspect:
    @pytest.mark.parametrize("ar,expected", [
        (4.0, True), (8.0, True), (0.25, True), (3.99, False),
        (0.26, False), (None, False), (0, False), ("x", False),
    ])
    def test_boundaries(self, ar, expected):
        from core.pipeline.ocr_support import _is_extreme_aspect

        assert _is_extreme_aspect(ar) is expected


class TestNormalizationDecision:
    def test_normal_page_not_normalized(self):
        from core.pipeline.ocr_support import _normalization_decision

        assert _normalization_decision(595, 842, True) == (False, None, 1.0)

    def test_small_raster_page_normalized(self):
        from core.pipeline.ocr_support import _normalization_decision

        needs, reason, zoom = _normalization_decision(200, 120, True)
        assert needs is True and reason == "small_box" and zoom > 1.0

    def test_small_vector_page_preserved(self):
        """矢量/文本微型页保留保真 → 不重渲染。"""
        from core.pipeline.ocr_support import _normalization_decision

        assert _normalization_decision(200, 120, False) == (False, None, 1.0)

    def test_large_box_normalized_regardless_of_raster(self):
        from core.pipeline.ocr_support import _normalization_decision

        needs, reason, zoom = _normalization_decision(3000, 4000, False)
        assert needs is True and reason == "large_box"


# ── O1：微型页面盒放大到目标 DPI ──────────────────────────────────────
class TestO1SmallBox:
    def test_small_raster_page_upsampled_density(self, tmp_path):
        from core.pipeline.ocr_support import (
            _pdf_page_diagnostics,
            _prepare_ocr_pdf,
        )

        src = gen.small_box_pdf(tmp_path / "o1.pdf")
        out_path, normalized = _prepare_ocr_pdf(str(src), "job-o1")

        assert normalized == [1]
        assert Path(out_path).exists() and out_path != str(src)

        with fitz.open(out_path) as doc:
            # 页盒尺寸不变（放大后按 300dpi 摆放回原幅面）
            assert abs(doc[0].rect.width - 200) < 2
            assert abs(doc[0].rect.height - 120) < 2

        # 密度提升到目标 DPI（不再整页稀疏）
        diag = _pdf_page_diagnostics(out_path)[1]
        assert diag["effective_dpi"] is not None
        assert 250 <= diag["effective_dpi"] <= 350
        assert diag["low_dpi"] is False

    def test_vector_micro_page_untouched(self, tmp_path):
        from core.pipeline.ocr_support import _prepare_ocr_pdf

        src = gen.small_text_only_pdf(tmp_path / "vec.pdf")
        out_path, normalized = _prepare_ocr_pdf(str(src), "job-vec")
        assert normalized == []
        assert out_path == str(src)


# ── O2：极端长宽比 ────────────────────────────────────────────────────
class TestO2ExtremeAspect:
    def test_extreme_aspect_short_side_preserved(self, tmp_path):
        from core.pipeline.ocr_support import (
            _pdf_page_diagnostics,
            _prepare_ocr_pdf,
        )

        src = gen.extreme_aspect_pdf(tmp_path / "o2.pdf")
        out_path, normalized = _prepare_ocr_pdf(str(src), "job-o2")

        assert normalized == [1]
        diag = _pdf_page_diagnostics(out_path)[1]
        # 短边像素被抬到 1024px 下限（正文不被压碎）
        assert diag["image_pixels"][0] >= 1024
        # 长边受 8192px 上限约束
        assert diag["image_pixels"][1] <= 8192
        # 长宽比保持（8:1）
        with fitz.open(out_path) as doc:
            ar = doc[0].rect.width / doc[0].rect.height
        assert abs((1 / ar) - 8) < 0.3


# ── O3：混合尺寸按目标密度统一 ────────────────────────────────────────
class TestO3MixedSize:
    def test_only_outliers_normalized(self, tmp_path):
        from core.pipeline.ocr_support import _prepare_ocr_pdf

        src = gen.mixed_size_pdf(tmp_path / "o3.pdf")
        out_path, normalized = _prepare_ocr_pdf(str(src), "job-o3")

        assert normalized == [2]           # 仅超大盒页
        with fitz.open(out_path) as doc:
            assert doc.page_count == 3
            # 正常幅面原样保留
            assert abs(doc[0].rect.width - 595) < 1
            assert abs(doc[2].rect.width - 842) < 1
            # 超大盒 → 300dpi 等效真实尺寸（3000*72/300=720）
            assert abs(doc[1].rect.width - 720) < 1
            assert abs(doc[1].rect.height - 960) < 1

    def test_all_normal_returns_original(self, tmp_path):
        from core.pipeline.ocr_support import _prepare_ocr_pdf

        src = gen.normal_pdf(tmp_path / "normal.pdf")
        out_path, normalized = _prepare_ocr_pdf(str(src), "job-n")
        assert normalized == []
        assert out_path == str(src)


# ── O6：小字号 + 低 DPI 联合标记 + O2 标记 ────────────────────────────
class TestO6AndFlags:
    def test_diagnostics_expose_font_and_coverage(self, tmp_path):
        from core.pipeline.ocr_support import _pdf_page_diagnostics

        src = gen.small_font_low_dpi_pdf(tmp_path / "o6.pdf")
        d = _pdf_page_diagnostics(str(src))[1]
        assert d["low_dpi"] is True
        assert d["small_font"] is True
        assert d["min_font_pt"] is not None and d["min_font_pt"] < 6
        assert d["image_coverage"] > 0.9

    def test_joint_marking_blocks_silent_success(self, tmp_path):
        from core.pipeline.ocr_support import (
            _pdf_page_diagnostics,
            _prepare_ocr_pdf,
            assess_ocr_page,
        )

        src = gen.small_font_low_dpi_pdf(tmp_path / "o6b.pdf")
        # 几何正常 → 不重渲染，质量证据保留给 assess
        out_path, normalized = _prepare_ocr_pdf(str(src), "job-o6")
        assert normalized == [] and out_path == str(src)

        pdf_diag = _pdf_page_diagnostics(str(src))[1]
        page = {
            "markdown": {"text": "## 第 1 页\n批号 1127011N250101 温度 25.0"},
            "_ocr_diagnostics": {"source": "mineru"},
        }
        diag, reasons = assess_ocr_page(page, pdf_diag)

        assert diag["integrity"] == "incomplete"
        assert any("小字号" in r and "低 DPI" in r for r in reasons)

    def test_low_dpi_without_small_font_uses_dpi_reason(self):
        from core.pipeline.ocr_support import assess_ocr_page

        page = {"markdown": {"text": "正常内容"}, "_ocr_diagnostics": {}}
        pdf_diag = {
            "media_box_pt": [595.0, 842.0], "aspect_ratio": 0.707,
            "effective_dpi": 72.0, "low_dpi": True,
            "low_dpi_reason": "有效 DPI 72 低于 150，识别质量可能不足",
        }
        diag, reasons = assess_ocr_page(page, pdf_diag)
        assert diag["integrity"] == "incomplete"
        assert any("72" in r for r in reasons)
        assert not any("小字号" in r for r in reasons)

    def test_small_font_without_low_dpi_soft_flag(self):
        from core.pipeline.ocr_support import assess_ocr_page

        page = {"markdown": {"text": "正常内容"}, "_ocr_diagnostics": {}}
        pdf_diag = {
            "media_box_pt": [595.0, 842.0], "aspect_ratio": 0.707,
            "effective_dpi": 300.0, "low_dpi": False,
            "small_font": True, "min_font_pt": 5.0,
        }
        diag, reasons = assess_ocr_page(page, pdf_diag)
        assert diag["integrity"] == "incomplete"
        assert any("字号" in r for r in reasons)

    def test_extreme_aspect_marked_for_manual_review(self):
        from core.pipeline.ocr_support import assess_ocr_page

        page = {"markdown": {"text": "正常内容"}, "_ocr_diagnostics": {}}
        pdf_diag = {"media_box_pt": [250.0, 1000.0], "aspect_ratio": 0.25}
        diag, reasons = assess_ocr_page(page, pdf_diag)
        assert diag["integrity"] == "incomplete"
        assert diag["extreme_aspect"] is True
        assert any("长宽比" in r for r in reasons)

    def test_extreme_aspect_suppresses_redundant_box_warning(self):
        """极端长宽比页不再重复告警"页面盒异常"（同属几何异常）。"""
        from core.pipeline.ocr_support import assess_ocr_page

        page = {"markdown": {"text": "正常内容"}, "_ocr_diagnostics": {}}
        pdf_diag = {"media_box_pt": [245.8, 1966.1], "aspect_ratio": 0.125}
        diag, reasons = assess_ocr_page(page, pdf_diag)
        assert diag["extreme_aspect"] is True
        assert any("长宽比" in r for r in reasons)
        assert not any("页面盒异常" in r for r in reasons)

    def test_extreme_aspect_boundary_not_flagged(self):
        from core.pipeline.ocr_support import assess_ocr_page

        page = {"markdown": {"text": "正常内容"}, "_ocr_diagnostics": {}}
        pdf_diag = {"media_box_pt": [595.0, 1600.0], "aspect_ratio": 0.372}
        diag, reasons = assess_ocr_page(page, pdf_diag)
        assert diag.get("extreme_aspect") is None
        assert not any("长宽比" in r for r in reasons)


class TestPageMinFont:
    def test_text_layer_min_font(self, tmp_path):
        from core.pipeline.ocr_support import _page_min_font_pt

        src = gen.small_text_only_pdf(tmp_path / "t.pdf")
        with fitz.open(str(src)) as doc:
            assert _page_min_font_pt(doc[0]) == pytest.approx(10.0, abs=0.5)

    def test_image_only_page_has_no_font(self, tmp_path):
        from core.pipeline.ocr_support import _page_min_font_pt

        src = gen.normal_pdf(tmp_path / "i.pdf")
        with fitz.open(str(src)) as doc:
            assert _page_min_font_pt(doc[0]) is None


class _StubPage:
    """最小页替身：只为覆盖 _page_image_stats / _page_min_font_pt 的
    边界与异常分支，避免为每个畸形结构真实构造 PDF。"""

    def __init__(self, images=None, text=None, rect=(595, 842)):
        self._images = images or []
        self._text = text
        self._rect = rect

    @property
    def rect(self):
        return fitz.Rect(0, 0, self._rect[0], self._rect[1])

    def get_image_info(self, xrefs=True):
        return self._images

    def get_text(self, kind):
        if isinstance(self._text, Exception):
            raise self._text
        return self._text or {"blocks": []}


class TestRobustnessHelperEdges:
    def test_min_font_returns_none_on_text_error(self):
        from core.pipeline.ocr_support import _page_min_font_pt

        assert _page_min_font_pt(_StubPage(text=RuntimeError("boom"))) is None

    def test_min_font_ignores_blank_and_sizeless_spans(self):
        from core.pipeline.ocr_support import _page_min_font_pt

        page = _StubPage(text={"blocks": [
            {"lines": [{"spans": [
                {"text": "   ", "size": 4},
                {"text": "x", "size": None},
                {"text": "y", "size": 0},
            ]}]},
        ]})
        assert _page_min_font_pt(page) is None

    def test_min_font_picks_smallest_nonblank_span(self):
        from core.pipeline.ocr_support import _page_min_font_pt

        page = _StubPage(text={"blocks": [
            {"lines": [{"spans": [{"text": "温度", "size": 12.4}]}]},
            {"lines": [{"spans": [{"text": "实测", "size": 5.2}]}]},
        ]})
        assert _page_min_font_pt(page) == pytest.approx(5.2)

    def test_image_stats_skips_degenerate_images(self):
        from core.pipeline.ocr_support import _page_image_stats

        page = _StubPage(images=[
            {"width": 0, "height": 0},
            {"width": 10, "height": 10, "bbox": (0, 0, 0, 0)},
        ])
        assert _page_image_stats(page) == {
            "image_pixels": [0, 0], "image_coverage": 0.0, "effective_dpi": None,
        }

    def test_image_stats_reports_coverage_and_dpi(self):
        from core.pipeline.ocr_support import _page_image_stats

        # 595x842pt 页内铺满 595x842px 图 → 覆盖率 1.0、有效 DPI = 72
        page = _StubPage(images=[{"width": 595, "height": 842, "bbox": (0, 0, 595, 842)}])
        stats = _page_image_stats(page)
        assert stats["image_pixels"] == [595, 842]
        assert stats["image_coverage"] == 1.0
        assert stats["effective_dpi"] == pytest.approx(72.0, abs=0.5)


class TestGeneratorBuildAll:
    def test_build_all_writes_deterministic_samples(self, tmp_path):
        files = gen.build_all(tmp_path / "s")
        assert set(files) == {
            "o1_small_box", "o2_extreme_aspect", "o3_mixed_size",
            "o3_normal", "o6_small_font_low_dpi", "guard_small_text_only",
        }
        for p in files.values():
            assert Path(p).exists() and Path(p).stat().st_size > 0

    def test_structurally_deterministic(self, tmp_path):
        """确定性：两次生成结构一致（可作最小回归基线）。

        PDF 容器字节含时间/ID，故断言结构特征（页面盒/像素/字号）而非
        字节级相等。
        """
        from core.pipeline.ocr_support import _pdf_page_diagnostics

        a = gen.build_all(tmp_path / "a")
        b = gen.build_all(tmp_path / "b")
        for name in a:
            da, db = _pdf_page_diagnostics(a[name]), _pdf_page_diagnostics(b[name])
            assert set(da) == set(db)
            for pno in da:
                assert da[pno]["media_box_pt"] == db[pno]["media_box_pt"]
                assert da[pno]["image_pixels"] == db[pno]["image_pixels"]
                assert da[pno]["min_font_pt"] == db[pno]["min_font_pt"]
