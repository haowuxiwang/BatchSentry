"""Active provider switch + display names."""
from __future__ import annotations

import json
import logging
import os
import uuid

from fastapi import HTTPException, Request
from pydantic import BaseModel

from config import config, update_config
from core.security import is_local_request
from db.client import get_db
from llm.client import reset_llm_client
from api.settings import router
from api.settings.read import _providers_payload, _settings_config_path
from api.settings.write import _validate_provider_name

logger = logging.getLogger(__name__)


class SetActiveRequest(BaseModel):
    """切换 active provider 请求。"""
    provider: str


def _persist_config_field(env_key: str, value: str) -> None:
    """原子写入单个字段到 config.json + 同步 os.environ（复用 POST 端点逻辑）。"""
    config_path = _settings_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing_config: dict[str, str] = {}
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8-sig") as f:
                existing_config = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing_config = {}
    existing_config[env_key] = value
    tmp_path = config_path.parent / f"config.json.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(existing_config, f, indent=2, ensure_ascii=False)
        tmp_path.replace(config_path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    os.environ[env_key] = value


@router.post("/api/settings/set_active_provider")
async def set_active_provider(req: SetActiveRequest, request: Request):
    """切换 active provider — 立即持久化到 config.json + 内存热更新 + 重建 LLM 单例。

    业界做法（OpenAI/Anthropic/Linear）：切换 active 配置即生效，
    不需要用户再点底部"保存"按钮。避免用户切换后忘记保存导致配置丢失。
    """
    if not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")

    name = req.provider.strip().lower()
    if not _validate_provider_name(name):
        raise HTTPException(400, f"Invalid provider name: {name!r}")

    providers = config["providers"]
    if name not in providers:
        raise HTTPException(
            404,
            f"Provider {name!r} not in registry (available: {sorted(providers)})",
        )

    # 1. 持久化到 config.json
    _persist_config_field("LLM_PROVIDER", name)
    # 2. 内存热更新
    update_config({"llm_provider": name})
    # 3. 重建 LLM 单例
    reset_llm_client()

    # 对抗审查：provider 切换写 audit_log（GMP 追溯 — LLM 后端变更影响
    # 审计链，须留痕）。只记名称，不记任何凭据。
    try:
        db = await get_db()
        await db.execute(
            "INSERT INTO audit_log (job_id, action, detail, created_at) VALUES (?, ?, ?, datetime(\'now\',\'localtime\'))",
            ("system", "provider_switch", f"active_provider={name}"),
        )
        await db.commit()
    except Exception as e:
        logger.warning(f"Failed to write provider_switch audit log: {e}")

    logger.info(f"Active provider switched to {name!r} (live)")
    return {
        "ok": True,
        "active_provider": name,
        "message": f"已切换到 {display_name_for(name)}",
        "providers": _providers_payload(),
    }


def display_name_for(name: str) -> str:
    """Provider 名称显示转换（前端 DISPLAY_NAMES 的后端镜像）。"""
    display_map = {
        "deepseek": "DeepSeek",
        "siliconflow": "SiliconFlow",
        "glm": "GLM · 智谱",
        "kimi": "Kimi · 月之暗面",
        "qwen": "Qwen · 通义千问",
        "mimo": "MiMo · 小米",
        "anthropic": "Anthropic · Claude",
        "openai": "OpenAI",
    }
    return display_map.get(name, name)
