"""Updates — POST /api/settings: validation + env building + hot apply."""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel

from config import (
    FEISHU_ALLOWED_EVENTS,
    ProviderConfig,
    config,
    load_feishu_config,
    update_config,
)
from core.security import is_local_request, validate_external_url
from db.client import get_db
from llm.client import reset_llm_client
from api.settings import _ALLOWED_PROTOCOLS, _PROVIDER_NAME_RE, router
from api.settings.read import _mask, _providers_payload, _settings_config_path

logger = logging.getLogger(__name__)


# config.py 各持一份，加新模式需改两处，改一半会行为漂移 —


_PER_PROVIDER_FIELDS = ("protocol", "api_key", "base_url", "model")


_STATIC_FIELDS = {
    "llm_provider": "LLM_PROVIDER",
    "llm_providers_add": "LLM_PROVIDERS",  # 追加到 LLM_PROVIDERS env var
    "llm_providers_remove": "LLM_PROVIDERS",  # 从 LLM_PROVIDERS env var 差集移除
    "ocr_backend": "OCR_BACKEND",
    "ocr_slices": "OCR_SLICES",  # MinerU 分片 OCR（流式输出）页数/片
    "paddle_ocr_api_url": "PADDLE_OCR_API_URL",
    "paddle_ocr_token": "PADDLE_OCR_TOKEN",
    "paddle_ocr_model": "PADDLE_OCR_MODEL",
    "mineru_token": "MINERU_TOKEN",
    "mineru_base_url": "MINERU_BASE_URL",
    "mineru_model_version": "MINERU_MODEL_VERSION",
    "mineru_language": "MINERU_LANGUAGE",
    "mineru_enable_formula": "MINERU_ENABLE_FORMULA",
    "mineru_enable_table": "MINERU_ENABLE_TABLE",
    # 飞书通知（Phase 12）— 全走 _read_raw_config/_write_raw_config 落 config.json
    "feishu_enabled": "feishu_enabled",
    "feishu_mode": "feishu_mode",
    "feishu_webhook_url": "feishu_webhook_url",
    "feishu_secret": "feishu_secret",
    "feishu_app_id": "feishu_app_id",
    "feishu_app_secret": "feishu_app_secret",
    "feishu_open_id": "feishu_open_id",
    "feishu_mobile": "feishu_mobile",
    "feishu_events": "feishu_events",
}


class SettingsUpdate(BaseModel):
    """设置更新请求。所有字段可选，只更新提供的字段。

    动态 provider 字段命名约定：<provider>_<field>，其中 field ∈
    {protocol, api_key, base_url, model}。例如：
      glm_api_key / glm_base_url / glm_model / glm_protocol
      kimi_api_key / kimi_base_url / ...
      anthropic_api_key / anthropic_base_url / ...

    新增 provider：通过 llm_providers_add 字段传入逗号分隔的 provider 名
    （如 "glm,kimi,qwen,mimo,anthropic"），系统会加入注册表，然后可用
    上述 per-provider 字段配置。
    """
    llm_provider: Optional[str] = None
    llm_providers_add: Optional[str] = None
    llm_providers_remove: Optional[str] = None
    # 向后兼容字段（旧前端仍可使用）
    deepseek_api_key: Optional[str] = None
    deepseek_base_url: Optional[str] = None
    deepseek_model: Optional[str] = None
    siliconflow_api_key: Optional[str] = None
    siliconflow_base_url: Optional[str] = None
    siliconflow_model: Optional[str] = None
    ocr_backend: Optional[str] = None
    ocr_slices: Optional[int] = None
    paddle_ocr_api_url: Optional[str] = None
    paddle_ocr_token: Optional[str] = None
    paddle_ocr_model: Optional[str] = None
    mineru_token: Optional[str] = None
    mineru_model_version: Optional[str] = None
    mineru_language: Optional[str] = None
    mineru_enable_formula: Optional[bool] = None
    mineru_enable_table: Optional[bool] = None

    # 允许任意 <provider>_<field> 形式的额外字段（Pydantic 模式 extra=allow
    # 由 model_config 控制）。在端点里做白名单校验。
    model_config = {"extra": "allow"}


