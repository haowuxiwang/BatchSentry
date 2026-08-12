"""Settings API — 读取/更新 LLM 与 OCR 配置。

GET /api/settings  返回当前配置（敏感 key 脱敏）
POST /api/settings 更新 config.json 文件（运行时写入 + 内存热更新）

Phase 7: LLM 配置从硬编码 deepseek/siliconflow 升级为动态 provider 注册表。
- 返回的 llm.providers 是一个 list（用于 UI 动态渲染）
- 新增 llm_providers_add 字段允许前端添加新 provider（如 glm/kimi/qwen/mimo/anthropic）
- 每个 provider 单独配置 protocol/api_key/base_url/model
- 保存后调用 reset_llm_client() 让单例在下一次调用时重建

配置写入的 JSON 路径：
  - 开发模式: 项目根 config.json
  - 打包模式: %APPDATA%/PBC/config.json
"""
import json
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from config import config, update_config, _config_path, load_user_rules
from core.security import validate_external_url, is_local_request
from db.client import get_db
from llm.client import reset_llm_client

logger = logging.getLogger(__name__)
router = APIRouter()


def _settings_config_path() -> Path:
    """返回 JSON 配置文件路径（与 config.py 的加载逻辑一致）。"""
    return _config_path()


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
    """构造 provider 列表用于 GET 响应（脱敏 + 标记是否已配置）。

    Phase 8: improved "configured" detection — filters out obvious test
    placeholders (sk-test, sk-glm-test, sk-ant-test, sk-example, etc.) so
    the UI doesn't mislead users into thinking a provider is ready when
    only a test value was set.
    """
    payload = []
    for name, prov in config["providers"].items():
        is_real_key = _is_real_api_key(prov.api_key)
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


# Obvious test/placeholder values that should NOT count as "configured".
# These are common patterns users leave in config files during development.
# Real API keys have format: sk-<random 20+ chars> or sk-ant-<random>.
_TEST_KEY_PATTERNS = (
    "sk-test",
    "sk-glm-test",
    "sk-ant-test",
    "sk-example",
    "sk-placeholder",
    "sk-your-",
    "test-key",
    "placeholder",
    "changeme",
    "xxxxx",
)


def _is_real_api_key(key: str) -> bool:
    """Return True only if the key looks like a real API key (not a test value).

    判定逻辑（业界做法 - 参考 OpenAI/Anthropic）：
      1. 非空（已配置就应被识别，不靠长度猜测）
      2. 不匹配明显的测试/占位模式（sk-test, placeholder 等）

    旧版用 len(key) >= 20 启发式判断"是否真实"，但这会误判
    PaddleOCR token（32 字符）和短测试 key（如 sk-test-new，11 字符），
    导致 UI 显示"未配置"但后端实际有 key 能调用 API，状态矛盾。
    现在只检查"非空 + 非明显测试模式"。
    """
    if not key:
        return False
    key_lower = key.lower()
    for pattern in _TEST_KEY_PATTERNS:
        if pattern in key_lower:
            return False
    return True


