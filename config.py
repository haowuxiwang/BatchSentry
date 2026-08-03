"""Configuration management — loads from .env, exposes typed config.

Phase 5B: supports PyInstaller frozen mode. When bundled, data files (DB,
output) are redirected to %APPDATA%/PBC on Windows so the app survives
upgrades and doesn't require write access to Program Files.

Phase 7: LLM provider registry. Providers are NO LONGER hardcoded — they
are loaded from env vars. The built-in providers (deepseek, siliconflow)
remain available for backward compatibility, plus any provider declared via
LLM_PROVIDERS env var (e.g. "glm,kimi,qwen,mimo,anthropic").

Each provider is configured by 4 env vars (prefix = UPPER(name)):
  <PREFIX>_PROTOCOL: "openai" (default) | "anthropic"
  <PREFIX>_API_KEY: API key (required for the provider to be enabled)
  <PREFIX>_BASE_URL: base URL
  <PREFIX>_MODEL: model identifier
"""
import os
import sys
from pathlib import Path
from dataclasses import dataclass
from dotenv import load_dotenv


def _app_data_dir() -> Path:
    """Get writable per-user data directory for the app.

    Returns %APPDATA%/PBC on Windows, ~/Library/Application Support/PBC on
    macOS, ~/.local/share/PBC on Linux. Created if missing.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    pbc_dir = base / "PBC"
    pbc_dir.mkdir(parents=True, exist_ok=True)
    return pbc_dir


def _is_frozen() -> bool:
    """True when running inside PyInstaller bundle."""
    return getattr(sys, "frozen", False)


# In frozen mode, ONLY load .env from %APPDATA%/PBC/.env (if exists).
# Never load .env from CWD in frozen mode — that would pick up the dev
# .env left in the project root during development, overriding the
# APPDATA redirection of database_path / output_dir.
if _is_frozen():
    _env_path = _app_data_dir() / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
    # else: no .env loaded; rely on OS env vars or defaults
else:
    load_dotenv()


# ============================================================
# Provider configuration (Phase 7)
# ============================================================

@dataclass
class ProviderConfig:
    """Generic LLM provider configuration.

    protocol:
      - "openai": OpenAI Chat Completions API (/v1/chat/completions).
        Used by DeepSeek, SiliconFlow, GLM (Zhipu), Kimi (Moonshot),
        Qwen (DashScope compatible mode), MiMo (Xiaomi), OpenAI itself.
      - "anthropic": Anthropic Messages API (/v1/messages) with x-api-key
        auth + anthropic-version header + top-level system field.
        Used by Claude models via the native Anthropic SDK.
    """
    name: str
    protocol: str = "openai"
    api_key: str = ""
    base_url: str = ""
    model: str = ""


# Backward-compatible aliases (so existing config["deepseek"] etc. keep working
# but are now instances of the same ProviderConfig type).
DeepSeekConfig = ProviderConfig
SiliconFlowConfig = ProviderConfig


@dataclass
class PaddleOCRConfig:
    api_url: str
    token: str
    model: str


@dataclass
class MinerUConfig:
    """MinerU 精准解析 API 配置。

    Token 在 https://mineru.net/apiManage 页面创建。
    model_version: pipeline (默认) / vlm (推荐,精度更高) / MinerU-HTML
    """
    token: str
    model_version: str
    language: str
    enable_formula: bool
    enable_table: bool


@dataclass
class AppConfig:
    host: str
    port: int
    database_path: str
    output_dir: str
    llm_provider: str
    ocr_backend: str  # "paddle" | "mineru"


def _load_provider(name: str) -> ProviderConfig:
    """Load a single provider from env vars by name.

    Env vars follow the pattern <UPPER(name)>_{PROTOCOL,API_KEY,BASE_URL,MODEL}.
    """
    prefix = name.upper()
    return ProviderConfig(
        name=name,
        protocol=os.getenv(f"{prefix}_PROTOCOL", "openai").lower(),
        api_key=os.getenv(f"{prefix}_API_KEY", ""),
        base_url=os.getenv(f"{prefix}_BASE_URL", ""),
        model=os.getenv(f"{prefix}_MODEL", ""),
    )


def _load_all_providers() -> dict[str, ProviderConfig]:
    """Load all configured providers from env vars.

    Strategy:
      1. Built-in defaults for deepseek + siliconflow (backward compatible).
      2. Additional providers declared in LLM_PROVIDERS env var
         (comma-separated list, e.g. "glm,kimi,qwen,mimo,anthropic").
      3. Any provider with an API key set is registered; providers without
         a key are still registered but flagged as "not configured" via
         the empty api_key (the Settings UI displays this state).
    """
    providers: dict[str, ProviderConfig] = {}

    # Built-in providers with sensible defaults (legacy behavior)
    providers["deepseek"] = ProviderConfig(
        name="deepseek",
        protocol=os.getenv("DEEPSEEK_PROTOCOL", "openai").lower(),
        api_key=os.getenv("DEEPSEEK_API_KEY", ""),
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
    )
    providers["siliconflow"] = ProviderConfig(
        name="siliconflow",
        protocol=os.getenv("SILICONFLOW_PROTOCOL", "openai").lower(),
        api_key=os.getenv("SILICONFLOW_API_KEY", ""),
        base_url=os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
        model=os.getenv("SILICONFLOW_MODEL", "deepseek-ai/DeepSeek-V4-Pro"),
    )

    # Custom providers via LLM_PROVIDERS env var
    custom_list = os.getenv("LLM_PROVIDERS", "")
    if custom_list:
        for raw_name in custom_list.split(","):
            name = raw_name.strip().lower()
            if not name or name in providers:
                continue
            providers[name] = _load_provider(name)

    return providers


def load_config():
    # Phase 5B: in frozen mode, redirect db/output to per-user APPDATA dir
    # so the bundled exe doesn't need write access to Program Files.
    if _is_frozen():
        data_dir = _app_data_dir()
        default_db = str(data_dir / "data.db")
        default_output = str(data_dir / "output")
    else:
        default_db = "data/pharma.db"
        default_output = "output"

    providers = _load_all_providers()
    llm_provider = os.getenv("LLM_PROVIDER", "deepseek").lower()
    # If the configured LLM_PROVIDER is not in the registry (e.g. typo or
    # the provider's API key was never set), fall back to deepseek.
    if llm_provider not in providers:
        llm_provider = "deepseek"

    return {
        # Backward-compatible top-level entries (singletons of the same dataclass)
        "deepseek": providers.get("deepseek", ProviderConfig(name="deepseek")),
        "siliconflow": providers.get("siliconflow", ProviderConfig(name="siliconflow")),
        # New registry — preferred for LLMClient + Settings UI
        "providers": providers,
        "paddle_ocr": PaddleOCRConfig(
            api_url=os.getenv("PADDLE_OCR_API_URL", ""),
            token=os.getenv("PADDLE_OCR_TOKEN", ""),
            model=os.getenv("PADDLE_OCR_MODEL", "PaddleOCR-VL-1.6"),
        ),
        "mineru": MinerUConfig(
            token=os.getenv("MINERU_TOKEN", ""),
            model_version=os.getenv("MINERU_MODEL_VERSION", "vlm"),
            language=os.getenv("MINERU_LANGUAGE", "ch"),
            enable_formula=os.getenv("MINERU_ENABLE_FORMULA", "true").lower() == "true",
            enable_table=os.getenv("MINERU_ENABLE_TABLE", "true").lower() == "true",
        ),
        "app": AppConfig(
            host=os.getenv("APP_HOST", "127.0.0.1"),
            port=int(os.getenv("APP_PORT", "8000")),
            database_path=os.getenv("DATABASE_PATH", default_db),
            output_dir=os.getenv("OUTPUT_DIR", default_output),
            llm_provider=llm_provider,
            ocr_backend=os.getenv("OCR_BACKEND", "paddle"),
        ),
    }


config = load_config()


def update_config(updates: dict):
    """运行时更新内存中的 config 对象（写入 .env 后调用）。

    解决供应商切换 bug：保存后立即生效，无需重启。

    Supports:
      - llm_provider / ocr_backend switches
      - Per-provider fields: <provider>_protocol / _api_key / _base_url / _model
      - PaddleOCR / MinerU fields
      - Adding a NEW provider via 'llm_providers_add' (comma-separated list)

    Args:
        updates: 字段名 -> 新值（与 SettingsUpdate 字段名一致）
    """
    if "llm_provider" in updates:
        config["app"].llm_provider = updates["llm_provider"]
    if "ocr_backend" in updates:
        config["app"].ocr_backend = updates["ocr_backend"]

    # Add new providers to the registry on the fly (Settings UI "add provider")
    if "llm_providers_add" in updates:
        for raw_name in str(updates["llm_providers_add"]).split(","):
            name = raw_name.strip().lower()
            if name and name not in config["providers"]:
                config["providers"][name] = ProviderConfig(name=name)

    # Per-provider updates: walk every registered provider and check for
    # matching <provider>_{protocol,api_key,base_url,model} keys.
    for name, prov_cfg in config["providers"].items():
        if f"{name}_protocol" in updates:
            prov_cfg.protocol = str(updates[f"{name}_protocol"]).lower()
        if f"{name}_api_key" in updates:
            prov_cfg.api_key = updates[f"{name}_api_key"]
        if f"{name}_base_url" in updates:
            prov_cfg.base_url = updates[f"{name}_base_url"]
        if f"{name}_model" in updates:
            prov_cfg.model = updates[f"{name}_model"]

    # Mirror deepseek / siliconflow singletons so legacy code reading
    # config["deepseek"] / config["siliconflow"] stays in sync.
    if "deepseek" in config["providers"]:
        config["deepseek"] = config["providers"]["deepseek"]
    if "siliconflow" in config["providers"]:
        config["siliconflow"] = config["providers"]["siliconflow"]

    # PaddleOCR
    if "paddle_ocr_api_url" in updates:
        config["paddle_ocr"].api_url = updates["paddle_ocr_api_url"]
    if "paddle_ocr_token" in updates:
        config["paddle_ocr"].token = updates["paddle_ocr_token"]
    if "paddle_ocr_model" in updates:
        config["paddle_ocr"].model = updates["paddle_ocr_model"]

    # MinerU
    if "mineru_token" in updates:
        config["mineru"].token = updates["mineru_token"]
    if "mineru_model_version" in updates:
        config["mineru"].model_version = updates["mineru_model_version"]
    if "mineru_language" in updates:
        config["mineru"].language = updates["mineru_language"]
    if "mineru_enable_formula" in updates:
        config["mineru"].enable_formula = updates["mineru_enable_formula"]
    if "mineru_enable_table" in updates:
        config["mineru"].enable_table = updates["mineru_enable_table"]
