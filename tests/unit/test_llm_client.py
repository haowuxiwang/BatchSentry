"""LLM 客户端单元测试。

覆盖：
- _parse_json 静态方法（JSON 结构化输出解析）
- 客户端初始化（从 config 读取 provider/model + 构建 adapter）
- chat() 重试逻辑（mock adapter）
- chat_json() JSON 解析集成
- reset_llm_client() 单例重置

Phase 7 架构变更：
- LLMClient 不再直接持有 AsyncOpenAI，而是持有 adapter
- adapter.chat() 返回 ChatResult 对象（不再是 raw SDK response）
- 测试改为 mock adapter.chat() 而非 SDK 内部方法
"""
import pytest
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock

from llm.client import LLMClient, get_llm_client, reset_llm_client
from config import ProviderConfig
from llm.adapters.base import ChatResult


def _make_provider(name="deepseek", protocol="openai", **kw):
    """Helper: build a ProviderConfig for tests."""
    return ProviderConfig(
        name=name,
        protocol=protocol,
        api_key=kw.get("api_key", "sk-test"),
        base_url=kw.get("base_url", "https://test.com/v1"),
        model=kw.get("model", "test-model"),
    )


def _mock_config(provider_name="deepseek"):
    """Build a mock config dict with `providers` registry + app.llm_provider."""
    prov = _make_provider(provider_name)
    return {
        "providers": {provider_name: prov},
        provider_name: prov,  # backward-compat top-level entry
        "app": MagicMock(llm_provider=provider_name),
    }


class TestParseJson:
    """_parse_json 静态方法 — 不需要实例化 LLMClient。"""

    def test_parse_valid_json(self):
        """标准 JSON 应正确解析。"""
        result = LLMClient._parse_json('{"key": "value", "num": 42}')
        assert result == {"key": "value", "num": 42}

    def test_parse_json_array(self):
        """JSON 数组应正确解析。"""
        result = LLMClient._parse_json('[1, 2, 3]')
        assert result == [1, 2, 3]

    def test_parse_markdown_fenced_json(self):
        """markdown 代码块包裹的 JSON 应正确解析。"""
        raw = '```json\n{"key": "value"}\n```'
        result = LLMClient._parse_json(raw)
        assert result == {"key": "value"}

    def test_parse_json_with_leading_text(self):
        """前缀文字 + JSON 应提取 JSON 部分。"""
        raw = 'Here is the result: {"steps": [], "findings": []}'
        result = LLMClient._parse_json(raw)
        assert "steps" in result
        assert "findings" in result

    def test_parse_malformed_sets_parse_error(self):
        """非 JSON 应返回 _parse_error 标记。"""
        result = LLMClient._parse_json("这不是 JSON")
        assert result.get("_parse_error") is True

    def test_parse_empty_response(self):
        """空响应应返回 _parse_error。"""
        result = LLMClient._parse_json("")
        assert result.get("_parse_error") is True

    def test_parse_truncated_json_recovers(self):
        """截断的 JSON（大括号不闭合）应尝试恢复。"""
        raw = '{"steps": [{"step_no": 1'
        result = LLMClient._parse_json(raw)
        # 恢复成功时返回 dict，失败时返回 _parse_error
        assert isinstance(result, (dict, list))


class TestLLMClientInit:
    """LLM 客户端初始化 — 从 config.providers 读取并构建 adapter。"""

    def test_init_with_mocked_config(self):
        """使用 mock config 初始化 deepseek provider，应构建 OpenAI adapter。"""
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient(provider="deepseek")
            assert client.provider == "deepseek"
            assert client.model == "test-model"
            assert client.adapter.protocol == "openai"
            assert client.adapter.provider_name == "deepseek"

    def test_init_defaults_to_config_provider(self):
        """不指定 provider 时应使用 config["app"].llm_provider。"""
        mock_cfg = _mock_config("siliconflow")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient()
            assert client.provider == "siliconflow"

    def test_init_falls_back_to_deepseek_when_provider_missing(self):
        """如果指定的 provider 不在注册表中，应回退到 deepseek。"""
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient(provider="nonexistent")
            assert client.provider == "deepseek"

    def test_init_anthropic_protocol(self):
        """protocol=anthropic 应构建 AnthropicAdapter（如果 SDK 已安装）。"""
        prov = _make_provider("claude", protocol="anthropic", model="claude-3")
        mock_cfg = {
            "providers": {"claude": prov},
            "claude": prov,
            "app": MagicMock(llm_provider="claude"),
        }
        # Try import anthropic; if not installed, expect RuntimeError
        try:
            import anthropic  # noqa: F401
            with patch("llm.client.config", mock_cfg):
                client = LLMClient(provider="claude")
                assert client.adapter.protocol == "anthropic"
        except ImportError:
            with patch("llm.client.config", mock_cfg):
                with pytest.raises(RuntimeError, match="anthropic"):
                    LLMClient(provider="claude")


