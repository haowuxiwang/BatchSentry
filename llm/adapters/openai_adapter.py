"""OpenAI Chat Completions protocol adapter.

Covers any provider that speaks OpenAI's `/v1/chat/completions` wire format:
  - DeepSeek (api.deepseek.com/v1)
  - SiliconFlow (api.siliconflow.cn/v1)
  - GLM / Zhipu (open.bigmodel.cn/api/paas/v4)
  - Kimi / Moonshot (api.moonshot.cn/v1)
  - Qwen / DashScope compatible mode (dashscope.aliyuncs.com/compatible-mode/v1)
  - MiMo / Xiaomi (api.mimo.xiaomi.com/v1) — when available
  - OpenAI itself (api.openai.com/v1)

Uses the official `openai` AsyncOpenAI SDK pointed at the provider's base_url.

⚠️ **SDK 必须延迟导入**（R95/启动性能，2026-10-21 实测）：`import openai`
会拉起 **1007 个模块**，而 `import main` 全链只有 **1340 个** —— 即启动期
75% 的模块导入来自这一个 SDK，而本模块只用到其中一个符号。冷缓存 + 杀软
实时扫描下每个模块文件都要读盘：实测用户首次双击到 `/health` 就绪
**35.9s**，其中 `import main` 段占 **19.4s**；同一产物缓存热时全程 **2.7s**。
⇒ 在 `__init__` 里导入（首次真正建客户端时才付这笔钱，那时已经在跑 pipeline，
分钟级任务里 1.3s 可忽略）。**不要把它移回模块顶部**，护栏
`tests/unit/test_startup_import_cost.py`。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型检查期可见；运行期零导入（见模块 docstring）
    from openai import AsyncOpenAI  # noqa: F401  只出现在注解里（`from __future__ import annotations`）

from config import ProviderConfig
from .base import (
    LLMAdapter, ChatResult, ContentInput, ImagePart, content_parts,
    normalize_finish_reason,
)

logger = logging.getLogger(__name__)


def _to_openai_content(user_content: ContentInput) -> str | list[dict]:
    """把协议无关的输入翻译成 OpenAI Chat Completions 的 content 形态。

    ⚠️ **纯文本必须原样返回字符串**（而不是 `[{"type":"text",...}]`）：
    两者在该协议下等价，但前者是既有全部调用路径的报文形态 —— 为了支持
    多模态而顺手统一成 parts 列表，会让每一次普通调用的请求体都变样，
    徒增与各家兼容网关的兼容风险。
    """
    parts = content_parts(user_content)
    if len(parts) == 1 and isinstance(parts[0], str):
        return parts[0]
    out: list[dict] = []
    for p in parts:
        if isinstance(p, ImagePart):
            out.append({"type": "image_url", "image_url": {"url": p.data_url()}})
        else:
            out.append({"type": "text", "text": p})
    return out


class OpenAIAdapter(LLMAdapter):
    """Adapter for OpenAI-compatible Chat Completions API."""

    def __init__(self, provider_cfg: ProviderConfig):
        super().__init__(provider_cfg)
        # 延迟导入（模块 docstring 有实测依据）：首次建客户端才付 SDK 的
        # 1007 个模块导入成本，冷启动因此不再替它读盘。
        from openai import AsyncOpenAI  # noqa: PLC0415

        # base_url may be empty for genuine OpenAI, but we always pass it
        # explicitly to avoid surprises when the env points elsewhere.
        self.client = AsyncOpenAI(
            api_key=provider_cfg.api_key or "EMPTY",
            base_url=provider_cfg.base_url or None,
        )

    async def chat(
        self,
        system_prompt: str,
        user_content: ContentInput,
        max_tokens: int = 4000,
        temperature: float = 0.1,
        timeout: float = 180.0,
        response_format: dict | None = None,
    ) -> ChatResult:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _to_openai_content(user_content)},
        ]
        kwargs = {}
        if response_format:
            # 结构化输出（LLM_JSON_MODE 开启时由 client 传入）：
            # OpenAI 兼容层的 json_object 模式在服务端约束输出为合法
            # JSON，从源头减少截断/围栏/前导文本三类解析失败。
            # 部分兼容网关不支持该参数（400）— client 捕获后降级重试。
            kwargs["response_format"] = response_format
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            **kwargs,
        )
        content = resp.choices[0].message.content or ""
        usage = resp.usage
        # 0-38：采集结束原因（`length` = 被 max_tokens 截断）。SDK 缺该字段时
        # 取 None ⇒ normalize 返回 ""（「未报」≠「正常收尾」）。
        raw_finish = getattr(resp.choices[0], "finish_reason", None)
        return ChatResult(
            content=content,
            prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            completion_tokens=getattr(usage, "completion_tokens", None) if usage else None,
            total_tokens=getattr(usage, "total_tokens", None) if usage else None,
            model=self.model,
            finish_reason=normalize_finish_reason(raw_finish, self.protocol),
            raw_finish_reason=str(raw_finish or ""),
        )

    def client_info(self) -> dict:
        return {
            "provider": self.provider_name,
            "protocol": self.protocol,
            "model": self.model,
            "base_url": self.provider_cfg.base_url,
            "configured": self.is_configured,
        }
