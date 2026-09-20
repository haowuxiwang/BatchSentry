"""Reading settings — GET /api/settings with masked secrets."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import HTTPException, Request

from config import _is_real_key, config, load_feishu_config
import config as _config_module
from core.security import is_local_request
from api.settings import router

logger = logging.getLogger(__name__)


def _settings_config_path() -> Path:
    """返回 JSON 配置文件路径（与 config.py 的加载逻辑一致）。"""
    # Runtime resolution — tests monkeypatch api.settings._config_path.
    from api.settings import _config_path
    return _config_path()


def _mask(value: str) -> str:
    """脱敏：只显示前4位和后4位，中间用 **** 代替。"""
    if not value or len(value) <= 12:
        return "*" * len(value) if value else ""
    return value[:4] + "****" + value[-4:]


def _providers_payload() -> list[dict]:
    """构造 provider 列表用于 GET 响应（脱敏 + 标记是否已配置）。

    Phase 8: improved "configured" detection — filters out obvious test
    placeholders (sk-test, sk-glm-test, sk-ant-test, sk-example, etc.) so
    the UI doesn't mislead users into thinking a provider is ready when
    only a test value was set.
    """
    payload = []
    for name, prov in config["providers"].items():
        is_real_key = _is_real_key(prov.api_key)
        payload.append({
            "name": prov.name,
            "protocol": prov.protocol,
            "api_key": _mask(prov.api_key),
            "base_url": prov.base_url,
            "model": prov.model,
            "configured": is_real_key,
        })
    # 按名称排序，保证 UI 顺序稳定
    payload.sort(key=lambda p: p["name"])
    return payload


# 🔴 B4-4：`_is_real_api_key` 原是 `config._is_real_key` 的**手抄副本**（同样子串
# 匹配）⇒ 两处会各自漂移，且"UI 的 configured 标志"与"启动期的切换判定"口径不一。
# 现已统一为 `config._is_real_key`（唯一实现点，精确校验）。
# ⚠️ 保留本别名仅为兼容可能残留的引用；**新代码请直接用 `_is_real_key`**。
_is_real_api_key = _is_real_key


@router.get("/api/settings")
async def get_settings(request: Request):
    """返回当前配置（敏感字段脱敏）。

    对抗审查（cr-15）：GET 是简单请求（无 preflight），此前无守卫 —
    任意网页可跨站读取本机配置（含 config 绝对路径、provider base_url、
    飞书接收人）。与 POST /api/settings 的守卫对齐。
    """
    if not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    cfg = config
    config_file = _settings_config_path()
    # 单次读取飞书配置（此前每个字段各 load_feishu_config() 一次，共 9 次文件 IO）
    feishu_cfg = load_feishu_config()
    return {
        "config_file": str(config_file),
        "config_exists": config_file.exists(),
        "llm": {
            "provider": cfg["app"].llm_provider,
            # S3: 新增 active_provider 别名（明确语义，与 provider 字段保持一致）
            "active_provider": cfg["app"].llm_provider,
            # 🔴 B4-4 ②：启动期若发生过 provider 自动改写（或被规则**拒绝**改写），
            # 把事实回传给前端显示 —— 否则用户看到的就是"provider 被静默换掉"。
            # 形如 {"applied": bool, "from": str, "to": str|None, "reason": str}；
            # None = 本次启动无事发生。**决策本身只发生在 config 一处**。
            # ⚠️ 必须**调用期**读属性（`_config_module.AUTO_ACTIVATE_NOTICE`），
            # 不能用 `from config import AUTO_ACTIVATE_NOTICE` —— 那是**导入期取值**，
            # 进程内该状态后续变化时此处读到的仍是旧值（实测被集成测试抓住）。
            "auto_activated": _config_module.AUTO_ACTIVATE_NOTICE,
            # 动态 provider 注册表（前端按 list 渲染表单）
            "providers": _providers_payload(),
            # 向后兼容字段（旧前端仍可读取，但应迁移到 providers）
            "deepseek": {
                "api_key": _mask(cfg["deepseek"].api_key),
                "base_url": cfg["deepseek"].base_url,
                "model": cfg["deepseek"].model,
                "configured": _is_real_api_key(cfg["deepseek"].api_key),
            },
            "siliconflow": {
                "api_key": _mask(cfg["siliconflow"].api_key),
                "base_url": cfg["siliconflow"].base_url,
                "model": cfg["siliconflow"].model,
                "configured": _is_real_api_key(cfg["siliconflow"].api_key),
            },
        },
        "ocr": {
            "backend": cfg["app"].ocr_backend,
            "slices": getattr(cfg["app"], "ocr_slices", 1),
            "dual_compare": getattr(cfg["app"], "ocr_dual_compare", False),
            "json_mode": getattr(cfg["app"], "llm_json_mode", False),
            "paddle": {
                "api_url": cfg["paddle_ocr"].api_url,
                "token": _mask(cfg["paddle_ocr"].token),
                "model": cfg["paddle_ocr"].model,
                "configured": _is_real_api_key(cfg["paddle_ocr"].token),
            },
            "mineru": {
                "token": _mask(cfg["mineru"].token),
                "base_url": getattr(cfg["mineru"], "base_url", ""),
                "model_version": cfg["mineru"].model_version,
                "language": cfg["mineru"].language,
                "enable_formula": cfg["mineru"].enable_formula,
                "enable_table": cfg["mineru"].enable_table,
                "configured": _is_real_api_key(cfg["mineru"].token),
            },
        },
        "app": {
            "host": cfg["app"].host,
            "port": cfg["app"].port,
        },
        "feishu": {
            "enabled": bool(feishu_cfg.get("enabled", False)),
            "mode": feishu_cfg.get("mode", "webhook"),
            "webhook_url": _mask(feishu_cfg.get("webhook_url", "")),
            "secret": _mask(feishu_cfg.get("secret", "")),
            "app_id": feishu_cfg.get("app_id", ""),
            "app_secret": _mask(feishu_cfg.get("app_secret", "")),
            "open_id": feishu_cfg.get("open_id", ""),
            "mobile": feishu_cfg.get("mobile", ""),
            "events": feishu_cfg.get("events") or ["review", "partial_review", "error"],
        },
    }
