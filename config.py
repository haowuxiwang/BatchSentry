"""Configuration management — loads from JSON config file, exposes typed config.

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

Config persistence: Settings API writes to a JSON file (config.json) in
the app data directory. On startup, JSON values are loaded into os.environ
so the rest of this module reads them via os.getenv() unchanged.
If config.json doesn't exist but a legacy .env does, it's auto-migrated.
"""
import json
import logging
import os
import sys
from pathlib import Path
from dataclasses import dataclass
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


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


def _config_path() -> Path:
    """返回 JSON 配置文件路径。

    frozen 模式: %APPDATA%/PBC/config.json
    开发模式: 项目根 config.json
    """
    if _is_frozen():
        return _app_data_dir() / "config.json"
    return Path("config.json")


def _persist_env_to_config(env_key: str, value: str) -> None:
    """原子写入单个字段到 config.json + 同步 os.environ。

    用于启动时自动激活迁移：当 active provider 未配置 Key 时，
    自动切换到已配置的 provider 并持久化，避免每次启动重复迁移。
    """
    import json as _json
    import uuid as _uuid
    config_path = _config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                existing = _json.load(f)
        except (ValueError, OSError):
            existing = {}
    existing[env_key] = value
    tmp = config_path.parent / f"config.json.tmp.{os.getpid()}.{_uuid.uuid4().hex[:8]}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(existing, f, indent=2, ensure_ascii=False)
        tmp.replace(config_path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        # 持久化失败不阻塞启动 — 内存已更新，下次启动会再次迁移
    os.environ[env_key] = value


def _env_path_legacy() -> Path:
    """返回旧版 .env 文件路径（仅用于自动迁移）。"""
    if _is_frozen():
        return _app_data_dir() / ".env"
    return Path(".env")


def _migrate_env_to_json(env_path: Path, json_path: Path) -> None:
    """一次性迁移：读取 .env 文件内容，写入 JSON 配置文件。

    保留 .env 文件原样（不删除，不重命名），仅创建 JSON 副本。
    迁移后 config.py 只读 JSON，.env 不再生效。

    对抗审查 P2-I：原实现用简易正则 ^([A-Z_]+)=..."?(.*)"?$ 解析，三个缺陷：
    (a) 行内注释 `KEY=sk-xxx # comment` 整段进 value → 迁移后密钥带垃圾
        尾巴，LLM/OCR 认证神秘失败；(b) `export KEY=...` 行首字母小写
        不匹配 → 配置静默丢失；(c) 写盘非原子。改用 dotenv 官方解析器
        （项目本就依赖 python-dotenv）+ tmp+replace 原子写盘。
    """
    import uuid
    from dotenv import dotenv_values
    config_data = dotenv_values(str(env_path))
    if not config_data:
        logger.info(f"No parseable keys in {env_path} — skipping migration")
        return
    json_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = json_path.parent / f"config.json.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2, ensure_ascii=False)
        tmp.replace(json_path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise  # 迁移失败上抛，由调用方决定是否继续
    logger.info(f"Migrated {len(config_data)} keys from {env_path} → {json_path}")


def _load_json_config() -> None:
    """加载 JSON 配置文件到 os.environ。

    优先级（从高到低）：
      1. JSON 配置文件（config.json）— 设置页面管理的正式配置
      2. OS 环境变量 / .env — 无 config.json 时的部署/迁移来源
      3. 代码默认值
      注意：1 优先于 2 — 设置页保存只写 config.json（os.environ 只是
      进程级镜像）；若 OS 环境变量残留旧值（如 LLM_PROVIDER 曾 export），
      JSON 必须压过它，否则每次启动设置被回滚（对抗审查 cr-17）。

    迁移逻辑：JSON 不存在但 .env 存在 → 读取 .env 写入 JSON，
    后续只读 JSON。
    """
    json_path = _config_path()

    if json_path.exists():
        # 对抗审查 P1-C：config.json 损坏（PowerShell 5.1 BOM / 手改语法
        # 错误 / 半写文件）此前在模块导入期 json.load 直接崩 → 应用无法
        # 启动（frozen 模式 exit code 1，无任何降级）。与 load_user_rules
        # 同款防御：utf-8-sig 吞 BOM + 解析失败回退空配置并告警。
        config_data: dict = {}
        try:
            with open(json_path, "r", encoding="utf-8-sig") as f:
                config_data = json.load(f)
        except (ValueError, OSError) as e:
            logger.warning(f"config.json unreadable ({e}) — starting with defaults")
        if not isinstance(config_data, dict):
            config_data = {}
        for key, value in config_data.items():
            # 无条件载入（覆盖 OS 环境变量）：设置页只写 config.json，
            # env 是进程级镜像 — 残留 env 值（重启回滚 bug 根因）必须
            # 被 JSON 压过。非标量值（user_rules 列表等）不进入
            # os.environ，由 load_user_rules() 等专用读取器直接从
            # JSON 文件获取。
            if isinstance(value, (str, int, float, bool)):
                os.environ[key] = str(value)
        logger.info(f"Loaded {len(config_data)} config keys from {json_path}")
        return

    # JSON 不存在 — 检查是否有旧版 .env 需要迁移
    env_path = _env_path_legacy()
    if env_path.exists():
        logger.info(f"Config JSON not found, migrating from {env_path}")
        try:
            _migrate_env_to_json(env_path, json_path)
        except Exception as e:
            # 对抗审查：迁移失败（磁盘满/权限/畸形 .env）此前直接上抛 —
            # 模块导入期崩溃，frozen 模式 exit code 1。迁移只是便利功能，
            # 失败不应阻断启动（用户可稍后手工修复或重跑迁移）。
            logger.warning(
                f".env migration failed ({e}) — continuing with defaults; "
                f"review {env_path} manually"
            )
        # 迁移后重新加载 JSON
        config_data = {}
        try:
            with open(json_path, "r", encoding="utf-8-sig") as f:
                config_data = json.load(f)
        except (ValueError, OSError) as e:
            logger.warning(f"config.json unreadable after migration ({e})")
        if not isinstance(config_data, dict):
            config_data = {}
        for key, value in config_data.items():
            if isinstance(value, (str, int, float, bool)):
                os.environ[key] = str(value)
        logger.info(f"Loaded {len(config_data)} config keys from {json_path} (after migration)")
        return

    # JSON 和 .env 都不存在 — 开发模式尝试 load_dotenv（项目根 .env）
    if not _is_frozen():
        load_dotenv()
    # else: frozen 模式无配置文件，依赖 OS 环境变量或默认值


# 加载配置到 os.environ
_load_json_config()


# ============================================================
# User-defined compliance rules (Phase 10)
# ============================================================

_USER_RULES_KEY = "user_rules"

# 用户规则上限 — 单一来源（T2.4：settings API 校验与 load_user_rules 截断
# 共用，此前两处各写一遍，调整上限时容易改一处漏一处）
USER_RULES_MAX = 100
USER_RULES_TEXT_MAX = 1000


def load_user_rules() -> list[dict]:
    """读取用户在设置页填写的合规规则（config.json 的 user_rules 数组）。

    规则结构: {"id": str, "text": str, "active": bool, "created_at": str}
    过滤掉结构不合法或 text 为空的条目。active 缺省视为启用。
    返回的列表用于跨页分析时注入 LLM prompt（工厂/产品专属合规约束）。
    """
    json_path = _config_path()
    if not json_path.exists():
        return []
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        rules = data.get(_USER_RULES_KEY, [])
    except (json.JSONDecodeError, OSError, TypeError):
        logger.warning(f"Failed to read user_rules from {json_path}")
        return []
    if not isinstance(rules, list):
        return []
    valid = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        text = r.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        valid.append({
            "id": str(r.get("id", "")) or uuid_str(),
            "text": text.strip(),
            "active": bool(r.get("active", True)),
            "created_at": str(r.get("created_at", "")),
        })
    return valid[: USER_RULES_MAX]


def uuid_str() -> str:
    """生成短 UUID（规则 id 等使用）。"""
    import uuid as _uuid
    return _uuid.uuid4().hex[:12]


# 规则开关（M4/T4.0）：config.json 顶层 rules.disabled 数组，按规则 id 关闭。
_RULES_KEY = "rules"


_KB_KEY = "kb"


def load_disabled_kb_sources() -> list[str]:
    """读取被关闭的知识库来源 id（config.json 的 `kb.disabled_sources`）。

    供 core.kb.retriever.enabled_source_ids() 使用：允许按来源关闭语料
    （如现场只认中国法规、不需要 FDA/EU 条款）。与 load_disabled_rule_ids
    同构：读取失败/结构非法一律返回空列表（= 全部启用）——知识库是增强项，
    绝不因配置问题而静默清空检索结果。
    """
    json_path = _config_path()
    if not json_path.exists():
        return []
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, TypeError):
        logger.warning(f"Failed to read kb config from {json_path}")
        return []
    kb = data.get(_KB_KEY, {})
    if not isinstance(kb, dict):
        return []
    disabled = kb.get("disabled_sources", [])
    if not isinstance(disabled, list):
        return []
    return [str(x) for x in disabled if isinstance(x, (str, int))]


def load_disabled_rule_ids() -> list[str]:
    """读取被关闭的规则 id（config.json 的 `rules.disabled` 数组）。

    供 core.rules.registry.enabled_rule_specs() 使用：允许按 id 关闭某条规则
    （如现场无该工艺），使"规则是否合理"可配置可审计（T4.0）。
    读取失败/结构非法一律返回空列表（= 全部启用）—— 绝不因配置问题而
    静默禁用规则（合规检查宁可多报不可漏检）。
    """
    json_path = _config_path()
    if not json_path.exists():
        return []
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, TypeError):
        logger.warning(f"Failed to read rules config from {json_path}")
        return []
    rules = data.get(_RULES_KEY, {})
    if not isinstance(rules, dict):
        return []
    disabled = rules.get("disabled", [])
    if not isinstance(disabled, list):
        return []
    return [str(x) for x in disabled if isinstance(x, (str, int))]


