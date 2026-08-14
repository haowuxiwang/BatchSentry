"""日志系统单元测试。

覆盖：
- RequestIdFilter 注入 request_id
- generate_request_id 格式
- setup_logging 初始化
"""
import pytest
import logging

from logging_config import (
    setup_logging,
    generate_request_id,
    request_id_var,
    RequestIdFilter,
)


class TestGenerateRequestId:
    """generate_request_id。"""

    def test_returns_string(self):
        rid = generate_request_id()
        assert isinstance(rid, str)

    def test_length_is_8(self):
        """request_id 应为 8 位短 ID。"""
        rid = generate_request_id()
        assert len(rid) == 8

    def test_uniqueness(self):
        """连续调用应产生不同 ID。"""
        ids = {generate_request_id() for _ in range(100)}
        assert len(ids) == 100  # 全部唯一


class TestRequestIdFilter:
    """RequestIdFilter。"""

    def test_filter_injects_request_id(self):
        """应注入 request_id 到 record。"""
        filter = RequestIdFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="test.py", lineno=1,
            msg="test message", args=(), exc_info=None,
        )
        assert filter.filter(record) is True
        assert hasattr(record, "request_id")
        assert hasattr(record, "job_id")

    def test_filter_preserves_existing_request_id(self):
        """已设置 request_id 的 record 应保留。"""
        filter = RequestIdFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="test.py", lineno=1,
            msg="test", args=(), exc_info=None,
        )
        record.request_id = "custom-id"
        filter.filter(record)
        assert record.request_id == "custom-id"

    def test_filter_uses_context_var(self):
        """应从 context-var 读取 request_id。"""
        token = request_id_var.set("ctx-id-123")
        try:
            filter = RequestIdFilter()
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname="test.py", lineno=1,
                msg="test", args=(), exc_info=None,
            )
            filter.filter(record)
            assert record.request_id == "ctx-id-123"
        finally:
            request_id_var.reset(token)


class TestSetupLogging:
    """setup_logging。

    注意：conftest.py 设置 PBC_NO_FILE_LOG=1 跳过文件 handler，
    这两个测试需要清除该环境变量才能验证文件 handler 创建逻辑。
    """

    @pytest.fixture(autouse=True)
    def enable_file_logging(self, monkeypatch):
        """临时清除 PBC_NO_FILE_LOG，让 setup_logging 创建文件 handler。"""
        monkeypatch.delenv("PBC_NO_FILE_LOG", raising=False)
        yield

    def test_setup_creates_handlers(self, tmp_path):
        """应创建 console + file + pipeline + error handlers。"""
        log_dir = str(tmp_path / "logs")
        setup_logging(log_dir=log_dir, level="INFO")
        root = logging.getLogger()
        assert len(root.handlers) >= 3  # console + file + error

    def test_pipeline_loggers_have_pipeline_handler(self, tmp_path):
        """pipeline 相关 logger 应有 pipeline handler。"""
        log_dir = str(tmp_path / "logs")
        setup_logging(log_dir=log_dir, level="INFO")
        pipeline_logger = logging.getLogger("core.pipeline")
        assert any("pipeline" in str(h.__class__.__name__).lower() or
                   "RotatingFileHandler" in str(h.__class__.__name__)
                   for h in pipeline_logger.handlers)

    def test_no_file_log_env_skips_file_handlers(self, tmp_path, monkeypatch):
        """PBC_NO_FILE_LOG=1 时应跳过文件 handler 创建。"""
        monkeypatch.setenv("PBC_NO_FILE_LOG", "1")
        log_dir = str(tmp_path / "logs")
        # 记录调用前的 handler 数量（root logger 是全局单例，可能有残留）
        root = logging.getLogger()
        before_file_handlers = [h for h in root.handlers
                                if isinstance(h, logging.FileHandler)]
        setup_logging(log_dir=log_dir, level="INFO")
        after_file_handlers = [h for h in root.handlers
                               if isinstance(h, logging.FileHandler)]
        # 新增的文件 handler 应为 0（PBC_NO_FILE_LOG=1 跳过文件 handler）
        assert len(after_file_handlers) == len(before_file_handlers)
