"""#159 — 多模态输入的**协议翻译**契约（adapter 层）。

**这一层此前根本不存在**：`llm/client.py` 的 `user_content` 是 `str`，
`openai_adapter` 直接把它塞进 `messages`。也就是说产品链路**喂不进图像**，
所以"视觉第四读"从来不是调提示词的问题，而是这条签名。

本文件锁三件事：

1. **纯文本路径字节级不变** —— 多模态是为新增场景服务的，不得顺手把每一次
   普通调用都改成 parts 列表（那会让所有既有 provider 的请求体变样）。
2. **协议差异留在 adapter 里** —— 同一份"字节 + MIME"的输入，OpenAI 侧出
   `image_url` data-URL、Anthropic 侧出 `image` + `source.base64`；
   调用方（含将来的 #159）不需要知道这件事。
3. **扩展签名的连带调用点** —— `chat_json` 的 fix-hint 分支原先做
   `user_content + "..."` 字符串拼接，多模态下会 TypeError。
"""
import base64
from types import SimpleNamespace

import pytest

from config import ProviderConfig
from llm.adapters import get_adapter
from llm.adapters.base import ImagePart, append_text_part, content_parts

PNG = b"\x89PNG\r\n\x1a\n-fake-png-bytes"


class _Recorder:
    """假 SDK 客户端：记录 `create(**kwargs)`，支持 `chat.completions.create`
    这类链式属性访问（`__getattr__` 对未知属性返回自身）。"""

    def __init__(self, result):
        self.kwargs: dict | None = None
        self._result = result

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self._result


def _openai_resp(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4, total_tokens=7),
    )


def _anthropic_resp(text: str):
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)],
        usage=SimpleNamespace(input_tokens=3, output_tokens=4),
    )


def _adapter(protocol: str, result):
    cfg = ProviderConfig(
        name=f"{protocol}-stub", protocol=protocol,
        api_key="k", base_url="http://127.0.0.1:1", model="m",
    )
    ad = get_adapter(cfg)
    ad.client = _Recorder(result)
    return ad


# ── 1. 中性输入的构造件 ───────────────────────────────────────────────


class TestContentPrimitives:
    def test_image_part_encodes_base64_and_data_url(self):
        p = ImagePart(data=PNG, media_type="image/png")
        assert p.base64_data == base64.b64encode(PNG).decode("ascii")
        assert p.data_url() == (
            "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")
        )

    def test_content_parts_normalizes_str(self):
        assert content_parts("hello") == ["hello"]
        assert content_parts(["a", "b"]) == ["a", "b"]

    def test_append_text_part_preserves_shape(self):
        """纯文本进 → 纯文本出；多模态进 → 追加 text 片段（不是 list+str）。"""
        assert append_text_part("hello", "!") == "hello!"
        out = append_text_part(["a", ImagePart(PNG)], "tail")
        assert isinstance(out, list) and out[0] == "a" and out[-1] == "tail"
        assert isinstance(out[1], ImagePart)


# ── 2. OpenAI 兼容协议 ────────────────────────────────────────────────


class TestOpenAIContent:
    @pytest.mark.asyncio
    async def test_plain_text_content_stays_a_string(self):
        """回归门：纯文本**不得**被包装成 parts 列表（报文形态漂移）。"""
        ad = _adapter("openai", _openai_resp("ok"))
        await ad.chat("sys", "纯文本提示")
        content = ad.client.kwargs["messages"][1]["content"]
        assert isinstance(content, str), (
            f"纯文本被改写成了 {type(content).__name__} —— "
            "每一次普通调用的请求体都变了，兼容性风险由既有 provider 承担"
        )
        assert content == "纯文本提示"

    @pytest.mark.asyncio
    async def test_image_becomes_image_url_data_url(self):
        ad = _adapter("openai", _openai_resp("ok"))
        await ad.chat("sys", ["看这张图", ImagePart(PNG, "image/png")])
        content = ad.client.kwargs["messages"][1]["content"]
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "看这张图"}
        img = content[1]
        assert img["type"] == "image_url", f"OpenAI 协议应用 image_url：{img}"
        assert img["image_url"]["url"].startswith("data:image/png;base64,")


# ── 3. Anthropic 协议（报文形态**不同**，正是本层存在的理由）───────────


class TestAnthropicContent:
    @pytest.mark.asyncio
    async def test_plain_text_content_stays_a_string(self):
        pytest.importorskip("anthropic")
        ad = _adapter("anthropic", _anthropic_resp("ok"))
        await ad.chat("sys", "纯文本提示")
        content = ad.client.kwargs["messages"][0]["content"]
        assert isinstance(content, str) and content == "纯文本提示"

    @pytest.mark.asyncio
    async def test_image_becomes_base64_source_block(self):
        pytest.importorskip("anthropic")
        ad = _adapter("anthropic", _anthropic_resp("ok"))
        await ad.chat("sys", ["看这张图", ImagePart(PNG, "image/png")])
        content = ad.client.kwargs["messages"][0]["content"]
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "看这张图"}
        img = content[1]
        assert img["type"] == "image", f"Anthropic 协议应用 image 块：{img}"
        assert img["source"] == {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(PNG).decode("ascii"),
        }
        # 反面：不得把 OpenAI 的 image_url 形态透传给 Anthropic
        assert "image_url" not in str(content), "OpenAI 报文形态漏进了 Anthropic"


# ── 4. client 层：fix-hint 重试路径必须兼容多模态 ─────────────────────


class _FakeClientAdapter:
    """第一轮返回不可解析文本，第二轮返回合法 JSON。"""

    def __init__(self):
        self.seen: list = []
        self.protocol = "openai"

    async def chat(self, system_prompt, user_content, **kwargs):
        from llm.adapters.base import ChatResult
        self.seen.append(user_content)
        if len(self.seen) == 1:
            return ChatResult(content="这不是 JSON")
        return ChatResult(content='{"ok": true}')


class TestClientJsonFixHintPath:
    @pytest.mark.asyncio
    async def test_fix_hint_retry_works_with_multimodal_content(self):
        """`user_content + "..."` 在 list 形态下会 TypeError —— 必须已改掉。

        这条路径只在**输出不可解析**时触发，日常测试打不到；一旦漏改，
        表现为"带图的页在 JSON 修复重试时抛 TypeError 直接失败"，
        而且只在偶发解析失败时出现 —— 典型的"上线才炸"。
        """
        from llm.client import LLMClient

        adapter = _FakeClientAdapter()
        client = LLMClient.__new__(LLMClient)
        client.provider = "test"
        client.adapter = adapter
        client.model = "m"

        out = await client.chat_json("sys", ["看图", ImagePart(PNG)], retries=1)
        assert out == {"ok": True}, "fix-hint 重试未成功"
        assert len(adapter.seen) == 2, "应发生一次修复重试"
        assert isinstance(adapter.seen[0], list), "首轮应收到多模态片段"
        second = adapter.seen[1]
        assert isinstance(second, list), (
            "修复重试把多模态内容降级成了字符串（图像被丢弃）"
        )
        assert "系统提示" in second[-1], "修复提示未追加到末尾"
