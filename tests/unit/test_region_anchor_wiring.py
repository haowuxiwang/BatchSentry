"""P0-3 区域级证据锚 —— 接线（写入 / 读取 / 呈现）的机检不变式。

纯函数本身由 `test_regions_anchor.py` 覆盖；本文件锁定**链路**：stage1 是否
真的把归一化区域落库、读取端是否与 SSR 共用同一推导、呈现端是否有宽高比
闸门。这些是"功能看着做了、实际没接上"最容易漏的地方。
"""
from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

STAGE1 = REPO / "core" / "pipeline" / "stage1.py"
REVIEW_PY = REPO / "api" / "review.py"
MAIN_PY = REPO / "main.py"
MINERU = REPO / "core" / "mineru_client.py"
REVIEW_JS = REPO / "static" / "review.js"
REVIEW_HTML = REPO / "templates" / "review.html"
SCHEMA_SQL = REPO / "db" / "schema.sql"

_REAL_MINERU_ZIP = REPO / "output" / "e7321b72-0f2" / "mineru_original.zip"


class TestWritePath:
    def test_stage1_persists_normalized_regions(self):
        src = STAGE1.read_text(encoding="utf-8")
        assert "from core.pipeline.regions import extract_regions" in src
        assert "regions_json" in src, "stage1 必须把区域写入 page_cache.regions_json"
        assert "extract_regions(page)" in src

    def test_stage1_never_writes_raw_bbox(self):
        """归一化必须是唯一出口：stage1 不得自行拼 bbox/坐标数字。"""
        src = STAGE1.read_text(encoding="utf-8")
        for forbidden in ("block_bbox", "parsing_res_list", "bbox[0]", "page_size"):
            assert forbidden not in src, (
                f"stage1 不得直接触碰原始坐标字段 {forbidden} —— "
                f"归一化收敛在 core.pipeline.regions 单点"
            )

    def test_backfill_does_not_overwrite_existing_text(self):
        """补写区域锚的 UPDATE 必须带空值守卫 —— 不得覆盖已有原文（证据链）。"""
        src = STAGE1.read_text(encoding="utf-8")
        assert "UPDATE page_cache SET regions_json" in src
        assert "regions_json IS NULL OR regions_json = ''" in src

    def test_schema_declares_regions_column_once(self):
        schema = SCHEMA_SQL.read_text(encoding="utf-8")
        assert "regions_json" in schema
        # 唯一声明处：迁移体只加列，不复制建表
        client_src = (REPO / "db" / "client.py").read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS page_cache" not in client_src

    def test_mineru_threads_layout_page_size(self):
        src = MINERU.read_text(encoding="utf-8")
        assert "_layout_page_sizes" in src, "MinerU 必须读 layout.json 的 page_size"
        assert "page_size" in src
        assert "_page_regions" in src
        # 无坐标系即不产出区域（宁缺勿错）
        assert 'if not space or not blocks:' in src


class TestReadPath:
    def test_api_and_ssr_share_one_anchor_function(self):
        """SSR 与 AJAX 必须共用同一推导 —— 两套实现必然漂移（翻页前后不一致）。"""
        for path in (REVIEW_PY, MAIN_PY):
            src = path.read_text(encoding="utf-8")
            assert "from core.pipeline.regions import region_anchor" in src, path.name
            assert 'f["region_ref"] = region_anchor(' in src, path.name

    def test_api_reuses_single_page_cache_scan(self):
        """锚所需 regions_json 必须搭已有那次 page_cache 扫描，不额外全表扫。"""
        src = REVIEW_PY.read_text(encoding="utf-8")
        assert "SELECT page, structured_json, regions_json FROM page_cache" in src

    def test_ssr_injects_anchor_map_into_ctx(self):
        """首屏 findings 由 SSR 渲染、不经过 AJAX —— 锚点必须随 ctx 注入，
        否则首屏"定位原图"按钮点了没反应（翻页后才生效的假缺陷）。"""
        main_src = MAIN_PY.read_text(encoding="utf-8")
        assert '"region_refs": region_refs' in main_src or \
            '"region_refs": region_refs,' in main_src
        html = REVIEW_HTML.read_text(encoding="utf-8")
        assert "region_refs: {{ region_refs | tojson }}" in html
        js = REVIEW_JS.read_text(encoding="utf-8")
        assert "ctx.region_refs" in js