# ============================================================
# Feishu notification (Phase 12)

# 通知触发状态白名单 — 单一来源（T2.4：load_feishu_config 过滤与
# settings API 校验共用；此前两份同义 set，改白名单要同步两处）
FEISHU_ALLOWED_EVENTS = ("review", "partial_review", "error", "cancelled")

_FEISHU_DEFAULTS: dict = {
    "enabled": False,
    "mode": "webhook",        # "webhook" (group custom bot) | "app_bot" (self-built app DM)
    "webhook_url": "",
    "secret": "",
    "app_id": "",
    "app_secret": "",
    "open_id": "",            # receiver open_id (app-scoped, ou_ prefix)
    "mobile": "",             # receiver mobile (optional helper to resolve open_id)
    "events": ["review", "partial_review", "error"],
}


def _feishu_bool_flag(data: dict, key: str, default: bool = False) -> bool:
    """Parse a possibly-string boolean from raw config.json."""
    raw = data.get(key, default)
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(raw)


def load_feishu_config() -> dict:
    """读取飞书通知配置（config.json 顶层键，Settings 页可编辑）。

    键: feishu_enabled / feishu_mode / feishu_webhook_url / feishu_secret /
        feishu_app_id / feishu_app_secret / feishu_open_id / feishu_mobile /
        feishu_events
    mode 支持两种通道：
      - "webhook": 自定义群机器人（发到群里，附签名/关键词）
      - "app_bot": 企业自建应用机器人（发单聊给个人，需 app_id +
        app_secret + 接收者 open_id 或手机号）
    events 为逗号分隔的触发状态白名单（默认 review,partial_review,error）。
    读取失败时返回默认值（与 load_user_rules 同款防御式读取）。
    """
    json_path = _config_path()
    out = dict(_FEISHU_DEFAULTS)
    if not json_path.exists():
        return out
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, TypeError):
        logger.warning(f"Failed to read feishu config from {json_path}")
        return out
    out["enabled"] = _feishu_bool_flag(data, "feishu_enabled", False)
    mode = str(data.get("feishu_mode", "webhook")).strip().lower()
    out["mode"] = mode if mode in ("webhook", "app_bot") else "webhook"
    out["webhook_url"] = str(data.get("feishu_webhook_url", "")).strip()
    out["secret"] = str(data.get("feishu_secret", "")).strip()
    out["app_id"] = str(data.get("feishu_app_id", "")).strip()
    out["app_secret"] = str(data.get("feishu_app_secret", "")).strip()
    out["open_id"] = str(data.get("feishu_open_id", "")).strip()
    out["mobile"] = str(data.get("feishu_mobile", "")).strip()
    events = data.get("feishu_events")
    # 对抗审查：feishu_events 理论上应是逗号分隔字符串；若 config.json
    # 被手改/旧版写入成 list/bool，str() 会把它变成 "['a', 'b']" 之类的
    # 垃圾事件名（触发条件误判）。非字符串按未配置处理。
    if isinstance(events, str) and events.strip():
        parsed_events = [e.strip() for e in events.split(",") if e.strip()]
        # 白名单：非法状态名直接丢弃（防脏数据进通知开关）
        parsed_events = [e for e in parsed_events if e in FEISHU_ALLOWED_EVENTS]
        if parsed_events:
            out["events"] = parsed_events
    return out


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


