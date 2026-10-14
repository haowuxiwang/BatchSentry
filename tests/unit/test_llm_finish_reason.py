"""0-38：LLM 截断（``finish_reason == "length"``）治理 —— 机检护栏。

为什么需要（本轮实测现场）：
    ``e2e_test.pdf`` page 2 **稳定**把 LLM 输出打满 ``_PAGE_MAX_TOKENS=8000``
    ⇒ ``_parse_json`` 失败 ⇒ ``chat_json`` 的 fix-hint 重试**再发一次**（仍打满
    8000）⇒ 单页 373.7s + 903.2s ≈ **21 分钟**。``finish_reason`` 由 SDK 提供，
    但此前 ``ChatResult`` **未采集**它 ⇒ 产品无法区分「被 max_tokens 截断」与
    「格式错」，而 fix-hint 对截断型失败是**结构性无产**（同 prompt + 同
    max_tokens ⇒ 必然再次打满）。最坏 ``3 × 1440s = 4320s``（= 0-10 的算术）。

本模块把三件事变成机检（对应 docs/TODO.md 0-38）：

1. **归一化** —— 两套协议枚举不同（openai ``length`` / anthropic ``max_tokens``），
   各自判字符串必然漂移。收敛为统一 ``FINISH_*``：``length``/``max_tokens`` →
   ``length``；``stop``/``end_turn``/``stop_sequence`` → ``stop``；未报 → ``""``；
   未知 → ``other``。**判定一律看归一值**，原始值只做排障。
2. **决策** —— ``finish_reason == "length"`` 时 ``chat_json`` **不发** fix-hint
   重试（fail-closed：该页转 failed_pages / 人工复核），其余情形照旧重试。
3. **留痕** —— ``llm_call_audit.finish_reason`` 落库（v13 迁移 + ``schema.sql``
   单一声明处）。

⚠️ 已知边界（**不得**读成"已覆盖"）：
    - ``chat()``（非 JSON 路径）**不**检查截断 —— 现有调用方只有健康探测 /
      设置页探测（``core/health.py`` / ``api/settings/probe.py``），不消费正文，
      故不构成正确性风险；真实判定路径一律走 ``chat_json``。
    - 「截断但解析成功」的结果**仍会被返回**（仅告警）。理由：``_parse_json``
      的截断恢复会把被切断的值补 ``null``，下游 ``_validate_page_result`` 是
      第二道防线（0-40 已复核「缺 page_info/steps ⇒ 必然失败」）。本模块只钉住
      「**有告警**」，不假装它已被拦下。
"""
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import ProviderConfig
from llm.adapters.base import (
    FINISH_LENGTH,
    FINISH_OTHER,
    FINISH_STOP,
    ChatResult,
    normalize_finish_reason,
)
from llm.client import LLMClient

REPO = Path(__file__).resolve().parents[2]

#: 归一化的**唯一**声明处 —— 适配器只许调用它，不得各自判字面量。
_ADAPTERS = ("openai_adapter.py", "anthropic_adapter.py")


@pytest.fixture(autouse=True)
def _isolate_json_mode_flag():
    """``_json_mode_disabled`` 是**类级**字典 ⇒ 跨用例会残留，必须隔离。"""
    saved = dict(LLMClient._json_mode_disabled)
    LLMClient._json_mode_disabled.clear()
    yield
    LLMClient._json_mode_disabled.clear()
    LLMClient._json_mode_disabled.update(saved)


@pytest.fixture
def patched_config():
    """注入 mock provider 配置（patch 覆盖整个用例，``chat_json`` 也要读到）。

    用 ``SimpleNamespace`` 而非 ``MagicMock``：``getattr(app, "llm_json_mode", False)``
    在 MagicMock 上会拿到**真值对象**（truthy）⇒ 悄悄打开 json_mode，让用例
    的请求体形态与真实默认值脱节。
    """
    prov = ProviderConfig(
        name="deepseek", protocol="openai", api_key="sk-test",
        base_url="https://test.com/v1", model="test-model",
    )
    cfg = {
        "providers": {"deepseek": prov},
        "deepseek": prov,
        "app": SimpleNamespace(llm_provider="deepseek", llm_json_mode=False),
    }
    with patch("llm.client.config", cfg):
        yield cfg


