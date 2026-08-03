"""Settings API — 读取/更新 LLM 与 OCR 配置。

GET /api/settings  返回当前配置（敏感 key 脱敏）
POST /api/settings 更新 .env 文件（运行时写入 + 内存热更新）

Phase 7: LLM 配置从硬编码 deepseek/siliconflow 升级为动态 provider 注册表。
- 返回的 llm.providers 是一个 list（用于 UI 动态渲染）
- 新增 llm_providers_add 字段允许前端添加新 provider（如 glm/kimi/qwen/mimo/anthropic）
- 每个 provider 单独配置 protocol/api_key/base_url/model
- 保存后调用 reset_llm_client() 让单例在下一次调用时重建

配置写入的 .env 路径：
  - 开发模式: 项目根 .env
  - 打包模式: %APPDATA%/PBC/.env
"""
import logging
import os
import re
import sys
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from config import config, update_config
from core.security import validate_external_url, is_local_request
from llm.client import reset_llm_client

logger = logging.getLogger(__name__)
router = APIRouter()


def _env_path() -> Path:
    """返回 .env 文件路径（与 config.py 的加载逻辑一致）。"""
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        return base / "PBC" / ".env"
    return Path(".env")


def _mask(value: str) -> str:
    """脱敏：只显示前4位和后4位，中间用 **** 代替。"""
    if not value or len(value) <= 12:
        return "*" * len(value) if value else ""
    return value[:4] + "****" + value[-4:]


# 允许的协议白名单（防止用户输入任意字符串）
_ALLOWED_PROTOCOLS = {"openai", "anthropic"}
# 允许的 provider 名称字符集（小写字母+数字+下划线+连字符，2-32 字符）
_PROVIDER_NAME_RE = re.compile(r"^[a-z0-9_-]{2,32}$")


def _providers_payload() -> list[dict]:
    """构造 provider 列表用于 GET 响应（脱敏 + 标记是否已配置）。"""
    payload = []
    for name, prov in config["providers"].items():
        payload.append({
            "name": prov.name,
            "protocol": prov.protocol,
            "api_key": _mask(prov.api_key),
            "base_url": prov.base_url,
            "model": prov.model,
            "configured": bool(prov.api_key),
        })
    # 按名称排序，保证 UI 顺序稳定
    payload.sort(key=lambda p: p["name"])
    return payload


@router.get("/api/settings")
async def get_settings():
    """返回当前配置（敏感字段脱敏）。"""
    cfg = config
    env_file = _env_path()
    return {
        "env_file": str(env_file),
        "env_exists": env_file.exists(),
        "llm": {
            "provider": cfg["app"].llm_provider,
            # 动态 provider 注册表（前端按 list 渲染表单）
            "providers": _providers_payload(),
            # 向后兼容字段（旧前端仍可读取，但应迁移到 providers）
            "deepseek": {
                "api_key": _mask(cfg["deepseek"].api_key),
                "base_url": cfg["deepseek"].base_url,
                "model": cfg["deepseek"].model,
                "configured": bool(cfg["deepseek"].api_key),
            },
            "siliconflow": {
                "api_key": _mask(cfg["siliconflow"].api_key),
                "base_url": cfg["siliconflow"].base_url,
                "model": cfg["siliconflow"].model,
                "configured": bool(cfg["siliconflow"].api_key),
            },
        },
        "ocr": {
            "backend": cfg["app"].ocr_backend,
            "paddle": {
                "api_url": cfg["paddle_ocr"].api_url,
                "token": _mask(cfg["paddle_ocr"].token),
                "model": cfg["paddle_ocr"].model,
                "configured": bool(cfg["paddle_ocr"].token),
            },
            "mineru": {
                "token": _mask(cfg["mineru"].token),
                "model_version": cfg["mineru"].model_version,
                "language": cfg["mineru"].language,
                "enable_formula": cfg["mineru"].enable_formula,
                "enable_table": cfg["mineru"].enable_table,
                "configured": bool(cfg["mineru"].token),
            },
        },
        "app": {
            "host": cfg["app"].host,
            "port": cfg["app"].port,
        },
    }


# 允许的动态字段：仅这些 <provider>_<field> 组合可被前端写入
# 防止 attacker 通过 settings API 写入任意环境变量到 .env
_PER_PROVIDER_FIELDS = ("protocol", "api_key", "base_url", "model")