# MinerU 可配置取值（T2.2 集中单一来源；settings API 后端 + 前端下拉共用）
MINERU_MODEL_VERSIONS = {"pipeline", "vlm", "MinerU-HTML"}
MINERU_LANGUAGES = {"ch", "en"}


@dataclass
class MinerUConfig:
    """MinerU 精准解析 API 配置。

    Token 在 https://mineru.net/apiManage 页面创建。
    base_url: 默认官方 https://mineru.net/api/v4；私有化/代理场景覆盖。
    model_version: pipeline (默认) / vlm (推荐,精度更高) / MinerU-HTML
    language: ch / en
    """
    token: str
    base_url: str
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
    llm_concurrency: int  # Stage 2 并发 LLM 页面分析数
    ocr_slices: int  # MinerU 分片 OCR 页数/片（1=不分片，流式逐片分析）
    llm_context_window: int  # LLM 上下文窗口（tokens）— Round 7 跨页摘要预算推导依据
    ocr_dual_compare: bool  # 门禁 3：主后端成功后用备选后端复跑并对比（成本翻倍，opt-in）
    kb_prompt_inject: bool  # KB-2：知识库条文参考注入（RAG grounding，默认开）
    llm_json_mode: bool  # 结构化输出：openai 协议 json_object 模式（网关不支持自动降级）


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


