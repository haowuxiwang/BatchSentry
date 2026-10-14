"""测试 create_job 的 **OCR 配置拦截**（TODO 0-33）—— 未配置 OCR 后端时拒绝上传。

与 `tests/integration/test_upload_llm_guard.py` **对称**：
LLM 门在 `api/jobs/upload.py` 的「未配置 LLM 服务商」分支，OCR 门紧随其后、
**写盘之前**。为什么必须有 OCR 门：批记录识别依赖 OCR，未配置时上传必然在
Stage 1 失败；实测样本 138MB / 51 页 ⇒ 用户白传 + 白等，且错误发生在中途。

判定复用**单一来源** `core.pipeline.ocr_support.ocr_chain_configured()`，
与上传页横幅（`main.py::index` 的 `ocr_setup_needed`）同源 ⇒ 不产生口径漂移。

⚠️ 本模块**显式**把 docling 探测关掉：`ocr_chain_configured()` 的判据是
「任一远程后端凭据完整 **或** docling 可用」。若测试机恰好装了 docling（可选
依赖），「未配置」场景就构造不出来 ⇒ 用例会**假绿/假红**。显式关掉后，判据只
由远程凭据决定，与冻结产物（不含 docling）一致。
"""
import asyncio
import io

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport


def _pdf_bytes() -> io.BytesIO:
    """一页含文字的最小合法 PDF（走 fitz，项目已有依赖）。"""
    import fitz
    doc = fitz.open()
    doc.new_page().insert_text((50, 50), "Test")
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    buf.seek(0)
    return buf


async def _drain_pipeline_tasks() -> None:
    """取消本测试启动的后台 pipeline task。

    test_db fixture 结束后 DB 连接会关闭；仍在跑的 task 会在 teardown 报
    "Cannot operate on a closed database"（异步竞态，非产品缺陷）。
    """
    from core import pipeline as pipeline_mod
    for _jid, tasks in list(pipeline_mod._pipeline_tasks.items()):
        for task in list(tasks):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):        # noqa: BLE001
                pass


def _clear_ocr_config() -> tuple[str, str, str]:
    """清空全部 OCR 凭据，返回原值（调用方负责恢复）。"""
    from config import config as _cfg
    saved = (_cfg["paddle_ocr"].api_url, _cfg["paddle_ocr"].token,
             _cfg["mineru"].token)
    _cfg["paddle_ocr"].api_url = ""
    _cfg["paddle_ocr"].token = ""
    _cfg["mineru"].token = ""
    return saved


def _restore_ocr_config(saved: tuple[str, str, str]) -> None:
    from config import config as _cfg
    _cfg["paddle_ocr"].api_url, _cfg["paddle_ocr"].token, _cfg["mineru"].token = saved


@pytest_asyncio.fixture
async def no_ocr_client(test_db, monkeypatch):
    """无 OCR 后端配置的客户端（清空 paddle api_url/token + mineru token）。"""
    # 让「docling 可用」这条路也不成立（否则判据恒 True，场景构造不出来）。
    monkeypatch.setattr("core.docling_client.is_available", lambda: False)
    saved = _clear_ocr_config()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=__import__("main").app),
            base_url="http://localhost:8000",
        ) as c:
            yield c
    finally:
        _restore_ocr_config(saved)


class TestUploadOcrGuard:
    """未配置 OCR 后端时上传应被拦截（与 LLM 门对称）。"""

    @pytest.mark.asyncio
    async def test_upload_rejected_without_ocr_config(self, no_ocr_client):
        """未配置任何 OCR 后端时，上传应返回 400 + 点名 OCR 的引导文案。"""
        files = {"file": ("test.pdf", _pdf_bytes(), "application/pdf")}
        r = await no_ocr_client.post("/api/jobs", files=files)
        assert r.status_code == 400
        detail = r.json().get("detail", "")
        assert "OCR" in detail, f"拒绝文案未点名 OCR：{detail!r}"

    @pytest.mark.asyncio
    async def test_upload_allowed_when_ocr_configured(self, test_client):
        """已配置 OCR（test_db 注入）时上传应正常进行（不是 400 拦截）。"""
        files = {"file": ("valid.pdf", _pdf_bytes(), "application/pdf")}
        r = await test_client.post("/api/jobs", files=files)
        assert r.status_code == 200, r.text[:200]
        assert "job_id" in r.json()
        await _drain_pipeline_tasks()

    @pytest.mark.asyncio
    async def test_llm_gate_precedes_ocr_gate(self, test_db, monkeypatch):
        """两门都未满足时先报 **LLM** —— 锁住「LLM 门在前」的顺序不变量。"""
        monkeypatch.setattr("core.docling_client.is_available", lambda: False)
        from config import config as _cfg
        saved_keys = {n: p.api_key for n, p in _cfg["providers"].items()}
        for p in _cfg["providers"].values():
            p.api_key = ""
        saved_ocr = _clear_ocr_config()
        try:
            async with AsyncClient(
                transport=ASGITransport(app=__import__("main").app),
                base_url="http://localhost:8000",
            ) as c:
                r = await c.post(
                    "/api/jobs",
                    files={"file": ("t.pdf", _pdf_bytes(), "application/pdf")},
                )
                assert r.status_code == 400
                assert "LLM" in r.json().get("detail", "")
        finally:
            for n, k in saved_keys.items():
                if n in _cfg["providers"]:
                    _cfg["providers"][n].api_key = k
            _restore_ocr_config(saved_ocr)

    @pytest.mark.asyncio
    async def test_ocr_gate_fires_when_llm_configured(self, test_db, monkeypatch):
        """LLM 已配置 + OCR 未配置 ⇒ 报 **OCR** —— 证明 OCR 门确实在生效。

        没有这条，「OCR 门是否存在」就只由 `test_upload_rejected_without_ocr_config`
        覆盖；那条在 LLM 未配置时同样会 400，无法区分「哪道门拦的」。
        """
        monkeypatch.setattr("core.docling_client.is_available", lambda: False)
        saved_ocr = _clear_ocr_config()
        try:
            async with AsyncClient(
                transport=ASGITransport(app=__import__("main").app),
                base_url="http://localhost:8000",
            ) as c:
                r = await c.post(
                    "/api/jobs",
                    files={"file": ("t.pdf", _pdf_bytes(), "application/pdf")},
                )
                assert r.status_code == 400
                assert "OCR" in r.json().get("detail", "")
        finally:
            _restore_ocr_config(saved_ocr)
