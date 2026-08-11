"""Pytest 全局 fixture — 生产级测试基础设施。

提供：
- test_db: 内存 SQLite 数据库（隔离，自动清理）
- test_client: FastAPI TestClient（基于 httpx）
- mock_ocr: OCR 后端 mock（避免真实 HTTP 调用）
- mock_llm: LLM 客户端 mock
- sample_pdf: 临时 PDF 文件

测试环境日志策略：
- 禁用 RotatingFileHandler（避免 Windows WinError 32 文件占用冲突）
- 设置 multipart logger 为 WARNING（避免上传测试中 65536 字节 chunk 产生海量 debug 日志）
- 上述两项曾导致 pytest 输出 19MB 噪音 + 测试结果被淹没
"""
import os
import sys
import tempfile
import sqlite3
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient

# 将项目根加入 sys.path（确保 tests/ 能导入项目模块）
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── 测试环境日志净化（必须在 import main 之前） ──────────────────────
# 1. 告知 logging_config 不要创建文件 handler（Windows 下多进程持有同一日志文件
#    会导致 RotatingFileHandler.doRollover 触发 PermissionError [WinError 32]，
#    每次 log 都打印完整异常堆栈，淹没测试输出。）
os.environ.setdefault("PBC_NO_FILE_LOG", "1")

# 2. 抑制 python-multipart 的 debug 日志（上传测试中每读 65536 字节打一条日志，
#    放大日志轮转问题）
logging.getLogger("multipart").setLevel(logging.WARNING)
logging.getLogger("python_multipart").setLevel(logging.WARNING)


@pytest.fixture(autouse=True)
def _suppress_noisy_loggers():
    """每个测试前静噪第三方库的 debug 日志（autouse）。

    - multipart / python_multipart: 上传解析的逐 chunk debug 日志
    - asyncio: 事件循环内部 debug 日志
    - urllib3 / httpx: HTTP 请求 debug 日志（测试中用 mock，无需）
    """
    for name in ("multipart", "python_multipart", "asyncio", "urllib3", "httpx",
                 "httpcore", "h11"):
        logging.getLogger(name).setLevel(logging.WARNING)
    yield


@pytest_asyncio.fixture
async def test_db(tmp_path):
    """提供隔离的文件 SQLite 数据库。

    每个测试函数获得独立的数据库文件，自动初始化 schema，测试后清理。
    通过 patch config["app"].database_path 指向临时文件，并重置 db.client._db
    全局连接，确保测试间完全隔离。

    同时注入 mock LLM provider 配置（api_key="test-key"），让上传拦截
    （create_job 中的 needs_setup 检查）在测试中不生效。
    """
    db_path = tmp_path / "test.db"
    import db.client as db_mod
    from config import config as _cfg

    # 保存原始值，fixture 结束后恢复
    orig_db_path = _cfg["app"].database_path
    orig_output_dir = _cfg["app"].output_dir
    orig_db_global = db_mod._db
    # _cfg["providers"] 在 load_config() 时已注入 deepseek / siliconflow
    # （_load_all_providers 总是注册这两个内置 provider），保存其 api_key
    # 原值即可恢复。
    orig_deepseek_key = _cfg["providers"]["deepseek"].api_key

    # 指向临时数据库 + 临时输出目录
    _cfg["app"].database_path = str(db_path)
    _cfg["app"].output_dir = str(tmp_path / "output")
    db_mod._db = None  # 强制 get_db() 重新连接

    # 注入 mock LLM api_key（绕过 create_job 的"未配置 LLM 服务商"拦截）。
    # 直接修改 _cfg["providers"]["deepseek"].api_key —— 与 production 读取
    # 路径完全一致（api/jobs.py 用 config["providers"]）。早期 fixture 误注入
    # 到不存在的 "llm_providers" 键，与 production 路径脱节，掩盖了上传守卫
    # 的真实 bug（见对抗性审查 B-C1）。
    _cfg["providers"]["deepseek"].api_key = "sk-test-key-for-unit-test-only"

    try:
        db = await db_mod.get_db()  # 连接 + 自动 init_schema + migrate
        yield db
    finally:
        # 关闭连接并清理
        if db_mod._db:
            await db_mod._db.close()
        db_mod._db = orig_db_global
        _cfg["app"].database_path = orig_db_path
        _cfg["app"].output_dir = orig_output_dir
        _cfg["providers"]["deepseek"].api_key = orig_deepseek_key
        if db_path.exists():
            db_path.unlink()


@pytest_asyncio.fixture
async def test_client(test_db):
    """提供 FastAPI 测试客户端。

    使用 ASGITransport 直接调用 ASGI app，无需网络端口。
    httpx >= 0.28 移除了 AsyncClient(app=) 参数，必须用 ASGITransport。
    """
    from httpx import ASGITransport
    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:8000") as client:
        yield client


@pytest.fixture
def mock_ocr():
    """Mock OCR 后端 — 返回固定页面数据，避免真实 HTTP 调用。"""
    async def fake_run_ocr(pdf_path, pages=None):
        return [
            {"markdown": {"text": f"# 第 {p} 页\n\n测试内容 {p}"}} for p in (pages or [1, 2, 3])
        ]
    return fake_run_ocr


@pytest.fixture
def mock_llm():
    """Mock LLM 客户端 — 返回固定结构化 JSON。"""
    async def fake_analyze_page(page_text, page_no):
        return {
            "steps": [
                {
                    "step_no": 1,
                    "time": "2024-01-01 10:00",
                    "measurements": [
                        {"name": "温度", "values": {"A": "25.5", "B": "26.0"}},
                    ],
                }
            ],
            "findings": [
                {"type": "参数越界", "severity": "warning", "description": "温度偏高"},
            ],
            "overall_confidence": "high",
        }
    return fake_analyze_page


@pytest.fixture
def sample_pdf(tmp_path):
    """生成临时 PDF 文件用于测试。"""
    # 用 PyMuPDF 生成最小 PDF
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 50), "Test PDF content for batch checker")
    pdf_path = tmp_path / "test_batch.pdf"
    doc.save(str(pdf_path))
    doc.close()
    return str(pdf_path)


@pytest.fixture
def sample_job_data():
    """返回模拟的 job 数据字典（不写数据库）。"""
    return {
        "id": "test-job-001",
        "filename": "test_batch.pdf",
        "pdf_path": "/tmp/test.pdf",
        "status": "review",
        "total_pages": 5,
        "created_at": "2026-01-01 10:00:00",
        "finished_at": "2026-01-01 10:05:00",
        "stage1_ms": 30000,
        "stage2_ms": 60000,
        "stage3_ms": 5000,
        "failed_pages": "[]",
        "error_message": None,
    }


@pytest.fixture
def sample_finding_data():
    """返回模拟的 finding 数据。"""
    return {
        "id": 1,
        "job_id": "test-job-001",
        "page": 1,
        "type": "时间逻辑",
        "severity": "critical",
        "source": "rule",
        "description": "第10页工序3早于第9页工序2结束",
        "ocr_text": "工序3开始时间 14:30",
        "status": "pending",
    }