# 对抗审查(cr-11): env 中的非法整数值（误配置）不应导致 import 崩溃
def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        logger.warning(f"Invalid {key} in env, using default {default}")
        return default


# ── 上传限额（单一真值）──────────────────────────────────────────────
#
# **三处都由这里派生**：后端强制（api/jobs）、前端预检（static/upload.js）、
# 页面文案（templates/upload.html）。历史缺陷：200MB 被分别写死在
# `api/jobs/__init__._MAX_PDF_BYTES`、`api/jobs/upload.py` 的错误消息、
# `static/upload.js` 的比较式、`templates/upload.html` 的提示文字 —— 四处
# 各自独立，任一处调整都会静默漂移（前端放行、后端拒绝，反之亦然）。
#
# 页数上限的**定标依据**（实测推导，非照抄云厂商）：
#   实测真实 51 页批记录（job 95a27d88-52b，Paddle）：
#     Stage1 OCR   460.5s（9.0 s/页）
#     Stage2 逐页 LLM 分析 1163.7s（22.8 s/页）
#     全链 1889s（32.7 s/页）
#   ① 云厂商的 1,000~3,000 页**不可移植** —— 它们单页成本约 1–2s，比本链路
#      快 10~30 倍。可移植的量是**墙钟时间**，不是页数。
#   ② 既有的 core/ocr_client.POLL_TIMEOUT_MAX=3600s 在 100 页即封顶
#      （600 + 30×页数）。按 OCR 9.0 s/页，400 页即打满 1 小时；而拥堵日
#      单页可达数十秒（见 static/eta.js 注释中"单页排队 500-1000s"的记载），
#      故不能贴边取值。
#   ③ 取 200 页 = OCR 预估 1800s，对 3600s 上限留 **2× 余量**，同时高于任何
#      单批记录的合理规模（典型 20~120 页，本样本 51 页）。200 页全链预估
#      ≈ 1.8 小时，已属"必须事先告知用户"的量级。
#
# 两者正交：体积上限挡"巨文件"，页数上限挡"低密度但极长"的 PDF
# （数字排版件可能 50KB/页，200MB 能装下数千页而体积检查毫无察觉）。
#
# ⚠️ 页数读取失败时（pdf_page_count==0，见 upload.py 的容错分支）**不得**
# 以此为据拒绝 —— 部分损坏 PDF 云端 OCR 可能仍能处理。
UPLOAD_LIMITS = {
    "max_bytes": _env_int("MAX_UPLOAD_BYTES", 200 * 1024 * 1024),
    "max_pages": _env_int("MAX_UPLOAD_PAGES", 200),
    "warn_pages": _env_int("WARN_UPLOAD_PAGES", 80),
    # 单页耗时先验（秒）— 用于软告警里的"预估耗时"，取自上述实测。
    # 随上游拥堵波动，故表述为"预估"而非承诺。
    "sec_per_page_ocr": 9.0,
    "sec_per_page_total": 32.7,
}