class TestChatMethod:
    """chat() 方法 — mock adapter.chat 测试重试逻辑。"""

    @pytest.mark.asyncio
    async def test_chat_returns_content(self):
        """成功调用应返回 content 字符串。"""
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient(provider="deepseek")
            # Mock adapter.chat to return a ChatResult
            client.adapter.chat = AsyncMock(return_value=ChatResult(
                content="LLM response text",
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                model="test-model",
            ))
            result = await client.chat("system", "user")
            assert result == "LLM response text"

    @pytest.mark.asyncio
    async def test_chat_timeout_skips_retries(self):
        """Timeout 类异常应跳过客户端重试直接失败（SDK 已内部重试）。

        重试 3 次 × 240s 会让单页卡 12 分钟拖死 pipeline；快速失败让
        failed_pages 机制接管，页面转人工复核。
        """
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient(provider="deepseek")
            client.adapter.chat = AsyncMock(
                side_effect=asyncio.TimeoutError("read timed out")
            )
            with pytest.raises(RuntimeError, match="timed out"):
                await client.chat("system", "user", retries=3)
            assert client.adapter.chat.call_count == 1  # 未重试

    @pytest.mark.asyncio
    async def test_chat_retries_on_failure(self):
        """失败时应重试。"""
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient(provider="deepseek")
            # 第一次失败，第二次成功
            client.adapter.chat = AsyncMock(
                side_effect=[
                    Exception("timeout"),
                    ChatResult(content="ok", model="test-model"),
                ]
            )
            result = await client.chat("system", "user", retries=2)
            assert result == "ok"

    @pytest.mark.asyncio
    async def test_chat_fails_after_max_retries(self):
        """超过最大重试次数应抛出 RuntimeError。"""
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient(provider="deepseek")
            client.adapter.chat = AsyncMock(
                side_effect=Exception("persistent failure")
            )
            with pytest.raises(RuntimeError, match="LLM call failed"):
                await client.chat("system", "user", retries=2)

    @pytest.mark.asyncio
    async def test_chat_json_returns_parsed_dict(self):
        """chat_json 应返回解析后的 dict。"""
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient(provider="deepseek")
            client.adapter.chat = AsyncMock(return_value=ChatResult(
                content='{"key": "value"}', model="test-model",
            ))
            result = await client.chat_json("system", "user")
            assert result == {"key": "value"}


class TestSingleton:
    """get_llm_client / reset_llm_client 单例行为。"""

    def test_reset_llm_client_drops_singleton(self):
        """reset_llm_client 应让下次 get_llm_client 重建客户端。"""
        reset_llm_client()
        try:
            mock_cfg = _mock_config("deepseek")
            with patch("llm.client.config", mock_cfg):
                c1 = get_llm_client()
                reset_llm_client()
                c2 = get_llm_client()
                assert c1 is not c2
        finally:
            reset_llm_client()


