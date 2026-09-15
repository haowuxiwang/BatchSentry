"""P0-3 区域级证据锚 —— 读端（AJAX API + SSR 首屏）集成测试。

两层必须给出**同一套**锚：首屏由 SSR 渲染、翻页后由 AJAX 渲染，只要有一处
漏挂，就会出现"翻页前能定位、翻页后不能"（或反之）的假缺陷。
"""
import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# 归一化后的真实形态（Paddle 1440×1920 → aspect 0.75）
_PAYLOAD = {
    "backend": "paddle",
    "space": [1440, 1920],
    "space_aspect": 0.75,
    "regions": [
        {"label": "table", "bbox": [0.05, 0.05, 0.95, 0.5],
         "text": "T2101a 进料压力 0.16 MPa"},
        {"label": "text", "bbox": [0.05, 0.6, 0.3, 0.65],
         "text": "操作人 张三"},
    ],
}


def _by_prefix(findings: list[dict], prefix: str) -> dict:
    """按 description 前缀取条目（description 才是稳定的查找键）。"""
    for f in findings:
        if str(f.get("description") or "").startswith(prefix):
            return f
    raise AssertionError(f"未找到 description 以 {prefix!r} 开头的 finding")


def _extract_json_object(text: str, marker: str) -> dict:
    """从模板渲染结果里取出 ``marker`` 之后的那个 JSON 对象（花括号配平）。

    不能用 `index("}")` —— ctx 里的值是嵌套对象，首个 `}` 只是内层闭合，
    截出来的片段不是合法 JSON（会误判成"没注入"）。
    """
    start = text.index(marker) + len(marker)
    while text[start] in " \t":
        start += 1
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise AssertionError(f"未能在 {marker!r} 之后找到配平的 JSON 对象")


@pytest_asyncio.fixture
async def anchor_client(test_db):
    await test_db.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
        "VALUES (?, ?, ?, ?, ?)",
        ("anchor-job", "test.pdf", "/tmp/test.pdf", "review", 2),
    )
    await test_db.execute(
        "INSERT INTO page_cache (job_id, page, raw_html, regions_json) "
        "VALUES (?, ?, ?, ?)",
        ("anchor-job", 1, "<p>原文</p>", json.dumps(_PAYLOAD, ensure_ascii=False)),
    )
    # 第 2 页有原文但无区域（后端降级为纯文本拆分的情形）
    await test_db.execute(
        "INSERT INTO page_cache (job_id, page, raw_html) VALUES (?, ?, ?)",
        ("anchor-job", 2, "<p>第 2 页</p>"),
    )
    await test_db.executemany(
        "INSERT INTO findings (job_id, page, type, severity, source, "
        "description, ocr_text, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
        [
            # 命中区域 1（数值 0.16 在区域文本里）
            ("anchor-job", 1, "param_out_of_spec", "critical", "rule",
             "T2101a 进料压力 0.16 MPa 超出规格范围", "T2101a 进料压力 0.16 MPa"),
            # 无特征词 → 不锚（宁缺勿错）
            ("anchor-job", 1, "completeness", "info", "rule",
             "缺少复核人签名", ""),
            # 该页没有区域 → 不锚
            ("anchor-job", 2, "param_out_of_spec", "warning", "rule",
             "温度 42.1 超出规格", "温度 42.1"),
        ],
    )
    await test_db.commit()

    from main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8000"
    ) as client:
        yield client


class TestFindingsApiAnchor:
    @pytest.mark.asyncio
    async def test_anchor_returned_for_matching_finding(self, anchor_client):
        r = await anchor_client.get("/api/jobs/anchor-job/findings?page=1")
        assert r.status_code == 200
        hit = _by_prefix(r.json()["findings"], "T2101a")
        assert hit["region_ref"] is not None
        assert hit["region_ref"]["label"] == "table"
        assert hit["region_ref"]["bbox"] == [0.05, 0.05, 0.95, 0.5]
        assert hit["region_ref"]["space_aspect"] == 0.75
        assert hit["region_ref"]["backend"] == "paddle"

    @pytest.mark.asyncio
    async def test_no_anchor_when_nothing_matches(self, anchor_client):
        r = await anchor_client.get("/api/jobs/anchor-job/findings?page=1")
        miss = _by_prefix(r.json()["findings"], "缺少复核人")
        assert miss["region_ref"] is None

    @pytest.mark.asyncio
    async def test_no_anchor_when_page_has_no_regions(self, anchor_client):
        r = await anchor_client.get("/api/jobs/anchor-job/findings?page=2")
        assert r.status_code == 200
        assert r.json()["findings"][0]["region_ref"] is None

    @pytest.mark.asyncio
    async def test_anchor_coords_are_normalized(self, anchor_client):
        """禁止把未归一化坐标递给前端 —— 前端按 0..1 比例绘制。"""
        r = await anchor_client.get("/api/jobs/anchor-job/findings")
        for f in r.json()["findings"]:
            ref = f.get("region_ref")
            if not ref:
                continue
            assert all(0.0 <= v <= 1.0 for v in ref["bbox"]), ref


class TestSsrAnchor:
    @pytest.mark.asyncio
    async def test_ssr_page_renders_button_and_ctx_map(self, anchor_client):
        r = await anchor_client.get("/jobs/anchor-job/review?page=1")
        assert r.status_code == 200
        html = r.text
        assert 'id="region-overlay"' in html
        assert "locateFinding(event," in html, "首屏必须带定位入口"
        assert "region_refs:" in html, "首屏锚点必须随 ctx 注入"
        # ctx 里的锚与 API 一致（同一推导函数）
        refs = _extract_json_object(html, "region_refs: ")
        assert refs, "首屏 ctx 必须含锚点"
        ref = next(iter(refs.values()))
        assert ref["bbox"] == [0.05, 0.05, 0.95, 0.5]
        assert ref["space_aspect"] == 0.75

    @pytest.mark.asyncio
    async def test_ssr_page_without_regions_has_no_locate_button(self, anchor_client):
        r = await anchor_client.get("/jobs/anchor-job/review?page=2")
        assert r.status_code == 200
        assert "locateFinding(event," not in r.text, "锚不上时不得显示入口"