def _normalize_upload_limits(limits: dict) -> dict:
    """把误配置的限额收敛到自洽区间（非法值不崩溃、不产生荒谬语义）。

    - 页数上限必须 ≥2：1 页上限等于拒绝一切多页批记录，必是误配置；
    - 软告警阈值必须落在 [1, max_pages)：等于或超过硬上限会让告警永不触发，
      小于 1 会每份文件都告警。
    """
    out = dict(limits)
    max_pages = out.get("max_pages", 200)
    if not isinstance(max_pages, int) or max_pages < 2:
        logger.warning(f"Invalid max_pages={max_pages!r}, using 200")
        max_pages = 200
    out["max_pages"] = max_pages
    warn = out.get("warn_pages", 80)
    if not isinstance(warn, int) or not (1 <= warn < max_pages):
        logger.warning(
            f"Invalid warn_pages={warn!r} (max_pages={max_pages}), using default"
        )
        warn = min(80, max_pages - 1)
    out["warn_pages"] = warn
    return out


UPLOAD_LIMITS = _normalize_upload_limits(UPLOAD_LIMITS)


def check_upload_page_limits(
    page_count: int, limits: dict | None = None
) -> tuple[str | None, str | None]:
    """页数限额判定 —— 返回 ``(reject_detail, warn_text)``。

    **纯函数**：上下限文案与判定留在一处（与数值同源），既避免"数值改了、
    文案没改"的漂移，也让策略语义可脱离 HTTP 层直接单测（无需构造真实 PDF）。

    - ``page_count <= 0``：页数未知 / 读取失败 → 一律放行。部分损坏 PDF 云端
      OCR 可能仍能处理，拦掉等于把"可能成功"变成"必然失败"（既有行为，
      见 upload.py 的容错分支）。
    - 超硬上限：拒绝，提示含真实页数与上限，并给出可执行动作（按批次拆分）。
    - 超软阈值：放行并返回预估耗时文案 —— 对常态文件不吭声，否则等于没告警。
    """
    lim = limits if limits is not None else UPLOAD_LIMITS
    if page_count <= 0:
        return None, None
    if page_count > lim["max_pages"]:
        return (
            f"PDF 共 {page_count} 页，超过单文件上限 {lim['max_pages']} 页。"
            f"请按批次拆分后分别上传。",
            None,
        )
    if page_count > lim["warn_pages"]:
        est_minutes = page_count * lim["sec_per_page_total"] / 60
        return None, (
            f"页数较多（{page_count} 页），预估耗时约 {est_minutes:.0f} 分钟，"
            f"并会消耗较多 OCR / LLM 配额。"
        )
    return None, None


