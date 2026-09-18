"""#151 — Anthropic Messages 协议分支的**运行证据**。

**本项原先的缺口**：本机只配置了 `siliconflow` / `deepseek` 两个 provider，
`protocol` 都是 `openai` ⇒ `AnthropicAdapter` 这条分支**零运行证据**，
只有"代码看起来处理了四处协议差异"这一句话。

**本文件的做法**：起一个**忠实于 Anthropic Messages API 报文形态**的本地
HTTP 桩，让**真实的** `AnthropicAdapter` + `LLMClient` 走**真实 socket** 往返
（不是 mock 掉 SDK 调用），从而逐项验证那些"只写在注释里"的协议差异：

    - `x-api-key` 头（而非 `Authorization: Bearer`）
    - `anthropic-version` 头
    - `system` 是**顶层字段**，不是 messages 数组里的一条
    - 响应取 `content[].text` 拼接（而非 `choices[0].message.content`）
    - `usage.input_tokens` / `output_tokens` → prompt/completion/total

⚠️ **诚实边界**：这是**本地协议桩**，不是厂商真机。它证明了"我们的请求形态
与解析逻辑符合该协议"，**没有**证明"与 api.anthropic.com 实际连通"（那需要
真实 key 与出网）。未覆盖部分在 `docs/TODO.md` 的 #151 条目里如实留白。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

anthropic = pytest.importorskip(
    "anthropic", reason="anthropic SDK 未安装 —— 该项只能跳过（不伪绿）"
)

from config import ProviderConfig  # noqa: E402
from llm.adapters import get_adapter  # noqa: E402
from llm.client import LLMClient, LLMConfigError  # noqa: E402

MODEL = "claude-stub-1"


# ── 本地协议桩 ────────────────────────────────────────────────────────


class _StubServer:
    """记录收到的每个请求，并按调用序回应脚本里的下一项。"""

    def __init__(self, script):
        self.requests: list[dict] = []
        self._script = script
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802 (stdlib 命名)
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                outer.requests.append({
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": json.loads(raw.decode("utf-8")),
                })
                status, payload = outer._script(len(outer.requests) - 1)
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):  # 静音（否则 stderr 被 access log 淹没）
                pass

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._srv.serve_forever, daemon=True
        )
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._srv.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._thread.join(timeout=5)


def _ok_payload(text: str, in_tok: int = 11, out_tok: int = 22) -> dict:
    """一条合法的 Anthropic Messages 响应。"""
    return {
        "id": "msg_stub",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
    }


def _err_payload(kind: str, message: str) -> dict:
    return {"type": "error", "error": {"type": kind, "message": message}}


@pytest.fixture
def stub():
    servers: list[_StubServer] = []

    def _make(script):
        s = _StubServer(script)
        servers.append(s)
        return s

    yield _make
    for s in servers:
        s.close()


def _adapter_and_client(base_url: str, **cfg_overrides):
    cfg = ProviderConfig(
        name="anthropic-stub",
        protocol="anthropic",
        api_key="sk-ant-stub-key",
        base_url=base_url,
        model=MODEL,
    )
    for k, v in cfg_overrides.items():
        setattr(cfg, k, v)
    adapter = get_adapter(cfg)
    client = LLMClient.__new__(LLMClient)
    client.provider = cfg.name
    client.adapter = adapter
    client.client = getattr(adapter, "client", None)
    client.model = MODEL
    return adapter, client


# ── 1. 成功路径：协议差异逐项核对 ─────────────────────────────────────


class TestAnthropicWireFormat:
    @pytest.mark.asyncio
    async def test_round_trip_and_protocol_differences(self, stub):
        """真实 adapter + client 走真实 HTTP：四处协议差异逐项断言。"""
        srv = stub(lambda i: (200, _ok_payload("你好，这是模型回答。")))
        _, client = _adapter_and_client(srv.base_url)

        out = await client.chat(
            "你是 GMP 复核助手", "请复核第 1 页",
            max_tokens=1234, temperature=0.2, retries=1,
        )

        # ① 出站路径与鉴权头
        assert len(srv.requests) == 1, "应只发一次请求"
        req = srv.requests[0]
        assert req["path"].endswith("/v1/messages"), (
            f"Anthropic 协议应打到 /v1/messages，实测 {req['path']}"
        )
        assert req["headers"].get("x-api-key") == "sk-ant-stub-key", (
            "Anthropic 用 x-api-key 头，不是 Authorization: Bearer"
        )
        assert "authorization" not in req["headers"], (
            "出现 Authorization 头 ⇒ 走错了协议分支"
        )
        assert req["headers"].get("anthropic-version"), (
            "缺 anthropic-version 头（Anthropic 必填）"
        )

        # ② 请求体：system 是顶层字段，不在 messages 里
        body = req["body"]
        assert body.get("system") == "你是 GMP 复核助手", (
            "system 必须是顶层字段（OpenAI 才把 system 放进 messages）"
        )
        assert [m["role"] for m in body["messages"]] == ["user"], (
            "messages 里不应出现 system 角色"
        )
        assert body.get("max_tokens") == 1234, "max_tokens 未透传"
        assert body.get("temperature") == pytest.approx(0.2)
        assert body.get("model") == MODEL

        # ③ 响应解析：content[].text 拼接
        assert out == "你好，这是模型回答。"

    @pytest.mark.asyncio
    async def test_usage_fields_are_mapped_from_input_output_tokens(self, stub):
        """`input_tokens`/`output_tokens` 必须映射成 prompt/completion/total。

        这两套字段名不通用 —— 映射漏了会让 GMP 审计里的 token 用量恒为
        None（"没有用量信息"），而实际是取错了字段。
        """
        srv = stub(lambda i: (200, _ok_payload("{}", in_tok=100, out_tok=7)))
        adapter, _ = _adapter_and_client(srv.base_url)

        res = await adapter.chat("sys", "user")
        assert (res.prompt_tokens, res.completion_tokens, res.total_tokens) == (
            100, 7, 107
        ), f"用量字段映射错误：{res}"

    @pytest.mark.asyncio
    async def test_multiple_text_blocks_are_concatenated(self, stub):
        """多 content block 必须拼接（Anthropic 允许分块返回）。"""
        payload = _ok_payload("ignored")
        payload["content"] = [
            {"type": "text", "text": '{"a":'},
            {"type": "text", "text": "1}"},
        ]
        srv = stub(lambda i: (200, payload))
        adapter, _ = _adapter_and_client(srv.base_url)

        res = await adapter.chat("sys", "user")
        assert res.content == '{"a":1}'


# ── 2. 失败路径：与 #143 的分级判据对接 ───────────────────────────────


class TestAnthropicErrorClassification:
    """Anthropic 的异常也必须被 #143 的结构化判据正确分级。

    这条链路此前没有任何运行证据：若 anthropic SDK 的异常不带 `status_code`
    （或我们的判据只认 openai 的形状），401 会退化成"重试 3 次后页级失败"
    —— 即 #127 要消灭的假阴性，只是换了协议。
    """

    @pytest.mark.asyncio
    async def test_401_is_config_error_and_not_retried(self, stub):
        srv = stub(lambda i: (401, _err_payload(
            "authentication_error", "invalid x-api-key")))
        _, client = _adapter_and_client(srv.base_url)

        with pytest.raises(LLMConfigError):
            await client.chat("sys", "user", retries=3)
        assert len(srv.requests) == 1, "凭据失效不得重试"

    @pytest.mark.asyncio
    async def test_429_is_retryable_not_config_error(self, stub):
        """限流在 anthropic 协议下同样必须可重试（#143 的跨协议一致性）。"""
        srv = stub(lambda i: (429, _err_payload(
            "rate_limit_error", "rate limit exceeded")))
        _, client = _adapter_and_client(srv.base_url)

        with pytest.raises(RuntimeError) as excinfo:
            await client.chat("sys", "user", retries=2)
        assert not isinstance(excinfo.value, LLMConfigError), (
            "Anthropic 429 被判成配置级 ⇒ 会早停整份文档"
        )
        # ⚠️ 只断言"确实重试了"，不断言精确次数：实测 client `retries=2`
        # 时桩收到 **6** 个请求 —— 因为 anthropic SDK **自带** max_retries
        # （每次 create 最多 3 次尝试），两层重试相乘。这是既有行为，
        # 已登记为 #174（不在本轮修）；本用例刻意不锁死次数，免得把
        # "某个未定案的实现细节"固化成契约。
        assert len(srv.requests) >= 2, "限流应重试"

    @pytest.mark.asyncio
    async def test_500_is_retryable(self, stub):
        srv = stub(lambda i: (500, _err_payload(
            "api_error", "internal server error")))
        _, client = _adapter_and_client(srv.base_url)

        with pytest.raises(RuntimeError) as excinfo:
            await client.chat("sys", "user", retries=2)
        assert not isinstance(excinfo.value, LLMConfigError)
        assert len(srv.requests) >= 2


# ── 3. response_format 在 anthropic 侧必须被忽略 ──────────────────────


class TestAnthropicIgnoresResponseFormat:
    @pytest.mark.asyncio
    async def test_response_format_is_ignored_not_forwarded(self, stub):
        """Anthropic 无等价参数 ⇒ 必须忽略（而非透传成未知字段被 400）。

        `LLM_JSON_MODE` 开启时 client 会传 response_format；若 adapter 原样
        透传，每次调用都会被网关 400，然后触发"降级重试一次"——
        表现为**每页白白多打一次失败请求**，且日志持续刷 400。
        """
        srv = stub(lambda i: (200, _ok_payload("{}")))
        adapter, _ = _adapter_and_client(srv.base_url)

        await adapter.chat("sys", "user", response_format={"type": "json_object"})
        body = srv.requests[0]["body"]
        assert "response_format" not in body, (
            "response_format 被透传给 Anthropic（该协议无此参数）"
        )
