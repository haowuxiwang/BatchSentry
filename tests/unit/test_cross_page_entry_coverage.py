"""core/rules/__init__.py 主入口边界补测。

补全 test_cross_page_analyzer.py 未覆盖的：
- progress_cb 抛异常时被吞掉（4 个里程碑上报点）
- OCR 手写低置信列信号页统计日志
- 空 page_structures 早退
"""
from __future__ import annotations

import pytest

from core.rules import analyze_cross_page


def _page(page_no: int, **data_extra):
    data = {
        "steps": [],
        "findings": [],
        "page_info": {},
        "event_year_groups": {},
    }
    data.update(data_extra)
    return {"page": page_no, "data": data}


@pytest.fixture(autouse=True)
def _stub_llm(monkeypatch):
    """屏蔽真实 LLM 调用：兜底与语义检查均返回空。"""
    from unittest.mock import AsyncMock
    monkeypatch.setattr("core.rules._llm_fallback_check", AsyncMock(return_value=[]))
    monkeypatch.setattr("core.rules._llm_based_check", AsyncMock(return_value=[]))


class TestAnalyzeCrossPageEntry:
    @pytest.mark.asyncio
    async def test_empty_pages_returns_empty(self):
        assert await analyze_cross_page([], job_id="t") == []

    @pytest.mark.asyncio
    async def test_progress_cb_exception_is_swallowed(self):
        """progress_cb 每次上报都抛异常 → 不影响主流程返回。"""
        calls = []

        async def bad_cb(done, total, label):
            calls.append((done, total, label))
            raise RuntimeError("cb boom")

        result = await analyze_cross_page([_page(1)], job_id="t", progress_cb=bad_cb)
        assert isinstance(result, list)
        # 4 个里程碑（规则校验 / LLM 兜底 / LLM 语义 / 完成）均被触发
        assert len(calls) == 4

    @pytest.mark.asyncio
    async def test_ocr_handwriting_signal_page_counted(self):
        """携带 _ocr_low_conf_cols 的页进入手写信号统计（不抛异常）。"""
        pages = [
            _page(1, _ocr_low_conf_cols=["批号"]),
            _page(2),
        ]
        result = await analyze_cross_page(pages, job_id="t")
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_value_source_counting_branches(self):
        """参数/测量单元格的 value_source 覆盖率统计遍历不抛异常。"""
        page = _page(1)
        page["data"]["steps"] = [{
            "step_no": 1,
            "parameters": [{"name": "温度", "value_source": "llm"}],
            "measurements": [{"values": {"1": {"value_source": "ocr"}}}],
        }]
        result = await analyze_cross_page([page], job_id="t")
        assert isinstance(result, list)
