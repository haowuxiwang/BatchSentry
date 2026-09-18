"""Abstract LLM adapter — defines the uniform interface every protocol adapter
must implement. Keeping the surface tiny (just `chat`) makes adding a new
protocol a localized change.
"""
from __future__ import annotations

import abc
import base64
from dataclasses import dataclass
from typing import Union

from config import ProviderConfig


@dataclass
class ChatResult:
    """Normalized chat-completion result returned by every adapter.

    Attributes:
        content: The assistant message text.
        prompt_tokens / completion_tokens / total_tokens: Token usage if the
            upstream API reported it. None when the API doesn't return usage.
        model: The model identifier actually used (useful for audit logs and
            the health probe, which needs to report which model served the
            request).
    """
    content: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    model: str = ""


@dataclass(frozen=True)
class ImagePart:
    """**协议无关**的图像输入片段（#159）。

    调用方只提供"字节 + MIME"，由**各 adapter 自己**翻译成所属协议的报文
    形态（OpenAI 的 `image_url` data-URL / Anthropic 的 `image` + `source`）。
    这正是本模块存在的理由 —— 把协议细节留给调用方，等于要求 #159 的每个
    调用点都各自学一遍两套协议的差异，迟早漂移。
    """
    data: bytes
    media_type: str = "image/jpeg"

    @property
    def base64_data(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    def data_url(self) -> str:
        """OpenAI 兼容协议用的 data-URL 形态。"""
        return f"data:{self.media_type};base64,{self.base64_data}"


ContentPart = Union[str, ImagePart]
ContentInput = Union[str, list[ContentPart]]


def content_parts(user_content: ContentInput) -> list[ContentPart]:
    """归一成 list（纯文本 → 单元素）。adapter 翻译报文时用。"""
    if isinstance(user_content, str):
        return [user_content]
    return list(user_content)


def append_text_part(user_content: ContentInput, extra: str) -> ContentInput:
    """在 user_content 末尾追加一段文本，**并保持原有形态**。

    纯文本进 → 纯文本出（字节级不变：既有调用路径的请求体不得因本次扩展
    而漂移）；多模态进 → 追加一个 text 片段（`list + str` 会 TypeError，
    这正是扩展签名时必须一并处理的调用点。
    """
    if isinstance(user_content, str):
        return user_content + extra
    return [*user_content, extra]


class LLMAdapter(abc.ABC):
    """Protocol-agnostic LLM adapter.

    Subclasses wrap a specific SDK (openai, anthropic, ...) and translate
    our uniform `chat()` call into the provider's wire format.
    """

    def __init__(self, provider_cfg: ProviderConfig):
        self.provider_cfg = provider_cfg
        self.provider_name = provider_cfg.name
        self.protocol = (provider_cfg.protocol or "openai").lower()
        self.model = provider_cfg.model

    @abc.abstractmethod
    async def chat(
        self,
        system_prompt: str,
        user_content: ContentInput,
        max_tokens: int = 4000,
        temperature: float = 0.1,
        timeout: float = 180.0,
        response_format: dict | None = None,
    ) -> ChatResult:
        """Send a single chat completion request and return a ChatResult.

        user_content: 纯文本（`str`）或多模态片段列表（`list[str | ImagePart]`，
        #159）。**纯文本必须走原路径**——不得为了统一而把它包装成 parts 列表：
        那会改变每一次调用的请求体形态，影响所有既有 provider 的兼容性。

        response_format: 协议支持时的结构化输出约束（如 OpenAI json_object）。
        不支持的实现可忽略；不支持该参数的网关会 400，由 client 降级重试。

        Implementations should NOT retry — the LLMClient owns the retry loop.
        Implementations SHOULD raise on transport/auth errors so the retry
        loop can decide whether to back off.
        """
        ...

    @abc.abstractmethod
    def client_info(self) -> dict:
        """Return a small dict describing this adapter's underlying client.

        Used by the /api/health/downstream probe to report which provider /
        model / base_url is configured without exposing the API key.
        """
        ...

    @property
    def is_configured(self) -> bool:
        """True if this provider has an API key set (i.e. can be used)."""
        return bool(self.provider_cfg.api_key)