# ── 1. 归一化 ────────────────────────────────────────────────────────────
class TestNormalizeFinishReason:
    @pytest.mark.parametrize("raw,protocol,expected", [
        # ⚠️ 期望值一律写**字面量**（不写 FINISH_* 常量）：用常量当期望会让
        #    "常量值被改坏"这类变异变成同义反复（两边一起变 ⇒ 永远相等）。
        #    本表是**线上协议契约**的钉子，必须钉字面量。
        # openai 协议
        ("stop", "openai", "stop"),
        ("length", "openai", "length"),
        ("tool_calls", "openai", "other"),
        ("content_filter", "openai", "other"),
        # anthropic 协议（枚举名与 openai **不同**）
        ("end_turn", "anthropic", "stop"),
        ("stop_sequence", "anthropic", "stop"),
        ("max_tokens", "anthropic", "length"),
        ("tool_use", "anthropic", "other"),
        # 未报（None / 空串 / 纯空白）—— 与「正常收尾」是两件事
        (None, "openai", ""),
        ("", "openai", ""),
        ("   ", "anthropic", ""),
        # 大小写 / 空白容错
        ("LENGTH", "openai", "length"),
        (" Max_Tokens ", "anthropic", "length"),
        # 同协议内的未知枚举
        ("whatever", "openai", "other"),
    ])
    def test_table(self, raw, protocol, expected):
        assert normalize_finish_reason(raw, protocol) == expected

    def test_constants_pin_the_wire_values(self):
        """常量的**值**是线上契约（写进审计列、进日志 grep），单独钉字面量。"""
        assert (FINISH_STOP, FINISH_LENGTH, FINISH_OTHER) == (
            "stop", "length", "other")

    def test_unknown_protocol_degrades_to_other_not_length(self):
        """未登记协议 ⇒ ``other``（**不**猜成 length）。

        取舍：``other`` 会走 fix-hint 重试（多一次调用），而误判成 ``length``
        会**跳过**重试。两者最终都 fail-closed（重试仍失败 ⇒ ``_parse_error``），
        故宁可选"多花一次调用"也不选"基于猜测跳过重试"。新增协议时**必须**
        同步 ``_FINISH_TABLE``，否则该协议的截断不会被识别。
        """
        assert normalize_finish_reason("length", "gemini") == FINISH_OTHER
        assert normalize_finish_reason("max_tokens", "") == FINISH_OTHER

    def test_constants_are_pairwise_distinct(self):
        assert len({FINISH_STOP, FINISH_LENGTH, FINISH_OTHER}) == 3

    def test_chat_result_defaults_to_unreported(self):
        """默认值必须是「未报」（``""``）—— **不得**默认成 ``stop``。"""
        r = ChatResult(content="x")
        assert r.finish_reason == ""
        assert r.raw_finish_reason == ""


class TestNormalizationSingleSource:
    """归一化是**唯一**真值：适配器只许调用，不得各自判字面量。"""

    def test_adapters_delegate_to_normalize_finish_reason(self):
        for name in _ADAPTERS:
            src = (REPO / "llm" / "adapters" / name).read_text(encoding="utf-8")
            assert "normalize_finish_reason(" in src, name
            assert "finish_reason=normalize_finish_reason(" in src, name

    def test_no_adapter_hardcodes_a_truncation_literal(self):
        for name in _ADAPTERS:
            src = (REPO / "llm" / "adapters" / name).read_text(encoding="utf-8")
            for lit in ('== "length"', "== 'length'",
                        '== "max_tokens"', "== 'max_tokens'"):
                assert lit not in src, f"{name} 硬编码了判定字面量 {lit}"


_MISSING = object()


def _openai_resp(content="ok", finish_reason=_MISSING, usage=None):
    """构造最小 openai 响应对象（**不用** MagicMock：缺字段必须真的缺）。"""
    choice = SimpleNamespace(message=SimpleNamespace(content=content))
    if finish_reason is not _MISSING:
        choice.finish_reason = finish_reason
    return SimpleNamespace(choices=[choice], usage=usage)