@router.get("/api/settings")
async def get_settings():
    """返回当前配置（敏感字段脱敏）。"""
    cfg = config
    config_file = _settings_config_path()
    return {
        "config_file": str(config_file),
        "config_exists": config_file.exists(),
        "llm": {
            "provider": cfg["app"].llm_provider,
            # S3: 新增 active_provider 别名（明确语义，与 provider 字段保持一致）
            "active_provider": cfg["app"].llm_provider,
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
            "paddle": {
                "api_url": cfg["paddle_ocr"].api_url,
                "token": _mask(cfg["paddle_ocr"].token),
                "model": cfg["paddle_ocr"].model,
                "configured": _is_real_api_key(cfg["paddle_ocr"].token),
            },
            "mineru": {
                "token": _mask(cfg["mineru"].token),
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
    }


# 允许的动态字段：仅这些 <provider>_<field> 组合可被前端写入
# 防止 attacker 通过 settings API 写入任意字段到 config.json
_PER_PROVIDER_FIELDS = ("protocol", "api_key", "base_url", "model")

# 静态字段（非 per-provider）
_STATIC_FIELDS = {
    "llm_provider": "LLM_PROVIDER",
    "llm_providers_add": "LLM_PROVIDERS",  # 追加到 LLM_PROVIDERS env var
    "ocr_backend": "OCR_BACKEND",
    "ocr_slices": "OCR_SLICES",  # MinerU 分片 OCR（流式输出）页数/片
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
) -> tuple[dict[str, str], dict[str, object], list[str]]:
    """把请求字段拆为 (env_updates, mem_updates, errors)。

    env_updates: 写入 config.json 的 KEY=VALUE 字典
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
        # bool 字段转 true/false
        if isinstance(value, bool):
            env_updates[env_key] = "true" if value else "false"
        else:
            str_val = str(value)
            # S6: OCR token 清除支持 — paddle_ocr_token / mineru_token
            # 收到 "__CLEAR__" 标记时写入空字符串（清除已保存的 token）
            # 这与 per-provider api_key 的 _clear_key 机制保持一致
            if str_val == "__CLEAR__" and field in ("paddle_ocr_token", "mineru_token"):
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
            if not str(value).lower() in ("true", "1"):
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
            env_updates[env_key] = str(value)
            mem_updates[field] = value

    return env_updates, mem_updates, errors


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

    if errors:
        raise HTTPException(400, detail={"errors": errors})

    if not env_updates:
        return {"ok": True, "updated": 0, "message": "无更新字段"}

    config_path = _settings_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # 读取现有 config.json（如果存在）
    existing_config: dict[str, str] = {}
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
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
    # 过滤掉 llm_providers_add（update_config 已处理）
    per_field_updates = {k: v for k, v in mem_updates.items() if k != "llm_providers_add"}
    if per_field_updates:
        update_config(per_field_updates)

    # 重建 LLM 单例，确保下次调用用新配置
    reset_llm_client()

    logger.info(
        f"Settings updated and applied live: {list(env_updates.keys())} -> {config_path}"
    )
    return {
        "ok": True,
        "updated": len(env_updates),
        "fields": list(env_updates.keys()),
        "message": "配置已保存并立即生效",
        "config_file": str(config_path),
        "providers": _providers_payload(),
    }


# ============================================================
# S0: 用户自定义合规规则（注入跨页 LLM 分析）
# ============================================================

class UserRuleItem(BaseModel):
    """单条用户合规规则。"""
    id: Optional[str] = None
    text: str
    active: bool = True


class UserRulesUpdate(BaseModel):
    """全量替换用户规则列表请求。"""
    rules: list[UserRuleItem] = []


_USER_RULES_MAX = 100
_USER_RULES_TEXT_MAX = 1000


def _read_raw_config() -> dict:
    """读取 config.json 原始内容（不存在则返回空 dict）。"""
    config_path = _settings_config_path()
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read {config_path}: {e}")
        return {}


def _write_raw_config(existing: dict) -> None:
    """原子写入 config.json（临时文件 + replace，防并发半写）。"""
    config_path = _settings_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_path.parent / f"config.json.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        tmp_path.replace(config_path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


@router.get("/api/settings/rules")
async def get_user_rules(request: Request):
    """返回用户填写的合规规则（注入跨页 LLM 分析用）。"""
    if not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    return {"rules": load_user_rules()}


@router.put("/api/settings/rules")
async def update_user_rules(req: UserRulesUpdate, request: Request):
    """全量替换用户合规规则。

    校验：每条 text 非空且 ≤1000 字符，总数 ≤100。校验失败返回 400，
    不写入任何内容。成功后写 audit_log（GMP 追溯规则变更）。
    """
    if not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")

    errors: list[str] = []
    cleaned: list[dict] = []
    for i, r in enumerate(req.rules):
        text = r.text.strip()
        if not text:
            errors.append(f"规则 #{i + 1}: 内容不能为空")
            continue
        if len(text) > _USER_RULES_TEXT_MAX:
            errors.append(f"规则 #{i + 1}: 内容超过 {_USER_RULES_TEXT_MAX} 字符上限")
            continue
        cleaned.append({
            "id": r.id or uuid.uuid4().hex[:12],
            "text": text,
            "active": bool(r.active),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
    if len(cleaned) > _USER_RULES_MAX:
        errors.append(f"规则总数超过 {_USER_RULES_MAX} 条上限")
    if errors:
        raise HTTPException(400, detail={"errors": errors})

    existing = _read_raw_config()
    existing["user_rules"] = cleaned
    _write_raw_config(existing)

    try:
        db = await get_db()
        await db.execute(
            "INSERT INTO audit_log (job_id, action, detail) VALUES (?, ?, ?)",
            ("system", "user_rules_update",
             f"{len(cleaned)} rules (active={sum(1 for r in cleaned if r['active'])})"),
        )
        await db.commit()
    except Exception as e:
        logger.warning(f"Failed to write user_rules audit log: {e}")

    logger.info(f"User rules updated: {len(cleaned)} rules persisted to {_settings_config_path()}")
    return {
        "ok": True,
        "rules": cleaned,
        "message": f"已保存 {len(cleaned)} 条合规规则，将注入下次跨页分析",
    }


# ============================================================
# S1: 快速切换 active provider — 立即生效，无需提交整个表单
# ============================================================

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
            with open(config_path, "r", encoding="utf-8") as f:
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


# ============================================================
# S4: 单独测试指定 provider — 比 /api/health/downstream 更精细
# ============================================================

class TestProviderRequest(BaseModel):
    """测试指定 provider 请求。"""
    provider: str


@router.post("/api/settings/test_provider")
async def test_provider(req: TestProviderRequest, request: Request):
    """测试指定 provider 的连通性 — 用临时构建的 LLMClient 真实调用。

    与 /api/health/downstream 区别：
    - downstream 测试当前 active provider
    - 本端点测试指定 provider（即使非 active）
    用于每个 provider 卡片独立的"测试连接"按钮。
    """
    if not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")

    name = req.provider.strip().lower()
    providers = config["providers"]
    if name not in providers:
        raise HTTPException(404, f"Provider {name!r} not found")

    cfg = providers[name]
    if not cfg.api_key:
        return {
            "ok": False,
            "provider": name,
            "reason": "API key not configured",
        }

    # 用临时 client 测试，不影响全局单例
    from llm.client import LLMClient
    try:
        probe_client = LLMClient(provider=name)
        start = time.time()
        await probe_client.adapter.chat(
            system_prompt="",
            user_content="ping",
            max_tokens=1,
            temperature=0,
            timeout=8,
        )
        elapsed_ms = int((time.time() - start) * 1000)
        return {
            "ok": True,
            "provider": name,
            "model": cfg.model,
            "latency_ms": elapsed_ms,
            "reason": "",
        }
    except Exception as e:
        err_str = str(e).lower()
        if any(kw in err_str for kw in ["401", "authentication", "unauthorized"]):
            reason = "API Key 无效或已过期"
        elif any(kw in err_str for kw in ["403", "forbidden"]):
            reason = "访问被拒绝（可能 Key 无此模型权限）"
        elif any(kw in err_str for kw in ["timeout", "timed out"]):
            reason = f"请求超时（8s）"
        elif any(kw in err_str for kw in ["connection", "dns", "resolve"]):
            reason = "无法连接到 Base URL"
        else:
            reason = f"{e.__class__.__name__}: {str(e)[:200]}"
        return {
            "ok": False,
            "provider": name,
            "reason": reason,
        }