# 测试/占位 Key 模式 — 单一来源（T2.4：settings API 的 configured 标志
# 与启动时的 auto-activate 判定共用此常量，加新模式只改一处）
TEST_KEY_PATTERNS = (
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


def _is_real_key(key: str) -> bool:
    """Return True only if the key looks like a real API key (not a test value).

    判定逻辑（业界做法 - 参考 OpenAI/Anthropic）：
      1. 非空（已配置就应被识别，不靠长度猜测）
      2. 不匹配明显的测试/占位模式（sk-test, placeholder 等）
    """
    if not key:
        return False
    kl = key.lower()
    return not any(p in kl for p in TEST_KEY_PATTERNS)


def load_config():
    # Phase 5B: in frozen mode, redirect db/output to per-user APPDATA dir
    # so the bundled exe doesn't need write access to Program Files.
    #
    # E2E fix: the .env file at %APPDATA%/PBC/.env may contain relative
    # DATABASE_PATH / OUTPUT_DIR values (e.g. "data/pharma.db") left over
    # from development sessions or written by the Settings API. In frozen
    # mode these relative paths resolve against the exe's CWD, NOT APPDATA,
    # violating the constraint that data must live in %APPDATA%/PBC/.
    # Fix: in frozen mode, always force APPDATA paths for db/output,
    # ignoring any relative-path override from .env. An absolute path in
    # .env is still respected (intentional user configuration).
    if _is_frozen():
        data_dir = _app_data_dir()
        default_db = str(data_dir / "data.db")
        default_output = str(data_dir / "output")
        _env_db = os.getenv("DATABASE_PATH", "")
        _env_output = os.getenv("OUTPUT_DIR", "")
        # Only respect env override if it's an absolute path (intentional).
        # Relative paths from dev .env are ignored — they'd land in the exe
        # CWD and survive upgrades, defeating the APPDATA redirect.
        db_path = _env_db if (_env_db and os.path.isabs(_env_db)) else default_db
        output_dir = _env_output if (_env_output and os.path.isabs(_env_output)) else default_output
    else:
        db_path = os.getenv("DATABASE_PATH", "data/pharma.db")
        output_dir = os.getenv("OUTPUT_DIR", "output")

    providers = _load_all_providers()
    llm_provider = os.getenv("LLM_PROVIDER", "deepseek").lower()
    # If the configured LLM_PROVIDER is not in the registry (e.g. typo or
    # the provider's API key was never set), fall back to deepseek.
    if llm_provider not in providers:
        llm_provider = "deepseek"

    # Auto-activate migration (修复存量配置的死亡陷阱):
    # 若当前 active provider 未配置 Key，但另一个 provider 已配置 Key，
    # 自动切换到第一个已配置的 provider 并持久化。
    # 场景：用户配置了 SiliconFlow Key 但 active 仍是默认 deepseek（无 Key），
    # 导致所有 LLM 调用和 health probe 报 "API key not configured"。

    # 判定函数 _is_real_key / TEST_KEY_PATTERNS 见模块级定义（T2.4 单一来源）

    active_cfg = providers.get(llm_provider)
    if active_cfg and not _is_real_key(active_cfg.api_key):
        # active provider 未配置 — 找第一个已配置的 provider
        for name, cfg in providers.items():
            if name != llm_provider and _is_real_key(cfg.api_key):
                logger.warning(
                    f"Auto-activating provider {name!r} (active {llm_provider!r} "
                    f"has no API key configured)"
                )
                llm_provider = name
                os.environ["LLM_PROVIDER"] = name
                _persist_env_to_config("LLM_PROVIDER", name)
                break

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
            base_url=os.getenv("MINERU_BASE_URL", "https://mineru.net/api/v4"),
            model_version=os.getenv("MINERU_MODEL_VERSION", "vlm"),
            language=os.getenv("MINERU_LANGUAGE", "ch"),
            enable_formula=os.getenv("MINERU_ENABLE_FORMULA", "true").lower() == "true",
            enable_table=os.getenv("MINERU_ENABLE_TABLE", "true").lower() == "true",
        ),
        "app": AppConfig(
            host=os.getenv("APP_HOST", "127.0.0.1"),
            # Phase 8: prefer PORT (set by Electron + used by server.py) over
            # APP_PORT. Previously /api/settings returned port=8000 even when
            # the server was actually running on 58765 (Electron's default).
            port=_env_int("PORT", _env_int("APP_PORT", 8000)),
            database_path=db_path,
            output_dir=output_dir,
            llm_provider=llm_provider,
            ocr_backend=os.getenv("OCR_BACKEND", "paddle"),
            llm_concurrency=_env_int("LLM_CONCURRENCY", 5),
            # MinerU 分片 OCR：每片 N 页，片完成即分析（流式输出）。
            # 1 = 不分片（整份 PDF 一次提交，等全部 OCR 完成再分析）。
            ocr_slices=_env_int("OCR_SLICES", 1),
            # Round 7: LLM 上下文窗口（tokens）。跨页分析摘要按窗口预算
            # 推导（35% 窗口 × 1.6 字符/token），防止大 job（200 页+）
            # 或小窗口模型（GLM/Kimi 32K）的提示词溢出。默认 128K
            # （DeepSeek-V3.2 / 通用大窗口）。
            llm_context_window=_env_int("LLM_CONTEXT_WINDOW", 128_000),
            # 门禁 3（OCR_GOLDEN_CORPUS.md）：双后端输出对比。默认关闭 —
            # 每个任务多跑一次完整 OCR（分钟级 + 上游配额），按需开启。
            ocr_dual_compare=os.getenv("OCR_DUAL_COMPARE", "false").lower()
            in ("1", "true", "yes"),
            # KB-2: 知识库条文参考注入（RAG grounding）。默认开启；
            # 关闭后仅保留后置引用富集（findings.kb_refs 不受影响）。
            kb_prompt_inject=os.getenv("KB_PROMPT_INJECT", "true").lower()
            in ("1", "true", "yes"),
            # P1-7 结构化输出：**默认开启**（M2/T2.10）—— json_object 显著降低
            # "JSON 修复链"重试开销（实测曾触发 330s fix-hint 重试）。兼容网关
            # 首次 400 会自动降级重试一次并**按 provider 会话内禁用**，故默认开
            # 不会让不支持的网关失败。置 LLM_JSON_MODE=false 可强制关闭。
            llm_json_mode=os.getenv("LLM_JSON_MODE", "true").lower()
            in ("1", "true", "yes"),
        ),
    }