class TestChatAuditContext:
    """chat() 的 audit_ctx 分支 — 上下文标签构建 + 审计记录调用（lines 69-76, 110, 127）。"""

    @pytest.mark.asyncio
    async def test_chat_with_full_audit_ctx_records_success(self):
        """成功调用 + 完整 audit_ctx 时应记录审计（covers lines 69-76, 110）。"""
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient(provider="deepseek")
            client.adapter.chat = AsyncMock(return_value=ChatResult(
                content="ok",
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                model="test-model",
            ))

            audit_ctx = {
                "job_id": "job-abcdef123456",
                "page": 3,
                "stage": "page_analysis",
                "prompt_version": "v3",
            }

            with patch("llm.client._record_llm_call", new_callable=AsyncMock) as mock_record:
                result = await client.chat("sys", "user", audit_ctx=audit_ctx)
                assert result == "ok"
                mock_record.assert_awaited_once()
                # 确认 success=True
                call_kwargs = mock_record.call_args
                assert call_kwargs.kwargs.get("success") is True or call_kwargs[1].get("success") is True

    @pytest.mark.asyncio
    async def test_chat_with_audit_ctx_records_failure(self):
        """最终失败 + audit_ctx 时应记录审计 success=False（covers line 127）。"""
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient(provider="deepseek")
            client.adapter.chat = AsyncMock(side_effect=Exception("API down"))

            audit_ctx = {"job_id": "job-xyz", "page": 1, "stage": "page_analysis"}

            with patch("llm.client._record_llm_call", new_callable=AsyncMock) as mock_record:
                with patch("llm.client.asyncio.sleep", new_callable=AsyncMock):
                    with pytest.raises(RuntimeError, match="LLM call failed"):
                        await client.chat("sys", "user", retries=2, audit_ctx=audit_ctx)
                mock_record.assert_awaited_once()
                call_kwargs = mock_record.call_args
                assert call_kwargs.kwargs.get("success") is False or call_kwargs[1].get("success") is False

    @pytest.mark.asyncio
    async def test_chat_with_partial_audit_ctx(self):
        """audit_ctx 仅含部分字段时应正常构建 ctx_tag（不崩溃）。"""
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient(provider="deepseek")
            client.adapter.chat = AsyncMock(return_value=ChatResult(
                content="ok", model="test-model",
            ))

            # 只有 job_id，无 page / stage
            audit_ctx = {"job_id": "short-id"}
            with patch("llm.client._record_llm_call", new_callable=AsyncMock):
                result = await client.chat("sys", "user", audit_ctx=audit_ctx)
                assert result == "ok"

    @pytest.mark.asyncio
    async def test_chat_with_audit_ctx_page_zero(self):
        """audit_ctx.page=0 时应被正确处理（page is not None 分支）。"""
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient(provider="deepseek")
            client.adapter.chat = AsyncMock(return_value=ChatResult(
                content="ok", model="test-model",
            ))

            audit_ctx = {"job_id": "j1", "page": 0, "stage": "cross_page_llm"}
            with patch("llm.client._record_llm_call", new_callable=AsyncMock):
                result = await client.chat("sys", "user", audit_ctx=audit_ctx)
                assert result == "ok"


class TestChatJsonParseErrorLogging:
    """chat_json() 的 _parse_error 日志分支 — 含 audit_ctx 时构建 ctx_tag（lines 158-168）。"""

    @pytest.mark.asyncio
    async def test_chat_json_logs_parse_error_with_ctx(self, test_db):
        """LLM 返回非 JSON 时，chat_json 应记录 parse_error + ctx_tag。"""
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient(provider="deepseek")
            client.adapter.chat = AsyncMock(return_value=ChatResult(
                content="这不是 JSON 格式的响应",
                model="test-model",
            ))

            audit_ctx = {"job_id": "job-abc12345", "page": 2, "stage": "page_analysis"}
            result = await client.chat_json("sys", "user", audit_ctx=audit_ctx)
            assert result.get("_parse_error") is True

    @pytest.mark.asyncio
    async def test_chat_json_parse_error_without_ctx(self, test_db):
        """无 audit_ctx 时 _parse_error 仍应正常返回（ctx_tag 为空分支）。"""
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient(provider="deepseek")
            client.adapter.chat = AsyncMock(return_value=ChatResult(
                content="完全无法解析的内容",
                model="test-model",
            ))
            result = await client.chat_json("sys", "user")
            assert result.get("_parse_error") is True


