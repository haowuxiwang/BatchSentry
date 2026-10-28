"""判定产出采样温度**单一真值**守卫（R85 第二十五批）。

背景（`docs/TODO.md` 0-15）：同输入 / 同产物 / 同后端 / 同模型，两次跑
findings **41 vs 51（+24%）**。根因是判定产出路径用 `temperature=0.1`
—— 采样随机性逐页累积。修法：单一真值 `llm.client.DETERMINISTIC_TEMPERATURE
= 0.0`，页面分析（page_analyzer）与跨页语义检查（rules.llm_checks）共用。

本文件锁定三件事（"用例存在" ≠ "断言有效"：把任一处改回 0.1 必须变红）：
  1. 常量本身 = 0.0；
  2. `analyze_page` **真的**把该常量传给 LLM（行为断言，非源码扫描）；
  3. `_llm_fallback_check` **真的**把该常量传给 LLM（行为断言）；
  4. 两个判定产出模块内**不得**再出现裸 temperature 数字字面量（源码扫描，
     兜住日后新增的调用点）。
"""
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm.client import DETERMINISTIC_TEMPERATURE

_REPO = Path(__file__).resolve().parents[2]
_PRODUCERS = ("core/page_analyzer.py", "core/rules/llm_checks.py")


def test_constant_is_zero():
    """判定产出必须是 greedy 解码（0.0）—— 调高即把运行间方差放回来。"""
    assert DETERMINISTIC_TEMPERATURE == 0.0


@pytest.mark.asyncio
async def test_analyze_page_passes_deterministic_temperature():
    """`analyze_page` 传给 LLM 的温度必须等于单一真值。"""
    from core.page_analyzer import analyze_page

    mock_client = MagicMock()
    mock_client.chat_json = AsyncMock(
        return_value={"page_info": {"title": "x"}, "steps": []}
    )
    with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
        await analyze_page("<table></table>", page_num=1)

    kwargs = mock_client.chat_json.await_args.kwargs
    assert kwargs["temperature"] == DETERMINISTIC_TEMPERATURE


@pytest.mark.asyncio
async def test_llm_fallback_check_passes_deterministic_temperature():
    """`_llm_fallback_check` 传给 LLM 的温度必须等于单一真值。"""
    from core.rules.llm_checks import _llm_fallback_check

    mock_client = MagicMock()
    mock_client.chat_json = AsyncMock(
        return_value=[{"index": 1, "in_spec": True, "reason": "ok"}]
    )
    queue = [
        {"page": 1, "step_no": 1, "time": "08:00", "name": "温度",
         "spec": "26-30", "actual": "28", "unit": "℃"},
    ]
    with patch("core.rules.llm_checks.get_llm_client", return_value=mock_client):
        await _llm_fallback_check(queue, job_id="t-det")

    kwargs = mock_client.chat_json.await_args.kwargs
    assert kwargs["temperature"] == DETERMINISTIC_TEMPERATURE


def test_no_literal_temperature_in_findings_producers():
    """判定产出模块内不得出现裸数字 temperature —— 必须走单一真值常量。

    这是**源码扫描**（行为断言覆盖不到"以后新增的第 3 个调用点"），
    与 test_config_import_order_contract.py 同款用法。
    """
    pat = re.compile(r"temperature\s*=\s*\d")
    offenders = []
    for rel in _PRODUCERS:
        src = (_REPO / rel).read_bytes().decode("utf-8")
        for m in pat.finditer(src):
            line_no = src[: m.start()].count("\n") + 1
            offenders.append(f"{rel}:{line_no}")
    assert not offenders, (
        "判定产出模块出现裸 temperature 数字字面量（应改用 "
        "DETERMINISTIC_TEMPERATURE）：" + ", ".join(offenders)
    )
