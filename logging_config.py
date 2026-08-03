"""Logging configuration — 结构化日志 + request_id 追踪 + 文件轮转。

生产级日志系统：
- Console: 人类可读格式（带 request_id）
- File: 轮转 10MB × 5 份
- Pipeline: 独立文件，便于分析
- RequestIdFilter: 自动注入 request_id 到每条日志

用法：
    from logging_config import get_logger
    logger = get_logger(__name__)
    logger.info("Processing", extra={"request_id": "abc123", "job_id": "xyz"})
"""
import logging
import logging.handlers
import uuid
from contextvars import ContextVar
from pathlib import Path

# Context-var: 每个请求独立 request_id，自动注入到日志
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


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


def setup_logging(log_dir: str = "logs", level: str = "INFO"):
    """配置日志系统 — console + file + pipeline。"""
    Path(log_dir).mkdir(exist_ok=True)

    # 人类可读格式（含 request_id + job_id）
    fmt = "%(asctime)s [%(levelname)s] [req=%(request_id)s job=%(job_id)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(fmt, datefmt=datefmt)

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(getattr(logging, level))
    console.setFormatter(formatter)
    console.addFilter(RequestIdFilter())

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

    # Configure root logger
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(file_handler)
    root.addHandler(error_handler)

    # Pipeline loggers write to pipeline.log too
    for name in ("core.pipeline", "core.ocr_client", "core.mineru_client",
                 "core.page_analyzer", "core.cross_page_analyzer", "llm.client"):
        lg = logging.getLogger(name)
        lg.addHandler(pipeline_handler)


def get_logger(name: str) -> logging.Logger:
    """获取 logger（自动应用 RequestIdFilter）。"""
    return logging.getLogger(name)