class TestParseJsonTruncatedRecovery:
    """_parse_json 截断 JSON 恢复路径 — 成功恢复日志 + 块提取失败分支（lines 203-204, 216-220）。"""

    def test_parse_truncated_dict_recovers_and_logs(self):
        """截断的 dict（缺少闭合 }）应恢复成功并记录日志（covers lines 216-220）。"""
        raw = '{"key": "value", "num": 42'
        result = LLMClient._parse_json(raw)
        assert result == {"key": "value", "num": 42}

    def test_parse_truncated_array_recovers_and_logs(self):
        """截断的 array（缺少闭合 ]）应恢复成功。"""
        raw = '[1, 2, 3'
        result = LLMClient._parse_json(raw)
        assert result == [1, 2, 3]

    def test_parse_nested_truncated_recovers(self):
        """多层嵌套截断应恢复（添加多个闭合符，仅限同类型括号）。"""
        # 仅缺失 } 的嵌套 dict 可以恢复（恢复逻辑只追加同类型闭合符）
        raw = '{"outer": {"inner": "val"'
        result = LLMClient._parse_json(raw)
        assert isinstance(result, dict)
        assert result["outer"]["inner"] == "val"

    def test_parse_invalid_json_block_passes_through(self):
        """含 { } 但块内非 JSON 时应跳过块提取分支（covers lines 203-204）。"""
        # text 包含 { 和 }，但提取的块不是合法 JSON
        raw = 'prefix {not valid json} suffix'
        result = LLMClient._parse_json(raw)
        assert result.get("_parse_error") is True

    def test_parse_invalid_array_block_passes_through(self):
        """含 [ ] 但块内非 JSON 时应跳过块提取分支。"""
        raw = 'prefix [not valid json] suffix'
        result = LLMClient._parse_json(raw)
        assert result.get("_parse_error") is True


class TestChatJsonParseRetry:
    """chat_json 解析失败重试 — 修正提示重试 + 全部失败后兜底。"""

    @pytest.mark.asyncio
    async def test_parse_failure_retries_with_fix_hint_and_recovers(self, test_db):
        """首次输出不可解析，第二次（带修正提示）输出合法 JSON → 返回解析结果。"""
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient(provider="deepseek")
            client.adapter.chat = AsyncMock(side_effect=[
                ChatResult(content="```json\n{invalid", model="m"),
                ChatResult(content='{"page": 19}', model="m"),
            ])
            result = await client.chat_json("sys", "user")
            assert result == {"page": 19}
            assert client.adapter.chat.call_count == 2
            # 第二次调用应携带修正提示
            second_content = client.adapter.chat.call_args[1]["user_content"]
            assert "无法解析为合法 JSON" in second_content
            assert second_content.startswith("user")  # 原内容在前，提示追加在后

    @pytest.mark.asyncio
    async def test_parse_failure_all_attempts_fail_returns_parse_error(self, test_db):
        """3 次（1 原始 + 2 修正重试）全部失败 → 仍返回 _parse_error，不抛异常。"""
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient(provider="deepseek")
            client.adapter.chat = AsyncMock(return_value=ChatResult(
                content="```json\n{still invalid", model="m",
            ))
            result = await client.chat_json("sys", "user")
            assert result.get("_parse_error") is True
            assert client.adapter.chat.call_count == 3

    @pytest.mark.asyncio
    async def test_parse_retry_records_audit_rows(self, test_db):
        """解析失败重试的每次 API 调用都应有审计记录（success=1）。"""
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient(provider="deepseek")
            client.adapter.chat = AsyncMock(side_effect=[
                ChatResult(content="```json\n{invalid", model="m"),
                ChatResult(content='{"ok": true}', model="m"),
            ])
            audit_ctx = {"job_id": "job-audit", "page": 7, "stage": "page_analysis"}
            await test_db.execute(
                "INSERT INTO jobs (id, filename, status) VALUES ('job-audit', 'a.pdf', 'pending')"
            )
            await test_db.commit()
            result = await client.chat_json("sys", "user", audit_ctx=audit_ctx)
            assert result == {"ok": True}
            cursor = await test_db.execute(
                "SELECT COUNT(*) FROM llm_call_audit WHERE job_id = ? AND success = 1",
                ("job-audit",),
            )
            assert (await cursor.fetchone())[0] == 2