class TestSurface:
    def test_html_has_overlay_and_button(self):
        html = REVIEW_HTML.read_text(encoding="utf-8")
        assert 'id="region-overlay"' in html
        assert 'id="pdf-page-wrap"' in html, "overlay 需要定位基准包装层"
        assert "locateFinding(event," in html, "SSR 首屏也要有定位入口"
        assert "{% if f.region_ref %}" in html, "锚不上时不得显示入口"

    def test_js_overlay_positioned_from_image_pixels_not_percent(self):
        """放大时 img 宽度会超过包装层 100% —— 用百分比定位会跑偏。"""
        js = REVIEW_JS.read_text(encoding="utf-8")
        assert "function positionRegionOverlay" in js
        assert "img.offsetWidth" in js and "img.offsetHeight" in js

    def test_js_has_aspect_gate(self):
        """宽高比闸门：坐标系与渲染图不一致时必须明示无法定位，而不是画错框。

        闸门比的是后端给出的 ``page_aspect``（页面**应当**具有的宽高比，
        已按服务端上报的 rotation 折算），不是 OCR 空间的 ``space_aspect``
        —— 后者在旋转页上必然与页面不符，拿它比对会把本来能正确映射的页
        也一并拒掉。
        """
        js = REVIEW_JS.read_text(encoding="utf-8")
        assert "REGION_ASPECT_TOL" in js
        assert "ref.page_aspect" in js
        assert "无法自动定位" in js
        # 画的是页面空间坐标，不是 OCR 空间原始框
        assert "ref.page_bbox" in js

    def test_js_recomputes_on_zoom_load_and_resize(self):
        js = REVIEW_JS.read_text(encoding="utf-8")
        # 缩放
        zoom_fn = js[js.index("function applyZoom"): js.index("function zoomPdf")]
        assert "positionRegionOverlay()" in zoom_fn
        # 图片加载完成 / 窗口尺寸变化
        assert "addEventListener(\"resize\", positionRegionOverlay)" in js
        assert "positionRegionOverlay();" in js

    def test_js_clears_anchor_on_page_change(self):
        js = REVIEW_JS.read_text(encoding="utf-8")
        assert "clearRegionAnchor()" in js
        # 翻页时必须清 —— 否则上一页的框会落到新页上
        idx = js.index("img.src = `/api/jobs/${jobId}/page/${targetPage}`")
        assert "clearRegionAnchor()" in js[max(0, idx - 400): idx]

    def test_js_exports_locate_finding(self):
        js = REVIEW_JS.read_text(encoding="utf-8")
        assert "window.locateFinding = locateFinding;" in js, (
            "按钮是内联 onclick，必须挂到 window"
        )


@pytest.mark.skipif(
    not _REAL_MINERU_ZIP.exists(), reason="真实 MinerU 产物不在场（可选用例）"
)
def test_real_mineru_artifact_replays():
    """真实 zip 复算：layout.json 取坐标系 + content_list 块 bbox → 归一化区域。

    实测形态：page_size 595×842、块字段 type/bbox/content；块 bbox 是
    [x0,y0,x1,y1] 且与 page_size 同源。
    """
    from core.mineru_client import (
        _layout_page_sizes,
        _split_pages_by_content_list,
    )
    from core.pipeline.regions import CANONICAL_LABELS, extract_regions

    with zipfile.ZipFile(_REAL_MINERU_ZIP) as zf:
        names = zf.namelist()
        sizes = _layout_page_sizes(zf, names)
        assert sizes, "真实产物必须能取到 page_size"
        assert all(w > 0 and h > 0 for w, h in sizes.values())

        cl = next(n for n in names if n.endswith("content_list_v2.json"))
        pages, n_tables, _ = _split_pages_by_content_list(zf, cl, sizes)

    assert pages, "真实产物必须能解析出页"
    with_regions = 0
    for page in pages:
        payload = extract_regions(page)
        if payload is None:
            continue
        with_regions += 1
        assert payload["backend"] == "mineru"
        assert payload["space"] == [595, 842]
        for r in payload["regions"]:
            assert r["label"] in CANONICAL_LABELS
            assert 0.0 <= r["bbox"][0] < r["bbox"][2] <= 1.0
            assert 0.0 <= r["bbox"][1] < r["bbox"][3] <= 1.0
    assert with_regions == len(pages), "带坐标系时每页都应产出区域"


@pytest.mark.skipif(
    not _REAL_MINERU_ZIP.exists(), reason="真实 MinerU 产物不在场（可选用例）"
)
def test_real_mineru_regions_anchor_a_real_finding():
    """端到端语义检查：真实区域文本能否锚住一条同源 finding。"""
    from core.mineru_client import _layout_page_sizes, _split_pages_by_content_list
    from core.pipeline.regions import extract_regions, region_anchor

    with zipfile.ZipFile(_REAL_MINERU_ZIP) as zf:
        names = zf.namelist()
        sizes = _layout_page_sizes(zf, names)
        cl = next(n for n in names if n.endswith("content_list_v2.json"))
        pages, _, _ = _split_pages_by_content_list(zf, cl, sizes)

    # 取第一页已抽出的区域文本作为"已知存在于该页"的证据
    payload = None
    for page in pages:
        payload = extract_regions(page)
        if payload:
            break
    assert payload is not None
    target = next(
        (r for r in payload["regions"] if "B2024001" in r["text"]), None
    )
    if target is None:
        pytest.skip("真实样本形态已变（未含批号块）")
    anchor = region_anchor("批号 B2024001 与记录不一致", payload)
    assert anchor is not None
    assert anchor["bbox"] == target["bbox"]
