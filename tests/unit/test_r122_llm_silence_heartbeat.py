"""R122 / TODO 0-10：`analyzing` 阈值 vs LLM 重试链的**心跳粒度**。

## 缺陷

一次页分析内部最多 6 次 `chat_json`（主调用 + 2 次 JSON 修复 + schema 修复
+ 其修复链），每次最坏 3 个 SDK 尝试 × 480s。心跳若只在**整页结束**时写，
合法静默上界是 8640s —— 任何阈值都覆盖不住：低了把还在正常重试的任务判成
停滞（收敛动作 = 标记 error + 取消 task），抬到 8640 以上则真正挂死的 job
要等 2.4 小时才被发现。

## 修法

**加心跳，不抬阈值**：`chat_json` 每次发起调用前打点（`on_call_start`），
合法静默退回"单次调用"= 3 × 480 = 1440s。本文件把这条链路每一段都钉住：

1. 上界怎么算出来的 —— 含 adapter 的 SDK 尝试次数（**解析源码**核对，
   不写死；同时核对 openai 的默认值没变）；
2. 每次尝试**都**打点（客户端重试 / fix-hint 修复 / schema 修复）；
3. 打点抛异常不得打断调用（心跳只是可观测性）；
4. 看门狗与**界面提示**都认这条心跳 —— 只认 DB 时间戳会在 LLM 正常重试时
   劝用户取消一个正在工作的任务；
5. stage2 真的把心跳传下去了（R119/R121 教训：只测 helper 证明不了接线）。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from core import activity_clock
from core import page_analyzer as pa
from core import watchdog

# ─────────────────────────────────────────────────────────────────────────────
# 上界推导
# ─────────────────────────────────────────────────────────────────────────────


class TestBoundDerivation:
    def test_bound_is_per_call_not_per_page(self):
        """上界必须是**单次调用**的量级。

        若有人把它改回"整页 6 次叠加"（8640s），看门狗就失去兜底意义 ——
        这条同时钉住"改回去必须红"。
        """
        per_call = pa.page_analysis_silence_bound_s()
        whole_page = (
            pa.page_analysis_call_slots() * pa._SDK_ATTEMPTS_PER_CALL * pa._PAGE_TIMEOUT
        )
        assert per_call == pa._SDK_ATTEMPTS_PER_CALL * pa._PAGE_TIMEOUT
        assert per_call < whole_page, "上界又变回整页叠加了 —— 心跳没起作用"

    def test_call_slots_documents_the_chain(self):
        """6 = (主调用 + schema 修复) × (1 次主发 + 2 次 JSON 修复)。"""
        assert pa.page_analysis_call_slots() == 6

    def test_sdk_attempts_match_adapter_source(self):
        """`_SDK_ATTEMPTS_PER_CALL` 必须等于 adapter 的**实际**配置。

        单次 `adapter.chat()` 的耗时上界取决于 SDK 自己重试几次：源码里没传
        `max_retries` 就走 `openai.DEFAULT_MAX_RETRIES`。两处都核对 ——
        只核常量会让它变成没人管的字面量（R90 的教训：手写推导算错过一次）。
        """
        import openai

        from llm.adapters import openai_adapter

        src = Path(openai_adapter.__file__).read_text(encoding="utf-8")
        m = re.search(r"AsyncOpenAI\((.*?)\n\s*\)", src, re.S)
        assert m, "找不到 `AsyncOpenAI(...)` 构造 —— 解析规则要跟着改"
        explicit = re.search(r"max_retries\s*=\s*(\d+)", m.group(1))
        expected = (
            int(explicit.group(1)) + 1 if explicit else openai.DEFAULT_MAX_RETRIES + 1
        )
        assert pa._SDK_ATTEMPTS_PER_CALL == expected, (
            f"adapter 实际允许 {expected} 次 SDK 尝试，"
            f"但 page_analyzer 按 {pa._SDK_ATTEMPTS_PER_CALL} 算上界"
        )

    def test_json_repair_constant_matches_client_loop(self):
        """`_JSON_REPAIR_CALLS` 必须等于 `chat_json` 里循环的真实上界。

        `parse_attempt < 3` ⇒ 最多再发 2 次。改一处漏另一处 ⇒ 红。
        """
        from llm import client as client_mod

        src = Path(client_mod.__file__).read_text(encoding="utf-8")
        bounds = re.findall(r"parse_attempt\s*<\s*(\d+)", src)
        assert len(bounds) == 1, f"`parse_attempt <` 出现 {len(bounds)} 处，预期 1"
        assert int(bounds[0]) - 1 == pa._JSON_REPAIR_CALLS

    def test_watchdog_base_covers_bound_plus_margin(self):
        """看门狗基准 = 单次调用上界 + 余量；且任何页数下都不低于上界。"""
        assert watchdog.analyzing_silence_bound_s() == (
            pa.page_analysis_silence_bound_s() + watchdog._STALL_MARGIN_S
        )
        for pages in (1, 4, 51):
            limit = watchdog.stall_limit_seconds("analyzing", pages)
            assert limit >= pa.page_analysis_silence_bound_s()


# ─────────────────────────────────────────────────────────────────────────────
# 每次尝试都打点（llm/client 层）
# ─────────────────────────────────────────────────────────────────────────────


def _make_client():
    from config import ProviderConfig
    from llm.client import LLMClient

    prov = ProviderConfig(
        name="deepseek", protocol="openai", api_key="sk-test",
        base_url="https://test.com/v1", model="test-model",
    )
    cfg = {
        "providers": {"deepseek": prov},
        "deepseek": prov,
        "app": MagicMock(llm_provider="deepseek"),
    }
    with patch("llm.client.config", cfg):
        return LLMClient(provider="deepseek")


class TestHeartbeatPerAttempt:
    @pytest.mark.asyncio
    async def test_every_client_attempt_fires_the_hook(self):
        """客户端重试的**每一次**尝试都要打点，否则重试期间照样静默。"""
        from llm.adapters.base import ChatResult

        client = _make_client()
        client.adapter.chat = AsyncMock(side_effect=[
            RuntimeError("boom-1"),
            RuntimeError("boom-2"),
            ChatResult(content='{"ok": 1}', model="test-model"),
        ])
        seen: list[float] = []

        async def hb() -> None:
            seen.append(time.time())

        # 退避睡眠会把这条用例拖到 ~6s，且与断言无关
        with patch("llm.client.asyncio.sleep", new=AsyncMock()):
            out = await client.chat_json("sys", "user", retries=3, on_call_start=hb)

        assert out == {"ok": 1}
        assert len(seen) == 3, f"3 次尝试应打点 3 次，实际 {len(seen)}"

    @pytest.mark.asyncio
    async def test_fix_hint_chain_fires_the_hook(self):
        """JSON 修复链（主调用 + 2 次重发）也必须逐次打点。

        这是 TODO 0-10 的**原始触发面**：call1 成功但解析失败，随后两次
        修复调用各可耗 1440s —— 若这两次不打点，静默就是 2880s。
        """
        from llm.adapters.base import ChatResult

        client = _make_client()
        client.adapter.chat = AsyncMock(side_effect=[
            ChatResult(content="not json at all", model="test-model"),
            ChatResult(content="still not json", model="test-model"),
            ChatResult(content='{"ok": 1}', model="test-model"),
        ])
        seen: list[float] = []

        async def hb() -> None:
            seen.append(time.time())

        out = await client.chat_json("sys", "user", retries=1, on_call_start=hb)

        assert out == {"ok": 1}
        assert len(seen) == 3, f"主调用 + 2 次修复应打点 3 次，实际 {len(seen)}"

    @pytest.mark.asyncio
    async def test_hook_failure_does_not_break_the_call(self):
        """心跳是纯可观测性 —— 它抛异常绝不能把一次正常调用打红。"""
        from llm.adapters.base import ChatResult

        client = _make_client()
        client.adapter.chat = AsyncMock(
            return_value=ChatResult(content='{"ok": 1}', model="test-model")
        )
        calls: list[int] = []

        async def bad_hb() -> None:
            calls.append(1)
            raise RuntimeError("heartbeat exploded")

        out = await client.chat_json("sys", "user", on_call_start=bad_hb)

        assert out == {"ok": 1}
        assert calls == [1], "回调没被调用 —— 这条用例是空转"

    @pytest.mark.asyncio
    async def test_timeout_aborts_without_client_retry(self):
        """超时路径**不**做客户端重试（`_chat_result` 明确 raise）。

        这条锁住"上界不能按 `_PAGE_RETRIES` 乘"的前提：超时只打一次点。
        """
        client = _make_client()
        client.adapter.chat = AsyncMock(side_effect=TimeoutError("read timeout"))
        seen: list[int] = []

        async def hb() -> None:
            seen.append(1)

        with pytest.raises(RuntimeError):
            await client.chat_json("sys", "user", retries=3, on_call_start=hb)

        assert len(seen) == 1, f"超时只应发起 1 次调用，实际 {len(seen)}"


# ─────────────────────────────────────────────────────────────────────────────
# analyze_page 把心跳传下去
# ─────────────────────────────────────────────────────────────────────────────


class TestAnalyzePageForwardsHook:
    @pytest.mark.asyncio
    async def test_heartbeat_reaches_chat_json(self):
        """**逐次**记录 kwargs —— 用 `dict.update` 会让第二次调用覆盖第一次。

        这正是变异 M11 第一版 **MISSED** 的原因：载荷没带 `page_info.title`，
        触发 schema 修复 → 第二次 `chat_json`（仍带 hook）把第一次的记录盖掉，
        于是"主调用没转发"这个缺陷被后一次调用掩盖。
        """
        calls: list[dict] = []

        class FakeClient:
            async def chat_json(self, *args, **kwargs):
                calls.append(kwargs)
                return {
                    "page_info": {"title": "批生产记录"},
                    "steps": [], "findings": [], "overall_confidence": "high",
                }

        async def hb() -> None:
            return None

        with patch.object(pa, "get_llm_client", return_value=FakeClient()):
            await pa.analyze_page(
                "<table><tr><td>浓度 25.5</td></tr></table>", 1,
                job_id="j-hb", heartbeat=hb,
            )

        assert len(calls) == 1, f"合法载荷不该触发 schema 修复，实测 {len(calls)} 次调用"
        assert calls[0].get("on_call_start") is hb, (
            "analyze_page 没把 heartbeat 传给 chat_json ⇒ 心跳链在第二层就断了"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 看门狗 / 界面提示都认这条心跳
# ─────────────────────────────────────────────────────────────────────────────


def _stale(seconds: float = 999_999) -> str:
    return (datetime.now() - timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


async def _insert_job(db, job_id: str, *, status="analyzing", total_pages=1,
                      last_activity_at=None) -> None:
    await db.execute(
        "INSERT INTO jobs (id, filename, status, total_pages, last_activity_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (job_id, "x.pdf", status, total_pages, last_activity_at),
    )
    await db.commit()


class TestWatchdogHonorsClock:
    @pytest.fixture(autouse=True)
    def _clean_clock(self):
        activity_clock.reset()
        yield
        activity_clock.reset()

    @pytest.mark.asyncio
    async def test_fresh_llm_activity_keeps_job_alive(self, test_db):
        """DB 时间戳很旧、但进程内刚打过点 ⇒ 不得判停滞。"""
        await _insert_job(test_db, "live-1", last_activity_at=_stale(99_999))
        await activity_clock.note("live-1")

        assert await watchdog.find_stalled_jobs() == [], (
            "LLM 正在重试却被判停滞 —— 收敛动作会把这个任务杀掉"
        )

        # 阳性对照：清掉进程内记录后，同一条 job 必须被判停滞（否则上面是空转）
        activity_clock.reset()
        assert [j["id"] for j in await watchdog.find_stalled_jobs()] == ["live-1"]

    @pytest.mark.asyncio
    async def test_clock_cannot_make_a_job_look_older(self, test_db):
        """进程内记录很旧、DB 刚更新 ⇒ 取 DB 那一侧（`min` 只往"更新"方向）。"""
        await _insert_job(
            test_db, "live-2",
            last_activity_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        activity_clock._LAST["live-2"] = time.time() - 99_999

        assert await watchdog.find_stalled_jobs() == []

    def test_stall_report_uses_clock(self):
        """界面提示同一口径 —— 否则会在正常重试时劝用户取消。"""
        old = _stale(99_999)
        assert watchdog.stall_report("analyzing", 1, old)["overdue"] is True

        activity_clock._LAST["ui-1"] = time.time()
        try:
            got = watchdog.stall_report("analyzing", 1, old, job_id="ui-1")
            assert got is not None
            assert got["overdue"] is False
            assert got["idle_seconds"] < 60
        finally:
            activity_clock.reset()

    def test_stall_report_without_job_id_keeps_old_behaviour(self):
        """不传 job_id 时行为不变（既有调用方/用例不受影响）。"""
        assert watchdog.stall_report("analyzing", 1, _stale(99_999))["overdue"] is True


# ─────────────────────────────────────────────────────────────────────────────
# 管道层：stage2 真的把 heartbeat 传下去（R119/R121 同一教训）
# ─────────────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def pipeline_db(tmp_path):
    """与 `test_pipeline.py` 同款隔离库 fixture（**fixture 不跨文件共享**）。"""
    import db.client as db_mod
    from config import config as _cfg

    db_path = tmp_path / "test.db"
    orig = (_cfg["app"].database_path, _cfg["app"].output_dir, db_mod._db)
    _cfg["app"].database_path = str(db_path)
    _cfg["app"].output_dir = str(tmp_path / "output")
    db_mod._db = None
    try:
        db = await db_mod.get_db()
        yield db
    finally:
        import core.pipeline as p_mod

        p_mod.db_lock._loop = None
        p_mod.db_lock._waiters = None
        p_mod.db_lock._locked = False
        if db_mod._db:
            await db_mod._db.close()
        db_mod._db = orig[2]
        _cfg["app"].database_path = orig[0]
        _cfg["app"].output_dir = orig[1]


class TestStage2Wiring:
    """只测 `analyze_page`/`chat_json` 证明不了"线上会打点" —— 必须走 stage2。"""

    @pytest.fixture(autouse=True)
    def _clean_clock(self):
        activity_clock.reset()
        yield
        activity_clock.reset()

    @pytest.mark.asyncio
    async def test_analyze_one_passes_a_working_heartbeat(self, pipeline_db):
        import asyncio

        from core.pipeline import _analyze_one

        await _insert_job(pipeline_db, "job-hb", status="analyzing", total_pages=1)
        captured: dict = {}

        async def fake_analyze(raw_html, **kwargs):
            captured.update(kwargs)
            return {"steps": [], "findings": [], "overall_confidence": "high"}

        with patch("core.pipeline._is_cancelled", new=AsyncMock(return_value=False)), \
                patch("core.pipeline.analyze_page", new=fake_analyze):
            await _analyze_one(
                pipeline_db, "job-hb", 1, {"markdown": {"text": "page"}},
                asyncio.Semaphore(1), [], asyncio.Lock(), {"n": 0}, total_pages=1,
            )

        hb = captured.get("heartbeat")
        assert hb is not None, (
            "stage2 没传 heartbeat ⇒ 心跳链在最外层就断了（helper 再对也没用）"
        )
        assert activity_clock.silence_seconds("job-hb") is None
        await hb()
        assert activity_clock.silence_seconds("job-hb") is not None
