"""P2「停滞可见性」（2026-09-16）。

背景：停滞阈值经两轮抬高（1800 → 4200s）之后，看门狗会在**最长约 70 分钟**
之后才收敛一个卡死的 job，而用户在此期间看到的只有"进度条不动"。P2 的
要求是：在杀掉任务**之前**先给信号，让"要不要继续等"回到用户手里
（docs/RUNTIME_WATCHDOG.md §8.7）。

实现取舍：**纯派生**，不写库、不改 schema —— 空闲秒数与阈值都能从既有字段
算出，落库只会多一次 fsync，并让"这个字段过期了"成为新的失效模式。

注意：SSE 规范里 `error` 是保留事件类型，所以这里**不引入新的 event 类型**，
只用既有 message 帧里的一个字段（前端据此显示横幅）。
"""
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core import watchdog


def _ago(seconds: float) -> str:
    return (datetime.now() - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%d %H:%M:%S")


class TestStallReport:
    def test_not_applicable_for_terminal_status(self):
        """终态 job 不该报停滞（否则终态快照缓存会带上误导性的 warn）。"""
        for st in ("review", "partial_review", "error", "cancelled",
                   "archived", "done"):
            assert watchdog.stall_report(st, 10, _ago(99999)) is None

    def test_pending_needs_a_live_task(self):
        """`pending` 默认不监视（过渡态），只有被 pipeline 接管才算。"""
        assert watchdog.stall_report("pending", 10, _ago(99999)) is None
        got = watchdog.stall_report("pending", 10, _ago(99999),
                                    has_live_task=True)
        assert got is not None and got["overdue"] is True

    def test_unparseable_or_missing_heartbeat_returns_none(self):
        """判不了就说判不了 —— 不用 0 冒充"没停滞"。"""
        assert watchdog.stall_report("ocr_running", 5, None) is None
        assert watchdog.stall_report("ocr_running", 5, "") is None
        assert watchdog.stall_report("ocr_running", 5, "not-a-timestamp") is None

    def test_warn_threshold_is_sixty_percent_of_limit(self):
        limit = watchdog.stall_limit_seconds("ocr_running", 1)
        frac = watchdog._STALL_WARN_FRACTION

        below = watchdog.stall_report("ocr_running", 1, _ago(limit * frac - 30))
        assert below is not None
        assert below["warn"] is False and below["overdue"] is False

        above = watchdog.stall_report("ocr_running", 1, _ago(limit * frac + 30))
        assert above is not None
        assert above["warn"] is True and above["overdue"] is False

        over = watchdog.stall_report("ocr_running", 1, _ago(limit + 60))
        assert over is not None
        assert over["warn"] is True and over["overdue"] is True

    def test_limit_comes_from_the_single_threshold_table(self):
        """阈值只能有一处口径 —— 调用方另算一套必然漂移。"""
        got = watchdog.stall_report("analyzing", 12, _ago(60))
        assert got is not None
        assert got["limit_seconds"] == int(
            watchdog.stall_limit_seconds("analyzing", 12))

    def test_honours_the_field_scale_knob(self, monkeypatch):
        """现场放宽系数（PBC_WATCHDOG_SCALE）对预警同样生效（同一份口径）。"""
        monkeypatch.setenv("PBC_WATCHDOG_SCALE", "0.1")
        got = watchdog.stall_report("ocr_running", 1, _ago(1000))
        assert got is not None
        assert got["limit_seconds"] == int(
            watchdog.stall_limit_seconds("ocr_running", 1))
        assert got["limit_seconds"] < watchdog._OCR_BASE_STALL_S
        assert got["warn"] is True

    def test_report_is_json_safe(self):
        import json

        got = watchdog.stall_report("ocr_running", 51, _ago(30))
        assert got is not None
        json.dumps(got)  # 要塞进 SSE 帧，必须可序列化
        assert set(got) == {"idle_seconds", "limit_seconds", "warn", "overdue"}


class TestApiPayloadCarriesStall:
    """两个载荷（`GET /api/jobs/{id}` 与 SSE）都要带上 —— 只做一个等于
    一半场景没覆盖（复核页非终态时走 SSE）。"""

    def _payload(self, row):
        from api.jobs.status import _stall_payload

        return _stall_payload(row)

    def test_returns_none_when_column_absent(self):
        """明确"判不了"，而不是拿缺列当"没停滞"。"""
        assert self._payload({"status": "ocr_running", "total_pages": 3}) is None

    def test_fresh_job_has_no_warning(self):
        got = self._payload({"status": "ocr_running", "total_pages": 3,
                             "last_activity_at": _ago(5)})
        assert got is not None and got["warn"] is False

    def test_stalled_job_warns(self):
        limit = watchdog.stall_limit_seconds("ocr_running", 3)
        got = self._payload({"status": "ocr_running", "total_pages": 3,
                             "last_activity_at": _ago(limit * 0.8)})
        assert got is not None and got["warn"] is True

    def test_bad_input_never_breaks_status_query(self):
        """可见性是增强项：喂坏数据也只返回 None，绝不能把状态查询打断。"""
        got = self._payload({"status": "ocr_running", "total_pages": "not-a-number",
                             "last_activity_at": _ago(10)})
        assert got is None


_SRC_ROOT = Path(__file__).resolve().parents[2]


class TestWiringGuards:
    """**两端同步**的静态护栏：这类"功能写完了但没接上"的缺陷，
    运行时用例是抓不到的（payload 少个字段照样 200）。"""

    def test_sse_projection_selects_last_activity_at(self):
        """`_get_job_progress` 用显式列投影 —— 漏列会让 stall 静默变 None。"""
        src = (_SRC_ROOT / "api" / "jobs" / "status.py").read_text(encoding="utf-8")
        assert "last_activity_at FROM jobs WHERE id = ?" in src, (
            "SSE 快照的列投影里没有 last_activity_at —— 停滞可见性会静默失效"
        )

    def test_both_payloads_include_stall(self):
        src = (_SRC_ROOT / "api" / "jobs" / "status.py").read_text(encoding="utf-8")
        assert src.count('"stall": _stall_payload(job)') == 2, (
            "GET /api/jobs/{id} 与 SSE 快照都应带 stall 字段"
        )

    def test_frontend_renders_the_banner(self):
        js = (_SRC_ROOT / "static" / "review.js").read_text(encoding="utf-8")
        html = (_SRC_ROOT / "templates" / "review.html").read_text(encoding="utf-8")
        assert "stall-banner" in html and "stall-text" in html
        assert re.search(r"\bd\.stall\b", js), "review.js 未消费 stall 字段"
        # 文案里必须同时出现"原因"与"出路"（否则用户不知道能做什么）
        assert "可取消后重试" in js
        assert 'aria-live' in html.split("stall-banner")[1][:400]

    def test_no_new_sse_event_type_is_introduced(self):
        """SSE 规范里 `error` 是保留事件类型；P2 刻意**不新增事件类型**。

        只看真正被 `yield` 出去的帧（AST），不看注释 —— 源码里有一句注释
        正是"不能用 event: error"的说明，按文本匹配会把这条说明判成违规。
        """
        import ast

        tree = ast.parse((_SRC_ROOT / "api" / "jobs" / "status.py")
                         .read_text(encoding="utf-8"))
        events: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Yield):
                for part in ast.walk(node):
                    if isinstance(part, ast.Constant) and isinstance(part.value, str):
                        events.extend(re.findall(r"event:\s*(\w+)", part.value))
        assert "error" not in events, "error 是 SSE 保留事件类型，不得使用"
        assert "stall" not in events and "stalled" not in events
        assert set(events) <= {"done"}, f"P2 不应引入新事件类型：{sorted(set(events))}"