config = load_config()


def update_config(updates: dict):
    """运行时更新内存中的 config 对象（写入 config.json 后调用）。

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
    if "ocr_slices" in updates:
        try:
            config["app"].ocr_slices = max(1, int(updates["ocr_slices"]))
        except (TypeError, ValueError):
            config["app"].ocr_slices = 1
    if "ocr_dual_compare" in updates:
        config["app"].ocr_dual_compare = str(
            updates["ocr_dual_compare"]
        ).lower() in ("1", "true", "yes")
    if "kb_prompt_inject" in updates:
        config["app"].kb_prompt_inject = str(
            updates["kb_prompt_inject"]
        ).lower() in ("1", "true", "yes")
    if "llm_json_mode" in updates:
        config["app"].llm_json_mode = str(
            updates["llm_json_mode"]
        ).lower() in ("1", "true", "yes")

    # Add new providers to the registry on the fly (Settings UI "add provider")
    if "llm_providers_add" in updates:
        for raw_name in str(updates["llm_providers_add"]).split(","):
            name = raw_name.strip().lower()
            if name and name not in config["providers"]:
                config["providers"][name] = ProviderConfig(name=name)

    # Remove custom providers from the registry on the fly (Settings UI "remove
    # provider"). Built-in providers (deepseek/siliconflow) stay untouched.
    if "llm_providers_remove" in updates:
        for raw_name in str(updates["llm_providers_remove"]).split(","):
            name = raw_name.strip().lower()
            if name and name in config["providers"] and name not in ("deepseek", "siliconflow"):
                del config["providers"][name]
                # Keep the "active provider" pointer valid: fall back to deepseek
                if config["app"].llm_provider == name:
                    config["app"].llm_provider = "deepseek"

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
    if "mineru_base_url" in updates:
        config["mineru"].base_url = updates["mineru_base_url"]
        # mineru_client 运行时从 env 读取，同步进程镜像使热更新生效
        os.environ["MINERU_BASE_URL"] = str(updates["mineru_base_url"])
    if "mineru_model_version" in updates:
        config["mineru"].model_version = updates["mineru_model_version"]
    if "mineru_language" in updates:
        config["mineru"].language = updates["mineru_language"]
    if "mineru_enable_formula" in updates:
        config["mineru"].enable_formula = updates["mineru_enable_formula"]
    if "mineru_enable_table" in updates:
        config["mineru"].enable_table = updates["mineru_enable_table"]
