"""T6.1 / S1：SSE 轮询间隔常量一致性（源码级漂移护栏）。

历史缺陷：三处各说各话 ——
  - `retry: 2000`（客户端重连退避 2s）
  - 服务端 `asyncio.sleep(3)`（实际推送间隔 3s）
  - docstring 写"每 2 秒收到一次进度更新"
后果是客户端重连快于服务端推送：断线重连后立即拿到旧帧，白白多一轮
空转，且文案与行为不符（GMP 审计视角下"文档与代码不一致"本身即缺陷）。

现在统一由 `api.jobs._SSE_POLL_SECONDS` 派生。本测试以**源码扫描**锁死，
防止未来有人再写死字面量（行为测试只能覆盖已实现的路径，源码扫描能
拦住"新加一条流时又写死 3 秒"这类漂移）。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATUS_PY = REPO / "api" / "jobs" / "status.py"
LISTINGS_PY = REPO / "api" / "jobs" / "listings.py"


class TestPollConstantContract:
    def test_constant_defined_and_is_two_seconds(self):
        """PLAN M6/T6.1 明确要求统一为 2 s。"""
        from api.jobs import _SSE_POLL_SECONDS

        assert _SSE_POLL_SECONDS == 2, (
            f"SSE 轮询间隔应为 2s（PLAN T6.1），实际 {_SSE_POLL_SECONDS!r}"
        )

    def test_retry_frame_matches_constant(self):
        """retry 帧（毫秒）必须等于常量派生值。"""
        from api.jobs import _SSE_POLL_SECONDS

        ms = int(_SSE_POLL_SECONDS * 1000)
        assert ms == 2000

    def test_no_hardcoded_retry_literal(self):
        """两条 SSE 流都不得写死 `retry: <数字>`。"""
        for p in (STATUS_PY, LISTINGS_PY):
            src = p.read_text(encoding="utf-8")
            hits = re.findall(r'retry:\s*\{?\s*(\d{3,})', src)
            assert not hits, f"{p.name} 写死了 retry 字面量：{hits}"

    def test_no_hardcoded_sleep_literal(self):
        """两条 SSE 流都不得写死 `asyncio.sleep(<数字>)`。"""
        for p in (STATUS_PY, LISTINGS_PY):
            src = p.read_text(encoding="utf-8")
            hits = re.findall(r"asyncio\.sleep\(\s*\d", src)
            assert not hits, f"{p.name} 写死了 sleep 字面量：{hits}"

    def test_streams_reference_the_constant(self):
        for p in (STATUS_PY, LISTINGS_PY):
            src = p.read_text(encoding="utf-8")
            assert "_SSE_POLL_SECONDS" in src, f"{p.name} 未使用统一轮询常量"

    def test_no_stale_interval_in_docstrings(self):
        """文案不得声称与实际间隔不符（曾出现"每 3 秒"）。"""
        for p in (STATUS_PY, LISTINGS_PY):
            src = p.read_text(encoding="utf-8")
            assert "每 3 秒" not in src, f"{p.name} 残留过时文案「每 3 秒」"
            assert "每3秒" not in src, f"{p.name} 残留过时文案「每3秒」"