# 静态字段（非 per-provider）
_STATIC_FIELDS = {
    "llm_provider": "LLM_PROVIDER",
    "llm_providers_add": "LLM_PROVIDERS",  # 追加到 LLM_PROVIDERS env var
    "ocr_backend": "OCR_BACKEND",
    "paddle_ocr_api_url": "PADDLE_OCR_API_URL",
    "paddle_ocr_token": "PADDLE_OCR_TOKEN",
    "paddle_ocr_model": "PADDLE_OCR_MODEL",
    "mineru_token": "MINERU_TOKEN",
    "mineru_model_version": "MINERU_MODEL_VERSION",
    "mineru_language": "MINERU_LANGUAGE",
    "mineru_enable_formula": "MINERU_ENABLE_FORMULA",
    "mineru_enable_table": "MINERU_ENABLE_TABLE",
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
    # 向后兼容字段（旧前端仍可使用）
    deepseek_api_key: Optional[str] = None
    deepseek_base_url: Optional[str] = None
    deepseek_model: Optional[str] = None
    siliconflow_api_key: Optional[str] = None
    siliconflow_base_url: Optional[str] = None
    siliconflow_model: Optional[str] = None
    ocr_backend: Optional[str] = None
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
) -> tuple[dict[str, str], dict[str, object], list[str]]:
    """把请求字段拆为 (env_updates, mem_updates, errors)。

    env_updates: 写入 .env 的 KEY=VALUE 字典
    mem_updates: 传给 update_config() 的内存热更新字段
    errors: 校验错误信息（如有则拒绝本次写入）
    """
    env_updates: dict[str, str] = {}
    mem_updates: dict[str, object] = {}
    errors: list[str] = []

    raw = req.model_dump(exclude_none=True)

    # 1. 处理静态字段
    for field, env_key in _STATIC_FIELDS.items():
        if field not in raw:
            continue
        value = raw[field]
        # llm_provider 需校验为已注册的 provider（如果新增 provider 的
        # llm_providers_add 与 llm_provider 同批提交，注册表尚未更新，
        # 我们允许这次写入但内存热更新在 add 之后执行）
        if field == "llm_provider":
            name = str(value).strip().lower()
            if not _validate_provider_name(name):
                errors.append(f"invalid llm_provider name: {value!r}")
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
            # 读取现有 .env 中的 LLM_PROVIDERS（如果存在）
            existing = os.getenv("LLM_PROVIDERS", "")
            existing_set = {n.strip().lower() for n in existing.split(",") if n.strip()}
            merged = sorted(existing_set | set(new_names))
            env_updates[env_key] = ",".join(merged)
            mem_updates[field] = ",".join(new_names)
            continue
        # bool 字段转 true/false
        if isinstance(value, bool):
            env_updates[env_key] = "true" if value else "false"
        else:
            env_updates[env_key] = str(value)
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
        env_updates[env_key] = str(value)
        mem_updates[field] = value

    return env_updates, mem_updates, errors


@router.post("/api/settings")
async def update_settings(req: SettingsUpdate, request: Request):
    """更新 .env 文件 + 内存热更新。

    Security:
      1. ONLY accept requests from localhost (blocks CSRF from arbitrary
         web origins — a malicious page can't reconfigure PBC).
      2. URL fields (paddle_ocr_api_url, *_base_url) are validated to
         prevent SSRF — base_url cannot point to link-local / private / loopback.

    Flow:
      1. 校验来源（必须 localhost）
      2. 校验所有字段（provider 名、protocol 白名单、字段白名单、URL 安全性）
      3. 写入 .env 文件（保留未涉及的字段）
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

    env_updates, mem_updates, errors = _build_env_updates(req)

    # Phase 7 security: validate URLs to prevent SSRF
    # paddle_ocr_api_url and any <provider>_base_url must be external
    url_fields_to_check = {
        "PADDLE_OCR_API_URL": "PaddleOCR API URL",
    }
    for env_key, label in url_fields_to_check.items():
        if env_key in env_updates:
            ok, reason = validate_external_url(env_updates[env_key], kind=label)
            if not ok:
                errors.append(reason)
    # Check all provider base_url fields
    for env_key, value in env_updates.items():
        if env_key.endswith("_BASE_URL"):
            label = f"{env_key.removesuffix('_BASE_URL').lower()} base_url"
            ok, reason = validate_external_url(value, kind=label)
            if not ok:
                errors.append(reason)

    if errors:
        raise HTTPException(400, detail={"errors": errors})

    if not env_updates:
        return {"ok": True, "updated": 0, "message": "无更新字段"}

    env_path = _env_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)

    # 读取现有 .env
    lines: list[str] = []
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()

    # 更新已有行 / 追加新行
    updated_keys: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        m = re.match(r"^([A-Z_]+)=(.*)$", line)
        if m and m.group(1) in env_updates:
            new_lines.append(f"{m.group(1)}={env_updates[m.group(1)]}")
            updated_keys.add(m.group(1))
        else:
            new_lines.append(line)

    for env_key, value in env_updates.items():
        if env_key not in updated_keys:
            new_lines.append(f"{env_key}={value}")

    # 写入 .env（原子性：先写临时文件再 rename，避免并发读到半写状态）
    # 临时文件名带 PID + 随机后缀，防止两个并发 POST 请求互相覆盖 tmp 文件
    # 注意：不能用 Path.with_suffix()，因为 .env 的 stem=".env" suffix=""
    # 会导致 with_suffix(".env.tmp.xxx") 生成 ".env.env.tmp.xxx"
    tmp_path = env_path.parent / f".env.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write("\n".join(new_lines))
        if new_lines and not new_lines[-1].endswith("\n"):
            f.write("\n")
    tmp_path.replace(env_path)

    # 同步更新 os.environ，保证后续 _load_all_providers() 能读到新值
    for env_key, value in env_updates.items():
        os.environ[env_key] = value

    # 内存热更新：先 add 新 provider，再更新字段，再切 llm_provider
    if "llm_providers_add" in mem_updates:
        update_config({"llm_providers_add": mem_updates["llm_providers_add"]})
    # 过滤掉 llm_providers_add（update_config 已处理）
    per_field_updates = {k: v for k, v in mem_updates.items() if k != "llm_providers_add"}
    if per_field_updates:
        update_config(per_field_updates)

    # 重建 LLM 单例，确保下次调用用新配置
    reset_llm_client()

    logger.info(
        f"Settings updated and applied live: {list(env_updates.keys())} -> {env_path}"
    )
    return {
        "ok": True,
        "updated": len(env_updates),
        "fields": list(env_updates.keys()),
        "message": "配置已保存并立即生效",
        "env_file": str(env_path),
        "providers": _providers_payload(),
    }