def _validate_provider_name(name: str) -> bool:
    """Provider 名称必须是小写字母/数字/_/-，2-32 字符。"""
    return bool(_PROVIDER_NAME_RE.match(name))


def _build_env_updates(
    req: SettingsUpdate,
) -> tuple[dict[str, str], dict[str, object], list[str], list[str]]:
    """把请求字段拆为 (env_updates, mem_updates, errors, skipped)。

    env_updates: 写入 config.json 的 KEY=VALUE 字典
    mem_updates: 传给 update_config() 的内存热更新字段
    errors: 校验错误信息（如有则拒绝本次写入）
    skipped: 掩码回写被跳过的字段名（T2.3：此前静默跳过无提示，
             用户以为已修改；随响应 message 回显）
    """
    env_updates: dict[str, str] = {}
    mem_updates: dict[str, object] = {}
    errors: list[str] = []
    skipped: list[str] = []

    raw = req.model_dump(exclude_none=True)

    # 1. 处理静态字段
    for field, env_key in _STATIC_FIELDS.items():
        if field not in raw:
            continue
        value = raw[field]
        # 敏感字段：掩码回写保护 — 收到与当前掩码相同的值视为未修改，跳过
        # （feishu 三字段 + OCR token 两个 — T2.3 只覆盖了 per-provider
        # api_key；OCR token 同样会被用户从页面复制掩码粘贴回来，静默写入
        # 后 OCR 全部失败且难排查）
        if field in ("feishu_webhook_url", "feishu_secret", "feishu_app_secret",
                     "paddle_ocr_token", "mineru_token"):
            if field == "feishu_webhook_url":
                current = load_feishu_config().get("webhook_url", "")
            elif field == "feishu_secret":
                current = load_feishu_config().get("secret", "")
            elif field == "feishu_app_secret":
                current = load_feishu_config().get("app_secret", "")
            elif field == "paddle_ocr_token":
                current = config["paddle_ocr"].token or ""
            else:
                current = config["mineru"].token or ""
            if str(value).strip() == _mask(current):
                skipped.append(field)
                continue
        # 飞书模式白名单
        if field == "feishu_mode":
            mode = str(value).strip().lower()
            if mode not in ("webhook", "app_bot"):
                errors.append(f"invalid feishu_mode: {mode!r} (allowed: webhook, app_bot)")
                continue
            env_updates[env_key] = mode
            mem_updates[field] = mode
            continue
        # 飞书事件白名单校验
        if field == "feishu_events":
            events = [e.strip().lower() for e in str(value).split(",") if e.strip()]
            allowed = FEISHU_ALLOWED_EVENTS
            bad = [e for e in events if e not in allowed]
            if bad:
                errors.append(f"invalid feishu events: {bad} (allowed: {sorted(allowed)})")
                continue
            env_updates[env_key] = ",".join(events)
            mem_updates[field] = ",".join(events)
            continue
        # MinerU 取值白名单（对抗审查 T2.2：此前任意字符串落盘，
        # 手误拼写会静默走错解析链路；常量集中于 config.py）
        if field == "mineru_model_version":
            if str(value).strip() not in config.MINERU_MODEL_VERSIONS:
                errors.append(
                    f"invalid mineru_model_version: {value!r} "
                    f"(allowed: {sorted(config.MINERU_MODEL_VERSIONS)})"
                )
                continue
        if field == "mineru_language":
            if str(value).strip() not in config.MINERU_LANGUAGES:
                errors.append(
                    f"invalid mineru_language: {value!r} "
                    f"(allowed: {sorted(config.MINERU_LANGUAGES)})"
                )
                continue
        # llm_provider 需校验为已注册的 provider（如果新增 provider 的
        # llm_providers_add 与 llm_provider 同批提交，注册表尚未更新，
        # 我们允许这次写入但内存热更新在 add 之后执行）
        if field == "ocr_slices":
            try:
                n = int(value)
            except (TypeError, ValueError):
                errors.append(f"invalid ocr_slices: {value!r} (must be a positive integer)")
                continue
            if n < 1:
                errors.append(f"invalid ocr_slices: {value!r} (must be >= 1)")
                continue
            env_updates[env_key] = str(n)
            mem_updates[field] = n
            continue
        if field == "llm_provider":
            name = str(value).strip().lower()
            if not _validate_provider_name(name):
                errors.append(f"invalid llm_provider name: {value!r}")
                continue
            # 对抗审查：llm_provider 必须指向已注册 provider（内置注册表
            # 或本次同批 llm_providers_add 新增）。此前未注册名字直接落盘，
            # LLMClient 静默回退 deepseek（llm/client.py）— UI 徽章显示
            # GLM 实际跑 deepseek，GMP 场景"你以为的模型不是实际模型"。
            registered = set(config["providers"].keys())
            add_names = {
                n.strip().lower()
                for n in str(req.llm_providers_add or "").split(",")
                if n.strip()
            }
            if name not in registered and name not in add_names:
                errors.append(
                    f"llm_provider {name!r} is not a registered provider "
                    f"(registered: {sorted(registered)})"
                )
                continue
            env_updates[env_key] = name
            mem_updates[field] = name
            continue
        if field == "llm_providers_add":
            # 合并到现有 LLM_PROVIDERS env var（去重）
            new_names = [n.strip().lower() for n in str(value).split(",") if n.strip()]
            invalid = [n for n in new_names if not _validate_provider_name(n)]
            if invalid:
                errors.append(f"invalid provider names: {invalid}")
                continue
            # 读取现有 config.json 中的 LLM_PROVIDERS（如果存在）
            existing = os.getenv("LLM_PROVIDERS", "")
            existing_set = {n.strip().lower() for n in existing.split(",") if n.strip()}
            merged = sorted(existing_set | set(new_names))
            env_updates[env_key] = ",".join(merged)
            mem_updates[field] = ",".join(new_names)
            continue
        if field == "llm_providers_remove":
            # P1-3: provider 移除持久化 — 与 add 对称的差集合并。
            # 内置 provider（deepseek/siliconflow）不在 LLM_PROVIDERS 里，
            # 差集对它们幂等无操作（刷新后按设计复活）。
            remove_names = [n.strip().lower() for n in str(value).split(",") if n.strip()]
            existing = os.getenv("LLM_PROVIDERS", "")
            existing_set = {n.strip().lower() for n in existing.split(",") if n.strip()}
            remaining = sorted(existing_set - set(remove_names))
            env_updates[env_key] = ",".join(remaining)
            mem_updates[field] = ",".join(remove_names)
            continue
        # bool 字段转 true/false
        if isinstance(value, bool):
            env_updates[env_key] = "true" if value else "false"
        else:
            str_val = str(value)
            # __CLEAR__ 兼容路径：OCR token + 飞书凭据类字段
            # （对抗审查：此前仅 OCR token 可清空，飞书 webhook/secret/
            # app_secret 只能覆盖不能清除 — GMP 撤销凭据语义下能力不对称）
            if str_val == "__CLEAR__" and field in (
                "paddle_ocr_token", "mineru_token",
                "feishu_webhook_url", "feishu_secret", "feishu_app_secret",
                "feishu_app_id", "feishu_open_id", "feishu_mobile",
            ):
                env_updates[env_key] = ""
                mem_updates[field] = ""
                continue
            env_updates[env_key] = str_val
        mem_updates[field] = value

    # 2. 处理 per-provider 动态字段
    # 字段命名约定：<provider>_<field>，field ∈ {protocol, api_key, base_url, model}
    # 因为 provider 名本身可能含下划线（如 "my_custom"），不能用 rfind("_")
    # 切分；改为按已知字段后缀匹配。
    for field, value in raw.items():
        if field in _STATIC_FIELDS:
            continue
        # 兼容旧字段 deepseek_* / siliconflow_*：先按静态路径走，下面处理动态
        if field in {
            "deepseek_api_key", "deepseek_base_url", "deepseek_model",
            "siliconflow_api_key", "siliconflow_base_url", "siliconflow_model",
        }:
            # 这些已经被 _STATIC_FIELDS 处理过 — 但旧映射里没有，所以补一下
            # 把 deepseek_api_key -> DEEPSEEK_API_KEY 之类
            env_key = field.upper()
            env_updates[env_key] = str(value)
            mem_updates[field] = value
            continue
        # 通用 <provider>_<field> 形式：按已知后缀匹配
        prov_name = None
        prov_field = None
        for candidate in _PER_PROVIDER_FIELDS:
            suffix = f"_{candidate}"
            if field.endswith(suffix):
                prov_name = field[: -len(suffix)]
                prov_field = candidate
                break
        # 独立清除指令字段：<provider>_clear_key = true
        # 与文本值分离，避免 __CLEAR__ 字面值暴露给用户（B1 修复）
        if field.endswith("_clear_key"):
            prov_name = field[: -len("_clear_key")]
            if not _validate_provider_name(prov_name):
                errors.append(f"invalid provider name in field: {field}")
                continue
            if str(value).lower() not in ("true", "1"):
                continue  # 仅接受 truthy 值
            env_key = f"{prov_name.upper()}_API_KEY"
            env_updates[env_key] = ""
            mem_updates[f"{prov_name}_api_key"] = ""
            continue
        if prov_name is None:
            continue  # 不是 per-provider 字段，忽略
        if not _validate_provider_name(prov_name):
            errors.append(f"invalid provider name in field: {field}")
            continue
        if prov_field == "protocol":
            proto = str(value).strip().lower()
            if proto not in _ALLOWED_PROTOCOLS:
                errors.append(
                    f"invalid protocol {proto!r} for provider {prov_name!r}; "
                    f"allowed: {sorted(_ALLOWED_PROTOCOLS)}"
                )
                continue
            env_key = f"{prov_name.upper()}_PROTOCOL"
        else:
            env_key = f"{prov_name.upper()}_{prov_field.upper()}"
        # 向后兼容：前端旧版本可能仍发送 __CLEAR__ 字面值作为 api_key
        # 新版本改用 <provider>_clear_key: true 独立字段
        if prov_field == "api_key" and str(value).strip() == "__CLEAR__":
            env_updates[env_key] = ""
            mem_updates[field] = ""
        else:
            # T2.3: per-provider api_key 掩码回写保护（与 feishu 字段对齐）
            # — 用户从页面复制掩码值粘贴提交时，等于没改，跳过并提示
            if prov_field == "api_key" and str(value).strip() == _mask(
                config["providers"].get(prov_name, ProviderConfig(name=prov_name)).api_key or ""
            ):
                skipped.append(field)
                continue
            env_updates[env_key] = str(value)
            mem_updates[field] = value

    return env_updates, mem_updates, errors, skipped


