"""Logging configuration — 结构化日志 + request_id 追踪 + 文件轮转。

生产级日志系统：
- Console: 人类可读格式（带 request_id）
- File: 轮转 10MB × 5 份
- Pipeline: 独立文件，便于分析
- RequestIdFilter: 自动注入 request_id 到每条日志

frozen 模式下日志写入 %APPDATA%/PBC/logs/（与 DB/output 同目录），
确保用户能找到日志用于诊断。开发模式写入项目根 logs/。

用法：
    from logging_config import get_logger
    logger = get_logger(__name__)
    logger.info("Processing", extra={"request_id": "abc123", "job_id": "xyz"})
"""
import logging
import logging.handlers
import os
import sys
import uuid
from contextvars import ContextVar
from pathlib import Path


def _default_log_dir() -> str:
    """返回日志目录路径。

    frozen 模式：写入 %APPDATA%/PBC/logs/（与 DB/output 同目录），
    确保 用户能找到日志，且不依赖 exe 所在目录的写权限。
    开发模式：写入项目根 logs/。
    """
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        log_dir = base / "PBC" / "logs"
    else:
        log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    return str(log_dir)

# Context-var: 每个请求独立 request_id，自动注入到日志
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# Context-var: OCR 客户端日志的 job_id 前缀（robustness-F1）。
# pipeline 在 to_thread 调用前 set（asyncio.to_thread 自动拷贝当前 context），
# OCR 客户端模块 logger 挂上 JobIdFilter 后，Stage 1 全流程日志自动带
# [job_id]，排障时可按 job 反查上传/轮询/下载/页拆分完整链路。
ocr_job_id_var: ContextVar[str] = ContextVar("ocr_job_id", default="")


class JobIdFilter(logging.Filter):
    """为 OCR 客户端日志注入 [job_id] 前缀（若 ContextVar 已设置）。

    对抗审查(cr-4): 修改 record.msg 后必须清空 record.args —— formatter 的
    getMessage() 会执行第二次 `msg % args`，OCR 文本若含字面 %s/%d 会抛
    TypeError 导致整条日志丢失。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        job_id = ocr_job_id_var.get()
        if job_id and record.getMessage().find(f"[{job_id}]") == -1:
            record.msg = f"[{job_id}] {record.msg}"
            record.args = ()
        return True


class RequestIdFilter(logging.Filter):
    """注入 request_id 到每条日志记录。

    优先级：
    1. record 中显式传入的 request_id
    2. context-var 中的 request_id（由中间件设置）
    3. "-"
    """
    def filter(self, record):
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get("-")
        if not hasattr(record, "job_id"):
            record.job_id = "-"
        return True


def generate_request_id() -> str:
    """生成短 request_id（8 位）。"""
    return uuid.uuid4().hex[:8]


def setup_logging(log_dir: str = "", level: str = "INFO"):
    """配置日志系统 — console + file + pipeline。

    frozen 模式下 log_dir 默认为 %APPDATA%/PBC/logs/，开发模式默认 logs/。
    显式传入 log_dir 时覆盖默认值（用于测试）。

    环境变量 PBC_NO_FILE_LOG=1 时跳过所有文件 handler（用于测试环境，
    避免 Windows 下多进程持有同一日志文件导致 RotatingFileHandler
    doRollover 触发 PermissionError [WinError 32]）。
    """
    if not log_dir:
        log_dir = _default_log_dir()

    # 人类可读格式（含 request_id + job_id）
    fmt = "%(asctime)s [%(levelname)s] [req=%(request_id)s job=%(job_id)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(fmt, datefmt=datefmt)

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(getattr(logging, level))
    console.setFormatter(formatter)
    console.addFilter(RequestIdFilter())

    # Configure root logger
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # 避免重复添加 console handler（测试中可能多次 import main）
    has_console = any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    )
    if not has_console:
        root.addHandler(console)

    # 文件 handler — 测试环境跳过（PBC_NO_FILE_LOG=1）
    if os.getenv("PBC_NO_FILE_LOG", "").lower() in ("1", "true", "yes"):
        return

    Path(log_dir).mkdir(parents=True, exist_ok=True)

    # File handler (rotate at 10MB, keep 5 backups)
    file_handler = logging.handlers.RotatingFileHandler(
        f"{log_dir}/pharma.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(RequestIdFilter())
    root.addHandler(file_handler)

    # Error-only log（便于运维快速定位）
    error_handler = logging.handlers.RotatingFileHandler(
        f"{log_dir}/error.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    error_handler.addFilter(RequestIdFilter())
    root.addHandler(error_handler)

    # Pipeline-specific log
    pipeline_handler = logging.handlers.RotatingFileHandler(
        f"{log_dir}/pipeline.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    pipeline_handler.setLevel(logging.DEBUG)
    pipeline_handler.setFormatter(formatter)
    pipeline_handler.addFilter(RequestIdFilter())

    # Pipeline loggers write to pipeline.log too
    # (module refactor: core.rules replaces the core.cross_page_analyzer shim;
    #  core.pipeline prefix covers all core.pipeline.* submodules via inheritence)
    for name in ("core.pipeline", "core.ocr_client", "core.mineru_client",
                 "core.page_analyzer", "core.rules", "llm.client"):
        lg = logging.getLogger(name)
        lg.addHandler(pipeline_handler)


def get_logger(name: str) -> logging.Logger:
    """获取 logger（自动应用 RequestIdFilter）。"""
    return logging.getLogger(name)