class TestRecordLlmCall:
    """_record_llm_call — 审计记录写入 DB + 异常容错（lines 262-290）。"""

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        yield
        reset_llm_client()

    @pytest.mark.asyncio
    async def test_record_llm_call_success_writes_row(self, test_db):
        """成功调用时应向 llm_call_audit 表写入一行（covers lines 262-288）。"""
        from llm.client import _record_llm_call, get_llm_client, reset_llm_client
        from llm.adapters.base import ChatResult

        # 先插入 job 行以满足外键约束
        await test_db.execute(
            "INSERT INTO jobs (id, filename, status) VALUES (?, ?, ?)",
            ("audit-test-job", "test.pdf", "review"),
        )
        await test_db.commit()

        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            reset_llm_client()
            client = get_llm_client()

            ctx = {
                "job_id": "audit-test-job",
                "page": 1,
                "stage": "page_analysis",
                "prompt_version": "v3",
            }
            result = ChatResult(
                content="ok",
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                model="test-model",
            )

            await _record_llm_call(ctx, client, result, latency_ms=250, success=True)

            # 验证行已写入
            cursor = await test_db.execute(
                "SELECT * FROM llm_call_audit WHERE job_id = ?",
                ("audit-test-job",),
            )
            rows = await cursor.fetchall()
            assert len(rows) == 1
            row = dict(rows[0])
            assert row["provider"] == "deepseek"
            assert row["model"] == "test-model"
            assert row["prompt_tokens"] == 10
            assert row["total_tokens"] == 15
            assert row["latency_ms"] == 250
            assert row["success"] == 1

    @pytest.mark.asyncio
    async def test_record_llm_call_failure_writes_row(self, test_db):
        """失败调用时应写入 success=0 + error 信息。"""
        from llm.client import _record_llm_call, get_llm_client, reset_llm_client

        # 先插入 job 行以满足外键约束
        await test_db.execute(
            "INSERT INTO jobs (id, filename, status) VALUES (?, ?, ?)",
            ("audit-fail-job", "test.pdf", "error"),
        )
        await test_db.commit()

        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            reset_llm_client()
            client = get_llm_client()

            ctx = {"job_id": "audit-fail-job", "page": 2, "stage": "cross_page_llm"}

            await _record_llm_call(
                ctx, client, result=None, latency_ms=5000,
                success=False, error="API timeout",
            )

            cursor = await test_db.execute(
                "SELECT * FROM llm_call_audit WHERE job_id = ?",
                ("audit-fail-job",),
            )
            rows = await cursor.fetchall()
            assert len(rows) == 1
            row = dict(rows[0])
            assert row["success"] == 0
            assert row["error"] == "API timeout"
            assert row["prompt_tokens"] is None  # result=None

    @pytest.mark.asyncio
    async def test_record_llm_call_handles_db_error(self, test_db, monkeypatch):
        """DB 写入失败时应记录 warning 但不抛异常（covers lines 289-290）。"""
        from llm.client import _record_llm_call
        from llm.adapters.base import ChatResult

        # mock get_db 抛异常
        async def failing_get_db():
            raise RuntimeError("DB locked")

        monkeypatch.setattr("db.client.get_db", failing_get_db)

        ctx = {"job_id": "j", "page": 1, "stage": "page_analysis"}
        result = ChatResult(content="ok", model="m")

        # llm_client 传入 mock（DB 抛异常前不会访问 client 属性）
        fake_client = MagicMock()

        # 不应抛异常（best-effort）
        await _record_llm_call(ctx, fake_client, result, latency_ms=100, success=True)

    @pytest.mark.asyncio
    async def test_record_llm_call_with_none_result(self, test_db):
        """result=None 时应正确写入 NULL token 值。"""
        from llm.client import _record_llm_call, get_llm_client, reset_llm_client

        # 先插入 job 行以满足外键约束
        await test_db.execute(
            "INSERT INTO jobs (id, filename, status) VALUES (?, ?, ?)",
            ("none-result-job", "test.pdf", "error"),
        )
        await test_db.commit()

        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            reset_llm_client()
            client = get_llm_client()

            ctx = {"job_id": "none-result-job", "stage": "page_analysis"}
            await _record_llm_call(ctx, client, result=None, latency_ms=0, success=False, error="err")

            cursor = await test_db.execute(
                "SELECT * FROM llm_call_audit WHERE job_id = ?",
                ("none-result-job",),
            )
            rows = await cursor.fetchall()
            assert len(rows) == 1
            row = dict(rows[0])
            assert row["prompt_tokens"] is None
            assert row["completion_tokens"] is None
            assert row["total_tokens"] is None