@router.post("/api/settings")
async def update_settings(req: SettingsUpdate, request: Request):
    """更新 config.json 文件 + 内存热更新。

    Security:
      1. ONLY accept requests from localhost (blocks CSRF from arbitrary
         web origins — a malicious page can't reconfigure PBC).
      2. URL fields (paddle_ocr_api_url, *_base_url) are validated to
         prevent SSRF — base_url cannot point to link-local / private / loopback.

    Flow:
      1. 校验来源（必须 localhost）
      2. 校验所有字段（provider 名、protocol 白名单、字段白名单、URL 安全性）
      3. 写入 config.json 文件（保留未涉及的字段）
      4. 调用 update_config() 立即更新内存
      5. 调用 reset_llm_client() 让下次 pipeline 用新配置

    如有校验错误，返回 400 + errors 列表，不写入任何内容。
    """
    # Phase 7 security: block non-local requests to prevent CSRF
    if not is_local_request(request):
        logger.warning(
            f"Settings POST rejected: non-local origin "
            f"host={request.headers.get('host')!r} "
            f"origin={request.headers.get('origin')!r}"
        )
        raise HTTPException(403, "Settings can only be modified from localhost")

    env_updates, mem_updates, errors, skipped = _build_env_updates(req)

    # Phase 7 security: validate URLs to prevent SSRF
    # paddle_ocr_api_url and any <provider>_base_url must be external.
    # 空串 = 用户意图清空/恢复默认（MinerU 单后端或全新安装默认态），
    # 不做 URL 校验 — 与下方 *_BASE_URL 空串放行策略一致（对抗审查：
    # 此前空 Paddle URL 使整份 OCR 设置每次都 400 保存失败）。
    url_fields_to_check = {
        "PADDLE_OCR_API_URL": "PaddleOCR API URL",
    }
    for env_key, label in url_fields_to_check.items():
        if env_key in env_updates:
            value = env_updates[env_key]
            if not value or not value.strip():
                continue
            ok, reason = validate_external_url(value, kind=label)
            if not ok:
                errors.append(reason)
    # Check all provider base_url fields
    # 对抗审查(cr-10): 空串 = 用户意图"恢复默认 base_url"（客户端回退到
    # SDK 内建默认地址），不是恶意输入。此前空串被 validate_external_url
    # 拒绝，用户只能手工编辑文件才能改回默认值。
    for env_key, value in env_updates.items():
        if env_key.endswith("_BASE_URL"):
            if not value or not value.strip():
                continue
            label = f"{env_key.removesuffix('_BASE_URL').lower()} base_url"
            ok, reason = validate_external_url(value, kind=label)
            if not ok:
                errors.append(reason)
    # 飞书 webhook：保存路径与 test_feishu 一致地做 SSRF 校验 — notify 会
    # 以服务端身份 POST 到该 URL，私网地址（如 127.0.0.1 的蜜罐）被禁
    # （对抗审查：此前保存绕过校验、仅测试时校验，行为不一致）。
    if "feishu_webhook_url" in env_updates:
        wh_url = env_updates["feishu_webhook_url"]
        if wh_url and wh_url.strip():
            ok, reason = validate_external_url(wh_url, kind="feishu webhook URL")
            if not ok:
                errors.append(reason)

    if errors:
        raise HTTPException(400, detail={"errors": errors})

    if not env_updates:
        if skipped:
            return {
                "ok": True,
                "updated": 0,
                "skipped": skipped,
                "message": (
                    "已保存值未变化：以下字段与已保存内容相同（掩码），"
                    f"未修改：{'、'.join(skipped)}"
                ),
            }
        return {"ok": True, "updated": 0, "message": "无更新字段"}

    config_path = _settings_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # 读取现有 config.json（如果存在）
    existing_config: dict[str, str] = {}
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8-sig") as f:
                existing_config = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read existing config.json: {e}, starting fresh")
            existing_config = {}

    # 合并更新：新值覆盖旧值
    existing_config.update(env_updates)

    # 原子写入 JSON（先写临时文件再 rename，避免并发读到半写状态）
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

    # 同步更新 os.environ，保证后续 _load_all_providers() 能读到新值
    for env_key, value in env_updates.items():
        os.environ[env_key] = value

    # 内存热更新：先 add 新 provider，再更新字段，再切 llm_provider
    if "llm_providers_add" in mem_updates:
        update_config({"llm_providers_add": mem_updates["llm_providers_add"]})
    if "llm_providers_remove" in mem_updates:
        update_config({"llm_providers_remove": mem_updates["llm_providers_remove"]})
    # 过滤掉 add/remove（update_config 已处理）
    per_field_updates = {
        k: v for k, v in mem_updates.items()
        if k not in ("llm_providers_add", "llm_providers_remove")
    }
    if per_field_updates:
        update_config(per_field_updates)

    # 重建 LLM 单例，确保下次调用用新配置
    reset_llm_client()

    # 对抗审查：设置保存此前无 audit_log（GMP 追溯缺口 — 配置变更与
    # 复核操作同为质量体系事件，无记录则无法回答"配置何时被谁改过"）。
    # detail 只记字段名列表，不记录任何值（密钥类字段尤其不能落库）。
    try:
        db = await get_db()
        await db.execute(
            "INSERT INTO audit_log (job_id, action, detail, created_at) VALUES (?, ?, ?, datetime(\'now\',\'localtime\'))",
            ("system", "settings_update",
             "fields=" + ",".join(sorted(env_updates.keys()))),
        )
        await db.commit()
    except Exception as e:
        logger.warning(f"Failed to write settings_update audit log: {e}")

    logger.info(
        f"Settings updated and applied live: {list(env_updates.keys())} -> {config_path}"
    )
    msg = "配置已保存并立即生效"
    # T2.3: 掩码回写被跳过的字段显式提示（此前静默跳过，用户以为已修改）
    if skipped:
        msg += f"；以下字段与已保存值相同（掩码），未修改：{'、'.join(skipped)}"
    return {
        "ok": True,
        "updated": len(env_updates),
        "skipped": skipped,
        "fields": list(env_updates.keys()),
        "message": msg,
        "config_file": str(config_path),
        "providers": _providers_payload(),
    }