def _anthropic_resp(text="ok", stop_reason=_MISSING, usage=None):
    resp = SimpleNamespace(content=[SimpleNamespace(text=text)], usage=usage)
    if stop_reason is not _MISSING:
        resp.stop_reason = stop_reason
    return resp


class TestAdapterCapturesFinishReason:
    """采集层：两套协议各自的字段名**不同**（``finish_reason`` / ``stop_reason``）。

    这一层必须单独钉住 —— 归一化表写得再对，只要 adapter 取错字段（或干脆
    不取），产品侧永远拿到 ``""``（= 未报）⇒ 截断治理整体失效。
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw,expected", [
        ("length", FINISH_LENGTH),
        ("stop", FINISH_STOP),
        ("tool_calls", FINISH_OTHER),
    ])
    async def test_openai_captures_and_normalizes(self, raw, expected):
        from llm.adapters.openai_adapter import OpenAIAdapter

        adapter = OpenAIAdapter(ProviderConfig(
            name="deepseek", protocol="openai", api_key="sk-test",
            base_url="https://test.com/v1", model="test-model"))
        adapter.client.chat.completions.create = AsyncMock(
            return_value=_openai_resp(finish_reason=raw))
        result = await adapter.chat("sys", "user")
        assert result.finish_reason == expected
        assert result.raw_finish_reason == raw

    @pytest.mark.asyncio
    async def test_openai_missing_field_is_unreported(self):
        from llm.adapters.openai_adapter import OpenAIAdapter

        adapter = OpenAIAdapter(ProviderConfig(
            name="deepseek", protocol="openai", api_key="sk-test",
            base_url="https://test.com/v1", model="test-model"))
        adapter.client.chat.completions.create = AsyncMock(
            return_value=_openai_resp())  # 无 finish_reason 字段
        result = await adapter.chat("sys", "user")
        assert result.finish_reason == ""
        assert result.raw_finish_reason == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw,expected", [
        ("max_tokens", FINISH_LENGTH),
        ("end_turn", FINISH_STOP),
        ("stop_sequence", FINISH_STOP),
        ("tool_use", FINISH_OTHER),
    ])
    async def test_anthropic_captures_and_normalizes(self, raw, expected):
        import sys

        from llm.adapters.anthropic_adapter import AnthropicAdapter

        fake_mod = SimpleNamespace(AsyncAnthropic=MagicMock())
        with patch.dict(sys.modules, {"anthropic": fake_mod}):
            adapter = AnthropicAdapter(ProviderConfig(
                name="claude", protocol="anthropic", api_key="sk-ant-test",
                base_url="https://api.anthropic.com", model="claude-3-5-sonnet"))
        adapter.client.messages.create = AsyncMock(
            return_value=_anthropic_resp(stop_reason=raw))
        result = await adapter.chat("sys", "user")
        assert result.finish_reason == expected
        assert result.raw_finish_reason == raw

    @pytest.mark.asyncio
    async def test_anthropic_missing_field_is_unreported(self):
        import sys

        from llm.adapters.anthropic_adapter import AnthropicAdapter

        fake_mod = SimpleNamespace(AsyncAnthropic=MagicMock())
        with patch.dict(sys.modules, {"anthropic": fake_mod}):
            adapter = AnthropicAdapter(ProviderConfig(
                name="claude", protocol="anthropic", api_key="sk-ant-test",
                base_url="https://api.anthropic.com", model="claude-3-5-sonnet"))
        adapter.client.messages.create = AsyncMock(
            return_value=_anthropic_resp())  # 无 stop_reason 字段
        result = await adapter.chat("sys", "user")
        assert result.finish_reason == ""
        assert result.raw_finish_reason == ""


# ── 2. 决策：截断 ⇒ 不发 fix-hint 重试 ──────────────────────────────────
class TestTruncationSkipsFixHintRetry:
    @pytest.mark.asyncio
    async def test_length_skips_fix_hint_retry(self, patched_config, caplog):
        """核心口径：``finish_reason == "length"`` + 解析失败 ⇒ **只发 1 次**。"""
        client = LLMClient(provider="deepseek")
        client.adapter.chat = AsyncMock(return_value=ChatResult(
            content="```json\n{invalid", model="m",
            finish_reason=FINISH_LENGTH, raw_finish_reason="length",
        ))
        with caplog.at_level(logging.WARNING, logger="llm.client"):
            result = await client.chat_json("sys", "user")
        assert result.get("_parse_error") is True, "截断型失败仍须 fail-closed"
        assert client.adapter.chat.call_count == 1, "截断型失败不得再发 fix-hint"
        assert "truncated" in caplog.text
        assert "length" in caplog.text  # 原始枚举进日志（排障）

    @pytest.mark.asyncio
    @pytest.mark.parametrize("finish", [FINISH_STOP, FINISH_OTHER, ""])
    async def test_non_length_still_retries(self, patched_config, finish):
        """反向对照：非截断（含**未报**）必须照旧重试 —— 否则是过度拦截。

        ``""``（上游未报）尤其重要：若把「没报」当成截断，会让所有不返回
        finish_reason 的兼容网关**全部**退化为单发。
        """
        client = LLMClient(provider="deepseek")
        client.adapter.chat = AsyncMock(return_value=ChatResult(
            content="```json\n{still invalid", model="m", finish_reason=finish,
        ))
        result = await client.chat_json("sys", "user")
        assert result.get("_parse_error") is True
        assert client.adapter.chat.call_count == 3, (
            f"finish_reason={finish!r} 时应有 1 原始 + 2 fix-hint"
        )

    @pytest.mark.asyncio
    async def test_length_with_parseable_json_returns_and_warns(
            self, patched_config, caplog):
        """截断但解析成功 ⇒ 结果仍返回，但**必须**留痕，且措辞不得与事实不符。"""
        client = LLMClient(provider="deepseek")
        client.adapter.chat = AsyncMock(return_value=ChatResult(
            content='{"ok": true}', model="m",
            finish_reason=FINISH_LENGTH, raw_finish_reason="length",
        ))
        with caplog.at_level(logging.WARNING, logger="llm.client"):
            result = await client.chat_json("sys", "user")
        assert result == {"ok": True}
        assert client.adapter.chat.call_count == 1
        assert "content may be incomplete" in caplog.text
        # 这一支**没有**待跳过的重试 ⇒ 不得报 "skipping fix-hint retry"
        assert "skipping fix-hint retry" not in caplog.text

    @pytest.mark.asyncio
    async def test_length_with_recovered_payload_flags_truncation(
            self, patched_config, caplog):
        """截断被恢复链补全时，``_truncated_recovered`` 与告警都要在。"""
        client = LLMClient(provider="deepseek")
        client.adapter.chat = AsyncMock(return_value=ChatResult(
            content='{"findings": [{"id": 1}], "note": "abc', model="m",
            finish_reason=FINISH_LENGTH, raw_finish_reason="length",
        ))
        with caplog.at_level(logging.WARNING, logger="llm.client"):
            result = await client.chat_json("sys", "user")
        assert result.get("_truncated_recovered") is True
        assert result["note"] is None
        assert client.adapter.chat.call_count == 1
        assert "content may be incomplete" in caplog.text


# ── 3. 单一重试循环（chat / chat_json 共用 _chat_result）────────────────
class TestSingleRetryLoop:
    @pytest.mark.asyncio
    async def test_chat_returns_str_not_chat_result(self, patched_config):
        """向后兼容：``chat()`` 必须仍返回 ``str``（不得泄漏 ChatResult）。"""
        client = LLMClient(provider="deepseek")
        client.adapter.chat = AsyncMock(return_value=ChatResult(
            content="hello", model="m",
            finish_reason=FINISH_LENGTH, raw_finish_reason="length",
        ))
        out = await client.chat("sys", "user")
        assert isinstance(out, str)
        assert out == "hello"

    @pytest.mark.asyncio
    async def test_chat_delegates_to_chat_result(self, patched_config):
        """单一真值：``chat()`` 不得另起一份重试循环。"""
        client = LLMClient(provider="deepseek")
        sentinel = ChatResult(content="from-single-loop", model="m")
        with patch.object(client, "_chat_result",
                          new=AsyncMock(return_value=sentinel)) as m:
            out = await client.chat("sys", "user")
        m.assert_awaited_once()
        assert out == "from-single-loop"

    @pytest.mark.asyncio
    async def test_chat_json_delegates_to_chat_result(self, patched_config):
        client = LLMClient(provider="deepseek")
        sentinel = ChatResult(content='{"a": 1}', model="m")
        with patch.object(client, "_chat_result",
                          new=AsyncMock(return_value=sentinel)) as m:
            out = await client.chat_json("sys", "user")
        m.assert_awaited_once()
        assert out == {"a": 1}


# ── 4. 留痕：审计落库 ───────────────────────────────────────────────────
class TestFinishReasonAudit:
    @pytest.mark.asyncio
    async def test_finish_reason_persisted(self, test_db, patched_config):
        from llm.client import _record_llm_call

        await test_db.execute(
            "INSERT INTO jobs (id, filename, status) VALUES (?, ?, ?)",
            ("fr-job", "a.pdf", "review"),
        )
        await test_db.commit()

        client = LLMClient(provider="deepseek")
        ctx = {"job_id": "fr-job", "page": 2, "stage": "page_analysis"}
        await _record_llm_call(
            ctx, client,
            ChatResult(content="x", finish_reason=FINISH_LENGTH),
            latency_ms=100, success=True,
        )
        cur = await test_db.execute(
            "SELECT finish_reason FROM llm_call_audit WHERE job_id = ?",
            ("fr-job",),
        )
        assert (await cur.fetchone())["finish_reason"] == FINISH_LENGTH

    @pytest.mark.asyncio
    async def test_unreported_finish_reason_is_null(self, test_db, patched_config):
        """未报 / 异常路径 ⇒ ``NULL``（**不**写空串，也不写 stop）。"""
        from llm.client import _record_llm_call

        await test_db.execute(
            "INSERT INTO jobs (id, filename, status) VALUES (?, ?, ?)",
            ("fr-null-job", "a.pdf", "review"),
        )
        await test_db.commit()

        client = LLMClient(provider="deepseek")
        ctx = {"job_id": "fr-null-job", "page": 1, "stage": "page_analysis"}
        await _record_llm_call(ctx, client, ChatResult(content="x"),
                               latency_ms=10, success=True)
        await _record_llm_call(ctx, client, None, latency_ms=10, success=False,
                               error="boom")
        cur = await test_db.execute(
            "SELECT finish_reason FROM llm_call_audit WHERE job_id = ?",
            ("fr-null-job",),
        )
        assert [r["finish_reason"] for r in await cur.fetchall()] == [None, None]


# ── 5. schema / 迁移（v13）──────────────────────────────────────────────
class TestSchemaV13:
    def test_schema_sql_declares_finish_reason(self):
        sql = (REPO / "db" / "schema.sql").read_text(encoding="utf-8")
        assert "finish_reason" in sql

    def test_schema_version_is_13(self):
        from db.client import SCHEMA_VERSION

        assert SCHEMA_VERSION == 13

    @pytest.mark.asyncio
    async def test_migrate_v12_db_adds_finish_reason(self, tmp_path):
        """v12 库（无该列）→ migrate 必须补列，否则审计永远缺这一格。"""
        import aiosqlite

        from db.client import SCHEMA_VERSION, migrate

        db_path = str(tmp_path / "v12.db")
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("""
                CREATE TABLE llm_call_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT,
                    success INTEGER NOT NULL DEFAULT 1
                )
            """)
            await db.execute("PRAGMA user_version = 12")
            await db.commit()

            await migrate(db)
            cur = await db.execute("PRAGMA table_info(llm_call_audit)")
            cols = {row["name"] for row in await cur.fetchall()}
            assert "finish_reason" in cols
            cur = await db.execute("PRAGMA user_version")
            assert (await cur.fetchone())[0] == SCHEMA_VERSION

    @pytest.mark.asyncio
    async def test_fresh_schema_has_column(self, test_db):
        cur = await test_db.execute("PRAGMA table_info(llm_call_audit)")
        cols = {row["name"] for row in await cur.fetchall()}
        assert "finish_reason" in cols
